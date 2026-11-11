// These functions are self-contained because Chrome serializes them into the tab.
export function observeLogin(doc = document, pageLocation = location) {
  if (!['https://auth.openai.com', 'https://chatgpt.com'].includes(pageLocation.origin)) return {kind: 'none'};
  if (pageLocation.origin === 'https://chatgpt.com' && !pageLocation.pathname.startsWith('/auth/')) return {kind: 'none'};
  const body = (doc.body?.innerText || '').slice(0, 24000).toLowerCase();
  if (/invalid content type|route error\s*\(400/.test(body)) return {kind: 'error', code: 'route400'};
  const editable = element => !element.disabled && !element.readOnly && element.type !== 'hidden'
    && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0
    && getComputedStyle(element).visibility !== 'hidden';
  const visibleChallenge = [...doc.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]')]
    .some(frame => frame.getBoundingClientRect().width > 60 && frame.getBoundingClientRect().height > 40 && getComputedStyle(frame).visibility !== 'hidden');
  if (/verify you are human|prove you are human|checking your browser|人机验证|验证您是真人|complete the security check/.test(body)
      || visibleChallenge) return {kind: 'challenge'};
  const first = selector => [...doc.querySelectorAll(selector)].find(editable);
  const code = first('input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[inputmode="numeric"], input[maxlength="1"]');
  const password = first('input[type="password"]');
  const emailField = first('input[type="email"], input[autocomplete="username"]');
  const methodChoice = [...doc.querySelectorAll('button, a, [role="button"]')].map(element => {
    if (!editable(element)) return false;
    const text = (element.innerText || element.textContent || '').trim();
    if (/^(password|use password|use password instead|password login|log in with password|sign in with password|continue with password|try password|使用密码|密码)$/i.test(text)) return 'password';
    if (/^(try another way|try another method|other sign-in options|other methods|use a different method|其他方式|其他验证方式|选择其他方式)$/i.test(text)) return 'other_methods';
    return false;
  }).find(Boolean);
  const emailCodePage = /check your email|check your inbox|email verification|verification code|sent.{0,40}(email|code)|code.{0,35}(email|sent)|邮箱验证码|邮件验证码|验证码/.test(body);
  if (code) {
    if (emailCodePage && methodChoice) return {kind: 'password_choice', method: methodChoice, ready: doc.readyState === 'complete'};
    const totp = /authenticator|authentication app|two.factor|2fa|验证器|身份验证应用/.test(body);
    const mail = /check your email|sent.{0,40}(email|code)|code.{0,35}(email|sent)|收件箱|邮件验证码|邮箱验证码|验证码.{0,15}发送|发送.{0,20}验证码/.test(body);
    return {kind: totp ? 'totp' : mail ? 'email_code' : 'unknown_code', ready: doc.readyState === 'complete'};
  }
  const observedEmail = doc.querySelector('input[type="email"], input[autocomplete="username"]')?.value || '';
  if (password) return {kind: 'password', observedEmail, ready: doc.readyState === 'complete'};
  if (emailField) return {kind: 'email', ready: doc.readyState === 'complete'};
  if (methodChoice) return {kind: 'password_choice', method: methodChoice, ready: doc.readyState === 'complete'};
  if (/incorrect password|invalid email or password|wrong password|密码不正确|密码错误|验证码不正确|incorrect.*code|invalid verification code/.test(body)) return {kind: 'error', code: 'credentials'};
  const controls = [...doc.querySelectorAll('button, a, [role="button"]')];
  const retry = controls.some(element => editable(element) && /^(retry|try again|重试)$/i.test((element.innerText || element.textContent || '').trim()));
  const timeoutAlert = [...doc.querySelectorAll('[role="alert"], [aria-live="assertive"], [data-testid*="error" i]')]
    .some(element => editable(element) && /^(?:error\s*:\s*)?(?:operation timed out|request timed out|operation timeout|操作超时)[.!。]?$/i.test((element.innerText || element.textContent || '').trim()));
  if (timeoutAlert && retry) return {kind: 'operation_timeout', ready: doc.readyState === 'complete', retry: true};
  return {kind: 'none'};
}

export async function fillLogin(kind, value, expectedEmail) {
  if (!['https://auth.openai.com', 'https://chatgpt.com'].includes(location.origin)) return {state: 'waiting'};
  if (location.origin === 'https://chatgpt.com' && !location.pathname.startsWith('/auth/')) return {state: 'waiting'};
  if (kind === 'operation_timeout') {
    const button = [...document.querySelectorAll('button, a, [role="button"]')].find(element => {
      if (element.getBoundingClientRect().width <= 0 || element.getBoundingClientRect().height <= 0) return false;
      const text = (element.innerText || element.textContent || '').trim();
      return /^(retry|try again|重试)$/i.test(text);
    });
    if (!button || button.disabled) return {state: 'waiting'};
    button.click();
    return {state: 'submitted'};
  }
  if (kind === 'password_choice') {
    const button = [...document.querySelectorAll('button, a, [role="button"]')].find(element => {
      if (!(element.offsetWidth > 0 && element.offsetHeight > 0) || getComputedStyle(element).visibility === 'hidden') return false;
      const text = (element.innerText || element.textContent || '').trim();
      return value === 'other_methods'
        ? /^(try another way|try another method|other sign-in options|other methods|use a different method|其他方式|其他验证方式|选择其他方式)$/i.test(text)
        : /^(password|use password|use password instead|password login|log in with password|sign in with password|continue with password|try password|使用密码|密码登录|通过密码登录)$/i.test(text);
    });
    if (!button || button.disabled) return {state: 'waiting'};
    button.click();
    return {state: 'submitted'};
  }
  const visible = element => !element.disabled && !element.readOnly && element.type !== 'hidden'
    && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0
    && getComputedStyle(element).visibility !== 'hidden';
  const observed = document.querySelector('input[type="email"], input[autocomplete="username"]');
  if (kind !== 'email' && observed?.value && observed.value.trim().toLowerCase() !== expectedEmail.trim().toLowerCase()) return {state: 'account_mismatch'};
  const selector = kind === 'email' ? 'input[type="email"], input[autocomplete="username"]'
    : kind === 'password' ? 'input[type="password"]'
    : 'input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[inputmode="numeric"], input[maxlength="1"]';
  const field = [...document.querySelectorAll(selector)].find(visible);
  if (!field) return {state: 'waiting'};
  const form = field.closest('form');
  if (!form || document.readyState !== 'complete') return {state: 'waiting'};
  if (new URL(form.action || location.href, location.href).origin !== location.origin) return {state: 'waiting'};
  // React controlled inputs need the prototype setter plus bubbling input events.
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const write = (element, text) => {
    element.focus();
    setter.call(element, text);
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    element.blur();
  };
  const boxes = [...form.querySelectorAll('input[maxlength="1"]')].filter(visible);
  if (!['email', 'password'].includes(kind) && boxes.length === value.length && boxes.length >= 4) boxes.forEach((box, index) => write(box, value[index]));
  else write(field, value);
  await new Promise(resolve => setTimeout(resolve, 160));
  if (!field.isConnected) return {state: 'submitted'};
  if (!form.checkValidity()) return {state: 'waiting'};
  const buttons = [...form.querySelectorAll('button, input[type="submit"]')].filter(element => element.getBoundingClientRect().width > 0 && !element.hasAttribute('formaction'));
  const preferred = buttons.find(button => /^(continue|next|verify|sign in|log in|submit|继续|下一步|验证|登录|提交)$/i.test((button.innerText || button.value || '').trim()));
  const submit = preferred || buttons.find(button => button.type === 'submit');
  if (!submit || submit.disabled) return {state: 'waiting'};
  submit.click();
  return {state: 'submitted'};
}
