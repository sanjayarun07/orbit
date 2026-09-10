const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/executors.js', 'utf8');

function setup(fetch, relay = {}) {
  const context = {window: {OrbitRelay: relay}, fetch, encodeURIComponent};
  vm.runInNewContext(source, context);
  return context.window.OrbitExecutors;
}
const response = body => ({ok:true, json:async()=>body});

test('Jupiter uses sign-only and server-reviewed submission', async()=>{
  const calls=[];
  const executors=setup(async(path, options)=>{
    calls.push([path, options]);
    return response(path.endsWith('/wallet-transaction')
      ? {transaction:'unsigned', wallet_address:'wallet'}
      : {signature:'sig',status:'submitted'});
  });
  let signed=false;
  const prepared=await executors.jupiter.prepare({plan_id:'plan',confirmation_text:'CONFIRM plan',sign:async(tx,wallet)=>{
    assert.equal(tx,'unsigned');assert.equal(wallet,'wallet');signed=true;return 'signed';
  }});
  assert.equal(signed,false);
  const result=await executors.jupiter.execute(prepared);
  assert.equal(result.status,'submitted');
  assert.equal(JSON.parse(calls[1][1].body).signed_transaction,'signed');
  assert.ok(calls[1][0].endsWith('/submit-wallet-transaction'));
});

test('Relay claims tracking before entering wallet SDK', async()=>{
  const calls=[];
  const executors=setup(async()=>{calls.push('persist');return response({execution_claimed:true});}, {
    executeQuote:async()=>calls.push('wallet'),
  });
  await executors.executeRelay({steps:[{requestId:'request-1234567890'}]}, {}, ()=>{});
  assert.deepEqual(calls,['persist','wallet']);
});

test('Relay cannot execute a repeated or untrackable request', async()=>{
  let sent=0;
  const executors=setup(async()=>response({execution_claimed:false}), {executeQuote:async()=>sent++});
  await assert.rejects(executors.executeRelay({steps:[{requestId:'request-1234567890'}]}, {}, ()=>{}), /already attempted/);
  await assert.rejects(executors.executeRelay({steps:[]}, {}, ()=>{}), /no trackable request/);
  assert.equal(sent,0);
});

test('Relay persistence failure never enters SDK', async()=>{
  const executors=setup(async()=>{throw Error('offline');}, {executeQuote:async()=>assert.fail('must not sign')});
  await assert.rejects(executors.executeRelay({steps:[{requestId:'request-1234567890'}]}, {}, ()=>{}), /offline/);
});

test('Context replacement during claim cannot enter wallet SDK', async()=>{
  let current=true;
  const executors=setup(async()=>{current=false;return response({execution_claimed:true});}, {executeQuote:async()=>assert.fail('stale request signed')});
  await assert.rejects(executors.executeRelay({steps:[{requestId:'request-1234567890'}]}, {}, ()=>{}, {session_id:'chat',revision:1}, ()=>current), /superseded/);
});

test('Chat JavaScript parses and no longer broadcasts Jupiter plans directly', ()=>{
  const html=fs.readFileSync('app/static/index.html','utf8');
  for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  const card=html.slice(html.indexOf('function renderPlanCard('),html.indexOf('function findRelayChain('));
  assert.ok(card.includes('window.OrbitExecutors.jupiter'));
  assert.ok(!card.includes('signAndSend'));
  assert.ok(html.includes('if(!isCurrent()||!state)return;'));
  assert.ok(!html.includes('Fetching a fresh quote…'));
  assert.ok(html.includes('/executions/relay/session/'));
});
