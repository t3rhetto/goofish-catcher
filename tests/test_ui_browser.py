"""Offline browser regression: synthetic APIs, no user session or live website."""
import unittest
from pathlib import Path
from playwright.async_api import async_playwright


class UIBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_edit_add_save_and_layout_in_offline_browser(self):
        static=Path(__file__).parents[1]/'app/static'
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=await browser.new_page(viewport={'width':1300,'height':850})
            page.set_default_timeout(8000)
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            await page.route('**/*',lambda route:route.abort())
            html=static.joinpath('index.html').read_text()
            # This is a document fixture at about:blank, not a request to the app.
            import re
            html=re.sub(r'<script.*?</script>','',html,flags=re.S)
            html=re.sub(r'<link[^>]*>','',html)
            await page.set_content(html)
            await page.add_style_tag(content=static.joinpath('style.css').read_text())
            await page.evaluate('''() => {
              const saved=new Map();Object.defineProperty(window,'localStorage',{value:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)}});
              window.fixture={profiles:[{id:'one',name:'接口一',base_url:'https://fixture.invalid/v1',model:'old-model',has_key:true,json_mode:true,timeout_seconds:90}],active:'one',writes:[]};
              window.fetch=async (url,options={})=>{
                const f=window.fixture,method=options.method||'GET',p=String(url).replace('/api/','');let data={};
                if(p==='auth/status') data={configured:true,logged_in:true};
                else if(p==='state')data={rules:[],items:0,unread:0,drops:0,queue:0,account:{logged_in:true,status:'logged_in'}};
                else if(p==='ai/profiles'&&method==='GET')data={profiles:f.profiles,active_id:f.active};
                else if(p.startsWith('ai/profiles')&&['PUT','POST'].includes(method)){
                  const body=JSON.parse(options.body),id=method==='POST'?'two':p.split('/')[2];f.writes.push({method,id,body});
                  const old=f.profiles.find(c=>c.id===id)||{};data={...old,...body,id,has_key:!!body.api_key||old.has_key};delete data.api_key;
                  f.profiles=method==='POST'?[...f.profiles,data]:f.profiles.map(c=>c.id===id?data:c);
                }else if(p.startsWith('events'))data=[1,2].map(id=>({id,rule_name:'camera',kind:'new',created:1,price:200,item:{id:String(id),title:'fixture camera',url:'https://www.goofish.com/item?id='+id,price:200}}));
                else if(p.startsWith('items'))data=[1,2].map(id=>({id:String(id),title:'fixture camera',url:'https://www.goofish.com/item?id='+id,price:200,rule_name:'camera',image:'/api/item-images/'+id}));
                else if(p==='logs'||p.startsWith('favorites'))data=[];
                return {ok:true,status:200,json:async()=>data};
              };
            }''')
            await page.add_script_tag(content=static.joinpath('app.js').read_text())
            await page.locator('nav [data-tab="ai"]').click()
            await page.locator('[data-profile="one"][data-op="edit"]').click()
            self.assertTrue(await page.locator('#aiProfileDialog').evaluate('(e)=>e.open'))
            self.assertEqual(await page.locator('#aiModel').input_value(),'old-model')
            await page.locator('#aiModel').fill('new-model')
            await page.locator('#aiProfileName').fill('修改后的接口')
            await page.locator('#saveAI').click()
            await page.wait_for_function('!document.getElementById("aiProfileDialog").open')
            self.assertEqual(await page.evaluate('fixture.writes[0].method'),'PUT')
            self.assertEqual(await page.evaluate('fixture.writes[0].id'),'one')
            await page.locator('[data-profile="one"][data-op="edit"]').click()
            self.assertEqual(await page.locator('#aiModel').input_value(),'new-model')
            self.assertEqual(await page.locator('#aiKey').input_value(),'')
            self.assertEqual(await page.locator('#aiClearKey').count(),0)
            await page.locator('#closeAIEditor').click()
            await page.locator('#addAIProfile').click()
            await page.locator('#aiProfileName').fill('第二套')
            await page.locator('#aiModel').fill('second-model')
            await page.locator('#aiKey').fill('fake-offline-key')
            await page.locator('#saveAI').click()
            await page.wait_for_function('fixture.profiles.length===2')
            await page.locator('[data-profile="two"][data-op="edit"]').click()
            self.assertEqual(await page.locator('#aiProfileName').input_value(),'第二套')
            await page.locator('#aiModel').fill('edited-second-model')
            await page.locator('#saveAI').click()
            await page.wait_for_function('fixture.profiles[1].model==="edited-second-model"')
            await page.locator('nav [data-tab="events"]').click()
            self.assertEqual(await page.locator('#eventsList .event-row').count(),2)
            boxes=await page.locator('.event-row').evaluate_all('(es)=>es.map(e=>({y:e.getBoundingClientRect().y,h:e.getBoundingClientRect().height}))')
            self.assertGreater(boxes[1]['y'],boxes[0]['y']+boxes[0]['h'])
            await page.locator('nav [data-tab="finds"]').click()
            self.assertEqual(await page.locator('#findsList .product').count(),2)
            self.assertEqual(await page.locator('#findsList').evaluate('(e)=>getComputedStyle(e).display'),'grid')
            self.assertEqual(await page.locator('.version').inner_text(),'v1.4.0')
            await page.locator('#themeToggle').click()
            self.assertEqual(await page.locator('html').get_attribute('data-theme'),'dark')
            self.assertFalse(errors,errors)
            await browser.close()

if __name__=='__main__':unittest.main()
