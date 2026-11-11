"""Browser integration for the local Session extraction page using synthetic data."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from app import make_server
from test_session_file_extract import make_docx, synthetic_session


class SessionFileBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / 'data')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 900})
        self.context.add_init_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.testClipboard=text}}})")
        self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.page.goto(f'http://{self.server.address}/?page=extract#{self.server.token}', wait_until='networkidle')
        expect(self.page.get_by_role('heading', name='Session 提取', exact=True)).to_be_visible()
        expect(self.page.locator('.loading-skeleton')).to_have_count(0)

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.thread.join(3)
        self.server.engine.close()
        self.server.server_close()
        self.server.store.db.close()
        self.server.instance_lock.close()
        self.temp.cleanup()

    def test_select_txt_extracts_and_copies_compact_json_then_clears(self):
        session = synthetic_session()
        path = Path(self.temp.name) / 'synthetic.txt'
        path.write_text('test fixture\n' + json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
        self.page.locator('input[type=file]').set_input_files(str(path))
        output = self.page.get_by_label('提取的单行 Session')
        expect(output).to_be_visible()
        expected = json.dumps(session, ensure_ascii=False, separators=(',', ':'))
        expect(output).to_have_value(expected)
        self.page.get_by_role('button', name='复制 Session').click()
        self.page.wait_for_function('window.testClipboard !== undefined')
        self.assertEqual(self.page.evaluate('window.testClipboard'), expected)
        self.page.get_by_role('button', name='清除结果').click()
        expect(output).to_have_count(0)

    def test_drop_docx_extracts_from_word_paragraphs(self):
        session = synthetic_session()
        payload = make_docx('合成样本：' + json.dumps(session, ensure_ascii=False, indent=2))
        self.page.locator('.session-dropzone').evaluate("(element, bytes) => { const file=new File([new Uint8Array(bytes)], 'synthetic.docx'); const data=new DataTransfer(); data.items.add(file); element.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:data})); }", list(payload))
        expect(self.page.get_by_label('提取的单行 Session')).to_have_value(json.dumps(session, ensure_ascii=False, separators=(',', ':')))

    def test_rejects_old_doc_and_orders_half_plus_first_in_cards_and_recharge(self):
        path = Path(self.temp.name) / 'legacy.doc'
        path.write_bytes(b'not supported')
        self.page.locator('input[type=file]').set_input_files(str(path))
        expect(self.page.get_by_role('alert')).to_contain_text('仅支持 .docx 和 .txt')

        navigation = self.page.get_by_role('navigation', name='主导航')
        navigation.get_by_role('button', name='卡密管理').click()
        expect(self.page.locator('.stock-tabs button').nth(0)).to_contain_text('半价 Plus')
        expect(self.page.locator('.stock-tabs button').nth(1)).to_contain_text('Plus')
        navigation.get_by_role('button', name='单个充值').click()
        expect(self.page.locator('.product-options button').nth(0)).to_contain_text('半价 Plus')
        expect(self.page.locator('.product-options button').nth(1)).to_contain_text('Plus')


if __name__ == '__main__':
    unittest.main()
