import {loopbackOrigin, jobForSession, cookieSession} from './session-utils.mjs';
import {driveLogin} from './login-driver.mjs';
import {isolationBootstrap, jobsForConnection, jobIdentity} from './isolation-utils.mjs';

let scanning = false;
let lastScanTime = 0;
let nextScan;
let scanRequested = false;

async function status(message, connected = true) {
  await chrome.storage.session.set({status: {message, connected, time: Date.now()}});
  await chrome.action.setBadgeText({text: connected ? '·' : ''});
  await chrome.action.setBadgeBackgroundColor({color: '#235a4c'});
}

async function bridgeFetch(connection, path, body) {
  const origin = loopbackOrigin(connection?.origin);
  if (!origin || origin !== connection.origin || typeof connection.token !== 'string') throw new Error('connection');
  const response = await fetch(origin + path, {
    method: body ? 'POST' : 'GET',
    headers: {Authorization: 'Bearer ' + connection.token, 'X-Workbench-Helper-Version': chrome.runtime.getManifest?.().version || '0.3.0',
      'X-Workbench-Helper-InPrivate': await chrome.extension.isAllowedIncognitoAccess() ? '1' : '0', ...(body ? {'Content-Type': 'application/json'} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}),
    cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) throw new Error('bridge');
  return response.json();
}

async function heartbeatLoginPages(jobs, tabs, connection) {
  const {loginProgress = {}, loginTabMarkers = {}} = await chrome.storage.session.get(['loginProgress', 'loginTabMarkers']);
  for (const tab of tabs) {
    if (!tab.incognito || !Number.isInteger(tab.id) || !/^https:\/\/(chatgpt\.com|auth\.openai\.com)\//.test(tab.url || '')) continue;
    const requestId = loginTabMarkers[tab.id]
      || Object.keys(loginProgress).find(id => loginProgress[id]?.tabId === tab.id);
    const job = jobs.find(item => item.instance_id && item.request_id === requestId);
    if (!job) continue;
    try {
      const results = await chrome.scripting.executeScript({
        target: {tabId: tab.id}, world: 'MAIN',
        func: () => ['https://chatgpt.com', 'https://auth.openai.com'].includes(location.origin)
          && Boolean(document?.documentElement) && document.readyState !== 'uninitialized',
      });
      if (results[0]?.result !== true) continue;
      await bridgeFetch(connection, '/api/edge-session/step', {
        ...jobIdentity(job), kind: 'page_heartbeat', page_id: tab.id,
      });
    } catch { /* A failed page probe must not refresh the backend's liveness timer. */ }
  }
}

async function scan() {
  if (scanning) {scanRequested = true; return;}
  scanRequested = false;
  clearTimeout(nextScan);
  scanning = true;
  lastScanTime = Date.now();
  let pending = false;
  try {
    let {connection, autoConnectDisabled} = await chrome.storage.session.get(['connection', 'autoConnectDisabled']);
    if (!connection && !autoConnectDisabled) connection = await autoConnect();
    if (!connection) return;
    let result;
    try {result = await bridgeFetch(connection, '/api/edge-session/jobs');}
    catch {
      connection = await autoConnect();
      if (!connection) throw new Error('connection');
      result = await bridgeFetch(connection, '/api/edge-session/jobs');
    }
    const jobs = jobsForConnection(result.jobs, connection);
    if (!Array.isArray(jobs) || !jobs.length) {
      await status('已连接，等待工作台发起 Edge 登录');
      return;
    }
    pending = true;
    if (!await chrome.extension.isAllowedIncognitoAccess()) {
      await status('请在扩展详情中勾选“允许在 InPrivate 中使用”');
      return;
    }
    const tabs = await chrome.tabs.query({url: ['https://chatgpt.com/*', 'https://auth.openai.com/*']});
    await heartbeatLoginPages(jobs, tabs, connection);
    for (const tab of tabs) {
      if (!tab.incognito || !tab.id || !tab.url?.startsWith('https://chatgpt.com/')) continue;
      // Cookie stores are selected by the actual tab, never by a global default.
      const stores = await chrome.cookies.getAllCookieStores();
      const store = stores.find(item => item.tabIds.includes(tab.id));
      if (!store) continue;
      let data;
      try {
        const results = await chrome.scripting.executeScript({
          target: {tabId: tab.id}, world: 'MAIN',
          func: async () => {
            if (location.origin !== 'https://chatgpt.com' || location.pathname.startsWith('/auth/')) return null;
            try {
              const response = await fetch('/api/auth/session', {credentials: 'include', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(8000)});
              if (!response.ok || !(response.headers.get('content-type') || '').includes('application/json')) return null;
              return await response.json();
            } catch { return null; }
          },
        });
        data = results[0]?.result;
      } catch { continue; }
      const job = jobForSession(jobs, data, Boolean(tab.incognito));
      if (!job) {
        const {loginProgress = {}} = await chrome.storage.session.get('loginProgress');
        const bound = jobs.find(item => item.automatic && loginProgress[item.request_id]?.tabId === tab.id);
        if (bound && data?.user?.email && data.user.email.trim().toLowerCase() !== bound.email.trim().toLowerCase()) {
          await switchAccount(bound, tab, store.id, connection, loginProgress);
          return;
        }
        continue;
      }
      const cookies = await chrome.cookies.getAll({url: 'https://chatgpt.com/', storeId: store.id});
      const sessionToken = cookieSession(cookies, store.id);
      if (!sessionToken) {
        await status('已识别目标账号，但未读取到 Session Cookie；请检查 InPrivate 权限');
        return;
      }
      const session = {...data, sessionToken};
      if (new TextEncoder().encode(JSON.stringify(session)).length > 60000) {
        await status('Session 超出兑换组件大小限制，尚未交接');
        return;
      }
      await bridgeFetch(connection, '/api/edge-session/receive', {
        ...jobIdentity(job), session,
      });
      await status('Session 已自动交给本机工作台，请回到账号列表查看');
      await chrome.action.setBadgeText({text: 'OK'});
      pending = false;
      return;
    }
    const automaticJob = jobs.find(job => job.automatic && !job.recovery);
    if (automaticJob) await driveLogin(automaticJob, tabs, (path, body) => bridgeFetch(connection, path, body), status);
    else await status('正在等待已登录的目标账号 Session；不需要复制粘贴');
  } catch {
    await status('尚未完成交接：请确认工作台仍在运行，必要时重新连接', false);
  } finally {
    scanning = false;
    if (pending || scanRequested) {
      nextScan = setTimeout(() => void scan(), pending ? 1400 : 300);
      nextScan.unref?.();
    }
  }
}

async function pairTab(tab) {
  const bootstrap = isolationBootstrap(tab?.url);
  if (bootstrap) return pairInstance(tab, bootstrap);
  const origin = loopbackOrigin(tab?.url);
  if (!origin || !tab.id) throw new Error('请先切换到本机工作台标签页，再点击连接');
  const results = await chrome.scripting.executeScript({
    target: {tabId: tab.id},
    func: async () => {
      if (location.protocol !== 'http:' || location.hostname !== '127.0.0.1' || !location.port) return null;
      const token = sessionStorage.getItem('workbench-access');
      if (!token) return null;
      try {
        const response = await fetch('/api/edge-session/pair', {
          method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
          body: '{}', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(8000),
        });
        if (!response.ok) return null;
        return {origin: location.origin, token: (await response.json()).bridge_token};
      } catch { return null; }
    },
  });
  const connection = results[0]?.result;
  if (connection?.origin !== origin || typeof connection.token !== 'string' || connection.token.length < 32) {
    throw new Error('连接失败，请使用试验版启动器打开工作台后重试');
  }
  await chrome.storage.session.set({connection, autoConnectDisabled: false});
  await chrome.alarms.create('session-scan', {periodInMinutes: 0.5});
  return connection;
}

async function pairInstance(tab, bootstrap) {
  if (!tab?.id || !tab.incognito || !await chrome.extension.isAllowedIncognitoAccess()) {
    throw new Error('独立登录需要 InPrivate 窗口及助手权限，请返回工作台重试');
  }
  const response = await fetch(bootstrap.origin + '/api/edge-session/bind', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...bootstrap,incognito:true}), cache:'no-store', redirect:'error',
    signal:AbortSignal.timeout(12000),
  });
  if (!response.ok) throw new Error('本次独立登录连接已过期，请返回工作台重新开始');
  const paired = await response.json();
  if (paired.origin !== bootstrap.origin || paired.instance_id !== bootstrap.instance_id
      || typeof paired.token !== 'string' || paired.token.length < 32
      || !/^[A-Za-z0-9_-]{32,64}$/.test(paired.request_id || '')) throw new Error('instance');
  const connection = {origin:paired.origin,token:paired.token,instance_id:paired.instance_id};
  await chrome.storage.session.set({connection,autoConnectDisabled:false,
    loginTabMarkers:{[tab.id]:paired.request_id},loginProgress:{}});
  await chrome.alarms.create('session-scan', {periodInMinutes:0.5});
  await chrome.tabs.update(tab.id, {url:'https://chatgpt.com/auth/login#wb_login=' + paired.request_id});
  return connection;
}

