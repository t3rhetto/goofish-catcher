import asyncio
import base64
import fcntl
import socket
import shutil
import logging
import json
import time
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from .core import extract_payload, price
from .images import detail_image, image_url


class BrowserIssue(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


DOM_ITEMS = r'''() => [...document.querySelectorAll('a[href*="item?id="]')].map(a => {
  const titleEl = a.querySelector('[class*="title"], [class*="Title"]');
  const priceEl = a.querySelector('[class*="price"], [class*="Price"]');
  const img = a.querySelector('img');
  const area = a.querySelector('[class*="location"], [class*="area"], [class*="city"]');
  return {itemId:new URL(a.href,location.href).searchParams.get('id'),
    title:(titleEl?.innerText || img?.alt || '').trim(),
    priceText:(priceEl?.innerText || (a.innerText.match(/[¥￥]\s*[\d.,]+(?:万|千)?/)||[])[0] || ''),
    picUrl:img?.src || '', area:area?.innerText || ''};
})'''


class FishBrowser:
    def __init__(self, root):
        self.root = root
        self.pw = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        self.login_active = False
        self.login_started = 0
        self.state = '尚未登录'
        self.auth_verified = False
        self.auth_results = {}
        self.payloads = []
        self.response_tasks = set()
        self.flags = set()
        self.saw_search = False
        self.search_ok = False

        self.login_task = None
        self.login_phase = 'idle'
        self.login_error = ''
        self.login_attempt = 0
        self.last_screen = None
        self.screen_time = 0
        self.profile_guard = None

    def login_status(self):
        return {'phase':self.login_phase, 'message':self.login_error or self.state,
                'attempt':self.login_attempt, 'elapsed':round(time.time()-self.login_started) if self.login_active else 0}

    def request_login(self):
        if self.login_task and not self.login_task.done():
            return self.login_status()
        if self.login_active and self.login_phase == 'ready':
            return self.login_status()
        self.login_active = True
        self.login_started = time.time()
        self.login_attempt += 1
        self.login_phase = 'opening'
        self.login_error = ''
        self.last_screen = None
        self.state = '正在准备登录窗口'
        self.login_task = asyncio.create_task(self._prepare_login())
        return self.login_status()

    async def _prepare_login(self):
        try:
            await asyncio.wait_for(self.start_login(), timeout=75)
            await asyncio.wait_for(self.screenshot(), timeout=12)
            self.login_phase = 'ready'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stage = self.state
            self.login_phase = 'error'
            self.login_error = str(exc) if isinstance(exc, BrowserIssue) else f'{stage}未完成，请点击重试。若多次失败，请稍后再试'
            # Record stage and exception type, never response bodies or credentials.
            logging.getLogger('uvicorn.error').warning('Login failed at %s (%s)', stage, type(exc).__name__)
            await self.close()
            self.login_active = False

    async def cancel_login(self):
        task = self.login_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.close()
        self.login_task = None
        self.login_active = False
        self.login_phase = 'idle'
        self.login_error = ''
        self.last_screen = None

    def acquire_profile(self):
        profile = self.root / 'browser'
        profile.mkdir(parents=True, exist_ok=True)
        guard = (self.root / 'browser-owner.lock').open('a')
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Docker recreations change hostname and /tmp. Chromium leaves
            # Singleton symlinks behind if the container was stopped abruptly.
            # Never remove them while a live browser still owns its socket.
            endpoint = profile / 'SingletonSocket'
            if endpoint.exists():
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(.2)
                    try:
                        probe.connect(str(endpoint.resolve()))
                    except (ConnectionRefusedError, FileNotFoundError):
                        pass
                    else:
                        raise BrowserIssue('busy', '已有浏览器使用登录目录，请关闭另一个实例后重试')
            for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                (profile / name).unlink(missing_ok=True)
            self.profile_guard = guard
        except BaseException:
            guard.close()
            raise

    def release_profile(self):
        if self.profile_guard:
            self.profile_guard.close()
            self.profile_guard = None

    async def ensure(self):
        if self.context and self.page and not self.page.is_closed():
            return
        if self.pw or self.profile_guard:
            await self.close()
        self.acquire_profile()
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            str(self.root / 'browser'), headless=False,
            viewport={'width':1100, 'height':780}, locale='zh-CN', timezone_id='Asia/Shanghai',
            args=['--disable-gpu', '--disable-dev-shm-usage'],
            chromium_sandbox=False, timeout=30000)
        self.context.set_default_timeout(8000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.context.on('page', lambda p: p.on('response', self.observe))
        self.page.on('response', self.observe)
        await self.context.route('**/*', self.trim_login_assets)
        self.state = '浏览器已就绪'

    async def trim_login_assets(self, route):
        request = route.request
        # Keep login-frame images (including QR codes), but avoid rendering
        # unrelated product media and web fonts while the login window is open.
        resource = request.resource_type
        if self.login_active and (resource in ('font', 'media') or
                (resource == 'image' and request.frame == self.page.main_frame)):
            await route.abort()
        else:
            await route.continue_()

    def observe(self, response):
        if '/h5/mtop.' not in response.url:
            return
        t = asyncio.create_task(self.read_response(response))
        self.response_tasks.add(t)
        t.add_done_callback(self.response_tasks.discard)

    async def read_response(self, response):
        try:
            text = await asyncio.wait_for(response.text(), timeout=8)
            start, end = text.find('{'), text.rfind('}')
            if start < 0:
                return
            obj = json.loads(text[start:end+1])
            ret = ' '.join(str(x) for x in obj.get('ret', []))
            search = 'search' in response.url.lower()
            if search:
                self.saw_search = True
                self.search_ok = self.search_ok or ('SUCCESS' in ret)
                self.payloads.append(obj)
            for marker in ('loginuser.get', 'user.page.nav'):
                if marker not in response.url:
                    continue
                if 'SUCCESS' in ret:
                    self.auth_results[marker] = True
                elif any(word in ret for word in ('SESSION_EXPIRED', 'NEED_LOGIN', 'FAIL_SYS_LOGIN', 'NOT_LOGIN')):
                    self.auth_results[marker] = False
                self.auth_verified = any(self.auth_results.values()) and not any(value is False for value in self.auth_results.values())
            if search or 'loginuser.get' in response.url:
                if any(w in ret.lower() for w in ['captcha', 'rgv587', 'user_validate', 'baxia', 'punish']):
                    self.flags.add('captcha')
                if any(w in ret for w in ['SESSION_EXPIRED', 'LOGIN', 'TOKEN_EXPIRED']):
                    self.flags.add('auth')
        except Exception:
            pass

    async def reset_capture(self):
        # Discard old responses instead of waiting for a network body that may never finish.
        pending = list(self.response_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.payloads.clear()
        self.flags.clear()
        self.saw_search = False
        self.search_ok = False

    async def search(self, rule):
        async with self.lock:
            if self.login_active:
                raise BrowserIssue('login', '登录窗口正在使用，请先完成登录')
            await self.ensure()
            await self.reset_capture()
            params = {'q':rule['keyword']}
            if rule['min_price']:
                params['priceLower'] = str(rule['min_price'])
            if rule['max_price'] is not None:
                params['priceUpper'] = str(rule['max_price'])
            await self.page.goto('https://www.goofish.com/search?' + urlencode(params), wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            # Prefer the site's newest publication filter when available.
            latest = self.page.get_by_text('新发布', exact=True).first
            if await latest.count():
                try:
                    await latest.click(timeout=3000)
                    option = self.page.get_by_text('最新发布', exact=False).first
                    if await option.count():
                        await self.reset_capture()
                        await option.click(timeout=3000)
                        await asyncio.sleep(2)
                except Exception:
                    pass
            all_items = {}
            for pn in range(1, rule['pages']+1):
                if pn > 1:
                    box = self.page.locator('[class*="search-pagination-page-box"]').filter(has_text=str(pn)).first
                    if not await box.count():
                        break
                    await self.reset_capture()
                    await box.click(timeout=5000)
                    await asyncio.sleep(3)
                for _ in range(12):
                    if self.search_ok or self.flags:
                        break
                    await asyncio.sleep(0.5)
                if self.response_tasks:
                    await asyncio.wait(list(self.response_tasks), timeout=8)
                items = extract_payload(self.payloads)
                if not items:
                    items = extract_payload(await self.page.evaluate(DOM_ITEMS))
                if not items:
                    if 'captcha' in self.flags:
                        self.state = '需要人工验证'
                        raise BrowserIssue('captcha', '闲鱼要求验证。请打开登录窗口，人工完成后继续')
                    iframe_login = any('mini_login' in f.url for f in self.page.frames)
                    if 'auth' in self.flags or iframe_login or 'passport.' in self.page.url:
                        self.state = '需要扫码登录'
                        self.auth_verified = False
                        raise BrowserIssue('auth', '请先扫码登录闲鱼，再开始蹲守')
                    if not self.search_ok:
                        self.state = '页面未返回有效结果'
                        raise BrowserIssue('parse', '未收到有效搜索结果，可能页面尚未加载或接口变化；不会将其当作零商品')
                all_items.update({i['id']:i for i in items})
            self.state = '搜索正常'
            await self.save_login()
            return list(all_items.values())

    async def detail_main_image(self, item_id):
        async with self.lock:
            if self.login_active:
                raise BrowserIssue('login', '登录期间暂缓获取主图')
            await self.ensure()
            page = await self.context.new_page()
            done = asyncio.get_running_loop().create_future()
            readers = set()
            async def collect(response):
                if '/h5/mtop.taobao.idle.pc.detail/' not in response.url:
                    return
                try:
                    raw = await asyncio.wait_for(response.text(), timeout=5)
                    payload = json.loads(raw[raw.find('{'):raw.rfind('}')+1])
                    url = detail_image(payload, item_id)
                    if url and not done.done():
                        done.set_result(url)
                except Exception:
                    pass
            def observe_detail(response):
                task = asyncio.create_task(collect(response))
                readers.add(task)
                task.add_done_callback(readers.discard)
            page.on('response', observe_detail)
            try:
                await page.goto('https://www.goofish.com/item?id='+str(item_id), wait_until='domcontentloaded', timeout=25000)
                try:
                    return await asyncio.wait_for(asyncio.shield(done), timeout=10)
                except TimeoutError:
                    # Fallback only to explicit product metadata, never the first
                    # generic image which could belong to a seller or recommendation.
                    candidates = await asyncio.wait_for(page.evaluate(r'''() => {
                      const result=[];
                      for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
                        try {const obj=JSON.parse(el.textContent);const nodes=Array.isArray(obj)?obj:[obj,...(obj['@graph']||[])];
                          for(const p of nodes) if(p['@type']==='Product') {const imgs=Array.isArray(p.image)?p.image:[p.image];result.push(...imgs.map(i=>typeof i==='string'?i:i?.url));}
                        } catch {}
                      }
                      const og=document.querySelector('meta[property="og:image"]');if(og)result.push(og.content);
                      return result.filter(Boolean);
                    }'''),timeout=5)
                    return next((image_url(v) for v in candidates if image_url(v)), '')
            finally:
                page.remove_listener('response', observe_detail)
                for task in readers:
                    task.cancel()
                if readers:
                    await asyncio.gather(*readers,return_exceptions=True)
                if not done.done():
                    done.cancel()
                await asyncio.wait_for(page.close(), timeout=5)

    async def start_login(self):
        self.state = '正在等待浏览器空闲'
        async with self.lock:
            self.state = '正在启动登录浏览器'
            await self.ensure()
            if not self.login_active:
                self.login_active = True
                self.login_started = time.time()
            self.auth_verified = False
            self.auth_results.clear()
            await self.reset_capture()
            self.state = '正在加载闲鱼登录页'
            await self.page.goto('https://www.goofish.com/search?q=%E9%97%B2%E9%B1%BC', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(3)
            self.state = '正在准备扫码画面'
            # Modern login dialogs already show a QR code. Keep optional UI
            # interactions short: locator.count() itself has no timeout.
            if not any('mini_login' in f.url for f in self.page.frames):
                try:
                    await asyncio.wait_for(self.page.get_by_text('登录', exact=True).first.click(timeout=2000), timeout=3)
                    await asyncio.sleep(1)
                except Exception:
                    pass
            for frame in self.page.frames:
                if 'mini_login' not in frame.url:
                    continue
                try:
                    await asyncio.wait_for(frame.locator('.icon-qrcode').first.click(timeout=1000), timeout=2)
                    break
                except Exception:
                    pass
            self.state = '请扫码登录'

    async def has_login_cookies(self):
        cookies = await self.context.cookies('https://www.goofish.com')
        now = time.time()
        return any(c['name'] in ('unb', 'cookie2', 'sgcookie') and c.get('value') and
                   (c.get('expires', -1) <= 0 or c['expires'] > now) for c in cookies)

    async def save_login(self):
        target = self.root / 'storage_state.json'
        temporary = self.root / 'storage_state.pending'
        try:
            await self.context.storage_state(path=str(temporary))
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def auth_outcome(self):
        if any(value is False for value in self.auth_results.values()):
            return 'logged_out'
        if self.auth_verified:
            return 'logged_in'
        if 'captcha' in self.flags:
            return 'verification_required'
        return 'unknown'

    async def verify_session(self):
        async with self.lock:
            if self.login_active:
                return 'unknown'
            await self.ensure()
            if not await self.has_login_cookies():
                return 'logged_out'
            self.auth_verified = False
            self.auth_results.clear()
            await self.reset_capture()
            await self.page.goto('https://www.goofish.com/search?q=%E9%97%B2%E9%B1%BC',
                                 wait_until='domcontentloaded', timeout=30000)
            for _ in range(24):
                await asyncio.sleep(.5)
                if self.auth_verified:
                    break
            if not await self.has_login_cookies():
                return 'logged_out'
            outcome = self.auth_outcome()
            if outcome == 'logged_in':
                await self.save_login()
                self.state = '已登录'
            elif outcome == 'logged_out':
                self.state = '登录已失效，请重新扫码'
            elif outcome == 'verification_required':
                self.state = '需要人工验证'
            return outcome

    async def confirm(self, auto=False):
        async with self.lock:
            await self.ensure()
            if not auto:
                # Optional post-scan confirmation must never wait for a missing locator.
                for frame in self.page.frames:
                    if 'mini_login' not in frame.url:
                        continue
                    try:
                        await asyncio.wait_for(frame.get_by_text('快速进入', exact=False).first.click(timeout=1500), timeout=2)
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                if not self.auth_verified:
                    self.auth_results.clear()
                    await self.reset_capture()
                    await self.page.reload(wait_until='domcontentloaded', timeout=30000)
                    for _ in range(16):
                        await asyncio.sleep(.5)
                        if self.auth_verified:
                            break
            if not await self.has_login_cookies() or self.auth_outcome() != 'logged_in':
                return False
            await self.save_login()
            self.login_active = False
            self.login_phase = 'idle'
            self.login_error = ''
            self.last_screen = None
            self.state = '已登录'
            return True

    async def clear_login_data(self):
        async with self.lock:
            await self.close()
            # Take ownership before erasing the complete website profile. This
            # removes Cookies, IndexedDB, local/session storage, cache and history.
            self.acquire_profile()
            try:
                await asyncio.to_thread(shutil.rmtree, self.root / 'browser')
                for name in ('storage_state.json', 'storage_state.pending'):
                    (self.root / name).unlink(missing_ok=True)
            finally:
                self.release_profile()
            self.auth_verified = False
            self.auth_results.clear()
            self.payloads.clear()
            self.flags.clear()
            self.last_screen = None
            self.screen_time = 0
            self.login_active = False
            self.login_phase = 'idle'
            self.login_error = ''
            self.state = '已退出闲鱼登录'

    async def screenshot(self):
        async with self.lock:
            if not self.login_active or not self.page:
                raise BrowserIssue('login', '请先打开登录窗口')
            if self.last_screen and time.monotonic()-self.screen_time < 2:
                return self.last_screen
            # Capture the current frame directly. Waiting for all web fonts can block
            # screenshots indefinitely on third-party login pages.
            session = await self.context.new_cdp_session(self.page)
            try:
                result = await asyncio.wait_for(session.send('Page.captureScreenshot',
                    {'format':'jpeg','quality':80,'captureBeyondViewport':False}), timeout=8)
                self.last_screen = base64.b64decode(result['data'])
                self.screen_time = time.monotonic()
                return self.last_screen
            finally:
                try:
                    await asyncio.wait_for(session.detach(), timeout=2)
                except Exception:
                    pass

    async def click(self, x, y):
        async with self.lock:
            if not self.login_active:
                raise BrowserIssue('login', '登录窗口尚未开启')
            await self.page.mouse.click(max(0, min(1099, x)), max(0, min(779, y)))

    async def drag(self, x1, y1, x2, y2):
        async with self.lock:
            if not self.login_active:
                raise BrowserIssue('login', '登录窗口尚未开启')
            await self.page.mouse.move(max(0,min(1099,x1)), max(0,min(779,y1)))
            await self.page.mouse.down()
            await self.page.mouse.move(max(0,min(1099,x2)), max(0,min(779,y2)), steps=20)
            await self.page.mouse.up()

    async def close(self):
        pending = list(self.response_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        context, pw = self.context, self.pw
        self.context = self.pw = self.page = None
        try:
            for target, method in ((context, 'close'), (pw, 'stop')):
                if target:
                    try:
                        await asyncio.wait_for(getattr(target, method)(), timeout=5)
                    except Exception:
                        pass
        finally:
            self.release_profile()
