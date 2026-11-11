// Shared, side-effect-free protocol and business rules.
export const PROTOCOL = 'cdk-embed-v1';
export function unwrap(value) {
  return value && value.data && typeof value.data === 'object' && !Array.isArray(value.data) ? value.data : value || {};
}
export function businessOK(value) {
  return !value || value.code === undefined || value.code === 0 || value.code === '0';
}
export function validCode(value) {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{3,255}$/.test(value.trim());
}
export function validParent(value, ownOrigin) {
  try {
    const url = new URL(value);
    if (url.origin !== value || url.username || url.password) return false;
    if (url.protocol === 'https:') return true;
    const local = u => u.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname);
    return local(url) && local(new URL(ownOrigin));
  } catch { return false; }
}
export function acceptedMessage(event, parentWindow, parentOrigin, channel) {
  const data = event.data;
  return event.source === parentWindow && event.origin === parentOrigin && !!data && data.protocol === PROTOCOL && data.channel === channel;
}
export function parseSession(text) {
  if (typeof text !== 'string' || text.length > 60000) throw new Error('Session 过大，请重新复制完整 JSON');
  let value;
  try { value = JSON.parse(text); } catch { throw new Error('请粘贴完整 Session JSON'); }
  if (!value || Array.isArray(value) || typeof value !== 'object' || !String(value.sessionToken || value.session_token || '').trim()) throw new Error('Session JSON 必须包含 sessionToken，不能只填写 Access Token');
  if (typeof (value.sessionToken || value.session_token) !== 'string') throw new Error('sessionToken 格式不正确');
  return JSON.stringify(value);
}
export function planLabel(value) {
  const plan = String(value || '').toLowerCase();
  if (plan === 'pro_5x_renew') return 'Pro 5x 续费';
  if (plan === 'pro_20x_renew') return 'Pro 20x 续费';
  if (plan.includes('promax') || plan === 'pro_50x') return 'Pro 50x';
  if (plan.includes('prolite') || plan.includes('5x')) return 'Pro 5x';
  if (plan.includes('pro')) return 'Pro 20x';
  if (plan.includes('plus')) return 'Plus';
  if (plan === 'free') return '免费版';
  return value || '上游未提供';
}
export function supportedPlan(value) {
  return ['plus', 'pro', 'pro_5x', 'pro_20x', 'pro_50x', 'pro_5x_renew', 'pro_20x_renew', 'prolite', 'promax', 'chatgptpro'].includes(String(value || '').toLowerCase());
}
export function blockingReason(account, target, flow) {
  if (account.subscription_recovery_required === true || account.subscription_is_delinquent === true) return '当前订阅需要先处理，请联系客服处理后重新预检。';
  if (account.blocking_reason) return String(account.blocking_reason);
  if (account.can_purchase === false) return '当前账号暂不支持购买此套餐，请更换账号或联系客服。';
  if (flow === 'card_attach' || ['pro_5x_renew', 'pro_20x_renew'].includes(target) || account.subscription_has_active === false) return '';
  const rank = value => { const p = String(value || '').toLowerCase(); return p.includes('promax') || p === 'pro_50x' ? 4 : p.includes('prolite') || p.includes('5x') ? 2 : p.includes('pro') ? 3 : p.includes('plus') ? 1 : 0; };
  const requested = rank(target);
  if (requested && rank(account.currentPlan || account.current_plan) >= requested) return '账号已有同档或更高套餐，请在到期后再兑换，或更换账号。';
  return '';
}
export function lookupState(row) {
  if (!row || typeof row.status !== 'string') return { status: 'error', canRetry: false };
  if (row.status === 'used' && row.used === true) return { status: 'completed', canRetry: false };
  if (row.status === 'processing') return { status: 'pending', canRetry: false };
  if (row.status === 'failed') return { status: 'failed', canRetry: row.can_resubmit === true };
  if (row.status === 'unused' && row.can_resubmit === true) return { status: 'session', canRetry: true };
  if (['disabled', 'expired'].includes(row.status)) return { status: 'blocked', canRetry: false };
  return { status: 'error', canRetry: false };
}
export function safeResult(row) {
  const state = lookupState(row);
  return { ...state, email: typeof row?.account_email === 'string' ? row.account_email : '', message: typeof row?.message === 'string' ? row.message : '', plan: typeof row?.plan === 'string' ? row.plan : '', refund: row?.plus_refund || null };
}
export function remainingTime(value, now = Date.now()) {
  const timestamp = Date.parse(value || '');
  if (!Number.isFinite(timestamp)) return '上游未提供';
  const hours = Math.max(0, Math.floor((timestamp - now) / 3600000));
  return hours ? `${Math.floor(hours / 24)} 天 ${hours % 24} 小时` : timestamp > now ? '不足 1 小时' : '已到期';
}
export class SubmissionGate {
  constructor() { this.pending = false; this.confirmedReleased = false; }
  submit() {
    if (this.pending) return false;
    this.pending = true;
    this.confirmedReleased = false;
    return true;
  }
  observe(row) {
    // A network error, "unused" immediately after submit, or a failed order alone
    // is NOT permission to submit again. Release must be explicitly confirmed.
    if (row?.status === 'failed' && row.can_resubmit === true) this.confirmedReleased = true;
  }
  retry() {
    if (!this.confirmedReleased) return false;
    this.pending = false;
    this.confirmedReleased = false;
    return true;
  }
}
