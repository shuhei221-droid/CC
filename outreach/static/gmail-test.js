/* Diagnostic sends deliberately do not create sales contacts or campaigns. */
(() => {
  'use strict';
  const node = id => document.getElementById('gmail-test-' + id);
  let busy = false, approval = null, targets = [];
  const stateNames = {prepared:'未送信',sending:'送信中または結果確認待ち（再送しません）',
    sent:'Gmailで送信受付済み',unknown:'送信結果不明（Gmailの送信済みを確認してください。再送しません）'};
  async function request(path, data) {
    const options = {credentials:'same-origin',headers:{Accept:'application/json'}};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = document.querySelector('meta[name=csrf-token]').content;
      options.body = JSON.stringify(data);
    }
    const response = await fetch('/api/gmail/test/' + path, options);
    const result = await response.json();
    if (!response.ok) throw Error(typeof result.detail==='string'?result.detail:'処理を確認できません。');
    return result;
  }
  function controls() {
    node('recipient').disabled = busy || !targets.length;
    node('preview').disabled = busy || !targets.length;
    node('send').disabled = busy || !approval || !node('consent').checked;
    node('refresh').disabled = busy;
    node('consent').disabled = busy;
    node('history').querySelectorAll('button').forEach(b => {b.disabled=busy;});
  }
  function clearApproval() {
    approval = null;
    node('consent').checked = false;
    node('approval').hidden = true;
  }
  function text(tag, value) {const n=document.createElement(tag);n.textContent=value;return n;}
  async function refresh() {
    const state = await request('status');
    const selected = node('recipient').value;
    targets = state.recipients;
    node('recipient').replaceChildren(...targets.map(value => {
      const option=text('option',value); option.value=value; return option;
    }));
    if (targets.includes(selected)) node('recipient').value=selected;
    node('config').textContent = targets.length ? '許可済みのテスト先から選択してください。' :
      'テスト先が未設定です。Vercelの環境変数 GMAIL_TEST_RECIPIENTS に、自分が管理するメールアドレスを登録して再デプロイしてください。';
    node('history').replaceChildren(...state.history.map(row => {
      const article=document.createElement('article'); article.className='item';
      article.append(text('p',row.mailbox+' → '+row.recipient),text('p',stateNames[row.status]||row.status));
      if(row.status==='sent') {
        article.append(text('p','送信受付日時：'+row.sent_at+' UTC ／ 返信検出：'+row.reply_count+'件'));
        const button=text('button','このテストの返信を確認');button.type='button';button.className='btn';
        button.onclick=()=>run(async()=>{
          const result=await request('replies',{id:row.id});
          await refresh();
          node('notice').textContent=result.reply_count?'テスト先からの返信を確認しました。':'まだテスト先からの返信を確認できません。受信メールに返信した後、もう一度確認してください。';
        });article.append(button);
      }
      return article;
    }));
    controls();
  }
  async function run(action) {
    if(busy)return;
    busy=true;controls();node('notice').textContent='確認中…';
    try {await action();} catch(error) {node('notice').textContent=error.message||'処理結果を確認できません。履歴を更新してください。';}
    finally {busy=false;controls();}
  }
  node('recipient').onchange=()=>{clearApproval();controls();};
  node('consent').onchange=controls;
  node('preview').onclick=()=>run(async()=>{
    clearApproval();
    approval=await request('preview',{recipient:node('recipient').value});
    node('content').textContent='送信元：'+approval.mailbox+'\n宛先：'+approval.recipient+'\n件名：'+approval.subject+'\n\n'+approval.body;
    node('approval').hidden=false;
    node('notice').textContent='まだ送信していません。宛先と文面を確認してください。';
  });
  node('send').onclick=()=>run(async()=>{
    if(!approval||!node('consent').checked)throw Error('宛先・文面を確認してください。');
    const payload={id:approval.id,snapshot:approval.snapshot,confirm:true};
    clearApproval();
    try {
      const result=await request('send',payload);
      await refresh();
      node('notice').textContent=stateNames[result.status]+(result.status==='sent'?'。テスト先の受信トレイを確認し、このメールに返信してください。':'');
    } catch(error) {
      node('notice').textContent='送信結果を確認できません。再送せず「テスト履歴を更新」とGmailの送信済みを確認してください。 '+error.message;
    }
  });
  node('refresh').onclick=()=>run(async()=>{clearApproval();await refresh();node('notice').textContent='テスト履歴を更新しました。';});
  run(async()=>{await refresh();node('notice').textContent='';});
})();
