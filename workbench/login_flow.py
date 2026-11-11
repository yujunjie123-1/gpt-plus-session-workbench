"""Select editable controls and surface known login failures without logging secrets."""
import time

LOGIN_INPUTS = {
    'email': ['input[type="email"]', 'input[autocomplete="username"]'],
    'password': ['input[type="password"]'],
    'code': ['input[autocomplete="one-time-code"]', 'input[name*="code" i]',
             'input[inputmode="numeric"]', 'input[maxlength="1"]'],
}


async def editable_login_field(page, kind):
    for selector in LOGIN_INPUTS[kind]:
        candidates = page.locator(selector)
        for index in range(min(await candidates.count(), 12)):
            field = candidates.nth(index)
            if await field.is_visible() and await field.is_enabled() and await field.is_editable(timeout=1500):
                return field
    return None


async def login_step(page):
    # Password pages often retain a visible, read-only email/username control.
    for kind in ('code', 'password', 'email'):
        if await editable_login_field(page, kind) is not None:
            return kind
    return None


async def wait_for_login_client(page, field, timeout=25):
    """SSR inputs can be enabled before the site's submit handler is hydrated."""
    deadline = time.monotonic() + timeout
    while True:
        ready = await field.evaluate('''element => {
            const doc = element.ownerDocument, win = doc.defaultView;
            const form = element.form;
            const props = form && Object.keys(form).filter(k => k.startsWith('__reactProps$'));
            const handled = props && props.some(k => typeof form[k]?.onSubmit === 'function');
            const clientForm = !!(win.__reactRouterContext || win.__remixContext ||
                doc.querySelector('script[type="module"]') || props?.length ||
                (form && form.method.toLowerCase() === 'get'));
            return doc.readyState === 'complete' && (!clientForm || handled);
        }''')
        if ready:
            return
        if time.monotonic() >= deadline:
            raise ValueError('登录页面脚本尚未就绪，未提交本步骤。请等待页面加载完成后重新登录')
        await page.wait_for_timeout(200)


def login_page_error(body):
    if 'route error' in body and ('invalid content type' in body or '400' in body):
        return 'OpenAI 登录页返回 400（内容类型异常），本次登录已停止'
    if any(marker in body for marker in (
            'wrong email or password', 'incorrect password', 'invalid email or password',
            'incorrect email or password', '密码不正确', '密码错误', '电子邮件或密码不正确')):
        return 'OpenAI 提示邮箱或密码不正确，已停止自动提交；请核对账号资料后重新登录'
    if any(marker in body for marker in ('too many requests', 'too many attempts', '尝试次数过多', '请求过多')):
        return 'OpenAI 提示尝试过于频繁，已停止自动提交；请稍后重新登录'
    return ''
