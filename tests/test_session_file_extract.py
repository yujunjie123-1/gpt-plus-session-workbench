"""Synthetic, in-memory document parsing tests; never use real sessions here."""
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.session_file_extract import extract_session


def synthetic_session(email='synthetic@example.invalid'):
    return {
        'user': {'email': email, 'name': 'Synthetic User'},
        'accessToken': 'SYNTHETIC-ACCESS-TOKEN',
        'sessionToken': 'SYNTHETIC-SESSION-TOKEN',
        'expires': '2099-01-01T00:00:00.000Z',
    }


def make_docx(text, table=False):
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    ET.register_namespace('w', ns)
    document = ET.Element(f'{{{ns}}}document')
    body = ET.SubElement(document, f'{{{ns}}}body')
    if table:
        table_element = ET.SubElement(body, f'{{{ns}}}tbl')
        row = ET.SubElement(table_element, f'{{{ns}}}tr')
        cell = ET.SubElement(row, f'{{{ns}}}tc')
        paragraph = ET.SubElement(cell, f'{{{ns}}}p')
    else:
        paragraph = ET.SubElement(body, f'{{{ns}}}p')
    run = ET.SubElement(paragraph, f'{{{ns}}}r')
    value = ET.SubElement(run, f'{{{ns}}}t')
    value.text = text
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', ET.tostring(document, encoding='utf-8', xml_declaration=True))
    return stream.getvalue()


class SessionFileExtractTest(unittest.TestCase):
    def test_extracts_compact_session_from_utf8_text_with_surrounding_text(self):
        session = synthetic_session('合成@example.invalid')
        payload = ('仅用于测试：\n' + json.dumps(session, ensure_ascii=False, indent=2) + '\n结束').encode('utf-8-sig')
        result = extract_session(payload, 'txt')
        self.assertEqual(result['email'], session['user']['email'])
        self.assertEqual(result['value'], json.dumps(session, ensure_ascii=False, separators=(',', ':')))
        self.assertNotIn('\n', result['value'])

    def test_extracts_gb18030_text(self):
        session = synthetic_session()
        payload = ('说明：这是合成测试\n' + json.dumps(session, ensure_ascii=False)).encode('gb18030')
        self.assertEqual(extract_session(payload, '.txt')['email'], session['user']['email'])

    def test_extracts_docx_paragraph_and_table_content(self):
        session = synthetic_session()
        body = '会话：' + json.dumps(session, ensure_ascii=False, indent=2)
        for is_table in (False, True):
            with self.subTest(table=is_table):
                result = extract_session(make_docx(body, table=is_table), 'docx')
                self.assertEqual(json.loads(result['value']), session)

    def test_rejects_missing_or_malformed_session(self):
        for payload, kind in ((b'just ordinary notes', 'txt'), (b'{"sessionToken":"partial"}', 'txt'),
                              (b'not a word file', 'docx')):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                extract_session(payload, kind)

    def test_rejects_ambiguous_multiple_sessions(self):
        first = json.dumps(synthetic_session('first@example.invalid'))
        second = json.dumps(synthetic_session('second@example.invalid'))
        with self.assertRaisesRegex(ValueError, '多个 Session'):
            extract_session((first + '\n' + second).encode(), 'txt')

    def test_rejects_unsupported_type_empty_and_oversized_file(self):
        with self.assertRaisesRegex(ValueError, '仅支持'):
            extract_session(b'x', 'doc')
        with self.assertRaisesRegex(ValueError, '为空'):
            extract_session(b'', 'txt')
        with self.assertRaisesRegex(ValueError, '10 MiB'):
            extract_session(b'x' * (10 * 1024 * 1024 + 1), 'txt')


if __name__ == '__main__':
    unittest.main()
