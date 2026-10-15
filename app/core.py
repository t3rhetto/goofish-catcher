import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs


def price(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        for k in ('priceText', 'text', 'value', 'price', 'amount'):
            if k in value:
                p = price(value[k])
                if p is not None:
                    return p
        return None
    if isinstance(value, list):
        return price(''.join(str(v.get('text', '')) for v in value if isinstance(v, dict)))
    s = str(value).replace(',', '').strip()
    match = re.search(r'\d+(?:\.\d+)?', s)
    if not match:
        return None
    p = float(match.group())
    if '万' in s or re.search(r'\d\s*[wW]', s):
        p *= 10000
    elif '千' in s or re.search(r'\d\s*[kK]', s):
        p *= 1000
    return round(p, 2) if math.isfinite(p) else None


def words(s):
    return [x.strip().casefold() for x in re.split(r'[,，\n;；]', s or '') if x.strip()]


def matches(item, rule, use_ai=True):
    p = item.get('price')
    if p is None or not math.isfinite(p) or p < rule['min_price']:
        return False
    if rule['max_price'] is not None and p > rule['max_price']:
        return False
    title = item['title'].casefold()
    if any(w not in title for w in words(rule['include_words'])):
        return False
    if any(w in title for w in words(rule['exclude_words'])):
        return False
    if rule['region'] and rule['region'].casefold() not in item.get('region', '').casefold():
        return False
    if use_ai and rule.get('filter_mode', 'keyword') == 'ai':
        return item.get('ai', {}).get('selected') is True
    return True


def extract_payload(data):
    found = {}
    stack = [data]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
            continue
        if not isinstance(n, dict):
            continue
        stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
        title = next((str(n[k]).strip() for k in ('title', 'itemTitle', 'item_title', 'itemName', 'subject') if n.get(k)), '')
        item_id = next((str(n[k]) for k in ('itemId', 'item_id', 'auctionId', 'itemid', 'id') if n.get(k)), '')
        url = str(n.get('itemUrl') or n.get('url') or n.get('jumpUrl') or '')
        if not item_id and url:
            item_id = parse_qs(urlparse(url).query).get('id', [''])[0]
        if not title or not item_id.isdigit():
            continue
        p = None
        for key in ('priceText', 'displayPrice', 'salePrice', 'currentPrice', 'price', 'priceInfo', 'tradePrice'):
            if key in n:
                p = price(n[key])
                if p is not None:
                    break
        if p is None:
            continue
        image = str(n.get('picUrl') or n.get('imageUrl') or n.get('imgUrl') or n.get('pic') or '')
        if image.startswith('//'):
            image = 'https:' + image
        user = n.get('user') if isinstance(n.get('user'), dict) else {}
        found[item_id] = dict(id=item_id, title=title[:1000], price=p,
            url='https://www.goofish.com/item?id=' + item_id,
            image=image if image.startswith('https://') else '',
            seller=str(n.get('nick') or n.get('sellerNick') or user.get('nick') or ''),
            region=str(n.get('area') or n.get('city') or n.get('location') or n.get('province') or ''),
            description=str(n.get('description') or n.get('itemDesc') or n.get('subTitle') or n.get('desc') or '')[:1500],
            tags=[str(t.get('text') or t.get('name') or '')[:100] if isinstance(t,dict) else str(t)[:100] for t in (n.get('tags') or n.get('labels') or [])][:10] if isinstance(n.get('tags') or n.get('labels') or [],list) else [],
            publish_time=n.get('publishTime') or n.get('gmtCreate'))
    return list(found.values())


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'watch.db'
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS rules(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, keyword TEXT NOT NULL,
                min_price REAL NOT NULL DEFAULT 0, max_price REAL,
                include_words TEXT NOT NULL DEFAULT '', exclude_words TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '', interval_minutes INTEGER NOT NULL DEFAULT 10,
                pages INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
                baseline INTEGER NOT NULL DEFAULT 0, last_check REAL, next_check REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '等待检查', error TEXT NOT NULL DEFAULT '',
                failures INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(
                rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                item_id TEXT NOT NULL, data TEXT NOT NULL, last_price REAL NOT NULL,
                lowest_price REAL NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                PRIMARY KEY(rule_id,item_id));
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER REFERENCES rules(id) ON DELETE CASCADE,
                item_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                old_price REAL, price REAL NOT NULL, created REAL NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0, event_key TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS favorites(
                item_id TEXT PRIMARY KEY, data TEXT NOT NULL, rule_name TEXT NOT NULL,
                saved_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS dismissed_items(item_id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER, message TEXT NOT NULL, level TEXT NOT NULL, created REAL NOT NULL);
            ''')

        additions = {
            'filter_mode':"TEXT NOT NULL DEFAULT 'keyword'",
            'ai_candidate_limit':'INTEGER NOT NULL DEFAULT 30',
            'ai_batch_size':'INTEGER NOT NULL DEFAULT 30',
            'ai_top_n':'INTEGER NOT NULL DEFAULT 3',
            'ai_min_score':'REAL NOT NULL DEFAULT 70',
            'ai_value_weight':'INTEGER NOT NULL DEFAULT 80',
            'ai_personal_weight':'INTEGER NOT NULL DEFAULT 60',
            'ai_condition_weight':'INTEGER NOT NULL DEFAULT 50',
            'ai_personal_only':'INTEGER NOT NULL DEFAULT 0',
            'ai_risk_level':"TEXT NOT NULL DEFAULT 'cautious'",
            'ai_reference_price':'REAL DEFAULT NULL',
            'ai_recheck_minutes':'INTEGER NOT NULL DEFAULT 60',
            'ai_notes':"TEXT NOT NULL DEFAULT ''"
        }
        with self.db() as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(rules)')}
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE rules ADD COLUMN {name} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS ai_cache(rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE, item_id TEXT NOT NULL, fingerprint TEXT NOT NULL, decision TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(rule_id,item_id))')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            with c:
                yield c
        finally:
            c.close()

    def query(self, sql, args=()):
        with self.db() as d:
            return [dict(r) for r in d.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db() as d:
            return d.execute(sql, args).lastrowid

    def get(self, key, default=''):
        r = self.query('SELECT value FROM settings WHERE key=?', (key,))
        return r[0]['value'] if r else default

    def set(self, key, value):
        self.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def log(self, message, level='info', rule_id=None):
        with self.db() as d:
            d.execute('INSERT INTO logs(rule_id,message,level,created) VALUES(?,?,?,?)', (rule_id, message[:500], level, time.time()))
            d.execute('DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0)-1000 FROM logs)')

    def favorite(self, item_id):
        # Copy server-owned listing data; never trust client-supplied titles or URLs.
        with self.db() as d:
            if d.execute('SELECT 1 FROM favorites WHERE item_id=?', (item_id,)).fetchone():
                return True
            row = d.execute('SELECT s.data,r.name AS rule_name FROM snapshots s JOIN rules r ON r.id=s.rule_id WHERE s.item_id=? ORDER BY s.last_seen DESC LIMIT 1', (item_id,)).fetchone()
            if not row:
                row = d.execute('SELECT e.data,r.name AS rule_name FROM events e JOIN rules r ON r.id=e.rule_id WHERE e.item_id=? ORDER BY e.id DESC LIMIT 1', (item_id,)).fetchone()
            if not row:
                return False
            d.execute('INSERT INTO favorites(item_id,data,rule_name,saved_at) VALUES(?,?,?,?)', (item_id,row['data'],row['rule_name'],time.time()))
        return True

    def dismiss(self, item_id):
        # Favorites deliberately survive deletion of ordinary results or their rule.
        with self.db() as d:
            d.execute('INSERT OR IGNORE INTO dismissed_items(item_id,created) VALUES(?,?)', (item_id,time.time()))
            for table in ('snapshots','events','ai_cache'):
                d.execute(f'DELETE FROM {table} WHERE item_id=?', (item_id,))

    def process(self, rule, items):
        now = time.time()
        notifications = []
        with self.db() as d:
            for i in items:
                if d.execute('SELECT 1 FROM dismissed_items WHERE item_id=?', (i['id'],)).fetchone():
                    continue
                prev = d.execute('SELECT * FROM snapshots WHERE rule_id=? AND item_id=?', (rule['id'], i['id'])).fetchone()
                eligible = matches(i, rule)
                kind = None
                if eligible:
                    if prev is None:
                        kind = 'new' if rule['baseline'] else 'initial'
                    elif i['price'] < prev['lowest_price']:
                        kind = 'drop'
                    elif not matches(json.loads(prev['data']), rule):
                        kind = 'match'
                data = json.dumps(i, ensure_ascii=False)
                if kind:
                    key = hashlib.sha256(f"{rule['id']}:{i['id']}:{kind}:{i['price'] if kind != 'new' else ''}".encode()).hexdigest()
                    cur = d.execute('INSERT OR IGNORE INTO events(rule_id,item_id,kind,data,old_price,price,created,is_read,event_key) VALUES(?,?,?,?,?,?,?,?,?)',
                        (rule['id'], i['id'], kind, data, prev['last_price'] if prev else None, i['price'], now, int(kind == 'initial'), key))
                    if cur.rowcount and kind != 'initial':
                        notifications.append(dict(item=i, kind=kind))
                d.execute('INSERT INTO snapshots(rule_id,item_id,data,last_price,lowest_price,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) ON CONFLICT(rule_id,item_id) DO UPDATE SET data=excluded.data,last_price=excluded.last_price,lowest_price=MIN(snapshots.lowest_price,excluded.lowest_price),last_seen=excluded.last_seen',
                    (rule['id'], i['id'], data, i['price'], i['price'], now, now))
            d.execute('UPDATE rules SET baseline=1,last_check=?,next_check=?,status=?,error="",failures=0 WHERE id=?',
                (now, now + rule['interval_minutes'] * 60, f'发现 {len(items)} 件，符合 {sum(matches(i, rule) for i in items)} 件', rule['id']))
        return notifications
