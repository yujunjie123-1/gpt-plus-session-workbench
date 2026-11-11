"""Launch the actual Edge InPrivate window, then attach to its startup page."""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
from contextlib import asynccontextmanager


def edge_executable():
    for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
        root = os.environ.get(name)
        if root:
            candidate = Path(root) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if candidate.is_file():
                return str(candidate.resolve())
    raise RuntimeError('未找到 Microsoft Edge，请安装 Edge 后重试')


@asynccontextmanager
async def private_login_page(pw):
    temp = tempfile.TemporaryDirectory(prefix='gptplus-login-')
    process = None
    browser = None
    try:
        process = subprocess.Popen([
            edge_executable(), '--inprivate', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=0', '--user-data-dir=' + temp.name, 'about:blank',
        ], cwd=temp.name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active_port = Path(temp.name) / 'DevToolsActivePort'
        endpoint = None
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError('InPrivate 登录窗口启动失败，请重试')
            try:
                lines = active_port.read_text(encoding='utf-8').splitlines()
                if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith('/devtools/browser/'):
                    endpoint = 'ws://127.0.0.1:' + lines[0] + lines[1]
                    break
            except OSError:
                pass
            await asyncio.sleep(.1)
        if endpoint is None:
            raise RuntimeError('连接 InPrivate 登录窗口超时，请检查 Edge 是否被策略限制')
        browser = await pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        context = browser.contexts[0]
        # Reuse the InPrivate startup tab. Creating a context/page can open a normal window.
        page = context.pages[0] if context.pages else await context.wait_for_event('page', timeout=10000)
        yield browser, context, page
    finally:
        try:
            if browser:
                await browser.close()
        finally:
            if process and process.poll() is None:
                await asyncio.to_thread(subprocess.run,
                    ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await asyncio.to_thread(process.wait, timeout=10)
            for attempt in range(5):
                try:
                    temp.cleanup()
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(.3)
