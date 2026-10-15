"""Persistent account status with a bounded, serialized verification service."""
import asyncio
import time


class AccountService:
    def __init__(self, store, browser):
        self.store = store
        self.browser = browser
        self.lock = asyncio.Lock()
        self.task = None
        self.changing = False
        self.checking = False
        self.status = store.get('fish_account_status', 'unknown')
        self.checked_at = float(store.get('fish_account_checked_at', '0'))
        self.next_check = 0
        self.message = ''
        if self.status == 'unknown':
            self.store.set('auth_blocked', '1')

    def snapshot(self):
        labels = {'logged_in':'已登录闲鱼', 'logged_out':'尚未登录闲鱼',
                  'unknown':'登录状态待验证', 'verification_required':'需要人工验证'}
        return {'status':self.status, 'logged_in':self.status == 'logged_in',
                'checking':self.checking, 'changing':self.changing,
                'checked_at':self.checked_at,
                'message':self.message or labels.get(self.status, labels['unknown'])}

    def set_status(self, status, message=''):
        self.status = status
        self.checked_at = time.time()
        self.message = message
        self.store.set('fish_account_status', status)
        self.store.set('fish_account_checked_at', str(self.checked_at))
        self.next_check = time.monotonic() + 300

    def accepted_login(self):
        was_logged_in = self.status == 'logged_in'
        self.set_status('logged_in')
        self.store.set('auth_blocked', '0')
        self.store.execute('UPDATE rules SET next_check=0 WHERE enabled=1')
        if not was_logged_in:
            self.store.log('闲鱼登录已验证，已保存登录状态并恢复蹲守')

    def request_verify(self):
        if not self.changing and not self.browser.login_active and (not self.task or self.task.done()):
            self.checking = True
            self.message = '正在验证闲鱼登录状态'
            self.task = asyncio.create_task(self.verify())
        return self.snapshot()

    async def verify(self):
        async with self.lock:
            try:
                if not self.browser.context and not (self.browser.root / 'browser').exists() and not (self.browser.root / 'storage_state.json').exists():
                    outcome = 'logged_out'
                else:
                    outcome = await asyncio.wait_for(self.browser.verify_session(), timeout=85)
                if outcome == 'logged_in':
                    if self.status != 'logged_in':
                        self.accepted_login()
                    else:
                        self.set_status(outcome)
                elif outcome in ('logged_out', 'verification_required'):
                    old = self.status
                    self.set_status(outcome)
                    self.store.set('auth_blocked', '1')
                    if old == 'logged_in':
                        self.store.log('闲鱼登录需要更新，已暂停蹲守', 'warning')
                else:
                    self.message = '暂未取得验证结果，请稍后重试'
            except asyncio.CancelledError:
                raise
            except Exception:
                # A network error does not prove that a saved account was logged out.
                self.message = '验证暂时未完成，已保留原登录状态'
            finally:
                self.checking = False
                self.next_check = time.monotonic() + 300

    async def cancel_verify(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.checking = False

    async def run(self):
        while True:
            if not self.changing:
                if self.browser.login_active and self.browser.login_phase == 'ready' and self.browser.auth_verified:
                    try:
                        if await asyncio.wait_for(self.browser.confirm(auto=True), timeout=15):
                            self.accepted_login()
                    except Exception:
                        pass
                elif not self.browser.login_active and not self.browser.lock.locked() and time.monotonic() >= self.next_check:
                    if self.status != 'logged_out' or (self.browser.root / 'storage_state.json').exists():
                        self.request_verify()
                    else:
                        self.next_check = time.monotonic() + 300
            await asyncio.sleep(3)

    async def clear(self, before_clear, switch=False):
        if self.changing:
            raise RuntimeError('账号操作正在进行，请稍候')
        self.changing = True
        self.store.set('auth_blocked', '1')
        try:
            await self.cancel_verify()
            async with self.lock:
                await before_clear()
                await self.browser.cancel_login()
                await self.browser.clear_login_data()
                self.set_status('logged_out', '旧账号登录数据已清除' if switch else '已退出闲鱼登录')
                self.store.log('已清除旧闲鱼账号的登录数据，准备换登' if switch else '已退出闲鱼账号并清除登录数据')
                if switch:
                    return self.browser.request_login()
                return self.snapshot()
        finally:
            self.changing = False