async function autoConnect() {
  const tabs = await chrome.tabs.query({url: 'http://127.0.0.1/*'});
  for (const tab of tabs.sort((a, b) => b.id - a.id)) {
    if (!loopbackOrigin(tab.url)) continue;
    try {return await pairTab(tab);} catch { /* Other localhost apps are ignored. */ }
  }
  return null;
}

async function connectFromTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  await pairTab(tab);
  await status('已连接，自动登录时会实时监听并填写验证码');
  await scan();
}

async function switchAccount(job, tab, storeId, connection, progress) {
  if (job.instance_id) {
    await bridgeFetch(connection, '/api/edge-session/step', {...jobIdentity(job),kind:'failure',code:'account_mismatch'});
    return;
  }
  const state = progress[job.request_id];
  const body = {record_id: job.record_id, request_id: job.request_id};
  if (state.switched) {
    await bridgeFetch(connection, '/api/edge-session/step', {...body, kind: 'failure', code: 'account_mismatch'});
    return;
  }
  state.switched = true;
  state.submitted = {};
  state.firstSeen = {};
  await chrome.storage.session.set({loginProgress: progress});
  await bridgeFetch(connection, '/api/edge-session/step', {...body, kind: 'progress', stage: 'switching'});
  // Clear login cookies only in this task's InPrivate store. Security challenge
  // and browser device cookies are retained; normal-profile cookies are untouched.
  for (const domain of ['chatgpt.com', 'auth.openai.com']) {
    const cookies = await chrome.cookies.getAll({domain, storeId});
    for (const cookie of cookies) {
      const host = cookie.domain.replace(/^\./, '');
      if (host !== domain || /^(__cf_|cf_|_cf|.*turnstile|.*challenge|.*device|oai-did|did$)/i.test(cookie.name)) continue;
      if (domain === 'chatgpt.com' && !/session|csrf|callback|login|auth/i.test(cookie.name)) continue;
      await chrome.cookies.remove({url: 'https://' + host + cookie.path, name: cookie.name, storeId,
        ...(cookie.partitionKey ? {partitionKey: cookie.partitionKey} : {})});
    }
  }
  await chrome.tabs.update(tab.id, {url: 'https://chatgpt.com/auth/login#wb_login=' + job.request_id});
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id === chrome.runtime.id && message?.type === 'workbench_ready' && sender.tab && loopbackOrigin(sender.url || sender.tab.url)) {
    void scan();
    reply({ok: true});
    return false;
  }
  if (sender.id !== chrome.runtime.id || sender.tab || !sender.url?.startsWith(chrome.runtime.getURL(''))) return;
  (async () => {
    if (message?.type === 'connect') await connectFromTab();
    else if (message?.type === 'scan') await scan();
    else if (message?.type === 'disconnect') {
      await chrome.storage.session.clear();
      await chrome.storage.session.set({autoConnectDisabled: true});
      clearTimeout(nextScan);
      await chrome.alarms.clear('session-scan');
      await chrome.action.setBadgeText({text: ''});
    }
    const {connection, status: current} = await chrome.storage.session.get(['connection', 'status']);
    return {ok: true, connected: Boolean(connection), message: current?.message || '尚未连接本机工作台', incognito: await chrome.extension.isAllowedIncognitoAccess()};
  })().then(reply).catch(error => reply({ok: false, message: message?.type === 'connect' ? error.message : '操作未完成，请重试'}));
  return true;
});

chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'session-scan') void scan(); });
chrome.tabs.onCreated?.addListener(tab => {
  const url = tab.pendingUrl || tab.url || '';
  if (!tab.incognito || !url.startsWith('https://chatgpt.com/auth/login#wb_login=')) return;
  void chrome.storage.session.get('loginTabMarkers').then(({loginTabMarkers = {}}) => {
    loginTabMarkers[tab.id] = url.split('#wb_login=')[1];
    return chrome.storage.session.set({loginTabMarkers: Object.fromEntries(Object.entries(loginTabMarkers).slice(-30))});
  }).then(() => scan());
});
chrome.tabs.onUpdated.addListener((_id, change, tab) => {
  const url = change.url || tab.url || '';
  if (url.startsWith('https://chatgpt.com/auth/login#wb_login=')) {
    void chrome.storage.session.get('loginTabMarkers').then(({loginTabMarkers = {}}) => {
      loginTabMarkers[_id] = url.split('#wb_login=')[1];
      return chrome.storage.session.set({loginTabMarkers: Object.fromEntries(Object.entries(loginTabMarkers).slice(-30))});
    }).then(() => scan());
  } else if ((change.status === 'complete' || change.url) && /^https?:\/\/(chatgpt\.com|auth\.openai\.com|127\.0\.0\.1)([:/])/.test(url)) void scan();
});

void chrome.alarms.create('session-scan', {periodInMinutes: 0.5});
chrome.cookies.onChanged.addListener(({cookie}) => {
  if (cookie.domain.replace(/^\./, '') === 'chatgpt.com' && cookie.name.includes('session-token') && Date.now() - lastScanTime > 2000) void scan();
});
