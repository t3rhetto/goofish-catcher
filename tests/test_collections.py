import json
import tempfile
import unittest
from app.core import Store
from app.ai import AIService, AIConfig, AIProfile, AIIssue


class CollectionTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.rid=self.store.execute('INSERT INTO rules(name,keyword,created) VALUES(?,?,0)',('相机任务','camera'))
        self.rule=self.store.query('SELECT * FROM rules WHERE id=?',(self.rid,))[0]
        self.item={'id':'123','title':'camera','price':300,'url':'https://www.goofish.com/item?id=123','image':'','region':''}
        self.store.process(self.rule,[self.item])

    def tearDown(self):
        self.temp.cleanup()

    def test_favorite_survives_rule_changes_deletion_and_restart(self):
        self.assertTrue(self.store.favorite('123'))
        self.assertTrue(self.store.favorite('123'))
        self.assertEqual(len(self.store.query('SELECT * FROM favorites')),1)
        self.store.execute('DELETE FROM snapshots WHERE rule_id=?',(self.rid,))
        self.store.execute('DELETE FROM rules WHERE id=?',(self.rid,))
        reloaded=Store(self.temp.name)
        saved=reloaded.query('SELECT * FROM favorites')[0]
        self.assertEqual(json.loads(saved['data'])['price'],300)
        self.assertEqual(saved['rule_name'],'相机任务')
        self.assertFalse(reloaded.favorite('missing'))

    def test_dismiss_preserves_favorite_and_cannot_resurface(self):
        self.store.favorite('123')
        self.store.dismiss('123')
        self.assertEqual(self.store.query('SELECT * FROM snapshots'),[])
        self.assertEqual(self.store.query('SELECT * FROM events'),[])
        self.store.process(self.rule,[{**self.item,'price':100}])
        self.assertEqual(self.store.query('SELECT * FROM snapshots'),[])
        self.assertEqual(self.store.query('SELECT * FROM events'),[])
        saved=self.store.query('SELECT * FROM favorites')[0]
        self.assertEqual(json.loads(saved['data'])['price'],300)
        # Removing the favorite does not undo the explicit dismissal.
        self.store.execute('DELETE FROM favorites WHERE item_id=?',('123',))
        self.store.process(self.rule,[self.item])
        self.assertEqual(self.store.query('SELECT * FROM snapshots'),[])

    def test_favorite_from_event_without_snapshot(self):
        self.store.execute('DELETE FROM snapshots')
        self.assertTrue(self.store.favorite('123'))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.ai=AIService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def profile(self,name,key,host='https://provider.invalid/v1'):
        return AIProfile(name=name,base_url=host,model='test-model',api_key=key)

    def test_migration_retains_existing_key_and_model(self):
        original={**AIConfig(base_url='https://old.invalid/v1',model='old-model',api_key='old-test-secret').model_dump(),'revision':'old'}
        self.ai.path.write_text(json.dumps(original))
        migrated=AIService(self.store)
        self.assertEqual(migrated.config()['api_key'],'old-test-secret')
        self.assertEqual(migrated.config()['model'],'old-model')
        self.assertEqual(len(migrated.profiles()['profiles']),1)
        self.assertNotIn('old-test-secret',json.dumps(migrated.profiles()))
        self.assertEqual(migrated.path.stat().st_mode&0o777,0o600)

    def test_add_switch_delete_secrets_isolated_and_persistent(self):
        a=self.ai.save_profile(self.profile('first','first-secret'))
        b=self.ai.save_profile(self.profile('second','second-secret','https://other.invalid/v1'))
        self.assertEqual(self.ai.config()['api_key'],'first-secret')
        self.ai.activate(b['id'])
        restarted=AIService(self.store)
        self.assertEqual(restarted.config()['api_key'],'second-secret')
        self.assertNotIn('secret',json.dumps(restarted.profiles()))
        restarted.delete_profile(b['id'])
        self.assertIsNone(restarted.profiles()['active_id'])
        self.assertEqual(restarted.config()['model'],'')
        self.assertNotIn('second-secret',restarted.path.read_text())
        restarted.activate(a['id'])
        self.assertEqual(restarted.config()['api_key'],'first-secret')
        with self.assertRaises(AIIssue):restarted.activate(b['id'])

    def test_new_profile_never_inherits_another_key(self):
        a=self.ai.save_profile(self.profile('first','first-secret'))
        b=self.ai.save_profile(self.profile('local',''))
        self.assertFalse(b['has_key'])
        self.ai.save_profile(self.profile('renamed',''),a['id'])
        self.assertEqual(self.ai.config()['api_key'],'first-secret')
        with self.assertRaises(AIIssue):
            self.ai.save_profile(self.profile('moved','','https://changed.invalid/v1'),a['id'])
        self.ai.save_profile(AIProfile(name='moved',base_url='https://changed.invalid/v1',model='test-model',clear_key=True),a['id'])
        self.assertFalse(self.ai.public_config()['has_key'])


if __name__=='__main__':unittest.main()
