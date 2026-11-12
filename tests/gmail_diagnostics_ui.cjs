'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const code=fs.readFileSync(__dirname+'/../outreach/static/gmail-test.js','utf8');
const html=fs.readFileSync(__dirname+'/../outreach/templates/gmail.html','utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;if(nodes[0]?.tag==='option')this.value=nodes[0].value;}
  querySelectorAll(){return this.children.flatMap(n=>[...(n.tag==='button'?[n]:[]),...n.querySelectorAll()]);}
  set innerHTML(_){throw Error('Unsafe HTML rendering');}
}
async function fixture(){
  const nodes=new Map(),calls=[];
  for(const match of html.matchAll(/id="(gmail-test-[^"]+)"/g))nodes.set(match[1],new Node());
  const get=id=>{assert.ok(nodes.has(id),'Missing template id '+id);return nodes.get(id);};
  const state={recipients:['owner@example.test'],history:[]};
  const preview={id:'abc',snapshot:'sealed',mailbox:'sender@example.test',recipient:'owner@example.test',subject:'test',body:'fixed',status:'prepared'};
  let rejectSend=false;
  const context={document:{getElementById:get,querySelector:()=>({content:'csrf-fixture'}),createElement:tag=>new Node(tag)},
    fetch:async(url,options)=>{
      calls.push({url,options});let result;
      if(url.endsWith('/status'))result=state;
      else if(url.endsWith('/preview'))result=preview;
      else if(url.endsWith('/send')){
        assert.equal(options.headers['X-CSRF-Token'],'csrf-fixture');
        if(rejectSend)throw Error('Network interrupted');
        result={...preview,status:'sent',reply_count:0,sent_at:'2026-01-01 01:00:00'};state.history=[result];
      } else if(url.endsWith('/replies'))result={...preview,status:'sent',reply_count:1};
      else throw Error('Unexpected API');
      return {ok:true,json:async()=>result};
    }};
  vm.runInNewContext(code,context);await settle();
  return {node:id=>get('gmail-test-'+id),calls,state,reject:()=>{rejectSend=true;}};
}
test('preview requires checkbox; double clicks send once with CSRF and immutable snapshot',async()=>{
  const f=await fixture();
  assert.equal(f.calls.length,1);
  assert.equal(f.node('send').disabled,true);
  await f.node('preview').onclick();
  assert.match(f.node('content').textContent,/owner@example.test/);
  assert.equal(f.node('send').disabled,true);
  f.node('consent').checked=true;f.node('consent').onchange();
  assert.equal(f.node('send').disabled,false);
  await Promise.all([f.node('send').onclick(),f.node('send').onclick()]);
  const calls=f.calls.filter(c=>c.url.endsWith('/send'));
  assert.equal(calls.length,1);
  assert.deepEqual(JSON.parse(calls[0].options.body),{id:'abc',snapshot:'sealed',confirm:true});
  assert.match(f.node('notice').textContent,/送信受付済み/);
  assert.equal(f.node('send').disabled,true);
  await f.node('history').querySelectorAll()[0].onclick();
  assert.match(f.node('notice').textContent,/返信を確認しました/);
});
test('network uncertainty clears approval and never retries',async()=>{
  const f=await fixture();await f.node('preview').onclick();
  f.node('consent').checked=true;f.reject();await f.node('send').onclick();
  assert.match(f.node('notice').textContent,/再送せず/);
  assert.equal(f.node('send').disabled,true);
  assert.equal(f.calls.filter(c=>c.url.endsWith('/send')).length,1);
});
test('no targets disables sending; changing recipient invalidates prior preview',async()=>{
  const f=await fixture();await f.node('preview').onclick();
  f.node('recipient').onchange();
  assert.equal(f.node('send').disabled,true);
  f.state.recipients=[];await f.node('refresh').onclick();
  assert.equal(f.node('preview').disabled,true);
  assert.match(f.node('config').textContent,/GMAIL_TEST_RECIPIENTS/);
});
