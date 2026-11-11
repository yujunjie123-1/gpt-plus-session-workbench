import {observeLogin, fillLogin} from './login-dom.mjs';
import {jobIdentity} from './isolation-utils.mjs';

export async function driveLogin(job, tabs, bridge, setStatus) {
  if (!job.automatic || job.recovery) return;
  const stored = await chrome.storage.session.get(['loginProgress', 'loginTabMarkers']);
  const progress = stored.loginProgress || {};
  // Bound state contains identifiers and submission hashes, never credentials.
  for (const nonce of Object.keys(progress)) if (nonce !== job.request_id) delete progress[nonce];
  const state = progress[job.request_id] ||= {submitted: {}, attempts: {}, codeIds: [], firstSeen: {}};
  const candidates = tabs.filter(tab => tab.incognito && tab.id && /^https:\/\/(auth\.openai\.com|chatgpt\.com)\//.test(tab.url || ''));
  const marked = candidates.find(tab => tab.url?.includes('#wb_login=' + job.request_id)
    || stored.loginTabMarkers?.[tab.id] === job.request_id);
  const tab = candidates.find(item => item.id === state.tabId) || marked
    || (!job.instance_id && candidates.filter(item => item.url?.startsWith('https://auth.openai.com/') || item.url?.startsWith('https://chatgpt.com/auth/')).sort((a, b) => b.id - a.id)[0]);
  if (!tab) return;
  state.tabId = tab.id;
  await chrome.storage.session.set({loginProgress: progress});
  const send = body => bridge('/api/edge-session/step', {...jobIdentity(job), ...body});
  let observation;
  try {observation = (await chrome.scripting.executeScript({target: {tabId: tab.id}, func: observeLogin}))[0]?.result;}
  catch {await setStatus('请在 Edge 扩展页重新加载助手，授予 OpenAI 登录页访问权限'); return;}
  if (!observation || observation.kind === 'none') {
    await send({kind: 'progress', stage: 'waiting_login_result'});
    return;
  }
  if (observation.kind === 'error') {await send({kind: 'failure', code: observation.code}); return;}
  if (observation.kind === 'operation_timeout') {
    const attempts = state.attempts.operation_timeout || 0;
    if (!observation.retry || attempts >= 2) {
      await send({kind: 'failure', code: 'operation_timeout'});
      await setStatus('OpenAI 登录页面连续超时，已停止自动重试；请稍后重新获取 Session');
      return;
    }
    const authorized = await send({kind: 'operation_timeout'});
    if (authorized.available !== true) return;
    const result = (await chrome.scripting.executeScript({target: {tabId: tab.id}, func: fillLogin, args: ['operation_timeout', 'retry', job.email]}))[0]?.result;
    if (result?.state === 'submitted') {
      state.attempts.operation_timeout = attempts + 1;
      await chrome.storage.session.set({loginProgress: progress});
      await send({kind: 'progress', stage: 'operation_timeout_retry'});
      await setStatus(`OpenAI 登录页超时，已重试 ${attempts + 1}/2`);
    }
    return;
  }
  if (['challenge', 'unknown_code'].includes(observation.kind)) {
    await send({kind: 'progress', stage: observation.kind});
    await setStatus(observation.kind === 'challenge' ? '等待你完成人机或安全验证，完成后自动继续' : '正在等待确认验证码类型，可在 Edge 检查页面');
    return;
  }
  const kind = observation.kind;
  if (observation.observedEmail && observation.observedEmail.trim().toLowerCase() !== job.email.trim().toLowerCase()) {
    await send({kind: 'failure', code: 'account_mismatch'});
    return;
  }
  if (!observation.ready || (['email', 'password'].includes(kind) && state.submitted[kind])) return;
  if (kind === 'password_choice') {
    const method = observation.method === 'other_methods' ? 'other_methods' : 'password';
    const choiceKey = 'password_choice_' + method;
    if (state.submitted[choiceKey]) return;
    const authorized = await send({kind, method});
    if (authorized.available !== true || authorized.method !== method) return;
    const result = (await chrome.scripting.executeScript({target: {tabId: tab.id}, func: fillLogin, args: [kind, method, job.email]}))[0]?.result;
    if (result?.state === 'submitted') {
      state.submitted[choiceKey] = true;
      await chrome.storage.session.set({loginProgress: progress});
      const stage = method === 'other_methods' ? 'other_methods' : 'password_choice';
      await send({kind: 'progress', stage});
      await setStatus(method === 'other_methods' ? '正在打开其他登录方式' : '已切换为密码登录，正在等待密码步骤');
    }
    return;
  }
  const firstSeen = state.firstSeen[kind] ||= Date.now();
  await chrome.storage.session.set({loginProgress: progress});
  if (Date.now() - firstSeen < 700) return;
  if ((state.attempts[kind] || 0) >= 3) {
    await send({kind: 'failure', code: 'credentials'});
    return;
  }
  const credential = await send({kind});
  if (!credential.available || !credential.value) {if (credential.message) await setStatus(credential.message); return;}
  if (credential.code_id && state.codeIds.includes(credential.code_id)) return;
  // Persist the attempt guard before a possible navigation or worker suspension.
  state.attempts[kind] = (state.attempts[kind] || 0) + 1;
  if (['email', 'password'].includes(kind)) state.submitted[kind] = true;
  if (credential.code_id) state.codeIds.push(credential.code_id);
  await chrome.storage.session.set({loginProgress: progress});
  const result = (await chrome.scripting.executeScript({target: {tabId: tab.id}, func: fillLogin, args: [kind, credential.value, job.email]}))[0]?.result;
  if (result?.state === 'account_mismatch') {await send({kind: 'failure', code: 'account_mismatch'}); return;}
  if (result?.state === 'submitted') {
    state.submitted[kind] = true;
    await chrome.storage.session.set({loginProgress: progress});
    const completedStage = {email: 'email', password: 'password', email_code: 'email_code_submitted', totp: 'totp_submitted'}[kind];
    await send({kind: 'progress', stage: completedStage});
    await setStatus({email: '邮箱已自动填写', password: '密码已自动填写，正在监听验证码', email_code: '邮箱验证码已自动填写', totp: '2FA 动态验证码已自动填写'}[kind]);
  } else {
    state.attempts[kind] -= 1;
    delete state.submitted[kind];
    if (credential.code_id) state.codeIds = state.codeIds.filter(id => id !== credential.code_id);
    await chrome.storage.session.set({loginProgress: progress});
  }
}
