import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .core import Store, matches
from .browser import FishBrowser, BrowserIssue
from .account import AccountService
from .ai import AIService, AIConfig, AIProfile, AIIssue
from .images import ImageCache

ROOT = Path(os.environ.get('DATA_DIR', '/data'))
store = Store(ROOT)
browser = FishBrowser(ROOT)
account = AccountService(store, browser)
ai_service = AIService(store)
image_cache = ImageCache(store, browser)
sessions = {}
attempts = {}
queue = asyncio.Queue()
queued = set()
working = None
scan_task = None
service_tasks = []


class Rule(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=100)
    min_price: float = Field(default=0, ge=0, le=100000000, allow_inf_nan=False)
    max_price: float | None = Field(default=None, ge=0, le=100000000, allow_inf_nan=False)
    include_words: str = Field(default='', max_length=500)
    exclude_words: str = Field(default='', max_length=500)
    region: str = Field(default='', max_length=50)
    interval_minutes: int = Field(default=10, ge=2, le=10080)
    pages: int = Field(default=1, ge=1, le=3)
    enabled: bool = True
    filter_mode: Literal['keyword', 'ai'] = 'keyword'
    ai_candidate_limit: int = Field(default=30, ge=1, le=300)
    ai_batch_size: int = Field(default=30, ge=1, le=50)
    ai_top_n: int = Field(default=3, ge=1, le=50)
    ai_min_score: float = Field(default=70, ge=0, le=100, allow_inf_nan=False)
    ai_value_weight: int = Field(default=80, ge=0, le=100)
    ai_personal_weight: int = Field(default=60, ge=0, le=100)
    ai_condition_weight: int = Field(default=50, ge=0, le=100)
    ai_personal_only: bool = False
    ai_risk_level: Literal['cautious', 'balanced', 'open'] = 'cautious'
    ai_reference_price: float | None = Field(default=None, ge=0, le=100000000, allow_inf_nan=False)
    ai_recheck_minutes: int = Field(default=60, ge=0, le=1440)
    ai_notes: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def clean(self):
        self.name = self.name.strip()
        self.keyword = self.keyword.strip()
        self.region = self.region.strip()
        if not self.name or not self.keyword:
            raise ValueError('名称和关键词不能为空')
        if self.max_price is not None and self.max_price < self.min_price:
            raise ValueError('最高价不能低于最低价')
        if self.filter_mode == 'ai':
            if self.ai_top_n > self.ai_candidate_limit:
                raise ValueError('最终保留数量不能超过本轮筛选数量')
            if not self.ai_value_weight + self.ai_personal_weight + self.ai_condition_weight:
                raise ValueError('至少设置一项非零 AI 权重')
        return self


