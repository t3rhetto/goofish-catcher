"""Fetch a listing's detail cover once, and share its persistent local cache."""
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def image_url(value):
    if not isinstance(value,str):
        return ''
    value=value.strip()
    if value.startswith('//'):
        value='https:'+value
    try:
        parts=urlsplit(value)
        port=parts.port
    except ValueError:
        return ''
    host=(parts.hostname or '').lower()
    if parts.scheme!='https' or parts.username or parts.password or port not in (None,443):
        return ''
    if not any(host==d or host.endswith('.'+d) for d in ('alicdn.com','taobaocdn.com')):
        return ''
    return value


def detail_image(payload,item_id):
    """Only accept the requested product, never recommendation or seller images."""
    if not isinstance(payload,dict):
        return ''
    data=payload.get('data')
    if not isinstance(data,dict):
        return ''
    item=data.get('itemDO')
    if not isinstance(item,dict):
        return ''
    actual=item.get('itemId') or item.get('id')
    if actual is not None and str(actual)!=str(item_id):
        return ''
    infos=item.get('imageInfos')
    if isinstance(infos,list) and infos:
        first=infos[0]
        if isinstance(first,dict):
            for key in ('url','imageUrl','picUrl'):
                result=image_url(first.get(key))
                if result:
                    return result
        return image_url(first)
    return ''


def sniff_image(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith((b'GIF87a',b'GIF89a')):
        return 'image/gif'
    if len(data)>12 and data[:4]==b'RIFF' and data[8:12]==b'WEBP':
        return 'image/webp'
    return ''


class ImageCache:
    def __init__(self,store,browser):
        self.store=store
        self.browser=browser
        self.task=None
        self.root=Path(store.root)/'item-images'
        self.root.mkdir(parents=True,exist_ok=True)
        store.execute('CREATE TABLE IF NOT EXISTS item_images(item_id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT "pending",attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,media_type TEXT NOT NULL DEFAULT "",created REAL NOT NULL)')

    def ensure(self,item_id):
        if not re.fullmatch(r'\d{1,32}',str(item_id)):
            return
        self.store.execute('INSERT OR IGNORE INTO item_images(item_id,created) VALUES(?,?)',(str(item_id),time.time()))

    def decorate(self,item):
        iid=str(item['id'])
        self.ensure(iid)
        row=self.store.query('SELECT status FROM item_images WHERE item_id=?',(iid,))
        if row and row[0]['status']=='ready' and (self.root/(iid+'.img')).is_file():
            return {**item,'image':'/api/item-images/'+iid,'image_cached':True}
        return {**item,'image_cached':False}

    def cached(self,item_id):
        if not re.fullmatch(r'\d{1,32}',item_id):
            return None
        row=self.store.query('SELECT status,media_type FROM item_images WHERE item_id=?',(item_id,))
        path=self.root/(item_id+'.img')
        if row and row[0]['status']=='ready' and path.is_file():
            return path,row[0]['media_type']
        return None

    async def download(self,url):
        url=image_url(url)
        if not url:
            raise ValueError('Unsupported image URL')
        chunks=[]
        size=0
        async with httpx.AsyncClient(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False) as client:
            async with client.stream('GET',url,headers={'Referer':'https://www.goofish.com/','Accept':'image/webp,image/*'}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>8*1024*1024:
                        raise ValueError('Image too large')
                    chunks.append(chunk)
        data=b''.join(chunks)
        media=sniff_image(data)
        if not media:
            raise ValueError('Response is not a supported image')
        return data,media

    async def fetch_one(self,item_id):
        self.ensure(item_id)
        row=self.store.query('SELECT * FROM item_images WHERE item_id=?',(item_id,))
        if not row or row[0]['status']=='ready' or row[0]['attempts']>=3 or row[0]['next_attempt']>time.time():
            return
        # No other cache worker runs concurrently; browser navigation has its own lock.
        try:
            url=await asyncio.wait_for(self.browser.detail_main_image(item_id),timeout=50)
            if not url:
                raise ValueError('No product image in detail page')
            data,media=await asyncio.wait_for(self.download(url),timeout=20)
            temp=self.root/(item_id+'.pending')
            try:
                temp.write_bytes(data)
                temp.replace(self.root/(item_id+'.img'))
            finally:
                temp.unlink(missing_ok=True)
            self.store.execute('UPDATE item_images SET status="ready",media_type=?,attempts=attempts+1 WHERE item_id=?',(media,item_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failed pages are retried with backoff, never on every UI refresh.
            self.store.execute('UPDATE item_images SET status="unavailable",attempts=attempts+1,next_attempt=? WHERE item_id=?',(time.time()+1800,item_id))

    async def cancel_current(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)

    async def run(self,can_fetch):
        while True:
            try:
                if can_fetch():
                    # Also populate old favorites; discovery cards enqueue on display.
                    rows=self.store.query('SELECT item_id FROM favorites LIMIT 10000')
                    with self.store.db() as db:
                        db.executemany('INSERT OR IGNORE INTO item_images(item_id,created) VALUES(?,?)',[(r['item_id'],time.time()) for r in rows if re.fullmatch(r'\d{1,32}',r['item_id'])])
                    pending=self.store.query('SELECT c.item_id FROM item_images c WHERE c.status!="ready" AND c.attempts<3 AND c.next_attempt<=? AND (EXISTS(SELECT 1 FROM favorites f WHERE f.item_id=c.item_id) OR EXISTS(SELECT 1 FROM snapshots s WHERE s.item_id=c.item_id)) ORDER BY EXISTS(SELECT 1 FROM favorites f WHERE f.item_id=c.item_id) DESC,c.created LIMIT 1',(time.time(),))
                    if pending:
                        self.task=asyncio.create_task(self.fetch_one(pending[0]['item_id']))
                        await self.task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            finally:
                self.task=None
            await asyncio.sleep(5)
