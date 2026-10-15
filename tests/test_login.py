"""Regression checks for stalled login, cancellation and capture."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from app.browser import FishBrowser


class LoginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.browser = FishBrowser(Path(self.root.name))

    async def asyncTearDown(self):
        await self.browser.cancel_login()
        await self.browser.close()
        self.root.cleanup()

    async def test_open_returns_immediately_and_duplicate_does_not_start_again(self):
        gate = asyncio.Event()
        self.browser.start_login = AsyncMock(side_effect=gate.wait)
        self.browser.screenshot = AsyncMock(return_value=b'jpeg')
        result = self.browser.request_login()
        self.assertEqual(result['phase'], 'opening')
        task = self.browser.login_task
        self.browser.request_login()
        self.assertIs(task, self.browser.login_task)
        gate.set()
        await task
        self.assertEqual(self.browser.login_status()['phase'], 'ready')
        self.browser.start_login.assert_awaited_once()

    async def test_failed_capture_reports_error_and_retry_recovers(self):
        self.browser.start_login = AsyncMock()
        self.browser.screenshot = AsyncMock(side_effect=TimeoutError)
        self.browser.request_login()
        await self.browser.login_task
        self.assertEqual(self.browser.login_status()['phase'], 'error')
        self.assertFalse(self.browser.login_active)
        self.browser.screenshot = AsyncMock(return_value=b'jpeg')
        self.browser.request_login()
        await self.browser.login_task
        self.assertEqual(self.browser.login_status()['phase'], 'ready')
        self.assertEqual(self.browser.login_attempt, 2)

    async def test_close_cancels_pending_open(self):
        self.browser.start_login = AsyncMock(side_effect=asyncio.Event().wait)
        self.browser.request_login()
        task = self.browser.login_task
        await asyncio.sleep(0)
        await asyncio.wait_for(self.browser.cancel_login(), timeout=1)
        self.assertTrue(task.done())
        self.assertFalse(self.browser.login_active)
        self.assertEqual(self.browser.login_phase, 'idle')

    async def test_reset_does_not_wait_for_unfinished_network_body(self):
        pending = asyncio.create_task(asyncio.Event().wait())
        self.browser.response_tasks.add(pending)
        await asyncio.wait_for(self.browser.reset_capture(), timeout=.5)
        self.assertTrue(pending.cancelled())

    async def test_stale_container_lock_is_cleared_but_live_owner_is_protected(self):
        profile = Path(self.root.name) / 'browser'
        profile.mkdir()
        (profile/'SingletonLock').symlink_to('old-container-123')
        (profile/'SingletonSocket').symlink_to('/tmp/missing-browser-test-socket')
        (profile/'SingletonCookie').symlink_to('old-cookie-marker')
        self.browser.acquire_profile()
        self.assertFalse((profile/'SingletonLock').is_symlink())
        other = FishBrowser(Path(self.root.name))
        with self.assertRaises(BlockingIOError):
            other.acquire_profile()
        self.browser.release_profile()
        other.acquire_profile()
        other.release_profile()

    async def test_capture_does_not_wait_for_page_fonts(self):
        session = AsyncMock()
        session.send.return_value = {'data':base64.b64encode(b'frame').decode()}
        self.browser.context = AsyncMock()
        self.browser.context.new_cdp_session.return_value = session
        self.browser.page = AsyncMock()
        self.browser.login_active = True
        frame = await self.browser.screenshot()
        self.assertEqual(frame, b'frame')
        session.send.assert_awaited_once()
        session.detach.assert_awaited_once()
        self.browser.page.screenshot.assert_not_awaited()
        await self.browser.screenshot()
        session.send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
