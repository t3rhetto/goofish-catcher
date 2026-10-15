import os
import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

tmp=tempfile.TemporaryDirectory()
os.environ['DATA_DIR']=tmp.name
from fastapi.testclient import TestClient
from app.main import app, store, sessions
import app.main as main
from app.browser import BrowserIssue


class APITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(app)
        cls.client.post('/api/auth/setup',json={'password':'test-password-only'})

    def test_auth_required(self):
        guest=TestClient(app)
        self.assertEqual(guest.get('/api/state').status_code,401)
        self.assertEqual(guest.get('/health').status_code,200)
        self.assertEqual(guest.get('/').status_code,200)
        self.assertEqual(guest.post('/api/auth/login',json={'password':'wrong-password'}).status_code,401)

    def test_rule_crud_export_import(self):
        data={'name':'测试任务','keyword':'相机','min_price':10,'max_price':1000,'exclude_words':'租赁'}
        r=self.client.post('/api/rules',json=data)
        self.assertEqual(r.status_code,200,r.text)
        rid=r.json()['id']
        s=self.client.get('/api/state').json()
        self.assertTrue(any(r['id']==rid for r in s['rules']))
        exported=self.client.get('/api/export').json()
        self.assertTrue(any(r['name']=='测试任务' for r in exported['rules']))
        self.assertNotIn('password_hash',str(exported))
        edited=self.client.put(f'/api/rules/{rid}',json={**data,'max_price':900})
        self.assertEqual(edited.status_code,200)
        self.assertEqual(self.client.post('/api/import',json={'rules':exported['rules']}).status_code,200)
        self.assertEqual(self.client.delete(f'/api/rules/{rid}').status_code,200)
        self.assertEqual(self.client.get('/api/items').status_code,200)

    def test_ai_batch_limit_and_config_do_not_expose_secret(self):
        data={'name':'AI task','keyword':'camera','filter_mode':'ai','ai_batch_size':51}
        self.assertEqual(self.client.post('/api/rules',json=data).status_code,422)
        data['ai_batch_size']=50
        self.assertEqual(self.client.post('/api/rules',json=data).status_code,200)
        cfg={'base_url':'https://model.invalid/v1','model':'test-model','api_key':'test-only-api-secret'}
        self.assertEqual(self.client.put('/api/ai/config',json=cfg).status_code,200)
        self.assertNotIn('test-only-api-secret',self.client.get('/api/ai/config').text)
        self.assertNotIn('test-only-api-secret',self.client.get('/api/export').text)
        guest=TestClient(app)
        self.assertEqual(guest.get('/api/ai/config').status_code,401)
        self.assertEqual(guest.post('/api/fish/logout').status_code,401)

    def test_confirmed_account_blocks_scan_and_logout_keeps_manager_session(self):
        main.account.accepted_login()
        self.assertEqual(self.client.post('/api/fish/login').status_code,409)
        self.assertTrue(self.client.get('/api/fish/status').json()['account']['logged_in'])
        self.assertEqual(self.client.post('/api/fish/logout').status_code,200)
        self.assertFalse(self.client.get('/api/state').json()['account']['logged_in'])
        self.assertTrue(self.client.get('/api/auth/status').json()['logged_in'])

    def test_favorites_actions_clear_logs_and_auth(self):
        rid=self.client.post('/api/rules',json={'name':'favorite test','keyword':'camera'}).json()['id']
        rule=main.require_rule(rid)
        item={'id':'777','title':'camera','price':20,'region':'','url':'https://www.goofish.com/item?id=777'}
        store.process(rule,[item])
        self.assertEqual(self.client.post('/api/favorites/777').status_code,200)
        listed=self.client.get('/api/items?rule_id='+str(rid)).json()
        self.assertTrue(listed[0]['favorite'])
        self.assertEqual(self.client.delete('/api/items/777').status_code,200)
        self.assertEqual(self.client.get('/api/items?rule_id='+str(rid)).json(),[])
        self.client.delete('/api/rules/'+str(rid))
        self.assertTrue(any(i['id']=='777' for i in self.client.get('/api/favorites').json()))
        store.log('clear me')
        self.assertEqual(self.client.delete('/api/logs').status_code,200)
        self.assertEqual(self.client.get('/api/logs').json(),[])
        store.log('new log')
        self.assertEqual(len(self.client.get('/api/logs').json()),1)
        self.client.delete('/api/favorites/777')
        self.assertFalse(any(i['id']=='777' for i in self.client.get('/api/favorites').json()))
        guest=TestClient(app)
        for path,method in [('/api/favorites','get'),('/api/favorites/777','post'),('/api/items/777','delete'),('/api/logs','delete'),('/api/ai/profiles','get')]:
            self.assertEqual(getattr(guest,method)(path).status_code,401)

    def test_profile_api_add_edit_switch_delete_no_secret(self):
        body={'name':'API test','base_url':'https://provider.invalid/v1','model':'test-model','api_key':'profile-api-secret'}
        response=self.client.post('/api/ai/profiles',json=body)
        self.assertEqual(response.status_code,200)
        pid=response.json()['id']
        self.assertNotIn('profile-api-secret',response.text)
        self.assertEqual(self.client.post('/api/ai/profiles/'+pid+'/activate').status_code,200)
        self.assertEqual(self.client.get('/api/ai/profiles').json()['active_id'],pid)
        self.assertNotIn('profile-api-secret',self.client.get('/api/ai/profiles').text)
        self.assertEqual(self.client.put('/api/ai/profiles/'+pid,json={**body,'name':'renamed','api_key':''}).status_code,200)
        self.assertEqual(self.client.delete('/api/ai/profiles/'+pid).status_code,200)
        self.assertIsNone(self.client.get('/api/ai/profiles').json()['active_id'])

    def test_validation_and_csrf(self):
        base={'name':'task','keyword':'camera'}
        self.assertEqual(self.client.post('/api/rules',json={**base,'min_price':200,'max_price':100}).status_code,422)
        self.assertEqual(self.client.post('/api/rules',json={**base,'interval_minutes':1}).status_code,422)
        self.assertEqual(self.client.post('/api/rules',json=base,headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth/setup',json={'password':'new-password'}).status_code,409)


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_failure_stops_already_queued_tasks(self):
        class FakeBrowser:
            calls=0
            async def search(self, rule):
                self.calls+=1
                raise BrowserIssue('auth','需要登录')
        original=main.browser
        fake=FakeBrowser()
        main.browser=fake
        main.queue=asyncio.Queue()
        main.queued.clear()
        store.set('auth_blocked','0')
        store.set('paused','0')
        ids=[]
        task=None
        try:
            for k in ('one','two'):
                result=await main.add_rule(main.Rule(name=k,keyword=k))
                ids.append(result['id'])
                await main.enqueue(result['id'])
            with patch('app.main.asyncio.sleep',new=AsyncMock()):
                task=asyncio.create_task(main.worker())
                await asyncio.wait_for(main.queue.join(),timeout=3)
            self.assertEqual(fake.calls,1)
            self.assertEqual(store.get('auth_blocked'),'1')
            self.assertEqual(main.require_rule(ids[1])['status'],'等待检查')
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            main.browser=original
            store.set('auth_blocked','0')
            for rid in ids:
                store.execute('DELETE FROM rules WHERE id=?',(rid,))


if __name__=='__main__':
    unittest.main()
