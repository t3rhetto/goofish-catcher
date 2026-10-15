"""Real Goofish login smoke; isolated profile, no user credentials."""
import asyncio
import tempfile
import time
from pathlib import Path
from app.browser import FishBrowser


async def run():
    with tempfile.TemporaryDirectory() as root:
        browser = FishBrowser(Path(root))
        try:
            started = time.monotonic()
            print('LOGIN_START', browser.request_login(), flush=True)
            while browser.login_task and not browser.login_task.done():
                print('LOGIN_PROGRESS', browser.login_status(), flush=True)
                await asyncio.sleep(5)
            if browser.login_phase != 'ready':
                raise RuntimeError(browser.login_status())
            picture = await asyncio.wait_for(browser.screenshot(), timeout=12)
            assert len(picture) > 10000
            if Path('/qa').exists():
                Path('/qa/login-smoke.jpg').write_bytes(picture)
            print('LIVE_LOGIN_OK', 'seconds=', round(time.monotonic()-started),
                  'screenshot_bytes=', len(picture), 'frames=',len(browser.page.frames), flush=True)
            await browser.cancel_login()
            browser.request_login()
            await browser.login_task
            assert browser.login_phase == 'ready', browser.login_status()
            print('LIVE_REOPEN_OK', flush=True)
        finally:
            await browser.cancel_login()
            await browser.close()


asyncio.run(run())
