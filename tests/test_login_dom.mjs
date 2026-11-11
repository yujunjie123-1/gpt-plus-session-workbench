import assert from 'node:assert/strict';
import test from 'node:test';
import {observeLogin} from '../edge-extension/login-dom.mjs';

globalThis.getComputedStyle = () => ({visibility: 'visible'});

function input(type, options = {}) {
  return {type, disabled: false, readOnly: false, value: '', attrs: options,
    getBoundingClientRect: () => ({width: 180, height: 32}),
    innerText: '', textContent: '',
  };
}

function page({text = '', inputs = [], buttons = [], alerts = []} = {}) {
  const matches = (selector, element) => {
    if (selector.includes('one-time-code') && element.attrs?.autocomplete === 'one-time-code') return true;
    if ((selector.includes('name*="code"') || selector.includes('id*="code"'))
        && /code/i.test(element.attrs?.name || element.attrs?.id || '')) return true;
    if (selector.includes('inputmode="numeric"') && element.attrs?.inputmode === 'numeric') return true;
    if (selector.includes('maxlength="1"') && element.attrs?.maxlength === '1') return true;
    if (selector.includes('type="password"') && element.type === 'password') return true;
    if (selector.includes('type="email"') && element.type === 'email') return true;
    if (selector.includes('autocomplete="username"') && element.attrs?.autocomplete === 'username') return true;
    return false;
  };
  const doc = {
    body: {innerText: text}, readyState: 'complete',
    querySelectorAll(selector) {
      if (selector.startsWith('input[')) return inputs.filter(element => matches(selector, element));
      if (selector.startsWith('button,')) return buttons;
      if (selector.startsWith('iframe[')) return [];
      if (selector.startsWith('[role="alert"]')) return alerts;
      return [];
    },
    querySelector(selector) {
      return inputs.find(element => matches(selector, element)) || null;
    },
  };
  return {doc, location: {origin: 'https://auth.openai.com', pathname: '/log-in/password'}};
}

test('a visible password field takes priority over stale timeout text', () => {
  const current = page({text: 'Operation timed out', inputs: [input('password')]});
  assert.equal(observeLogin(current.doc, current.location).kind, 'password');
});

test('human verification takes priority over a visible login form', () => {
  const current = page({text: 'Verify you are human', inputs: [input('email')]});
  assert.equal(observeLogin(current.doc, current.location).kind, 'challenge');
});

test('email-code page can select password login and authenticator code is recognized', () => {
  const usePassword = {innerText: 'Use password', textContent: 'Use password', disabled: false,
    getBoundingClientRect: () => ({width: 100, height: 32})};
  let current = page({text: 'Check your email. We sent you a code.',
    inputs: [input('text', {autocomplete: 'one-time-code'})], buttons: [usePassword]});
  assert.deepEqual(observeLogin(current.doc, current.location),
    {kind: 'password_choice', method: 'password', ready: true});
  current = page({text: 'Enter the code from your authenticator app.',
    inputs: [input('text', {autocomplete: 'one-time-code'})]});
  assert.equal(observeLogin(current.doc, current.location).kind, 'totp');
});

test('only a precise visible timeout alert with retry control triggers timeout handling', () => {
  const retry = {innerText: 'Retry', textContent: 'Retry', disabled: false,
    getBoundingClientRect: () => ({width: 70, height: 30})};
  const alert = {innerText: 'Operation timed out', textContent: 'Operation timed out',
    getBoundingClientRect: () => ({width: 200, height: 30})};
  let current = page({text: 'Operation timed out', buttons: [retry], alerts: [alert]});
  assert.equal(observeLogin(current.doc, current.location).kind, 'operation_timeout');
  current = page({text: 'Operation timed out. Please refresh and try again.', buttons: [retry]});
  assert.equal(observeLogin(current.doc, current.location).kind, 'none');
});
