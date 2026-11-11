import assert from 'node:assert/strict';
import test from 'node:test';
import {cookieSession, jobForSession, loopbackOrigin} from '../edge-extension/session-utils.mjs';

const email = 'owner@example.com';
const session = {user: {email}, accessToken: 'TEST-ACCESS-SECRET', expires: '2099-01-01T00:00:00Z'};
const job = {record_id: 'test-record', request_id: 'test-request', email, incognito: true, expires_at: 4070908800};
const cookie = (name, value, extra = {}) => ({name, value, domain: '.chatgpt.com', path: '/', secure: true, storeId: '1', ...extra});

test('connection stays on numeric IPv4 loopback with an explicit port', () => {
  assert.equal(loopbackOrigin('http://127.0.0.1:12345/'), 'http://127.0.0.1:12345');
  for (const url of ['https://evil.test', 'http://127.0.0.1.evil.test:1234', 'http://127.0.0.1/', 'http://user:secret@127.0.0.1:1234', 'file:///test']) assert.equal(loopbackOrigin(url), '');
});

test('cookie chunks use only the target store, domain, and partition', () => {
  const name = '__Secure-next-auth.session-token';
  assert.equal(cookieSession([cookie(name, 'NORMAL', {storeId: '0'}), cookie(name + '.1', 'B'), cookie(name + '.0', 'A')], '1'), 'AB');
  assert.equal(cookieSession([cookie(name + '.1', 'B')], '1'), '');
  assert.equal(cookieSession([cookie(name, 'WRONG', {domain: '.evil.test'})], '1'), '');
  assert.equal(cookieSession([cookie(name, 'WRONG', {partitionKey: {topLevelSite: 'https://evil.test'}})], '1'), '');
  assert.equal(cookieSession([cookie(name, 'A'), cookie(name, 'B')], '1'), '');
});

test('session matching checks mailbox, expiry, and window mode', () => {
  assert.equal(jobForSession([job], session, true)?.record_id, job.record_id);
  assert.equal(jobForSession([job], session, false), null);
  assert.equal(jobForSession([job], {...session, user: {email: 'other@example.com'}}, true), null);
  assert.equal(jobForSession([job], {...session, expires: '2020-01-01T00:00:00Z'}, true), null);
});

let moduleIndex = 0;
async function harness({allowIncognito = true, data = session, currentJob = job, pageResponsive = true, loginTabMarkers = {}} = {}) {
  const stored = {connection: {origin: 'http://127.0.0.1:12345', token: 'TEST-BRIDGE-TOKEN', ...(currentJob.instance_id ? {instance_id: currentJob.instance_id} : {})}, loginTabMarkers};
  const sent = [];
  const scripts = [];
  let listener;
  const event = {addListener() {}};
  const originalFetch = globalThis.fetch;
  globalThis.chrome = {
    storage: {session: {
      async get() {return {...stored};}, async set(values) {Object.assign(stored, values);}, async clear() {for (const key of Object.keys(stored)) delete stored[key];},
    }},
    action: {async setBadgeText() {}, async setBadgeBackgroundColor() {}},
    extension: {async isAllowedIncognitoAccess() {return allowIncognito;}},
    runtime: {id: 'test-extension', getURL: path => 'chrome-extension://test-extension/' + path, onMessage: {addListener(fn) {listener = fn;}}},
    alarms: {async create() {}, async clear() {}, onAlarm: event},
    tabs: {async query() {return [{id: 1, incognito: false, url: 'https://chatgpt.com/'}, {id: 2, incognito: true, url: 'https://chatgpt.com/'}];}, onUpdated: event},
    cookies: {
      async getAllCookieStores() {return [{id: '0', tabIds: [1]}, {id: '1', tabIds: [2]}];},
      async getAll(filter) {assert.equal(filter.storeId, '1'); return [cookie('__Secure-next-auth.session-token', 'TEST-PRIVATE-COOKIE')];},
      onChanged: event,
    },
    scripting: {async executeScript(options) {
      scripts.push(options.target.tabId);
      assert.equal(options.target.tabId, 2);
      if (options.world === 'MAIN' && options.func.toString().includes('document?.documentElement')) {
        if (!pageResponsive) throw new Error('tab crashed');
        return [{result: true}];
      }
      if (options.world === 'MAIN') {
        if (!pageResponsive) throw new Error('tab crashed');
        return [{result: data}];
      }
      return [{result: {kind: 'none'}}];
    }},
  };
  globalThis.fetch = async (url, options) => {
    assert.ok(url.startsWith('http://127.0.0.1:12345/api/edge-session/'));
    assert.equal(options.headers.Authorization, 'Bearer TEST-BRIDGE-TOKEN');
    if (options.method === 'POST') {sent.push(JSON.parse(options.body)); return {ok: true, async json() {return {ok: true};}};}
    return {ok: true, async json() {return {jobs: [currentJob]};}};
  };
  await import('../edge-extension/background.js?test=' + ++moduleIndex);
  const invoke = type => new Promise(resolve => listener({type}, {id: 'test-extension', url: 'chrome-extension://test-extension/popup.html'}, resolve));
  return {stored, sent, scripts, invoke, restore() {globalThis.fetch = originalFetch; delete globalThis.chrome;}};
}

