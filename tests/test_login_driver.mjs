import assert from 'node:assert/strict';
import test from 'node:test';
import {driveLogin} from '../edge-extension/login-driver.mjs';

function harness() {
  const job = {record_id: 'record', request_id: 'nonce', email: 'owner@example.com', automatic: true};
  const tab = {id: 2, incognito: true, url: 'https://auth.openai.com/log-in/password'};
  const stored = {loginProgress: {nonce: {tabId: 2, submitted: {}, attempts: {}, codeIds: [], firstSeen: Object.fromEntries(['email', 'password', 'email_code', 'totp'].map(kind => [kind, Date.now() - 2000]))}}};
  const submitted = [];
  const requests = [];
  let observation = {kind: 'email', ready: true};
  globalThis.chrome = {
    storage: {session: {async get() {return stored;}, async set(data) {Object.assign(stored, data);}}},
    scripting: {async executeScript(options) {
      if (options.func.name === 'observeLogin') return [{result: observation}];
      assert.equal(options.func.name, 'fillLogin');
      submitted.push(options.args);
      return [{result: {state: 'submitted'}}];
    }},
  };
  const bridge = async (_path, body) => {
    requests.push(body);
    if (['progress', 'failure'].includes(body.kind)) return {ok: true};
    if (body.kind === 'password_choice') return {available: true, method: body.method};
    return {available: true, value: {email: job.email, password: 'TEST-PASSWORD', email_code: '123456', totp: '654321'}[body.kind],
      ...(['totp', 'email_code'].includes(body.kind) ? {code_id: body.kind + '-HASH'} : {})};
  };
  return {job, tab, stored, submitted, requests,
    observe(value) {observation = value;},
    run() {return driveLogin(job, [tab], bridge, async () => {});},
    restore() {delete globalThis.chrome;},
  };
}

test('email, password and a given OTP are submitted once while polling continues', async () => {
  const h = harness();
  try {
    for (const kind of ['email', 'password', 'email_code', 'totp']) {
      h.observe({kind, ready: true, observedEmail: kind === 'password' ? h.job.email : undefined});
      await h.run();
      await h.run();
    }
    assert.deepEqual(h.submitted.map(args => args[0]), ['email', 'password', 'email_code', 'totp']);
    assert.ok(h.requests.some(request => request.stage === 'email_code_submitted'));
    assert.ok(h.requests.some(request => request.stage === 'totp_submitted'));
    assert.ok(h.requests.some(request => request.stage === 'email_code_submitted'));
    assert.ok(h.requests.some(request => request.stage === 'totp_submitted'));
    const stored = JSON.stringify(h.stored);
    for (const secret of ['TEST-PASSWORD', '123456', '654321']) assert.ok(!stored.includes(secret));
  } finally {h.restore();}
});

test('security checks and unknown OTP types never request or submit credentials', async () => {
  const h = harness();
  try {
    for (const kind of ['challenge', 'unknown_code']) {h.observe({kind}); await h.run();}
    assert.equal(h.submitted.length, 0);
    assert.ok(h.requests.every(request => request.kind === 'progress'));
  } finally {h.restore();}
});

test('email-code screen switches to an explicit password option without requesting a code', async () => {
  const h = harness();
  const statuses = [];
  try {
    h.observe({kind: 'password_choice', ready: true});
    await driveLogin(h.job, [h.tab], async (_path, body) => {
      h.requests.push(body);
      return body.kind === 'password_choice' ? {available: true, method: body.method} : {ok: true};
    }, async message => statuses.push(message));
    await driveLogin(h.job, [h.tab], async (_path, body) => {
      h.requests.push(body);
      return {available: true, method: body.method};
    }, async message => statuses.push(message));
    assert.deepEqual(h.submitted.map(args => args[0]), ['password_choice']);
    assert.deepEqual(h.requests.map(request => request.kind), ['password_choice', 'progress']);
    assert.equal(h.requests[1].stage, 'password_choice');
    assert.equal(statuses.at(-1), '已切换为密码登录，正在等待密码步骤');
  } finally {h.restore();}
});

test('400 pages and a mismatched readonly email stop before requesting a password', async () => {
  const h = harness();
  try {
    h.observe({kind: 'error', code: 'route400'}); await h.run();
    h.observe({kind: 'password', ready: true, observedEmail: 'other@example.com'}); await h.run();
    assert.deepEqual(h.requests.map(request => request.code), ['route400', 'account_mismatch']);
    assert.equal(h.submitted.length, 0);
  } finally {h.restore();}
});
