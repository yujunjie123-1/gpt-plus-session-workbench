import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parseAccounts,parseAccountLine} from '../web/account-import.mjs';

for (const delimiter of ['----','--','//']) {
  test(`optional credential fields using ${delimiter}`,()=>{
    const email='example--alias+test@icloud.com',password='P%w@rd!9',totp='ABCDEFGHIJKLMNOP';
    assert.deepEqual(parseAccountLine(email),{email});
    const expected={email,password,totp:'',mail_url:''};
    assert.deepEqual(parseAccountLine([email,password].join(delimiter)),expected);
    assert.deepEqual(parseAccountLine([email,password,totp].join(delimiter)),{...expected,totp});
    const mail_url='https://mail.example.com/api?key=example--query//suffix';
    assert.deepEqual(parseAccountLine([email,password,totp,mail_url].join(delimiter)),{...expected,totp,mail_url});
    assert.deepEqual(parseAccountLine([email,password,mail_url].join(delimiter)),{...expected,mail_url});
    assert.deepEqual(parseAccountLine([email,password,'',mail_url].join(delimiter)),{...expected,mail_url});
    // ---- at the first boundary means the four-dash delimiter. Use CSV/JSON
    // for the ambiguous two-dash format with an empty password.
    if(delimiter!=='--') assert.deepEqual(parseAccountLine([email,'',totp].join(delimiter)),{...expected,password:'',totp});
  });
}
test('blank lines, BOM and original line numbers',()=>{
  assert.equal(parseAccounts('\ufeff example@icloud.com--pw--ABCDEFGH\n\nother@icloud.com').length,2);
  assert.throws(()=>parseAccounts('example@icloud.com\n\nnot-an-email'),/第 3 行/);
});
test('CSV and JSON retain delimiter-containing passwords',()=>{
  assert.equal(parseAccounts('email,password,totp\r\na@example.com,"p--w//,x",ABC')[0].password,'p--w//,x');
  assert.equal(parseAccounts('[{"email":"a@example.com","password":"p--w//"}]')[0].password,'p--w//');
});
test('errors never echo raw credential input',()=>{
  const secret='SYNTHETIC-SECRET';
  for(const input of [`a@example.com--${secret}--ABC--garbage`,`[{"password":"${secret}"`, 'bad']) {
    assert.throws(()=>parseAccounts(input),error=>!error.message.includes(secret));
  }
  assert.throws(()=>parseAccounts('a@example.com--pw--ABC--http://mail.example.com'),/链接可省略/);
  assert.throws(()=>parseAccounts('a@example.com--pw--ABC--https://user:pass@mail.example.com'),/HTTPS/);
});
