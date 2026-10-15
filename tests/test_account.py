import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from app.core import Store
from app.browser import FishBrowser
from app.account import AccountService


class AccountTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.browser=FishBrowser(Path(self.temp.name))
        self.account=AccountService(self.store,self.browser)

    async def asyncTearDown(self):
        await self.account.cancel_verify()
        await self.browser.cancel_login()
        await self.browser.close()
        self.temp.cleanup()

    async def test_verification_and_restart_preserve_login_metadata(self):
        (self.browser.root/'browser').mkdir()
        self.browser.verify_session=AsyncMock(return_value='logged_in')
        self.account.request_verify()
        await self.account.task
        self.assertTrue(self.account.snapshot()['logged_in'])
        restored=AccountService(self.store,self.browser)
        self.assertTrue(restored.snapshot()['logged_in'])
        self.assertGreater(restored.snapshot()['checked_at'],0)
        self.assertEqual(self.store.get('auth_blocked'),'0')

    async def test_network_failure_keeps_known_valid_login(self):
        (self.browser.root/'browser').mkdir()
        self.account.accepted_login()
        self.browser.verify_session=AsyncMock(side_effect=TimeoutError)
        self.account.request_verify()
        await self.account.task
        self.assertTrue(self.account.snapshot()['logged_in'])
        self.browser.verify_session=AsyncMock(return_value='logged_out')
        self.account.request_verify()
        await self.account.task
        self.assertFalse(self.account.snapshot()['logged_in'])
        self.assertEqual(self.store.get('auth_blocked'),'1')

    async def test_logout_removes_entire_website_profile_but_keeps_tasks(self):
        profile=self.browser.root/'browser'
        (profile/'Default'/'Local Storage').mkdir(parents=True)
        (profile/'Default'/'Local Storage'/'old-account').write_text('old-data')
        (self.browser.root/'storage_state.json').write_text('{}')
        self.store.execute('INSERT INTO rules(name,keyword,created) VALUES(?,?,0)',('task','camera'))
        self.account.accepted_login()
        stop=AsyncMock()
        await self.account.clear(stop)
        stop.assert_awaited_once()
        self.assertFalse(profile.exists())
        self.assertFalse((self.browser.root/'storage_state.json').exists())
        self.assertFalse(self.account.snapshot()['logged_in'])
        self.assertEqual(len(self.store.query('SELECT * FROM rules')),1)
        self.assertFalse(self.browser.auth_verified)

    async def test_switch_starts_fresh_login_after_clear(self):
        self.browser.request_login=lambda:{'phase':'opening'}
        self.account.accepted_login()
        result=await self.account.clear(AsyncMock(),switch=True)
        self.assertEqual(result['phase'],'opening')
        self.assertFalse(self.account.snapshot()['logged_in'])

if __name__=='__main__':unittest.main()
