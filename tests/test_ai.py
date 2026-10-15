import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.core import Store, matches
from app.ai import AIService, AIConfig, AIProfile, AIIssue


class AITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.ai=AIService(self.store)
        self.ai.save_config(AIConfig(base_url='https://provider.invalid/v1',model='test-model',api_key='secret-only-for-test'))
        self.rid=self.store.execute('INSERT INTO rules(name,keyword,filter_mode,ai_candidate_limit,ai_batch_size,ai_top_n,ai_min_score,ai_personal_weight,ai_condition_weight,created) VALUES(?,?,?,101,50,2,50,0,0,0)', ('test','camera','ai'))
        self.rule=self.store.query('SELECT * FROM rules WHERE id=?',(self.rid,))[0]

    def tearDown(self):
        self.temp.cleanup()

    def item(self,k):
        return {'id':str(k),'title':'camera 自用转让','price':100+k,'seller':'seller','region':'','description':'','tags':[]}

    def decision(self,iid,score=80,risk='low',personal='yes'):
        return {'id':str(iid),'relevant':True,'value_score':score,'personal_score':80 if personal=='yes' else None,'condition_score':80,'personal_seller':personal,'risk_level':risk,'confidence':'medium','reason':'仅依据公开信息进行比较','risks':[]}

    async def test_custom_batch_max_50_global_top_and_cache(self):
        counts=[]
        async def complete(c,messages):
            batch=json.loads(messages[1]['content'])['items']
            counts.append(len(batch))
            return {'items':[self.decision(i['id'],float(int(i['id'])%100)) for i in batch]}, {'total_tokens':10}
        self.ai.complete=AsyncMock(side_effect=complete)
        items=[self.item(i) for i in range(1,102)]
        out,stats=await self.ai.rank(self.rule,items)
        self.assertEqual(counts,[50,50,1])
        self.assertEqual(stats['calls'],3)
        self.assertEqual([i['id'] for i in out if matches(i,self.rule)],['98','99'])
        self.assertEqual(stats['selected'],2)
        _,cached=await self.ai.rank(self.rule,items)
        self.assertEqual(cached['calls'],0)
        self.assertEqual(cached['cached'],101)
        changed=[{**items[0],'price':99},*items[1:]]
        await self.ai.rank(self.rule,changed)
        self.assertEqual(counts[-1],1)

    async def test_reject_unknown_identity_when_required_and_high_risk(self):
        self.rule.update(ai_personal_only=1,ai_top_n=3,ai_candidate_limit=3)
        self.ai.complete=AsyncMock(return_value=({'items':[self.decision('1',95,personal='unknown'),self.decision('2',99,risk='high'),self.decision('3',80)]},{}))
        out,stats=await self.ai.rank(self.rule,[self.item(1),self.item(2),self.item(3)])
        self.assertEqual([i['id'] for i in out if matches(i,self.rule)],['3'])
        self.assertEqual(out[0]['ai']['personal_score'],None)

    async def test_hallucinated_or_missing_ids_are_not_accepted(self):
        self.ai.complete=AsyncMock(return_value=({'items':[self.decision('999')]},{}))
        with self.assertRaises(AIIssue):
            await self.ai.rank(self.rule,[self.item(1)])
        self.assertEqual(self.store.query('SELECT * FROM ai_cache'),[])

    async def test_failure_does_not_fall_back_to_keyword(self):
        self.ai.complete=AsyncMock(side_effect=AIIssue('AI 调用失败'))
        with self.assertRaises(AIIssue):
            await self.ai.rank(self.rule,[self.item(1)])
        self.assertFalse(matches(self.item(1),self.rule))
        self.assertTrue(matches(self.item(1),{**self.rule,'filter_mode':'keyword'}))

    async def test_compatible_http_format_and_safe_errors(self):
        real_client=httpx.AsyncClient
        seen=[]
        def handler(request):
            seen.append(request)
            self.assertEqual(request.url.path,'/v1/chat/completions')
            self.assertEqual(request.headers['Authorization'],'Bearer secret-only-for-test')
            payload=json.loads(request.content)
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            self.assertFalse(payload['stream'])
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}],'usage':{'total_tokens':8}})
        with patch('app.ai.httpx.AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw)):
            result=await self.ai.test()
        self.assertTrue(result['ok'])
        self.assertEqual(result['tokens'],8)
        def rejected(request):
            return httpx.Response(401,json={'error':'secret-only-for-test should never be echoed'})
        with patch('app.ai.httpx.AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(rejected),**kw)):
            with self.assertRaises(AIIssue) as caught:
                await self.ai.test()
        self.assertNotIn('secret-only-for-test',str(caught.exception))
        self.assertIn('401',str(caught.exception))

    async def test_switch_during_call_discards_old_results(self):
        other=self.ai.save_profile(AIProfile(name='second',base_url='https://second.invalid/v1',model='other',api_key='second-test-secret'))
        async def complete(c,messages):
            self.ai.activate(other['id'])
            return {'items':[self.decision('1')]},{}
        self.ai.complete=AsyncMock(side_effect=complete)
        with self.assertRaises(AIIssue):
            await self.ai.rank(self.rule,[self.item(1)])
        self.assertEqual(self.store.query('SELECT * FROM ai_cache'),[])

    def test_secret_redaction_and_address_change(self):
        self.assertNotIn('api_key',self.ai.public_config())
        self.ai.save_config(AIConfig(base_url='https://provider.invalid/v1',model='second-model'))
        self.assertEqual(self.ai.config()['api_key'],'secret-only-for-test')
        with self.assertRaises(AIIssue):
            self.ai.save_config(AIConfig(base_url='https://another.invalid/v1',model='model'))
        self.ai.save_config(AIConfig(base_url='https://another.invalid/v1',model='model',clear_key=True))
        self.assertFalse(self.ai.public_config()['has_key'])
        self.assertEqual(self.ai.path.stat().st_mode&0o777,0o600)

    def test_ai_selection_is_visible_as_new_match_then_deduplicated(self):
        item={**self.item(1),'ai':{'selected':False,'status':'reviewed'}}
        self.store.process(self.rule,[item])
        fresh=self.store.query('SELECT * FROM rules WHERE id=?',(self.rid,))[0]
        good={**item,'ai':{'selected':True,'status':'reviewed'}}
        self.assertEqual(self.store.process(fresh,[good])[0]['kind'],'match')
        self.assertEqual(self.store.process(fresh,[good]),[])

if __name__=='__main__':unittest.main()
