import { PROTOCOL, unwrap, businessOK, validCode, validParent, acceptedMessage, parseSession, planLabel, supportedPlan, blockingReason, lookupState, safeResult, remainingTime, SubmissionGate } from './core.mjs';

const root = document.getElementById('widget');
const screen = document.getElementById('screen');
const errorNode = document.getElementById('error');
const notice = document.getElementById('notice');
const params = new URLSearchParams(location.hash.slice(1));
const parentOrigin = params.get('parent') || '';
const channel = params.get('channel') || '';
const device = 'embed-' + crypto.randomUUID();
const gate = new SubmissionGate();
let code = '', orderId = '', token = '', preflight = '', plan = '', flow = '', account = null;
let state = 'loading', busy = false, timer = null, pollStarted = 0, lastRow = null, markerKey = '';
let initialized = false, destroyed = false, initLocked = false;

function send(type, data = {}) { if (parentOrigin && !destroyed) parent.postMessage({ protocol: PROTOCOL, channel, type, data }, parentOrigin); }
function setError(message = '') { errorNode.textContent = String(message); errorNode.hidden = !message; }
function status(next, canRetry = false) {
  state = next;
  send('status', { status: next, canRetry });
  const step = ['pending', 'completed', 'failed', 'blocked'].includes(next) ? 'result' : next === 'confirm' ? 'confirm' : 'session';
  root.querySelectorAll('[data-step]').forEach(node => { if (node.dataset.step === step) node.setAttribute('aria-current', 'step'); else node.removeAttribute('aria-current'); });
}
function storagePending(next) {
  try { if (next) sessionStorage.setItem(markerKey, 'pending'); else sessionStorage.removeItem(markerKey); } catch { /* Third-party storage may be blocked. */ }
}
function clearTimer() { if (timer) clearTimeout(timer); timer = null; }
function button(label, action, primary = false) { const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.dataset.action = action; if (primary) b.className = 'primary'; return b; }
function heading(text, success = false) { const h = document.createElement('h2'); h.textContent = text; if (success) h.className = 'result-title'; return h; }
function paragraph(text) { const p = document.createElement('p'); p.textContent = text; return p; }
function facts(items) {
  const dl = document.createElement('dl');
  for (const [label, value] of items) { const div = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = label; dd.textContent = String(value ?? '上游未提供'); div.append(dt, dd); dl.append(div); }
  return dl;
}
function actions(...buttons) { const div = document.createElement('div'); div.className = 'actions'; div.append(...buttons); return div; }
function focusAction() { const action = screen.querySelector('textarea, button'); if (action) action.focus({ preventScroll: true }); }
function formatTime(value) { const d = new Date(value); return Number.isFinite(d.getTime()) ? d.toLocaleString('zh-CN', { hour12: false }) : '上游未提供'; }

