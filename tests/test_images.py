import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from app.core import Store
from app.images import ImageCache,detail_image,image_url,sniff_image


class ImageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.browser=type('Browser',(),{})()
        self.browser.detail_main_image=AsyncMock(return_value='https://img.alicdn.com/test.jpg')
        self.cache=ImageCache(self.store,self.browser)
        self.cache.download=AsyncMock(return_value=(b'\xff\xd8\xfftest','image/jpeg'))

    def tearDown(self):
        self.temp.cleanup()

    async def test_once_across_refresh_restart_and_favorite(self):
        await self.cache.fetch_one('123')
        for _ in range(5):
            self.assertEqual(self.cache.decorate({'id':'123','image':''})['image'],'/api/item-images/123')
            await self.cache.fetch_one('123')
        restarted=ImageCache(self.store,self.browser)
        await restarted.fetch_one('123')
        self.browser.detail_main_image.assert_awaited_once()
        self.cache.download.assert_awaited_once()
        self.assertEqual(restarted.cached('123')[0].read_bytes(),b'\xff\xd8\xfftest')
        self.assertEqual(restarted.cached('123')[1],'image/jpeg')
        self.assertIsNone(restarted.cached('../123'))

    async def test_failure_backoff_and_no_broken_cached_url(self):
        self.browser.detail_main_image=AsyncMock(return_value='')
        await self.cache.fetch_one('123')
        await self.cache.fetch_one('123')
        self.browser.detail_main_image.assert_awaited_once()
        original='https://img.alicdn.com/listing.jpg'
        self.assertEqual(self.cache.decorate({'id':'123','image':original})['image'],original)
        self.assertFalse(self.cache.cached('123'))
        row=self.store.query('SELECT * FROM item_images')[0]
        self.assertEqual(row['attempts'],1)
        self.assertEqual(row['status'],'unavailable')

    async def test_account_operation_cancels_image_work(self):
        began=asyncio.Event()
        async def detail(_):
            began.set()
            await asyncio.sleep(100)
        self.browser.detail_main_image=detail
        self.cache.task=asyncio.create_task(self.cache.fetch_one('123'))
        await began.wait()
        await self.cache.cancel_current()
        self.assertTrue(self.cache.task.cancelled())
        self.assertEqual(self.store.query('SELECT attempts FROM item_images')[0]['attempts'],0)

    def test_correct_product_main_image_and_safe_hosts(self):
        payload={'data':{'itemDO':{'itemId':'123','imageInfos':[{'url':'//img.alicdn.com/cover.jpg'},{'url':'https://img.alicdn.com/second.jpg'}]},'recommendations':[{'imageUrl':'https://img.alicdn.com/wrong.jpg'}]}}
        self.assertEqual(detail_image(payload,'123'),'https://img.alicdn.com/cover.jpg')
        self.assertEqual(detail_image(payload,'999'),'')
        for url in ('http://127.0.0.1/a','https://img.alicdn.com.evil.test/a','https://user:pw@img.alicdn.com/a','https://img.alicdn.com:bad/a','https://img.alicdn.com:8080/a'):
            self.assertEqual(image_url(url),'')
        self.assertEqual(sniff_image(b'<svg>bad</svg>'),'')

    async def test_download_validates_image_and_refuses_redirects(self):
        real=httpx.AsyncClient
        original=ImageCache.download.__get__(self.cache)
        def handler(req):
            self.assertNotIn('cookie',req.headers)
            return httpx.Response(200,content=b'\x89PNG\r\n\x1a\nfixture')
        with patch('app.images.httpx.AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
            content,media=await original('https://img.alicdn.com/test.png')
            self.assertEqual(media,'image/png')
        for response in (httpx.Response(302,headers={'location':'http://127.0.0.1/'}),httpx.Response(200,content=b'<html>login</html>')):
            with patch('app.images.httpx.AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(lambda r:response),**kw)):
                with self.assertRaises((ValueError,httpx.HTTPStatusError)):
                    await original('https://img.alicdn.com/test.png')

if __name__=='__main__':unittest.main()
