import tempfile
import unittest
from app.core import Store, price, matches, extract_payload


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.rid=self.store.execute('INSERT INTO rules(name,keyword,min_price,max_price,exclude_words,created) VALUES(?,?,?,?,?,0)',
            ('镜头','适马',100,2000,'求购,租赁'))

    def tearDown(self):
        self.tmp.cleanup()

    def rule(self):
        return self.store.query('SELECT * FROM rules WHERE id=?',(self.rid,))[0]

    def item(self, iid='123', p=1800, title='适马 18-50 索尼'):
        return dict(id=iid,price=p,title=title,url='https://www.goofish.com/item?id='+iid,image='',region='上海')

    def test_baseline_new_and_dedup(self):
        self.assertEqual(self.store.process(self.rule(),[self.item()]),[])
        e=self.store.query('SELECT * FROM events')
        self.assertEqual(e[0]['kind'],'initial')
        self.assertEqual(e[0]['is_read'],1)
        self.assertEqual(self.store.process(self.rule(),[self.item()]),[])
        events=self.store.process(self.rule(),[self.item(),self.item('456',1500)])
        self.assertEqual([e['kind'] for e in events],['new'])
        self.assertEqual(self.store.process(self.rule(),[self.item('456',1500)]),[])

    def test_price_drop_and_rebound_no_repeat(self):
        self.store.process(self.rule(),[self.item()])
        self.assertEqual(self.store.process(self.rule(),[self.item(p=1600)])[0]['kind'],'drop')
        self.assertEqual(self.store.process(self.rule(),[self.item(p=1750)]),[])
        self.assertEqual(self.store.process(self.rule(),[self.item(p=1600)]),[])
        self.assertEqual(self.store.process(self.rule(),[self.item(p=1500)])[0]['kind'],'drop')

    def test_budget_crossing(self):
        self.store.process(self.rule(),[self.item(p=2500)])
        self.assertEqual(len(self.store.query('SELECT * FROM events')),0)
        self.assertEqual(self.store.process(self.rule(),[self.item(p=1900)])[0]['kind'],'drop')

    def test_filters(self):
        r=self.rule()
        self.assertFalse(matches(self.item(title='求购适马'),r))
        self.assertFalse(matches(self.item(p=99),r))
        self.assertFalse(matches(self.item(p=2001),r))
        self.assertTrue(matches(self.item(p=2000),r))
        r['include_words']='索尼,18-50'
        self.assertTrue(matches(self.item(),r))
        self.assertFalse(matches(self.item(title='适马佳能'),r))
        r['region']='上海'
        self.assertTrue(matches(self.item(),r))

    def test_price_units_and_real_schema(self):
        self.assertEqual(price('¥1.62万'),16200)
        self.assertEqual(price([{'text':'¥'},{'text':'1'},{'text':'.58'},{'text':'万'}]),15800)
        self.assertIsNone(price(True))
        self.assertIsNone(price('面议'))
        payload={'data':{'resultList':[{'data':{'item':{'main':{'exContent':{
            'itemId':'123456','title':'适马镜头','price':[{'text':'¥'},{'text':'1900'}],'picUrl':'//img.alicdn.com/test.jpg','area':'上海'}}}}}]}}
        items=extract_payload(payload)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['price'],1900)
        self.assertEqual(items[0]['image'],'https://img.alicdn.com/test.jpg')

    def test_delete_cascades(self):
        self.store.process(self.rule(),[self.item()])
        self.store.execute('DELETE FROM rules WHERE id=?',(self.rid,))
        self.assertEqual(self.store.query('SELECT * FROM snapshots'),[])
        self.assertEqual(self.store.query('SELECT * FROM events'),[])


if __name__=='__main__':
    unittest.main()