async function api(action, body, timeout = 30000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch('./api/' + action, { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json', 'X-Redemption-Device': device }, credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal: controller.signal });
    let payload;
    try { payload = await response.json(); } catch { throw new Error('服务暂时不可用，请稍后查询结果'); }
    if (!response.ok || !businessOK(payload)) {
      if (response.status === 429) throw new Error('查询过于频繁，请稍等一分钟再试');
      throw new Error(String(payload?.error || payload?.msg || payload?.message || '请求未完成，请稍后重试'));
    }
    return payload;
  } catch (err) {
    if (err.name === 'AbortError' || err instanceof TypeError) throw new Error('连接中断或超时，请查询结果确认状态');
    throw err;
  } finally { clearTimeout(timeoutId); }
}
async function lookup() {
  const payload = await api('lookup', { codes: [code] });
  const row = (unwrap(payload).results || payload.results || [])[0];
  if (!row || String(row.cdk_code || '').toUpperCase() !== code.toUpperCase()) throw new Error('未取得此订单的有效查询结果');
  return row;
}
function showSession() {
  status('session'); setError();
  const hint = paragraph('打开 ChatGPT Session 页面，复制完整 JSON。'); hint.id = 'session-hint'; hint.className = 'subtle';
  const link = document.createElement('a'); link.href = 'https://chatgpt.com/api/auth/session'; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = '打开 Session 页面';
  const label = document.createElement('label'); label.htmlFor = 'session-input'; label.textContent = 'Session JSON';
  const textarea = document.createElement('textarea'); textarea.id = 'session-input'; textarea.placeholder = '粘贴完整 JSON，必须包含 sessionToken'; textarea.spellcheck = false; textarea.autocomplete = 'off'; textarea.setAttribute('aria-describedby', 'session-hint'); textarea.maxLength = 60000;
  screen.replaceChildren(heading('填写 Session'), paragraph('已自动关联此订单的兑换资格 · ' + planLabel(plan)), hint, link, label, textarea, actions(button('预检账号', 'preflight', true)));
  notice.textContent = '无需填写卡密。Session 直接提交到 cdk.bar 兑换服务。';
}
function showConfirm() {
  status('confirm');
  const email = paragraph(account.email || account.account_email || '账号已验证'); email.className = 'email';
  const reason = blockingReason(account, plan, flow);
  const confirm = button('确认兑换', 'redeem', true); confirm.disabled = Boolean(reason);
  screen.replaceChildren(heading('确认兑换'), email, facts([
    ['目标套餐', planLabel(plan)], ['当前套餐', planLabel(account.currentPlan || account.current_plan)],
    ['订阅状态', account.subscription_has_active === true ? '有效' : account.subscription_has_active === false ? '未订阅 / 已到期' : '上游未提供'],
    ['套餐到期', formatTime(account.subscription_active_until)],
    ['当前剩余', remainingTime(account.subscription_active_until)],
    ['自动续费', account.subscription_will_renew === true ? '到期后自动续费' : account.subscription_will_renew === false ? '到期后不续费' : '上游未提供']
  ]), actions(button('返回修改', 'back'), confirm));
  setError(reason); notice.textContent = '请核对充值账号。提交后将查询进度，结果未确认时请勿重复兑换。';
}
function showResult(row) {
  lastRow = row;
  const info = safeResult(row);
  gate.observe(row);
  if (info.status === 'session' && gate.pending) { showPending(); return; }
  if (info.status === 'error') { showPending('暂未确认兑换状态，请稍后查询或联系客服。'); return; }
  if (info.status === 'completed') { clearTimer(); storagePending(false); }
  status(info.status, info.canRetry); setError();
  const title = { completed: '兑换成功', pending: '正在兑换', failed: '本次兑换失败', blocked: '暂时无法兑换' }[info.status] || '待确认';
  const nodes = [heading(title, info.status === 'completed'), paragraph(info.message || '正在核验订单状态')];
  if (info.email) { const p = paragraph(info.email); p.className = 'email'; nodes.push(p); }
  const values = [['套餐', planLabel(info.plan || plan)]];
  const refund = info.refund;
  if (info.status === 'completed' && refund?.state === 'recorded' && Number.isInteger(refund.remaining_full_days) && Number.isInteger(refund.refund_amount_fen)) {
    values.push(['成功时原 Plus 剩余整天', refund.remaining_full_days + ' 天'], ['退款参考', '¥' + (refund.refund_amount_fen / 100).toFixed(2)]);
  }
  if (info.status === 'completed' && refund && refund.state !== 'recorded') values.push(['原 Plus 剩余记录', refund.state === 'not_recorded' ? '未记录' : '待核验']);
  nodes.push(facts(values));
  const controls = [button('刷新结果', 'refresh')];
  if (info.status === 'failed' && info.canRetry) controls.push(button('使用原兑换资格重试', 'retry', true));
  nodes.push(actions(...controls)); screen.replaceChildren(...nodes);
  notice.textContent = info.status === 'failed' ? info.canRetry ? '卡密已确认可再次使用。重试不会重新进货或扣代理余额。' : '卡密尚未确认释放，请等待处理后刷新结果。' : info.status === 'completed' ? '兑换结果已保存，可通过此订单再次查看。退款参考不会自动发起退款。' : '正在查询结果，请勿重复提交。';
  if (info.status === 'pending' || (info.status === 'failed' && !info.canRetry)) schedulePoll();
}
function showPending(message = '兑换请求已提交，正在确认结果。') {
  status('pending');
  screen.replaceChildren(heading('正在确认兑换结果'), paragraph(message), actions(button('刷新结果', 'refresh')));
  notice.textContent = '结果未确认前不会再次提交。长时间未有结果时，请联系客服核验。';
  schedulePoll();
}
function schedulePoll() {
  clearTimer();
  if (destroyed || document.hidden || Date.now() - pollStarted > 10 * 60000) return;
  timer = setTimeout(() => { timer = null; refresh(); }, Date.now() - pollStarted > 2 * 60000 ? 15000 : 8000);
}
async function refresh() {
  if (!initialized || busy || destroyed || state === 'session' || state === 'confirm') return;
  busy = true;
  try { showResult(await lookup()); } catch (error) { setError(error.message); schedulePoll(); }
  finally { busy = false; }
}
async function initialize() {
  if (busy || destroyed) return;
  busy = true; clearTimer(); setError(); status('loading');
  screen.replaceChildren(heading('正在核验兑换资格'));
  try {
    const row = await lookup(); lastRow = row;
    const found = lookupState(row);
    if (gate.pending || ['completed', 'pending', 'failed', 'blocked'].includes(found.status)) {
      pollStarted = Date.now(); showResult(row); return;
    }
    if (!['unused', 'unknown'].includes(row.status)) throw new Error('卡密状态暂未确认，请稍后查询');
    const payload = await api('preview', { code }); const data = unwrap(payload);
    token = data.redemption_token || payload.redemption_token || data.token || payload.token || '';
    plan = String(data.plan || data.plan_type || '').toLowerCase(); flow = String(data.plan_flow || '').toLowerCase();
    if (!token) throw new Error('兑换资格验证未完成，请联系客服');
    if (!supportedPlan(plan)) { token = ''; throw new Error('此组件当前支持 ChatGPT Plus / Pro 的 Session 兑换，请联系店铺处理该商品'); }
    showSession();
  } catch (error) {
    status('error'); screen.replaceChildren(heading('暂时无法载入兑换资格'), actions(button('重新加载', 'initialize'))); setError(error.message); notice.textContent = '只重新查询已交付的卡密，不会重复扣款。';
  } finally { busy = false; }
}
async function doPreflight() {
  if (busy || state !== 'session' || gate.pending) return;
  const input = document.getElementById('session-input'); let session;
  try { session = parseSession(input.value); } catch (error) { setError(error.message); return; }
  busy = true; preflight = ''; account = null; setError();
  screen.querySelector('[data-action="preflight"]').disabled = true;
  notice.textContent = '正在校验 Session 和账号订阅，请稍候。';
  try {
    const payload = await api('preflight', { code, redemption_token: token, credential: { mode: 'session', session } }, 90000);
    const data = unwrap(payload);
    if (!data.preflight_token && !payload.preflight_token) throw new Error('预检未返回有效兑换凭证，请重新预检');
    account = data; preflight = data.preflight_token || payload.preflight_token;
    input.value = ''; session = ''; showConfirm(); focusAction();
  } catch (error) { setError(error.message); notice.textContent = '预检未通过，请检查 Session 后重新预检。'; const action = screen.querySelector('[data-action="preflight"]'); if (action) action.disabled = false; }
  finally { session = ''; busy = false; }
}
async function doRedeem() {
  if (busy || state !== 'confirm' || !preflight || !account || blockingReason(account, plan, flow) || !gate.submit()) return;
  busy = true; storagePending(true); setError(); clearTimer(); pollStarted = Date.now();
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(preflight));
  const requestId = 'embed-' + Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('').slice(0, 48);
  status('pending'); screen.replaceChildren(heading('正在提交兑换'), paragraph('请勿关闭页面或重复提交。'));
  try {
    await api('redeem', { redemption_token: token, preflight_token: preflight, client_request_id: requestId }, 150000);
    preflight = ''; account = null; showPending();
  } catch (error) { preflight = ''; account = null; showPending('请求结果暂未确认，正在查询此卡密状态。'); setError(error.message); }
  finally { busy = false; }
  await refresh();
}