class Password(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class Point(BaseModel):
    x: float = Field(ge=0, le=1100)
    y: float = Field(ge=0, le=780)


class Drag(BaseModel):
    x1: float = Field(ge=0, le=1100)
    y1: float = Field(ge=0, le=780)
    x2: float = Field(ge=0, le=1100)
    y2: float = Field(ge=0, le=780)


def require_rule(rid):
    result = store.query('SELECT * FROM rules WHERE id=?', (rid,))
    if not result:
        raise HTTPException(404, '蹲守任务不存在')
    return result[0]


async def enqueue(rid):
    if rid not in queued and rid != working:
        queued.add(rid)
        await queue.put(rid)


async def worker():
    global working, scan_task
    while True:
        rid = await queue.get()
        queued.discard(rid)
        working = rid
        try:
            rule = require_rule(rid)
            if not rule['enabled'] or store.get('paused') == '1' or store.get('auth_blocked') == '1':
                continue
            store.execute('UPDATE rules SET status=? WHERE id=?', ('正在检查', rid))
            scan_task = asyncio.create_task(browser.search(rule))
            items = await asyncio.wait_for(scan_task, timeout=180)
            dismissed = {r['item_id'] for r in store.query('SELECT item_id FROM dismissed_items')}
            items = [i for i in items if i['id'] not in dismissed]
            ai_stats = None
            if rule['filter_mode'] == 'ai':
                def progress(batch, total, count):
                    store.execute('UPDATE rules SET status=? WHERE id=?', (f'AI 筛选中：第 {batch}/{total} 批 · {count} 件', rid))
                scan_task = asyncio.create_task(ai_service.rank(rule, items, progress))
                items, ai_stats = await asyncio.wait_for(scan_task, timeout=600)
            # A task may have been deleted/paused/edited during the network request.
            fresh = require_rule(rid)
            fields = Rule.model_fields.keys()
            if store.get('auth_blocked') == '1' or not fresh['enabled'] or any(fresh[k] != rule[k] for k in fields):
                store.log('任务已修改，本次结果不写入；下轮按新配置检查', rule_id=rid)
                continue
            notices = store.process(fresh, items)
            for item in items:
                if matches(item, fresh):
                    image_cache.ensure(item['id'])
            if ai_stats:
                store.log(f"AI 筛选完成：分析 {ai_stats['reviewed']} 件，推荐 {ai_stats['selected']} 件，调用 {ai_stats['calls']} 次，复用 {ai_stats['cached']} 件；{len(notices)} 条新提醒", rule_id=rid)
            else:
                store.log(f"检查完成：{len(items)} 件商品，{sum(matches(i, fresh) for i in items)} 件符合，{len(notices)} 条新提醒", rule_id=rid)
        except HTTPException:
            pass
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            store.execute('UPDATE rules SET status=?,error=? WHERE id=?', ('等待登录', '', rid))
            store.log('账号操作已取消本轮检查', rule_id=rid)
        except Exception as exc:
            error = str(exc) if isinstance(exc, (BrowserIssue, AIIssue)) else ('页面加载超时' if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else '浏览器或网络异常，请查看登录窗口后重试')
            rows = store.query('SELECT failures,interval_minutes FROM rules WHERE id=?', (rid,))
            if rows:
                n = rows[0]['failures'] + 1
                backoff = min(3600, max(rows[0]['interval_minutes']*60, 120 * 2**min(n,5)))
                if isinstance(exc, BrowserIssue) and exc.code in ('auth','captcha','login'):
                    backoff = max(backoff, 900)
                    store.set('auth_blocked', '1')
                    account.set_status('verification_required' if exc.code == 'captcha' else 'logged_out')
                store.execute('UPDATE rules SET status=?,error=?,failures=?,next_check=? WHERE id=?', ('等待重试', error, n, time.time()+backoff, rid))
                store.log(error, 'warning', rid)
        finally:
            scan_task = None
            working = None
            queue.task_done()
        await asyncio.sleep(5)


async def scheduler():
    while True:
        try:
            if browser.login_active and time.time() - browser.login_started > 900:
                await browser.cancel_login()
                browser.state = '登录窗口已超时，请重新打开'
            if store.get('paused') != '1' and store.get('auth_blocked') != '1' and not browser.login_active:
                for r in store.query('SELECT id FROM rules WHERE enabled=1 AND next_check<=? ORDER BY next_check', (time.time(),)):
                    await enqueue(r['id'])
        except Exception:
            store.log('调度暂时异常，将在下一轮重试', 'warning')
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    service_tasks.clear()
    service_tasks.extend([asyncio.create_task(worker()), asyncio.create_task(scheduler()), asyncio.create_task(account.run()), asyncio.create_task(image_cache.run(lambda: working is None and not browser.login_active and not browser.lock.locked() and not account.snapshot()['checking'] and store.get('auth_blocked') != '1'))])
    store.log('闲鱼蹲蹲已启动')
    yield
    for t in service_tasks:
        t.cancel()
    await asyncio.gather(*service_tasks, return_exceptions=True)
    await account.cancel_verify()
    await browser.cancel_login()
    await browser.close()


app = FastAPI(title='闲鱼蹲蹲', lifespan=lifespan)


@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail':'请求来源不匹配'}, status_code=403)
    public = {'/api/auth/status', '/api/auth/login', '/api/auth/setup', '/health'}
    if path.startswith('/api/') and path not in public:
        token = request.cookies.get('fish_session', '')
        expires = sessions.get(token, 0)
        if expires < time.time():
            return JSONResponse({'detail':'请先登录管理页面'}, status_code=401)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith('/api/') and not path.startswith('/api/item-images/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(BrowserIssue)
async def browser_error(request, exc):
    return JSONResponse({'detail':str(exc)}, status_code=409)


@app.get('/health')
def health():
    return {'ok':True, 'version':'1.4.0'}


@app.get('/api/auth/status')
def auth_status(request: Request):
    return {'configured':bool(store.get('password_hash')), 'logged_in':sessions.get(request.cookies.get('fish_session',''),0)>time.time()}


def issue_session():
    for key, expires in list(sessions.items()):
        if expires < time.time():
            sessions.pop(key, None)
    token = secrets.token_urlsafe(32)
    sessions[token] = time.time() + 86400
    result = JSONResponse({'ok':True})
    result.set_cookie('fish_session', token, httponly=True, samesite='strict', max_age=86400)
    return result


@app.post('/api/auth/setup')
def setup(body: Password):
    if store.get('password_hash'):
        raise HTTPException(409, '管理密码已经设置')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', body.password.encode(), salt.encode(), 300000).hex()
    store.set('password_hash', salt + ':' + digest)
    return issue_session()


@app.post('/api/auth/login')
def login(body: Password, request: Request):
    ip = request.client.host if request.client else 'local'
    now = time.time()
    recent = [t for t in attempts.get(ip, []) if now-t<60]
    if len(recent) >= 8:
        raise HTTPException(429, '尝试较多，请一分钟后再试')
    attempts[ip] = recent + [now]
    saved = store.get('password_hash')
    if not saved:
        raise HTTPException(409, '请先设置管理密码')
    salt, digest = saved.split(':',1)
    candidate = hashlib.pbkdf2_hmac('sha256', body.password.encode(), salt.encode(),300000).hex()
    if not hmac.compare_digest(candidate, digest):
        raise HTTPException(401, '密码不正确')
    attempts.pop(ip, None)
    return issue_session()


@app.post('/api/auth/logout')
def logout(request: Request):
    sessions.pop(request.cookies.get('fish_session',''),None)
    response = JSONResponse({'ok':True})
    response.delete_cookie('fish_session')
    return response


@app.get('/api/state')
def state():
    rules = store.query('SELECT * FROM rules ORDER BY created DESC')
    total = store.query('SELECT COUNT(*) AS n FROM snapshots')[0]['n']
    unread = store.query('SELECT COUNT(*) AS n FROM events WHERE is_read=0')[0]['n']
    drops = store.query('SELECT COUNT(*) AS n FROM events WHERE kind="drop"')[0]['n']
    return {'rules':rules, 'items':total, 'unread':unread, 'drops':drops,
        'paused':store.get('paused')=='1', 'auth_blocked':store.get('auth_blocked')=='1',
        'browser':browser.state, 'login_active':browser.login_active,
        'working':working, 'queue':len(queued), 'account':account.snapshot(), 'version':'1.4.0'}


@app.post('/api/rules')
async def add_rule(body: Rule):
    data = body.model_dump()
    keys = list(data)
    rid = store.execute('INSERT INTO rules('+','.join(keys)+',created) VALUES('+','.join('?' for _ in keys)+',?)', tuple(data.values()) + (time.time(),))
    store.log('创建蹲守任务：' + body.name, rule_id=rid)
    return {'id':rid}


@app.put('/api/rules/{rid}')
async def edit_rule(rid: int, body: Rule):
    old = require_rule(rid)
    data = body.model_dump()
    reset = any(old[k] != data[k] for k in data if k not in ('name','interval_minutes','pages','enabled'))
    with store.db() as d:
        d.execute('UPDATE rules SET '+','.join(k+'=?' for k in data)+',next_check=0,error="",status="等待检查",baseline=? WHERE id=?', tuple(data.values())+(0 if reset else old['baseline'],rid))
        if reset:
            d.execute('DELETE FROM snapshots WHERE rule_id=?', (rid,))
            d.execute('DELETE FROM ai_cache WHERE rule_id=?', (rid,))
    return {'ok':True}


@app.delete('/api/rules/{rid}')
def delete_rule(rid:int):
    require_rule(rid)
    store.execute('DELETE FROM rules WHERE id=?', (rid,))
    return {'ok':True}


@app.post('/api/rules/{rid}/check')
async def check(rid:int):
    r = require_rule(rid)
    if not r['enabled']:
        raise HTTPException(409,'请先启用这个任务')
    if store.get('paused')=='1':
        raise HTTPException(409,'请先恢复全局蹲守')
    if browser.login_active:
        raise HTTPException(409,'请先完成登录或关闭登录窗口')
    store.set('auth_blocked','0')
    await enqueue(rid)
    return {'ok':True}


@app.post('/api/pause')
def pause():
    store.set('paused','0' if store.get('paused')=='1' else '1')
    return {'ok':True}


@app.get('/api/items')
def items(rule_id:int=0, offset:int=0, limit:int=100, ai_reviews:bool=False):
    offset, limit = max(0,offset), min(200,max(1,limit))
    where, args = ('WHERE s.rule_id=?', [rule_id]) if rule_id else ('',[])
    rows = store.query(f'SELECT s.*,r.name AS rule_name FROM snapshots s JOIN rules r ON r.id=s.rule_id {where} ORDER BY s.last_seen DESC LIMIT ? OFFSET ?', tuple(args + [limit,offset]))
    result=[]
    for r in rows:
        item=json.loads(r.pop('data'))
        rule = require_rule(r['rule_id'])
        if matches(item, rule) or (ai_reviews and rule['filter_mode']=='ai' and item.get('ai',{}).get('status')=='reviewed'):
            item['favorite'] = bool(store.query('SELECT 1 FROM favorites WHERE item_id=?', (item['id'],)))
            result.append({**r, **image_cache.decorate(item)})
    return result


@app.get('/api/favorites')
def favorites(offset:int=0, limit:int=200):
    rows = store.query('SELECT * FROM favorites ORDER BY saved_at DESC,item_id LIMIT ? OFFSET ?', (min(200,max(1,limit)),max(0,offset)))
    return [{**image_cache.decorate(json.loads(r['data'])), 'rule_name':r['rule_name'], 'saved_at':r['saved_at'], 'favorite':True} for r in rows]


@app.get('/api/item-images/{item_id}')
def cached_item_image(item_id:str):
    cached=image_cache.cached(item_id)
    if not cached:
        raise HTTPException(404,'商品主图尚未缓存')
    path,media=cached
    return FileResponse(path,media_type=media,headers={'Cache-Control':'private, max-age=86400'})


@app.post('/api/favorites/{item_id}')
def add_favorite(item_id:str):
    if not store.favorite(item_id):
        raise HTTPException(404,'商品记录不存在，请刷新列表')
    return {'ok':True}


@app.delete('/api/favorites/{item_id}')
def remove_favorite(item_id:str):
    store.execute('DELETE FROM favorites WHERE item_id=?', (item_id,))
    return {'ok':True}


@app.delete('/api/items/{item_id}')
def dismiss_item(item_id:str):
    store.dismiss(item_id)
    return {'ok':True}


@app.get('/api/events')
def events(limit:int=100):
    rows=store.query('SELECT e.*,r.name AS rule_name FROM events e JOIN rules r ON r.id=e.rule_id ORDER BY e.id DESC LIMIT ?', (min(200,max(1,limit)),))
    for r in rows:
        r['item']=json.loads(r.pop('data'))
        r['item']['favorite'] = bool(store.query('SELECT 1 FROM favorites WHERE item_id=?', (r['item_id'],)))
    return rows


@app.post('/api/events/read')
def read_events():
    store.execute('UPDATE events SET is_read=1 WHERE is_read=0')
    return {'ok':True}


@app.get('/api/logs')
def logs():
    return store.query('SELECT * FROM logs ORDER BY id DESC LIMIT 100')


@app.delete('/api/logs')
def clear_logs():
    store.execute('DELETE FROM logs')
    return {'ok':True}


@app.post('/api/fish/login')
async def fish_login():
    if account.changing:
        raise HTTPException(409, '正在切换账号，请稍候')
    if account.snapshot()['logged_in']:
        raise HTTPException(409, '闲鱼已登录。如需使用其他账号，请在设置中换登')
    await image_cache.cancel_current()
    await account.cancel_verify()
    return browser.request_login()


@app.get('/api/fish/status')
async def fish_status():
    return {**browser.login_status(), 'account':account.snapshot()}


@app.get('/api/fish/screen')
async def screen():
    if browser.login_phase != 'ready':
        raise HTTPException(409, browser.login_error or '登录画面还在准备中')
    try:
        picture = await asyncio.wait_for(browser.screenshot(), timeout=12)
        return Response(picture, media_type='image/jpeg')
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, '画面刷新超时，请重新打开登录页')


@app.post('/api/fish/click')
async def click(body:Point):
    await browser.click(body.x,body.y)
    return {'ok':True}


@app.post('/api/fish/drag')
async def drag(body:Drag):
    await browser.drag(body.x1,body.y1,body.x2,body.y2)
    return {'ok':True}


@app.post('/api/fish/confirm')
async def confirm():
    if account.snapshot()['logged_in']:
        return {'ok':True, 'account':account.snapshot()}
    try:
        if browser.login_phase != 'ready':
            raise BrowserIssue('login', '请等待扫码画面加载完成')
        ok=await asyncio.wait_for(browser.confirm(), timeout=45)
    except Exception:
        raise HTTPException(502,'登录校验暂时失败，请刷新登录画面后重试')
    if not ok:
        raise HTTPException(409,'还没检测到有效登录，请扫码或完成验证后重试')
    account.accepted_login()
    return {'ok':True, 'account':account.snapshot()}


@app.post('/api/fish/cancel')
async def cancel():
    await browser.cancel_login()
    return {'ok':True}


async def stop_account_work():
    await image_cache.cancel_current()
    task = scan_task
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@app.post('/api/fish/verify')
async def verify_account():
    await image_cache.cancel_current()
    return account.request_verify()


@app.post('/api/fish/logout')
async def logout_account():
    try:
        return await asyncio.wait_for(account.clear(stop_account_work), timeout=35)
    except Exception:
        raise HTTPException(502, '退出登录未完成，请稍后重试')


@app.post('/api/fish/switch')
async def switch_account():
    try:
        return await asyncio.wait_for(account.clear(stop_account_work, switch=True), timeout=35)
    except Exception:
        raise HTTPException(502, '旧账号清理未完成，请稍后重试')


@app.get('/api/ai/profiles')
def ai_profiles():
    return ai_service.profiles()


@app.post('/api/ai/profiles')
def add_ai_profile(body:AIProfile):
    try:
        return ai_service.save_profile(body)
    except AIIssue as exc:
        raise HTTPException(400,str(exc))


@app.put('/api/ai/profiles/{pid}')
def edit_ai_profile(pid:str, body:AIProfile):
    try:
        return ai_service.save_profile(body,pid)
    except AIIssue as exc:
        raise HTTPException(400,str(exc))


@app.post('/api/ai/profiles/{pid}/activate')
def activate_ai_profile(pid:str):
    try:
        return ai_service.activate(pid)
    except AIIssue as exc:
        raise HTTPException(400,str(exc))


@app.delete('/api/ai/profiles/{pid}')
def delete_ai_profile(pid:str):
    try:
        return ai_service.delete_profile(pid)
    except AIIssue as exc:
        raise HTTPException(400,str(exc))


@app.post('/api/ai/profiles/{pid}/test')
async def test_ai_profile(pid:str):
    try:
        return await asyncio.wait_for(ai_service.test(pid),timeout=190)
    except AIIssue as exc:
        raise HTTPException(400,str(exc))
    except TimeoutError:
        raise HTTPException(504,'AI 接口测试超时')


@app.get('/api/ai/config')
def ai_config():
    return ai_service.public_config()


@app.put('/api/ai/config')
def save_ai_config(body:AIConfig):
    try:
        result = ai_service.save_config(body)
    except AIIssue as exc:
        raise HTTPException(400, str(exc))
    store.log('AI 接口配置已更新')
    return result


@app.post('/api/ai/test')
async def test_ai_config():
    try:
        return await asyncio.wait_for(ai_service.test(), timeout=190)
    except AIIssue as exc:
        raise HTTPException(502, str(exc))
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, 'AI 接口测试超时')


@app.get('/api/export')
def export():
    rule_fields = list(Rule.model_fields)
    rules=[{k:r[k] for k in rule_fields} for r in store.query('SELECT * FROM rules ORDER BY id')]
    return Response(json.dumps({'version':1,'rules':rules}, ensure_ascii=False, indent=2),
        media_type='application/json', headers={'Content-Disposition':'attachment; filename="xianyu-rules.json"'})


class ImportRules(BaseModel):
    version:int=1
    rules:list[Rule]=Field(max_length=100)


@app.post('/api/import')
async def import_rules(body:ImportRules):
    for r in body.rules:
        await add_rule(r)
    return {'ok':True, 'count':len(body.rules)}


STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')
