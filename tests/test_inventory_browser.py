"""Exercise the built UI against isolated data and a fake redemption provider."""
import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from app import make_server
from test_complete_flow import session
from test_inventory import StockProvider


class InventoryBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='msedge',headless=True)
        cls.artifacts=ROOT/'output'/'playwright'
        cls.artifacts.mkdir(parents=True,exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp=TemporaryDirectory()
        self.server=make_server(Path(self.temp.name)/'data')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.context=self.browser.new_context(viewport={'width':1530,'height':1000})
        self.context.add_init_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.testClipboard=text}}})")
        self.page=self.context.new_page()
        self.page.set_default_timeout(8000)
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.on('console',lambda message:self.errors.append(message.text) if message.type=='error' and 'favicon.ico' not in message.text else None)
        self.provider=StockProvider()
        async def provider(_):return self.provider
        self.server.engine.provider=provider

    def tearDown(self):
        result=getattr(self._outcome,'result',None)
        if any(test is self for test,_ in getattr(result,'failures',[])+getattr(result,'errors',[])):
            self.page.screenshot(path=str(self.artifacts/(self._testMethodName+'-failed.png')),full_page=True)
            (self.artifacts/(self._testMethodName+'-failed.json')).write_text(json.dumps({'errors':self.errors,'text':self.page.locator('body').inner_text(),'hidden':self.page.locator('[aria-hidden=true]').evaluate_all('nodes=>nodes.map(n=>n.outerHTML.slice(0,350))')},ensure_ascii=False,indent=2),encoding='utf-8')
        self.context.close()
        self.server.shutdown()
        self.thread.join(3)
        self.server.engine.close()
        self.server.server_close()
        self.server.store.db.close()
        self.server.instance_lock.close()
        self.temp.cleanup()

    def open(self,tab='accounts'):
        self.page.goto('http://'+self.server.address+'/?page='+tab+'#'+self.server.token,wait_until='networkidle')
        expect(self.page.get_by_role('heading',name={'accounts':'账号池','risk':'风控账号池','extract':'Session 提取','cards':'卡密管理','single':'单个充值','batch':'批量充值','records':'充值记录'}[tab],exact=True)).to_be_visible()
        expect(self.page.locator('.loading-skeleton')).to_have_count(0)

    def nav(self,label):
        self.page.get_by_role('navigation',name='主导航').get_by_role('button',name=label,exact=True).click()

    def seed(self,count=1,cards=3):
        ids=self.server.store.inventory_import_accounts([{'email':'owner@example.com' if index==0 else f'account{index+1}@example.com'} for index in range(count)])['ids']
        for key in ids:
            self.server.engine.sessions[key]=session(self.server.store.get(key)['email'])
        if cards:self.server.store.inventory_import_cards('plus',[f'UI-PLUS-CARD-{index+1:04}' for index in range(cards)])
        return ids

    def test_isolated_batch_exposes_five_and_ten_concurrency_and_completes_ten_assignments(self):
        keys=self.seed(10,10)
        providers={key:StockProvider(self.server.store.get(key)['email']) for key in keys}
        async def provider(key):return providers[key]
        self.server.engine.provider=provider
        self.open('batch')
        expect(self.page.get_by_text('独立无痕登录已启用',exact=True)).to_be_visible()
        selector=self.page.get_by_label('并发数量',exact=True)
        expect(selector.locator('option')).to_have_count(10)
        selector.select_option('5')
        expect(selector).to_have_value('5')
        selector.select_option('10')
        expect(selector).to_have_value('10')
        self.page.get_by_label('选择本页全部',exact=True).check()
        self.page.locator('.batch-start-actions').get_by_role('button',name='批量充值',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog.locator('.assignment-list > div')).to_have_count(10)
        dialog.get_by_role('button',name='确认充值',exact=True).click()
        expect(dialog).to_have_count(0)
        self.page.wait_for_function('''async()=>{
          const response=await fetch('/api/inventory/state',{headers:{Authorization:'Bearer '+sessionStorage.getItem('workbench-access')}});
          const data=await response.json();return data.tasks.length===10&&data.tasks.every(task=>task.status==='completed');
        }''',timeout=20000)
        self.assertEqual(self.server.engine.limit,10)
        self.assertTrue(all(provider.redeems==1 for provider in providers.values()))
        self.assertEqual(self.errors,[])
        self.page.screenshot(path=str(self.artifacts/'v220-ten-account-batch.png'),full_page=True)

    def test_account_pool_session_button_uses_default_five_and_has_visible_independent_control(self):
        from test_isolated_batch import Handle
        keys=self.server.store.inventory_import_accounts([{'email':f'queue-{index}@example.com'} for index in range(6)])['ids']
        with patch('workbench.edge_session.open_ordinary_edge',side_effect=lambda *args,**kwargs:Handle()) as launched:
            self.open()
            selector=self.page.get_by_label('Session 并发数量',exact=True)
            expect(selector).to_have_value('5')
            self.assertEqual(self.server.engine.limit,1)
            self.page.get_by_label('选择本页全部',exact=True).check()
            self.page.get_by_role('button',name='获取 Session',exact=True).click()
            expect(self.page.locator('.session-concurrency')).to_contain_text('处理中 5 · 排队 1')
            self.assertEqual(len(self.server.engine.edge_jobs()),5)
            self.assertEqual(launched.call_count,5)
            expect(selector.locator('option')).to_have_count(5)
            self.assertEqual(self.server.engine.session_limit,5)
            self.page.reload(wait_until='networkidle')
            expect(selector).to_have_value('5')
            self.server.engine.command('stop',keys)
            self.assertEqual(self.errors,[])

    def test_one_click_selects_only_missing_sessions_up_to_concurrency_and_available_count(self):
        keys=self.server.store.inventory_import_accounts([
            {'email':f'select-session-{index}@example.com'} for index in range(6)])['ids']
        self.server.engine.sessions[keys[0]]=session('select-session-0@example.com')
        self.server.store.update(keys[4],session_meta={'acquired_at':'2026-11-09T00:00:00+08:00'})
        self.open()
        selector=self.page.get_by_label('Session 并发数量',exact=True)
        selector.select_option('4')
        expect(selector).to_have_value('4')
        self.page.get_by_label('选择 select-session-0@example.com',exact=True).check()

        self.page.get_by_role('button',name='一键选择未获取 Session',exact=True).click()
        for key in [*keys[1:4],keys[5]]:
            expect(self.page.get_by_label('选择 '+self.server.store.get(key)['email'],exact=True)).to_be_checked()
        expect(self.page.get_by_label('选择 select-session-0@example.com',exact=True)).not_to_be_checked()
        expect(self.page.get_by_label('选择 '+self.server.store.get(keys[4])['email'],exact=True)).not_to_be_checked()
        expect(self.page.get_by_role('status')).to_contain_text('已选择 4 个未获取 Session 的账号')
        self.assertEqual(self.server.engine.jobs,{})

        for index,key in enumerate(keys[1:5],1):
            self.server.engine.sessions[key]=session(f'select-session-{index}@example.com')
        self.page.wait_for_function('''async()=>{
          const response=await fetch('/api/inventory/state',{headers:{Authorization:'Bearer '+sessionStorage.getItem('workbench-access')}});
          const data=await response.json();return data.accounts.filter(row=>row.session_info.state==='missing'&&!row.busy).length===1;
        }''')
        expect(self.page.get_by_text('未获取',exact=True)).to_have_count(1)
        self.page.get_by_role('button',name='一键选择未获取 Session',exact=True).click()
        expect(self.page.get_by_label('选择 '+self.server.store.get(keys[5])['email'],exact=True)).to_be_checked()
        for key in keys[:5]:
            expect(self.page.get_by_label('选择 '+self.server.store.get(key)['email'],exact=True)).not_to_be_checked()
        expect(self.page.get_by_role('status')).to_contain_text('已选择 1 个未获取 Session 的账号')
        self.assertEqual(self.server.engine.jobs,{})
        self.assertEqual(self.errors,[])

    def test_independent_imports_deduplication_search_and_five_stock_tabs(self):
        self.open()
        self.page.get_by_role('button',name='导入账号',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        dialog.get_by_label('账号资料',exact=True).fill('owner@example.com----TEST-PASSWORD--------\nsecond@example.com\nOWNER@example.com')
        dialog.get_by_label('资格标签（可选）').select_option('3')
        dialog.get_by_role('button',name='确认导入').click()
        expect(dialog).to_have_count(0)
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.assertEqual(len(self.server.store.cards),0)
        self.assertEqual(self.page.get_by_role('columnheader',name='卡密',exact=True).count(),0)
        self.assertIn('001',self.page.locator('tbody tr').first.inner_text())
        self.page.get_by_label('搜索账号',exact=True).fill('second')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.get_by_label('搜索账号',exact=True).fill('')
        self.nav('卡密管理')
        for sku,label in [('plus','Plus'),('plus_half','半价 Plus'),('pro100','Pro 100 (5×)'),('pro200','Pro 200 (10×)'),('pro500','Pro 500 (25×)')]:
            self.page.get_by_role('button',name=label,exact=True).click()
            self.page.get_by_role('button',name='导入卡密',exact=True).click()
            dialog=self.page.get_by_role('dialog')
            dialog.get_by_label('卡密资料',exact=True).fill(f'UI-{sku}-CARD-0001\nUI-{sku}-CARD-0001')
            dialog.get_by_role('button',name='确认导入').click()
            expect(dialog).to_have_count(0)
            expect(self.page.locator('tbody tr')).to_have_count(1)
            self.assertNotIn('owner@example.com',self.page.locator('main').inner_text())
        self.assertEqual(len(self.server.store.cards),5)
        self.assertEqual(len(self.server.store.rows),2)
        self.assertEqual(self.errors,[])

    def test_card_detail_explicitly_shows_and_copies_full_code(self):
        code='UI-CARD-DETAIL-SYNTHETIC-001'
        self.server.store.inventory_import_cards('plus',[code])
        self.open('cards')
        self.page.get_by_role('button',name='Plus',exact=True).click()
        self.assertNotIn(code,self.page.locator('main').inner_text())
        row=self.page.locator('tbody tr').first
        self.provider.redeems=1
        row.get_by_role('button',name='核对',exact=True).click()
        expect(row).to_contain_text('已使用')
        row.get_by_role('button',name='详情',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text(code)
        expect(dialog).to_contain_text('ow••••er@example.com')
        expect(dialog).to_contain_text('上游产品')
        expect(dialog).to_contain_text('上游套餐')
        expect(dialog).to_contain_text('上游使用时间')
        expect(dialog).to_contain_text('plus')
        dialog.get_by_role('button',name='复制完整卡密',exact=True).click()
        expect(dialog.locator('.copy-status')).to_have_text('已复制')
        self.assertEqual(self.page.evaluate('window.testClipboard'),code)
        self.assertEqual(self.errors,[])

    def test_session_extract_direct_recharge_creates_history_without_account(self):
        class DirectProvider:
            def __init__(self):self.redeems=0;self.calls=[]
            async def lookup(self,code):
                await asyncio.sleep(.2)
                return {'cdk_code':code,'status':'unused','used':False,'can_resubmit':True}
            async def call(self,operation,body):
                self.calls.append((operation,body))
                if operation=='preview':
                    return {'product':'ios','plan':'5x','redemption_token':'SYNTHETIC-DIRECT-QUALIFICATION'}
                if operation=='preflight':
                    return {'email':'direct@example.com','current_plan':'free',
                            'subscription_has_active':False,'preflight_token':'SYNTHETIC-DIRECT-PREFLIGHT'}
                raise AssertionError(operation)
            async def redeem(self,redemption_token,preflight_token,request_id):
                self.redeems+=1
                return {'status':'queued'}
            async def result(self,token):
                return {'status':'completed'}
            async def close(self):pass
        direct=DirectProvider()
        async def provider(_):return direct
        self.server.engine.provider=provider
        payload=json.dumps(session('direct@example.com'),separators=(',',':'))
        self.open('extract')
        self.page.get_by_label('选择 DOCX 或 TXT 文件',exact=True).set_input_files({
            'name':'session.txt','mimeType':'text/plain','buffer':payload.encode('utf-8')})
        expect(self.page.get_by_text('direct@example.com',exact=True)).to_be_visible()
        self.page.get_by_label('直充卡密',exact=True).fill('UI-DIRECT-CARD-SYNTHETIC-01')
        expect(self.page.get_by_label('直充规格',exact=True)).to_have_count(0)
        expect(self.page.get_by_text('充值规格',exact=True)).to_have_count(0)
        self.page.get_by_role('button',name='预检卡密并充值',exact=True).click()
        expect(self.page.locator('.direct-feedback')).to_contain_text('正在核对')
        expect(self.page.locator('.direct-quote')).to_contain_text('ios / 5x')
        self.page.locator('.direct-quote').get_by_role('button',name='确认直充',exact=True).click()
        self.page.wait_for_function('''async()=>{
          const response=await fetch('/api/inventory/state',{headers:{Authorization:'Bearer '+sessionStorage.getItem('workbench-access')}});
          const state=await response.json();return state.tasks.length===1&&state.tasks[0].status==='completed';
        }''',timeout=12000)
        expect(self.page.locator('.direct-feedback')).to_contain_text('自动记账',timeout=8000)
        state=self.page.evaluate('''async()=>{const response=await fetch('/api/inventory/state',{headers:{Authorization:'Bearer '+sessionStorage.getItem('workbench-access')}});return response.json()}''')
        self.assertEqual(state['accounts'],[])
        self.assertEqual(state['cards'],[])
        self.assertEqual(state['tasks'][0]['source'],'session_extract')
        self.assertEqual(state['tasks'][0]['email'],'direct@example.com')
        self.assertEqual((state['tasks'][0]['provider_product'],state['tasks'][0]['provider_plan']),('ios','5x'))
        calls=dict(direct.calls)
        self.assertNotIn('sku',calls['preview'])
        self.assertNotIn('sku',calls['preflight'])
        self.assertEqual(direct.redeems,1)
        self.assertNotIn('sessionToken',json.dumps(state))
        self.open('records')
        expect(self.page.locator('tbody tr')).to_contain_text('ios / 5x')
        self.page.locator('tbody tr').get_by_role('button',name='详情',exact=True).click()
        detail=self.page.get_by_role('dialog',name='充值记录 1')
        expect(detail).to_contain_text('自动归属邮箱')
        expect(detail).to_contain_text('direct@example.com')
        self.assertEqual(detail.get_by_role('button',name='确认已到账到此账号',exact=True).count(),0)
        detail.get_by_role('button',name='关闭',exact=True).last.click()
        with self.page.expect_download() as download_info:
            self.page.get_by_role('button',name='导出记录',exact=True).click()
        downloaded=download_info.value
        export_path=Path(self.temp.name)/'direct-records.csv'
        downloaded.save_as(str(export_path))
        exported=export_path.read_text(encoding='utf-8-sig')
        self.assertIn('ios / 5x',exported)
        for secret in ('SYNTHETIC-DIRECT-SESSION-SECRET','SYNTHETIC-DIRECT-QUALIFICATION','UI-DIRECT-CARD-SYNTHETIC-01'):
            self.assertNotIn(secret,exported)
        self.assertEqual(self.errors,[])

    def test_half_plus_spec_edit_on_mobile_keeps_failed_history_and_changes_only_stock(self):
        key=self.seed(cards=1)[0]
        card=next(iter(self.server.store.cards))
        failed=self.server.store.inventory_reserve(self.server.store.inventory_quote([key],'plus'))[0]
        self.server.store.update(key,status='error',submitted=False,message='Synthetic classification failure')
        self.page.set_viewport_size({'width':390,'height':844})
        self.open('cards')
        self.page.get_by_role('button',name='Plus',exact=True).click()
        self.page.locator('tbody tr').get_by_role('button',name='修改规格',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text('原规格：Plus')
        dialog.get_by_label('新的卡密规格',exact=True).select_option('plus_half')
        dialog.get_by_role('button',name='保存规格',exact=True).click()
        expect(dialog).to_have_count(0)
        expect(self.page.locator('tbody tr')).to_contain_text('半价 Plus')
        self.assertEqual(self.server.store.cards[card]['sku'],'plus_half')
        self.assertEqual(self.server.store.charge_tasks[failed]['sku'],'plus')
        self.assertEqual(self.provider.redeems,0)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),391)
        self.page.screenshot(path=str(self.artifacts/'v232-half-plus-stock-mobile.png'),full_page=True)
        self.page.get_by_role('button',name='Plus',exact=True).click()
        expect(self.page.locator('tbody tr')).to_have_count(0)
        self.nav('充值记录')
        expect(self.page.locator('tbody tr')).to_contain_text('Plus')
        expect(self.page.locator('tbody tr')).not_to_contain_text('半价 Plus')
        self.assertEqual(self.errors,[])

    def test_half_plus_single_confirmation_progress_history_and_export_remain_distinct(self):
        key=self.seed(cards=1)[0]
        self.server.store.inventory_import_cards('plus_half',['SYNTHETIC-HALF-UI'])
        class HalfProvider(StockProvider):
            async def call(self,kind,body):
                if kind=='preview':return {'product':'gpt','plan':'plus_half','redemption_token':'SYNTHETIC-HALF-TOKEN'}
                return await super().call(kind,body)
        self.provider=HalfProvider()
        self.open('single')
        self.page.get_by_label('选择充值账号',exact=True).select_option(key)
        self.page.get_by_role('group',name='充值规格',exact=True).get_by_role('button',name='半价 Plus 1 张可用',exact=True).click()
        self.page.get_by_role('button',name='一键充值',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text('使用 半价 Plus 卡密')
        self.assertEqual(self.provider.redeems,0)
        dialog.get_by_role('button',name='确认充值',exact=True).click()
        expect(self.page.locator('[data-testid=task-progress]')).to_contain_text('充值成功')
        expect(self.page.locator('.result-facts').last).to_contain_text('半价 Plus')
        self.assertEqual(self.provider.redeems,1)
        self.assertEqual(next(card for card in self.server.store.cards.values() if card['sku']=='plus')['status'],'unused')
        self.page.screenshot(path=str(self.artifacts/'v232-half-plus-single-synthetic.png'),full_page=True)
        self.nav('卡密管理')
        self.page.get_by_role('button',name='半价 Plus',exact=True).click()
        expect(self.page.locator('tbody tr').get_by_role('button',name='修改规格',exact=True)).to_be_disabled()
        self.nav('充值记录')
        expect(self.page.locator('tbody tr')).to_contain_text('半价 Plus')
        with self.page.expect_download() as download:
            self.page.get_by_role('button',name='导出记录',exact=True).click()
        file=Path(self.temp.name)/'synthetic-half-results.csv'
        download.value.save_as(str(file))
        self.assertIn('半价 Plus',file.read_text(encoding='utf-8-sig'))
        self.assertEqual(self.errors,[])

    def test_half_plus_batch_confirmation_only_allocates_half_stock(self):
        self.seed(count=2,cards=2)
        self.server.store.inventory_import_cards('plus_half',['SYNTHETIC-HALF-BATCH-1','SYNTHETIC-HALF-BATCH-2'])
        self.open('batch')
        self.page.get_by_role('group',name='充值规格',exact=True).get_by_role('button',name='半价 Plus 2 张可用',exact=True).click()
        self.page.get_by_label('选择本页全部',exact=True).check()
        self.server.engine.command('pause',[])
        self.page.locator('.batch-start-actions').get_by_role('button',name='批量充值',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text('使用 半价 Plus 卡密')
        dialog.get_by_role('button',name='确认充值',exact=True).click()
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.assertTrue(all(task['sku']=='plus_half' for task in self.server.store.charge_tasks.values()))
        self.assertTrue(all(card['status']=='unused' for card in self.server.store.cards.values() if card['sku']=='plus'))
        self.page.get_by_role('button',name='停止本批',exact=True).click()
        expect(self.page.locator('.notice')).to_contain_text('已停止 2 个本地任务')
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.errors,[])

    def test_session_lifetime_beijing_status_scope_switch_and_refresh_button(self):
        from workbench.session_lifecycle import iso
        ids=self.server.store.inventory_import_accounts([
            {'email':'lifetime-ready@example.com','password':'SYNTHETIC-PASSWORD'},
            {'email':'lifetime-expired@example.com','password':'SYNTHETIC-PASSWORD'},
            {'email':'lifetime-used@example.com','password':'SYNTHETIC-PASSWORD'},
            {'email':'lifetime-missing@example.com'},
        ])['ids']
        self.server.engine.remember_session(ids[0],{**session('lifetime-ready@example.com'),'expires':'2030-01-01T00:00:00Z'})
        self.server.engine.sessions[ids[1]]={**session('lifetime-expired@example.com'),'expires':'2020-01-01T00:00:00Z'}
        self.server.store.update(ids[2],last_successful_recharge={'status':'completed'})
        calls=[]
        async def login(key,**kwargs):
            calls.append(key)
            self.server.engine.remember_session(key,session(self.server.store.get(key)['email']))
            self.server.store.update(key,status='ready')
        self.server.engine.edge_login=login
        self.open()
        switch=self.page.get_by_role('switch',name='自动更新 Session',exact=True)
        expect(switch).not_to_be_checked()
        ready=self.page.locator('tbody tr').filter(has_text='lifetime-ready@example.com')
        expect(ready).to_contain_text('可用（本地检查）')
        expect(ready).to_contain_text('2030/1/1 08:00:00')
        expect(ready).to_contain_text('令牌到期：未知')
        expect(ready).to_contain_text('服务端：尚未验证')
        expect(self.page.locator('tbody tr').filter(has_text='lifetime-expired@example.com')).to_contain_text('已过期')
        self.page.screenshot(path=str(self.artifacts/'v230-session-lifetimes.png'),full_page=True)
        switch.check()
        expect(switch).to_be_checked()
        expect(self.page.locator('tbody tr').filter(has_text='lifetime-expired@example.com')).to_contain_text('可用（本地检查）')
        self.assertEqual(calls,[ids[1]])
        switch.uncheck()
        expect(switch).not_to_be_checked()
        self.page.get_by_role('button',name='更新符合条件的 Session',exact=True).click()
        expect(self.page.locator('.notice')).to_contain_text('已排队 2 个账号')
        self.assertNotIn(ids[2],calls)
        self.assertNotIn(ids[3],calls)
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.server.store.charge_tasks,{})
        self.assertEqual(self.errors,[])

    def test_account_pool_copies_uncharged_and_charged_accounts_without_login(self):
        ids=self.server.store.inventory_import_accounts([
            {'email':'copy-unpaid@example.com','password':'TEST-COPY-%--密码','totp':'ABCDEFGHIJKLMNOP','eligibility_months':3},
            {'email':'copy-charged@example.com','eligibility_months':1},
        ])['ids']
        self.server.store.update(ids[1],last_successful_recharge={'status':'completed','sku':'plus'})
        origin='http://'+self.server.address
        response=self.context.request.post(origin+'/api/inventory/copy-account',data={'id':ids[0]},headers={'Origin':origin})
        self.assertEqual(response.status,403)
        response=self.context.request.post(origin+'/api/inventory/copy-account',data={'id':ids[0]},
            headers={'Origin':'https://example.com','Authorization':'Bearer '+self.server.token})
        self.assertEqual(response.status,403)
        self.open()
        row=self.page.locator('tbody tr').filter(has_text='copy-unpaid@example.com')
        row.get_by_role('button',name='一键复制',exact=True).click()
        expect(self.page.locator('.notice')).to_contain_text('账号资料已复制')
        self.assertEqual(self.page.evaluate('window.testClipboard'),
            '邮箱账号：copy-unpaid@example.com\nGPT密码：TEST-COPY-%--密码\n2FA密钥：ABCDEFGHIJKLMNOP\n在线获取2FA网站：https://2fa.vip/index.html')
        self.assertNotIn('TEST-COPY-%--密码',self.page.locator('body').inner_text())
        self.page.get_by_role('button',name='已充值',exact=True).click()
        expect(self.page.locator('tbody tr')).to_have_count(1)
        # A prior success does not gate the copy action, including on a phone.
        self.page.set_viewport_size({'width':390,'height':844})
        button=self.page.get_by_role('button',name='一键复制',exact=True)
        button.scroll_into_view_if_needed()
        expect(button).to_be_in_viewport()
        button.click()
        expect(self.page.locator('.notice')).to_contain_text('账号资料已复制')
        self.page.wait_for_function("window.testClipboard?.includes('copy-charged@example.com')")
        self.assertIn('GPT密码：未配置\n2FA密钥：未配置',self.page.evaluate('window.testClipboard'))
        self.assertEqual(self.server.engine.sessions,{})
        self.assertEqual(self.server.store.charge_tasks,{})
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.errors,[])

    def test_account_copy_clipboard_denial_has_selectable_fallback_and_errors_are_clear(self):
        self.server.store.inventory_import_accounts([{'email':'copy-fallback@example.com','password':'FALLBACK-TEST'}])
        self.open()
        self.page.evaluate("()=>{navigator.clipboard.writeText=async()=>{throw new Error('clipboard denied')};document.execCommand=()=>false}")
        self.page.get_by_role('button',name='一键复制',exact=True).click()
        dialog=self.page.get_by_role('dialog',name='复制账号资料',exact=True)
        expect(dialog).to_be_visible()
        field=dialog.get_by_label('待复制的账号资料')
        self.assertIn('GPT密码：FALLBACK-TEST',field.input_value())
        field.focus()
        self.assertTrue(field.evaluate('node=>node.selectionStart===0&&node.selectionEnd===node.value.length'))
        self.page.evaluate('()=>{navigator.clipboard.writeText=async text=>{window.testClipboard=text}}')
        dialog.get_by_role('button',name='复制资料',exact=True).click()
        expect(dialog).to_have_count(0)
        expect(self.page.locator('.notice')).to_contain_text('账号资料已复制')
        self.assertNotIn('FALLBACK-TEST',self.page.locator('body').inner_text())
        self.page.route('**/api/inventory/copy-account',lambda route:route.fulfill(status=400,
            content_type='application/json',body=json.dumps({'error':'记录不存在'})))
        self.page.get_by_role('button',name='一键复制',exact=True).click()
        expect(self.page.locator('.notice-error')).to_contain_text('记录不存在')
        expect(self.page.get_by_role('dialog')).to_have_count(0)
        expect(self.page.get_by_role('button',name='一键复制',exact=True)).to_be_enabled()

    def test_account_copy_marks_only_clicked_row_and_replaces_previous_session_clipboard(self):
        keys=self.server.store.inventory_import_accounts([
            {'email':'copy-first@example.com'},
            {'email':'copy-second@example.com'},
        ])['ids']
        self.server.engine.sessions[keys[0]]=session('copy-first@example.com')
        self.open()
        first=self.page.locator('tbody tr').filter(has_text='copy-first@example.com')
        second=self.page.locator('tbody tr').filter(has_text='copy-second@example.com')
        self.page.evaluate('''()=>{navigator.clipboard.write=async items=>{
          window.copyWriteStarted=true;
          const blob=await items[0].getType('text/plain');
          window.testClipboard=await blob.text();
        }}''')
        self.page.evaluate('''()=>{
          const original=window.fetch.bind(window);
          window.fetch=async(...args)=>{
            if(String(args[0]).includes('/api/inventory/copy-account'))
              await new Promise(resolve=>setTimeout(resolve,500));
            return original(...args);
          };
        }''')
        first.get_by_role('button',name='复制 Session',exact=True).click()
        expect(self.page.locator('.notice')).to_contain_text('Session 已复制')
        self.assertIn('sessionToken',self.page.evaluate('window.testClipboard'))
        self.page.evaluate('window.copyWriteStarted=false')
        first.get_by_role('button',name='一键复制',exact=True).click()
        self.assertTrue(self.page.evaluate('window.copyWriteStarted'))
        expect(first.get_by_role('button',name='正在复制',exact=True)).to_be_disabled()
        expect(second.get_by_role('button',name='一键复制',exact=True)).to_be_enabled()
        expect(self.page.locator('.notice')).to_contain_text('账号资料已复制')
        expect(first.get_by_role('button',name='一键复制',exact=True)).to_be_enabled()
        expect(second.get_by_role('button',name='一键复制',exact=True)).to_be_enabled()
        copied=self.page.evaluate('window.testClipboard')
        self.assertIn('邮箱账号：copy-first@example.com',copied)
        self.assertNotIn('sessionToken',copied)
        expect(first.get_by_role('checkbox')).not_to_be_checked()
        expect(second.get_by_role('checkbox')).not_to_be_checked()
        self.assertEqual(self.errors,[])

    def test_session_view_copy_is_single_line_and_never_reserves_stock(self):
        key=self.seed()[0]
        self.open()
        row=self.page.locator('tbody tr').filter(has_text='owner@example.com')
        row.get_by_role('button',name='复制 Session',exact=True).click()
        self.page.wait_for_function("window.testClipboard?.includes('owner@example.com')")
        direct=self.page.evaluate('window.testClipboard')
        self.assertNotIn('\n',direct)
        self.assertEqual(json.loads(direct)['user']['email'],'owner@example.com')
        expect(self.page.get_by_role('dialog')).to_have_count(0)
        self.page.get_by_role('button',name='详情',exact=True).first.click()
        self.page.get_by_role('button',name='查看 / 复制 Session',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_have_count(1)
        value=self.page.get_by_label('转换后的单行 Session').input_value()
        self.assertNotIn('\n',value)
        self.assertEqual(json.loads(value)['user']['email'],'owner@example.com')
        self.page.get_by_role('button',name='复制 Session',exact=True).click()
        expect(self.page.locator('.copy-status')).to_have_text('已复制')
        self.assertEqual(self.page.evaluate('window.testClipboard'),value)
        self.page.get_by_role('button',name='关闭',exact=True).last.click()
        expect(self.page.get_by_label('转换后的单行 Session')).to_have_count(0)
        self.page.get_by_label('选择 owner@example.com',exact=True).check()
        self.page.get_by_role('button',name='获取 Session',exact=True).click()
        expect(self.page.get_by_role('status').filter(has_text='同时获取最多')).to_be_visible()
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(len(self.server.store.charge_tasks),0)
        self.assertTrue(all(card['status']=='unused' for card in self.server.store.cards.values()))
        self.assertIn(key,self.server.engine.sessions)
        self.assertEqual(self.errors,[])

    def test_success_with_different_provider_email_is_auto_attributed_without_confirmation_ui(self):
        key=self.seed(cards=0)[0]
        self.server.store.inventory_import_cards('plus',['SYNTHETIC-ATTRIBUTION-CARD'])
        task_id=self.server.store.inventory_reserve(self.server.store.inventory_quote([key],'plus'))[0]
        self.server.store.update(key,status='pending',submitted=True,message='Synthetic submission')
        self.server.engine._inventory_call(self.server.engine.apply_lookup(key,{
            'status':'used','used':True,'account_email':'other@example.com'}))
        task=self.server.store.charge_tasks[task_id]
        self.assertEqual(task['status'],'completed')
        self.assertEqual(task['result_email'],'owner@example.com')
        self.assertEqual(task['provider_result_email'],'other@example.com')
        self.open('records')
        row=self.page.locator('tbody tr').filter(has_text='owner@example.com')
        expect(row).to_contain_text('充值成功')
        row.get_by_role('button',name='详情',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text('自动归属邮箱')
        expect(dialog).to_contain_text('owner@example.com')
        expect(dialog).to_contain_text('other@example.com')
        self.assertEqual(dialog.get_by_role('button',name='确认已到账到此账号',exact=True).count(),0)
        self.assertEqual(dialog.get_by_role('button',name='确认由其他邮箱使用',exact=True).count(),0)
        self.assertTrue(self.server.store.inventory_public()['accounts'][0]['has_recharged'])
        self.page.get_by_role('button',name='关闭',exact=True).last.click()
        self.nav('卡密管理')
        self.page.get_by_role('button',name='Plus',exact=True).click()
        expect(self.page.locator('tbody tr')).to_contain_text('已使用')
        self.assertEqual(self.errors,[])

    def test_phone_flexible_import_direct_risk_actions_and_consumed_card_delete(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.open()
        self.page.get_by_role('button',name='导入账号',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        dialog.get_by_label('账号资料',exact=True).fill('synthetic1@example.com--Test%Password--ABCDEFGHIJKLMNOP\nsynthetic2@example.com//AnotherPassword\nsynthetic3@example.com----Password----ABCDEFGHIJKLMNOP')
        dialog.get_by_role('button',name='确认导入',exact=True).click()
        expect(dialog).to_have_count(0)
        expect(self.page.locator('tbody tr')).to_have_count(3)
        row=self.page.locator('tbody tr').filter(has_text='synthetic1@example.com')
        expect(row.get_by_role('button',name='复制 Session',exact=True)).to_be_disabled()
        self.assertEqual(row.get_by_role('button',name='移入风控池',exact=True).count(),0)
        row.get_by_role('checkbox').check()
        toolbar=self.page.locator('.selection-bar')
        buttons=toolbar.get_by_role('button').all_text_contents()
        self.assertLess(buttons.index('移入风控池'),buttons.index('删除'))
        move=toolbar.get_by_role('button',name='移入风控池',exact=True)
        move.scroll_into_view_if_needed()
        expect(move).to_be_in_viewport()
        box=move.bounding_box()
        self.assertGreaterEqual(box['x'],0)
        self.assertLessEqual(box['x']+box['width'],390)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),391)
        move.click()
        self.page.get_by_role('button',name='确认移入',exact=True).click()
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.nav('风控账号池')
        self.page.get_by_role('button',name='添加风控账号',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        dialog.get_by_label('账号资料',exact=True).fill('synthetic2@example.com\nsynthetic4@example.com')
        dialog.get_by_role('button',name='添加到风控池',exact=True).click()
        expect(dialog).to_have_count(0)
        expect(self.page.locator('tbody tr')).to_have_count(3)
        self.assertTrue(all(row['risk']['active'] for row in self.server.store.rows.values() if row['email']!='synthetic3@example.com'))
        self.nav('单个充值')
        expect(self.page.get_by_label('选择充值账号',exact=True).locator('option')).to_have_count(2)
        key=next(row['id'] for row in self.server.store.rows.values() if row['email']=='synthetic3@example.com')
        self.server.store.inventory_import_cards('plus',['SYNTHETIC-CONSUMED'])
        self.server.store.inventory_reserve(self.server.store.inventory_quote([key],'plus'))
        self.server.store.update(key,status='completed',submitted=True)
        self.nav('卡密管理')
        self.page.get_by_role('button',name='Plus',exact=True).click()
        expect(self.page.locator('tbody tr')).to_have_count(1)
        expect(self.page.locator('tbody tr')).to_contain_text('已使用')
        delete=self.page.locator('tbody tr').get_by_role('button',name='删除',exact=True)
        delete.scroll_into_view_if_needed()
        expect(delete).to_be_in_viewport()
        delete.click()
        expect(self.page.get_by_role('dialog')).to_contain_text('重复导入也不会变成可用卡密')
        self.page.get_by_role('button',name='确认删除',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_have_count(0)
        expect(self.page.locator('tbody tr')).to_have_count(0)
        self.nav('充值记录')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.screenshot(path=str(self.artifacts/'v221-mobile-history-retained.png'),full_page=True)
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.errors,[])

    def test_single_recharge_one_confirmation_then_synchronizes_and_survives_reload(self):
        keys=self.seed()
        self.open('single')
        self.page.get_by_label('选择充值账号',exact=True).select_option(keys[0])
        self.page.get_by_role('button',name='一键充值',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_contain_text('owner@example.com')
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(len(self.server.store.charge_tasks),0)
        self.page.get_by_role('button',name='确认充值',exact=True).click()
        expect(self.page.locator('[data-testid=task-progress]')).to_contain_text('充值成功',timeout=10000)
        self.assertEqual(self.provider.redeems,1)
        self.assertEqual(sum(card['status']=='used' for card in self.server.store.cards.values()),1)
        expect(self.page.get_by_role('dialog')).to_have_count(0)
        expect(self.page.locator('.result-facts').first).to_contain_text('owner@example.com')
        self.page.screenshot(path=str(self.artifacts/'v2-single-success.png'),full_page=True)
        self.nav('账号池')
        expect(self.page.locator('tbody tr').first).to_contain_text('充值成功')
        self.nav('卡密管理')
        self.page.get_by_role('button',name='Plus',exact=True).click()
        expect(self.page.locator('tbody tr').first).to_contain_text('已使用')
        self.nav('充值记录')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.reload(wait_until='networkidle')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.assertEqual(self.provider.redeems,1)
        self.assertEqual(self.errors,[])

    def test_account_picker_search_repeated_selection_and_empty_result_remain_usable(self):
        keys=self.seed(count=24)
        self.open('single')
        picker=self.page.get_by_label('选择充值账号',exact=True)
        for key in keys[:6]+list(reversed(keys[:6])):
            picker.click()
            picker.select_option(key)
            expect(picker).to_have_value(key)
            expect(self.page.get_by_role('heading',name='创建充值',exact=True)).to_be_visible()
        self.page.get_by_label('搜索充值账号',exact=True).fill('account20')
        picker.select_option(keys[19])
        self.page.get_by_label('搜索充值账号',exact=True).fill('does-not-exist')
        expect(self.page.get_by_text('没有匹配的邮箱，请调整搜索。',exact=True)).to_be_visible()
        expect(picker).to_have_value(keys[19])
        self.page.get_by_label('搜索充值账号',exact=True).fill('')
        picker.select_option('')
        expect(self.page.get_by_role('button',name='一键充值',exact=True)).to_be_disabled()
        picker.select_option(keys[0])
        self.page.get_by_role('button',name='查看数据保存位置',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_contain_text(str(self.server.store.path))
        self.page.get_by_role('button',name='关闭',exact=True).last.click()
        self.nav('账号池')
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.errors,[])

    def test_disconnected_backend_does_not_pretend_the_database_is_empty(self):
        self.page.route('**/api/inventory/state',lambda route:route.abort())
        self.page.goto('http://'+self.server.address+'/#'+self.server.token)
        expect(self.page.get_by_role('heading',name='尚未读取到已保存的数据',exact=True)).to_be_visible()
        expect(self.page.locator('.data-table')).to_have_count(0)
        self.page.unroute('**/api/inventory/state')
        self.page.get_by_role('button',name='重新连接后端',exact=True).click()
        expect(self.page.locator('.data-table')).to_have_count(1)

    def test_batch_reserves_exact_stock_and_pause_stop_preserves_unused_cards(self):
        self.seed(count=3,cards=2)
        self.open('batch')
        self.page.get_by_label('选择本页全部',exact=True).check()
        expect(self.page.locator('.batch-start')).to_contain_text('卡密不足')
        self.assertTrue(self.page.get_by_role('button',name='批量充值',exact=True).last.is_disabled())
        self.page.get_by_label('选择 account3@example.com',exact=True).uncheck()
        self.server.engine.command('pause',[])
        self.page.get_by_role('button',name='批量充值',exact=True).last.click()
        expect(self.page.locator('.assignment-list>div')).to_have_count(2)
        self.assertEqual(len(self.server.store.charge_tasks),0)
        self.page.get_by_role('button',name='确认充值',exact=True).click()
        expect(self.page.get_by_role('button',name='本次进度',exact=True)).to_have_attribute('aria-pressed','true')
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.assertEqual(sum(card['status']=='reserved' for card in self.server.store.cards.values()),2)
        self.assertEqual(self.provider.redeems,0)
        self.page.get_by_role('button',name='停止本批',exact=True).click()
        expect(self.page.locator('.notice')).to_contain_text('已停止 2 个本地任务')
        self.assertTrue(all(card['status']=='unused' for card in self.server.store.cards.values()))
        self.assertEqual(self.errors,[])

    def test_responsive_themes_screenshots_and_accessibility(self):
        keys=self.seed(count=8,cards=4)
        self.server.store.quarantine_account(keys[-1],'approve_not_allowed')
        self.server.store.inventory_import_cards('pro100',['UI-PRO100-STOCK'])
        self.open()
        axe=(ROOT/'frontend'/'node_modules'/'axe-core'/'axe.min.js').read_text(encoding='utf-8')
        self.context.route('**/assets/axe-audit.js',lambda route:route.fulfill(content_type='text/javascript',body=axe))
        self.page.add_script_tag(url='/assets/axe-audit.js')
        report=[]
        for theme in ('light','dark'):
            self.page.get_by_label('外观主题').select_option(theme)
            expect(self.page.locator('html')).to_have_attribute('data-theme',theme)
            for label,slug in [('账号池','accounts'),('风控账号池','risk'),('Session 提取','extract'),('卡密管理','cards'),('单个充值','single'),('批量充值','batch'),('充值记录','records')]:
                self.nav(label)
                violations=self.page.evaluate("async()=> (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}))")
                report.append({'page':slug,'theme':theme,'violations':violations})
                for width in (1530,1100,390):
                    self.page.set_viewport_size({'width':width,'height':1000})
                    self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width+1,(theme,slug,width))
                    if width!=1100:
                        self.page.screenshot(path=str(self.artifacts/f'v2-{slug}-{theme}-{width}.png'),full_page=True)
                self.page.set_viewport_size({'width':1530,'height':1000})
        (self.artifacts/'v2-accessibility.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        self.assertEqual([item for item in report if item['violations']],[])
        self.assertEqual(self.errors,[])

    def test_risk_and_successful_accounts_are_excluded_and_manual_restore_reenters_selection(self):
        keys=self.seed(count=3,cards=3)
        self.server.store.quarantine_account(keys[1],'approve_not_allowed')
        self.server.store.inventory_reserve(self.server.store.inventory_quote([keys[2]],'plus'))
        self.server.store.update(keys[2],status='completed',submitted=True)
        self.open('single')
        picker=self.page.get_by_label('选择充值账号',exact=True)
        self.assertEqual(picker.locator('option').evaluate_all('nodes=>nodes.map(n=>n.value)'),['',keys[0]])
        self.nav('批量充值')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.get_by_label('选择本页全部',exact=True).check()
        self.server.store.quarantine_account(keys[0],'card_declined')
        expect(self.page.locator('tbody tr')).to_have_count(0)
        expect(self.page.locator('.batch-start')).to_contain_text('分配 0 张')
        expect(self.page.get_by_role('button',name='批量充值',exact=True).last).to_be_disabled()
        self.nav('风控账号池')
        expect(self.page.locator('tbody tr')).to_have_count(2)
        expect(self.page.locator('main')).to_contain_text('银行卡拒付')
        expect(self.page.locator('main')).to_contain_text('approve 未允许')
        self.page.get_by_label('搜索风控账号',exact=True).fill('account2')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.get_by_role('button',name='移回普通池',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_contain_text('不会自动重试或兑换')
        self.page.get_by_role('button',name='确认移回',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_have_count(0)
        self.nav('单个充值')
        self.assertEqual(picker.locator('option').evaluate_all('nodes=>nodes.map(n=>n.value)'),['',keys[1]])
        self.nav('账号池')
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.assertEqual(self.provider.redeems,0)
        self.assertEqual(self.errors,[])

    def test_payment_failure_moves_to_risk_pool_and_remains_after_reload(self):
        from test_risk_pool import PaymentFailureProvider
        self.seed(count=1,cards=1)
        self.provider=PaymentFailureProvider()
        self.open('single')
        key=next(iter(self.server.store.rows))
        self.page.get_by_label('选择充值账号',exact=True).select_option(key)
        self.page.get_by_role('button',name='一键充值',exact=True).click()
        self.page.get_by_role('button',name='确认充值',exact=True).click()
        expect(self.page.locator('[data-testid=task-progress]')).to_contain_text('银行卡拒付')
        expect(self.page.locator('[data-testid=task-progress]')).to_contain_text('已移入风控池')
        expect(self.page.get_by_label('选择充值账号',exact=True).locator('option')).to_have_count(1)
        self.nav('风控账号池')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        expect(self.page.locator('tbody tr')).to_contain_text('已养号 0 天')
        self.page.reload(wait_until='networkidle')
        expect(self.page.locator('tbody tr')).to_have_count(1)
        self.page.get_by_role('button',name='详情',exact=True).click()
        expect(self.page.get_by_role('dialog')).to_contain_text('银行卡拒付')
        expect(self.page.get_by_role('button',name='移回普通账号池',exact=True)).to_be_enabled()
        self.assertEqual(self.provider.redeems,1)
        self.assertEqual(next(iter(self.server.store.cards.values()))['status'],'unused')
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main(verbosity=2)