root.addEventListener('click', async event => {
  const action = event.target.closest('button[data-action]');
  if (!action || action.disabled || busy) return;
  switch (action.dataset.action) {
    case 'initialize': await initialize(); break;
    case 'delivery-retry': send('delivery-retry'); break;
    case 'preflight': await doPreflight(); break;
    case 'back': preflight = ''; account = null; showSession(); focusAction(); break;
    case 'redeem': await doRedeem(); break;
    case 'refresh': await refresh(); break;
    case 'retry':
      if (lastRow?.status !== 'failed' || lastRow.can_resubmit !== true) return;
      gate.observe(lastRow); if (gate.pending && !gate.retry()) return;
      storagePending(false); preflight = ''; account = null; await initializeAfterRelease(); break;
  }
});
async function initializeAfterRelease() {
  if (busy) return; busy = true; clearTimer(); setError();
  try {
    const row = await lookup();
    if (row.can_resubmit !== true || !['failed', 'unused'].includes(row.status)) { showResult(row); return; }
    const payload = await api('preview', { code }); const data = unwrap(payload);
    token = data.redemption_token || payload.redemption_token || data.token || '';
    plan = String(data.plan || data.plan_type || '').toLowerCase(); flow = String(data.plan_flow || '').toLowerCase();
    if (!token || !supportedPlan(plan)) throw new Error('兑换资格暂不可用，请稍后刷新');
    showSession(); focusAction();
  } catch (error) { showResult(lastRow); setError(error.message); }
  finally { busy = false; }
}

