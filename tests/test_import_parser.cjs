const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const parse=require(path.resolve(__dirname,'../web/import-parser.js'));

test('four-part account preserves plus email, password and private mail URL',()=>{
  const result=parse.account('owner+alias@example.com----Example@Pass----JBSWY3DPEHPK3PXP----https://mail.example.com/api/v1/code?key=TEST_ONLY');
  assert.equal(result.email,'owner+alias@example.com');
  assert.equal(result.password,'Example@Pass');
  assert.equal(result.totp,'JBSWY3DPEHPK3PXP');
  assert.equal(result.mail_url,'https://mail.example.com/api/v1/code?key=TEST_ONLY');
});
test('malformed account and unsafe mail link are rejected without echoing secrets',()=>{
  for(const text of ['owner@example.com----PRIVATE', 'owner@example.com----PRIVATE----SEED----http://mail.example.com/key',
    'owner@example.com----PRIVATE----SEED----https://user:PRIVATE@mail.example.com/']){
    assert.throws(()=>parse.account(text),e=>!e.message.includes('PRIVATE'));
  }
});
test('Chinese delivery text, CRLF and plain single card are recognized',()=>{
  assert.deepEqual(parse.card('卡密：DEMO-PLUS-TEST\r\n兑换地址：https://cdk.bar/'),{code:'DEMO-PLUS-TEST',address:'https://cdk.bar/'});
  assert.equal(parse.card('DEMO-PLUS-TEST').code,'DEMO-PLUS-TEST');
});
test('multiple cards and alternate redemption origins cannot be silently accepted',()=>{
  for(const text of ['CODE-ONE\nCODE-TWO','卡密：CODE-ONE\n兑换地址：https://evil.example/',
    '卡密：CODE-ONE\n兑换地址：https://cdk.bar/?key=SECRET','卡密：CODE-ONE\n兑换地址：https://user:pass@cdk.bar/']){
    assert.throws(()=>parse.card(text));
  }
});
