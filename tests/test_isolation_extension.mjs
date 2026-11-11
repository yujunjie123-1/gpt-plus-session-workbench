import assert from 'node:assert/strict';
import test from 'node:test';
import {isolationBootstrap,jobsForConnection,jobIdentity} from '../edge-extension/isolation-utils.mjs';
import {driveLogin} from '../edge-extension/login-driver.mjs';

test('bootstrap accepts only a loopback page and two bounded random identifiers',()=>{
  const instance='a'.repeat(43),nonce='b'.repeat(43);
  const url=`http://127.0.0.1:12345/edge-session-bridge#instance=${instance}&nonce=${nonce}`;
  assert.deepEqual(isolationBootstrap(url),{origin:'http://127.0.0.1:12345',instance_id:instance,bootstrap_nonce:nonce});
  for(const value of [url.replace('127.0.0.1','evil.test'),url.replace('/edge-session-bridge','/'),url+'&nonce=again',url.replace('#','?x=1#'),url.replace(nonce,'short')])assert.equal(isolationBootstrap(value),null);
});

test('legacy and isolated helpers cannot claim each others jobs',()=>{
  const jobs=[{record_id:'legacy'},{record_id:'one',instance_id:'one'},{record_id:'two',instance_id:'two'}];
  assert.deepEqual(jobsForConnection(jobs,{instance_id:'one'}),[jobs[1]]);
  assert.deepEqual(jobsForConnection(jobs,{}),[jobs[0]]);
  assert.deepEqual(jobsForConnection(jobs,{instance_id:'unknown'}),[]);
  assert.deepEqual(jobIdentity({...jobs[1],request_id:'nonce',password:'NEVER-SEND'}),{record_id:'one',instance_id:'one',request_id:'nonce'});
});

test('isolated login never falls back to another unmarked authentication tab',async()=>{
  const stored={};let scripts=0,requests=0;
  globalThis.chrome={storage:{session:{async get(){return stored;},async set(data){Object.assign(stored,data);}}},
    scripting:{async executeScript(){scripts++;return [{result:{kind:'password',ready:true}}];}}};
  try{
    await driveLogin({record_id:'one',instance_id:'instance',request_id:'own-nonce',automatic:true,email:'one@example.com'},
      [{id:7,incognito:true,url:'https://auth.openai.com/log-in/password'}],async()=>{requests++;},async()=>{});
    assert.equal(scripts,0);assert.equal(requests,0);
  }finally{delete globalThis.chrome;}
});

test('a bound isolated tab retains its instance on every credential and progress request',async()=>{
  const stored={loginProgress:{nonce:{tabId:7,submitted:{},attempts:{},codeIds:[],firstSeen:{email:Date.now()-2000}}}};
  const sent=[];
  globalThis.chrome={storage:{session:{async get(){return stored;},async set(data){Object.assign(stored,data);}}},
    scripting:{async executeScript(options){return [{result:options.func.name==='observeLogin'?{kind:'email',ready:true}:{state:'submitted'}}];}}};
  try{
    await driveLogin({record_id:'one',instance_id:'instance',request_id:'nonce',automatic:true,email:'one@example.com'},
      [{id:7,incognito:true,url:'https://auth.openai.com/log-in'}],async(_path,body)=>{
        sent.push(body);return {available:true,value:'one@example.com'};
      },async()=>{});
    assert.deepEqual(sent.map(body=>body.kind),['email','progress']);
    assert.ok(sent.every(body=>body.instance_id==='instance'&&body.record_id==='one'&&body.request_id==='nonce'));
  }finally{delete globalThis.chrome;}
});