if (parent === window || !validParent(parentOrigin, location.origin) || !/^[a-f0-9]{48}$/.test(channel)) {
  status('error'); screen.replaceChildren(heading('请从已付款订单打开兑换')); notice.textContent = '此页面需要通过店铺的兑换组件载入。';
} else {
  window.addEventListener('message', async event => {
    if (!acceptedMessage(event, parent, parentOrigin, channel)) return;
    const message = event.data;
    if (message.type === 'init' && !initLocked) {
      const data = message.data || {};
      if (!validCode(data.code) || !/^[A-Za-z0-9._:-]{1,128}$/.test(String(data.orderId || ''))) return;
      initLocked = true; initialized = true; code = data.code.trim(); orderId = String(data.orderId);
      document.getElementById('order-label').textContent = '订单 ' + orderId;
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(parentOrigin + '\n' + orderId));
      markerKey = 'cdk-embed-pending-' + Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
      try { if (sessionStorage.getItem(markerKey) === 'pending') gate.pending = true; } catch { /* No credentials are stored. */ }
      await initialize();
    } else if (message.type === 'refresh' && initialized) {
      if (state === 'error') await initialize(); else await refresh();
    } else if (message.type === 'delivery-error' && !initialized) {
      status('error'); screen.replaceChildren(heading('订单尚未取得兑换资格'), paragraph('请确认订单已付款并完成发货。'), actions(button('重新获取订单资格', 'delivery-retry', true))); notice.textContent = '重新获取不会发起新的进货请求。';
    }
  });
  send('ready');
}
let lastHeight = 0;
new ResizeObserver(() => { const height = Math.ceil(root.getBoundingClientRect().height); if (height !== lastHeight) { lastHeight = height; send('resize', { height }); } }).observe(root);
document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimer(); else if (state === 'pending' || (state === 'failed' && !lastRow?.can_resubmit)) refresh(); });
window.addEventListener('pagehide', () => { destroyed = true; clearTimer(); code = ''; token = ''; preflight = ''; account = null; const field = document.getElementById('session-input'); if (field) field.value = ''; });
