"""Own one disposable native Edge profile per login; never reuse a daily profile."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from .private_browser import edge_executable

SOURCE = Path(__file__).resolve().parents[1] / 'edge-extension'
PUBLIC_KEY = ('MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2BlKA/WLUrfumHlMPx6CilSWom/zPDaL1gjZ/xjVKjQTQvgSQ7wETCtMT1dBDV/p1Lncn+oCylyhMljm25Bv1OxfNbG6c1S+2Wo+pYGfjKBRabIBlUeC5A2eoryxfZczJYqxUHZP3FFkSlgXr8DjOTm3SSGPndQnFNP8buxXzUhMEplFeQwveFOoVcFV8vumLCnOFozt3+XlpJi8JT4Ou/aHHf2vBpMIhEdsSrFwuWodQjyh9Ac4orNk2M9pMzLSDeipyyiRY10BW3nT1buFO8r7fx4ofxY/t1tTGTu9llyoXukLW1kyDZmbEMqaMXKevYzpKTEq4P6Zuj0F1WTgjwIDAQAB')
EXTENSION_ID = ''.join(chr(97 + int(n, 16)) for n in hashlib.sha256(base64.b64decode(PUBLIC_KEY)).hexdigest()[:32])
_template_lock = threading.Lock()
_copy_lock = threading.Lock()
_PROFILE_MANIFEST_FIELDS = (
    'key', 'manifest_version', 'permissions', 'optional_permissions',
    'host_permissions', 'optional_host_permissions', 'incognito',
)


def _check_cancel(cancel):
    if cancel.is_set():
        raise RuntimeError('登录已停止，未打开新的登录窗口')


def _profile_process_ids(profile):
    """Find only Edge processes using this disposable profile."""
    marker = str(Path(profile).resolve()).replace("'", "''")
    script = (
        "$marker='" + marker + "'; "
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for value in result.stdout.splitlines():
        try:
            pid = int(value.strip())
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _stop(process, profile=None):
    if not process:
        return
    # The Edge launcher can exit while its renderer and utility children keep
    # the InPrivate window alive. Always target this app-owned PID so that an
    # already-exited parent does not make us skip the process-tree cleanup.
    try:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if profile is not None:
        # Edge may detach children from its launcher; target only processes
        # whose command line includes this login's unique user-data directory.
        for _ in range(3):
            pids = _profile_process_ids(profile)
            if not pids:
                break
            for pid in pids:
                try:
                    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            time.sleep(.2)


def _remove_profile(profile, root):
    candidate, boundary = profile.resolve(), root.resolve()
    if candidate == boundary or not candidate.is_relative_to(boundary) or len(candidate.relative_to(boundary).parts) != 2:
        raise RuntimeError('浏览器资料目录边界不正确，未执行清理')
    for attempt in range(6):
        try:
            shutil.rmtree(candidate)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 5:
                # Closed profile remnants may be cleaned manually; never delete a broader directory.
                return
            time.sleep(.2)


def _profile_fingerprint(executable, helper):
    manifest=json.loads((Path(helper)/'manifest.json').read_text(encoding='utf-8'))
    profile_config={name:manifest.get(name) for name in _PROFILE_MANIFEST_FIELDS}
    identity=json.dumps({
        'edge':str(Path(executable).resolve()),
        'edge_mtime_ns':Path(executable).stat().st_mtime_ns,
        'extension_profile_config':profile_config,
    },sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(identity).hexdigest()[:32]


def prepare_helper(root):
    root = Path(root).resolve()
    destination = root / '_isolated-helper'
    if not destination.resolve().is_relative_to(root):
        raise RuntimeError('助手保存目录无效')
    with _copy_lock:
        destination.mkdir(parents=True, exist_ok=True)
        for file in SOURCE.iterdir():
            if not file.is_file() or file.suffix not in {'.js', '.mjs', '.json', '.html', '.css'}:
                continue
            if not file.resolve().is_relative_to(SOURCE.resolve()):
                raise RuntimeError('助手资源路径不正确')
            value = file.read_bytes()
            if file.name == 'manifest.json':
                manifest = json.loads(value)
                manifest['key'] = PUBLIC_KEY
                value = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            target = destination / file.name
            if not target.is_file() or target.read_bytes() != value:
                temporary = target.with_suffix(target.suffix + '.pending')
                temporary.write_bytes(value)
                temporary.replace(target)
    return destination.resolve()


def _initialize(profile, helper, cancel):
    process = None
    clean_exit = False
    try:
        process = subprocess.Popen([
            edge_executable(), '--headless=new', '--no-first-run', '--no-default-browser-check',
            '--disable-background-networking', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=0', '--user-data-dir=' + str(profile),
            '--load-extension=' + str(helper), 'about:blank',
        ], cwd=str(profile), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        port_file = profile / 'DevToolsActivePort'
        endpoint = None
        for _ in range(150):
            _check_cancel(cancel)
            if process.poll() is not None:
                raise RuntimeError('独立浏览器准备失败，请检查 Edge 安装和本机策略')
            try:
                lines = port_file.read_text(encoding='utf-8').splitlines()
                if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith('/devtools/browser/'):
                    endpoint = 'ws://127.0.0.1:' + lines[0] + lines[1]
                    break
            except OSError:
                pass
            time.sleep(.1)
        if endpoint is None:
            raise RuntimeError('独立浏览器准备超时，请检查 Edge 是否被策略限制')
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
            page = browser.contexts[0].new_page()
            page.goto('edge://extensions/', wait_until='domcontentloaded', timeout=15000)
            _check_cancel(cancel)
            if not page.evaluate('typeof chrome.developerPrivate?.updateExtensionConfiguration === "function"'):
                raise RuntimeError('当前 Edge 不支持自动准备助手，请升级 Edge 后重试')
            # Wait for this unpacked helper to be registered, then use Edge's own
            # configuration API. Do not edit protected preferences or policies.
            page.wait_for_function('''id => new Promise(resolve => chrome.developerPrivate.getExtensionsInfo(
                {includeDisabled:true}, extensions => resolve(extensions.some(item => item.id === id))))''',
                arg=EXTENSION_ID, timeout=10000)
            configured = page.evaluate('''id => new Promise(resolve => chrome.developerPrivate.updateExtensionConfiguration(
                {extensionId:id,incognitoAccess:true}, () => resolve(!chrome.runtime.lastError)))''', EXTENSION_ID)
            if configured is not True:
                raise RuntimeError('Edge 未允许助手在 InPrivate 中使用，请检查浏览器管理策略')
            _check_cancel(cancel)
            # Explicit Browser.close is required; browser.close() can merely
            # detach a CDP client and leave the initialization process running.
            channel = browser.new_browser_cdp_session()
            try:
                channel.send('Browser.close')
            except Exception:
                pass
        try:
            process.wait(timeout=8)
            clean_exit = True
        except subprocess.TimeoutExpired:
            raise RuntimeError('浏览器准备进程未退出，已停止本次登录；请重试') from None
    finally:
        _stop(process, profile)
    if not clean_exit:
        raise RuntimeError('独立浏览器尚未准备完成')
    # No debugger or initialization port remains when a GPT page is opened.
    (profile / 'DevToolsActivePort').unlink(missing_ok=True)


class NativeEdge:
    def __init__(self, process, profile, root):
        self.process, self.profile, self.root = process, profile, root

    def close(self):
        _stop(self.process, self.profile)
        _remove_profile(self.profile, self.root)


def prepare_profile_template(profile_root, cancel):
    """Only this never-logged-in profile is reused; login profiles never return here."""
    if not Path(profile_root).is_absolute():
        raise ValueError('浏览器资料目录必须是绝对路径')
    root=Path(profile_root).resolve()
    helper=prepare_helper(root)
    executable=Path(edge_executable())
    fingerprint=_profile_fingerprint(executable,helper)
    template=root/'_pristine'/fingerprint
    ready=template/'.workbench-template-ready'
    while not _template_lock.acquire(timeout=.2):
        _check_cancel(cancel)
    try:
        _check_cancel(cancel)
        if ready.is_file() and ready.read_text(encoding='ascii')==fingerprint:
            return helper,template
        if template.exists():_remove_profile(template,root)
        template.mkdir(parents=True,exist_ok=False)
        try:
            _initialize(template,helper,cancel)
            _check_cancel(cancel)
            ready.write_text(fingerprint,encoding='ascii')
        except BaseException:
            _remove_profile(template,root)
            raise
        return helper,template
    finally:
        _template_lock.release()


def launch_isolated_edge(*, profile_root, profile_key, instance_id, bootstrap_url, cancel):
    if not Path(profile_root).is_absolute():
        raise ValueError('浏览器资料目录必须是绝对路径')
    root = Path(profile_root).resolve()
    if not re.fullmatch(r'[a-f0-9]{32}', profile_key) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', instance_id):
        raise ValueError('独立登录实例标识无效')
    parsed = urlsplit(bootstrap_url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.path != '/edge-session-bridge' or not parsed.fragment):
        raise ValueError('本机登录连接地址无效')
    profile = (root / profile_key / instance_id).resolve()
    if not profile.is_relative_to(root):
        raise ValueError('独立浏览器目录无效')
    profile.mkdir(parents=True, exist_ok=False)
    process = None
    try:
        helper,template=prepare_profile_template(root,cancel)
        _check_cancel(cancel)
        shutil.copytree(template,profile,dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.workbench-template-ready','DevToolsActivePort','Singleton*','LOCK','lockfile'))
        _check_cancel(cancel)
        process = subprocess.Popen([
            edge_executable(), '--inprivate', '--new-window', '--no-first-run',
            '--no-default-browser-check', '--user-data-dir=' + str(profile),
            '--load-extension=' + str(helper), bootstrap_url,
        ], cwd=str(profile), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return NativeEdge(process, profile, root)
    except BaseException:
        _stop(process, profile)
        _remove_profile(profile, root)
        raise
