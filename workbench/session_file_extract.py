"""In-memory extraction of a single Session JSON object from local documents."""
from io import BytesIO
import json
import zipfile
import xml.etree.ElementTree as ET


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 25 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000
WORD_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _document_text(payload):
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise ValueError('DOCX 文件结构过于复杂')
            info = archive.getinfo('word/document.xml')
            if info.file_size > MAX_DOCUMENT_XML_BYTES:
                raise ValueError('DOCX 文本超过解析上限')
            xml = bytearray()
            with archive.open(info) as document:
                while chunk := document.read(65536):
                    if len(xml) + len(chunk) > MAX_DOCUMENT_XML_BYTES:
                        raise ValueError('DOCX 文本超过解析上限')
                    xml.extend(chunk)
            root = ET.fromstring(xml)
    except ValueError:
        raise
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError, ET.ParseError):
        raise ValueError('无法读取 DOCX，请确认文件未损坏') from None

    chunks = []
    for element in root.iter():
        if element.tag == WORD_NS + 't':
            chunks.append(element.text or '')
        elif element.tag == WORD_NS + 'tab':
            chunks.append('\t')
        elif element.tag == WORD_NS + 'br':
            chunks.append('\n')
        elif element.tag == WORD_NS + 'p':
            chunks.append('\n')
    return ''.join(chunks)


def _candidate(value):
    if not isinstance(value, dict):
        return False
    user = value.get('user')
    return (isinstance(user, dict)
            and isinstance(user.get('email'), str) and bool(user['email'].strip())
            and isinstance(value.get('accessToken'), str) and bool(value['accessToken'].strip())
            and isinstance(value.get('sessionToken'), str) and bool(value['sessionToken'].strip())
            and isinstance(value.get('expires'), str) and bool(value['expires'].strip()))


def _find_sessions(text):
    decoder = json.JSONDecoder()
    sessions = []
    position = 0
    attempts = 0
    while position < len(text):
        start = text.find('{', position)
        if start < 0:
            break
        attempts += 1
        if attempts > 20000:
            raise ValueError('文档结构过于复杂，未能安全解析')
        try:
            value, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, RecursionError):
            position = start + 1
            continue
        if _candidate(value):
            sessions.append(value)
            if len(sessions) > 1:
                raise ValueError('发现多个 Session 对象，请整理为单个后重试')
            position = end
        else:
            position = start + 1
    return sessions


def extract_session(payload, file_type):
    """Return {email, value} without writing document or token data to disk."""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise ValueError('文件内容为空')
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError('文件超过 10 MiB 上限')

    kind = str(file_type or '').strip().lower().lstrip('.')
    if kind == 'txt':
        try:
            text = bytes(payload).decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = bytes(payload).decode('gb18030')
            except UnicodeDecodeError:
                raise ValueError('TXT 编码无法识别，请另存为 UTF-8 后重试') from None
    elif kind == 'docx':
        text = _document_text(bytes(payload))
    else:
        raise ValueError('仅支持 .docx 和 .txt 文件')

    sessions = _find_sessions(text)
    if not sessions:
        raise ValueError('未找到符合工作台格式的 Session JSON')
    session = sessions[0]
    return {
        'email': session['user']['email'].strip(),
        'value': json.dumps(session, ensure_ascii=False, separators=(',', ':')),
    }