test('worker hands off only the private tab and never stores captured Session', async () => {
  const h = await harness();
  try {
    const reply = await h.invoke('scan');
    assert.equal(reply.ok, true);
    assert.deepEqual(h.scripts, [2]);
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].session.sessionToken, 'TEST-PRIVATE-COOKIE');
    assert.equal(h.sent[0].request_id, job.request_id);
    for (const text of [JSON.stringify(h.stored), JSON.stringify(reply)]) {
      assert.ok(!text.includes('TEST-PRIVATE-COOKIE'));
      assert.ok(!text.includes('TEST-ACCESS-SECRET'));
    }
    assert.ok(!JSON.stringify(reply).includes('TEST-BRIDGE-TOKEN'));
  } finally {h.restore();}
});

test('worker skips a logged-in account that does not match the pending job', async () => {
  const h = await harness({data: {...session, user: {email: 'other@example.com'}}});
  try {await h.invoke('scan'); assert.equal(h.sent.length, 0);} finally {h.restore();}
});

test('worker reports missing InPrivate permission without reading login data', async () => {
  const h = await harness({allowIncognito: false});
  try {
    const reply = await h.invoke('scan');
    assert.equal(reply.incognito, false);
    assert.equal(h.scripts.length, 0);
    assert.equal(h.sent.length, 0);
  } finally {h.restore();}
});

test('worker sends a page heartbeat only after the bound InPrivate tab responds to a probe', async () => {
  const currentJob = {...job, instance_id: 'TEST-INSTANCE', automatic: false};
  const h = await harness({data: null, currentJob, loginTabMarkers: {2: currentJob.request_id}});
  try {
    await h.invoke('scan');
    assert.equal(h.sent.filter(item => item.kind === 'page_heartbeat').length, 1);
    assert.equal(h.sent[0].instance_id, currentJob.instance_id);
    assert.equal(h.sent[0].page_id, 2);
  } finally {h.restore();}
});

test('worker does not heartbeat a crashed InPrivate login page', async () => {
  const currentJob = {...job, instance_id: 'TEST-INSTANCE', automatic: false};
  const h = await harness({data: null, currentJob, pageResponsive: false, loginTabMarkers: {2: currentJob.request_id}});
  try {
    await h.invoke('scan');
    assert.equal(h.sent.filter(item => item.kind === 'page_heartbeat').length, 0);
  } finally {h.restore();}
});

test('explicit disconnect suppresses automatic reconnection', async () => {
  const h = await harness();
  try {
    await h.invoke('disconnect');
    await h.invoke('scan');
    assert.equal(h.stored.autoConnectDisabled, true);
    assert.equal(h.scripts.length, 0);
    assert.equal(h.sent.length, 0);
  } finally {h.restore();}
});
