// M1.5 のウォークスルー用の画面（PLAN M1.5、REQUIREMENTS C-6〜C-12）。
// index.html のビューアーの上に、チャット・要約・Facet の選択・確認のモーダルを足す。
// AI の中身は Claude が wizard/oz.py で書き込み、この画面は状態を1.5秒ごとに読み直して表示する。
// サーバーでウォークスルーが有効なとき（LATTICE_WIZARD=1）だけ動く。
fetch(`/api/wiz/state?s=${encodeURIComponent(SUBJ)}`).then(r=>r.ok&&(()=>{
let WZ={discussions:[],edits:[],rev:-1},cur=null,tab=0,modalOpen=false;
const ev=(type,o={})=>fetch('/api/wiz/event',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({subject:SUBJ,type,discussion:cur,...o})}).then(async r=>{const j=await r.json().catch(()=>({}));if(!r.ok){ui.alert(j.detail||'できませんでした',{title:'できませんでした'});}return j}).then(r=>poll().then(()=>r));  // 断られたら理由を出す  // 状態を読み直してから次へ進む
const D=()=>WZ.discussions.find(d=>d.id===cur)||(String(cur).startsWith('new:')?{id:'（新しい Discussion）',title:'',state:'open',origin:String(cur).slice(4),
  messages:[],summary:null,candidates:[],drafts:[],waiting:null}:undefined);
const STATE={open:'会話中',review:'確認待ち',closed:'完了',discarded:'廃棄'};
// 待ち時間に、AI（Gemini）とアプリのやりとりを流して見せる（退屈しのぎ。中身を精読するためのものではない）
const FEEDICON={ask:'→',back:'←',think:'💭',tool:'⚙',err:'✕'};
function feed(d){const f=(d.feed||[]).slice(-8);if(!f.length)return'';
  return `<div class="feed">${f.map((x,i)=>`<div class="fl ${x.k}${i===f.length-1?' last':''}"><span class="fi">${FEEDICON[x.k]||'·'}</span>${esc(x.t)}</div>`).join('')}</div>`}
const WAIT={reply:'考えています…',summary:'要約を作っています…',drafts:'L1・L2 の新しい案を作っています…',judge:'修正の内容を判断しています…',carryover:'取り下げた Facet の要点を持ち越しに書いています…'};
const CH={update:'更新する',material:'材料として残す',none:'何もしない'};
const old=p=>F[p]&&!F[p].missing?F[p].body:null;
// 確認の画面の「旧」は、draft を書く直前の本文（サーバーが持つ）。いまのファイルは draft で上書きされている
// 待ちの経過時間（Claude Code のように）。サーバーが待ちの始まりの時刻を持ち、画面は1秒ごとに数字だけ書き換える
const since=d=>d.wait_since?` <span class="elapsed" data-since="${d.wait_since}"></span>`:'';
// 書き換えは、数字が変わったときだけ（同じ値を書くと画面の変化の見張りがまた呼ばれ、止まらなくなる）
const tick=()=>document.querySelectorAll('[data-since]').forEach(e=>{const s=Math.max(0,Math.floor(Date.now()/1000-+e.dataset.since));
  const t=`（${s>=60?Math.floor(s/60)+'分'+String(s%60).padStart(2,'0'):s}秒）`;if(e.textContent!==t)e.textContent=t});
setInterval(tick,1000);new MutationObserver(tick).observe(document.body,{childList:true,subtree:true});
const OLD={};const oldOf=f=>OLD[f.path]=('old' in f?f.old:old(f.path));
// 変わった行に印を付けてから表示する。相手側に同じ行が無い行を、新では <mark>、旧では <del> で囲む
// （行頭の見出し・箇条書きの記号はそのまま残す）
function marked2(text,other,tag){
  const set=new Set(String(other??'').split('\n').map(l=>l.trim()));
  return md(String(text??'').split('\n').map(l=>{const t=l.trim();if(!t||set.has(t))return l;
    const m=l.match(/^(\s*(?:#{1,6}\s|[-*]\s|\d+\.\s)?)(.*)$/);return `${m[1]}<${tag}>${m[2]}</${tag}>`}).join('\n'));
}

// ---- 画面の骨組み ----
document.head.insertAdjacentHTML('beforeend','<link rel="stylesheet" href="wizard.css">');
{const g=document.createElement('script');g.src='gem.js';document.head.appendChild(g)}
document.body.insertAdjacentHTML('beforeend',`<div id="chat"></div><div id="wzmodal"></div><div id="wzedit"></div>`);
const fitChat=()=>document.documentElement.style.setProperty('--hh',document.querySelector('header').getBoundingClientRect().bottom+'px');
new ResizeObserver(fitChat).observe(document.querySelector('header'));addEventListener('resize',fitChat);
const _setView=setView;
setView=function(){_setView();$('#chat').style.display=view==='C'?'flex':'none';if(view==='C')$('#main').style.display=$('#disc').style.display='none'};

// ---- Discussion の一覧（入口1） ----
const _renderDisc=renderDisc;
renderDisc=function(){_renderDisc();
  const list=WZ.discussions.filter(d=>d.state==='open'||d.state==='review');  // 完了したものは下の表に並ぶ
  const rows=list.map(d=>`<tr class="wzrow" data-wz="${d.id}"><td>${esc(d.id)}</td><td>${esc(d.title||'（題名は要約のときに付きます）')}</td>
    <td><span class="wzst ${d.state}">${STATE[d.state]}</span>${d.state==='review'?' <b class="wzalert">承認を待っています</b>':''}</td><td class="muted">${esc(origin(d))}</td></tr>`).join('');
  const edits=WZ.edits.map(e=>`<li>${esc(e.path)} — ${e.waiting?WAIT.judge:e.judgement==='minor'?'体裁だけの修正（log.md に1行）':'Discussion '+esc(e.discussion||'')+' を始めた'}${e.reason&&e.judgement==='minor'?'：'+esc(e.reason):''}</li>`).join('');
  // 開いている要約の下に「引き継いで始める」（C-10）。完了した Discussion はどれも引き継げる
  const br=document.querySelector('#disc tr.bodyrow td');
  if(br&&openDisc&&F[openDisc])br.insertAdjacentHTML('afterbegin',`<button class="wzprimary" data-wzcont="${esc(F[openDisc].fm.discussion_id)}" style="margin-bottom:8px">この Discussion を引き継いで、新しい Discussion を始める</button>`);
  $('#disc').insertAdjacentHTML('afterbegin',`<div class="wzbox"><div class="wzhead"><b>会話中・確認待ちの Discussion</b>
    <button id="wznew" class="wzprimary">＋ 新しい Discussion を始める</button></div>
    ${rows?`<table class="wztable"><tbody>${rows}</tbody></table>`:'<div class="muted">ありません。完了した Discussion は下の表に並びます。</div>'}
    ${edits?`<div class="muted" style="margin-top:6px">人の修正（C-6）</div><ul class="wzedits">${edits}</ul>`:''}</div>`);
  const mg=WZ.migration;
  if(mg&&mg.status!=='done')$('#disc').insertAdjacentHTML('afterbegin',`<div class="wzro">${mg.status==='running'&&!mg.stalled?'<lattice-gem></lattice-gem> ':''}<b>${esc(mg.from)} の中身を移しています</b>　${mg.done} / ${mg.total} Facet${mg.current?'（いま: '+esc(facetLabel(mg.current))+'）':mg.done===0?'（割り当てを決めています）':''}。終わるまで、書き込む操作はできません。
    ${mg.status==='failed'||mg.stalled?`<div><b class="wzalert">止まりました</b> ${esc(mg.error||'（サーバーの入れ替わりなど）')} <button class="wzprimary" data-wzresume="1">続きから再開する</button></div>`:''}</div>`);
  if(WZ.readonly)$('#disc').insertAdjacentHTML('afterbegin',`<div class="wzro"><b>閲覧のみ</b>　この Subject は Drive で閲覧のみの権限です。読むことと、これを元に新しい Subject を作ることはできますが、Discussion を始める・本文を直すなどの書き込みはできません。</div>`);
  // 巻き戻し（E-2）。会話中・確認待ちが無いときだけ出す
  const rw=WZ.rewindable||[];
  if(!list.length&&(rw.length||WZ.undo_rewind))$('#disc').insertAdjacentHTML('beforeend',`<details class="wzbox wzrewind"><summary><b>直前の状態に戻す</b> <span class="muted">（直近10個・30日以内の Discussion）</span></summary>
    ${WZ.undo_rewind?`<div style="margin:6px 0"><button class="wzprimary" data-wzundo="1">${esc(WZ.undo_rewind)} の直後に戻したのを取り消す</button> <span class="muted">次の Discussion を始めるまでできます</span></div>`:''}
    <table class="wztable"><tbody>${rw.map((x,i)=>`<tr><td>${esc(x.id)}</td><td class="muted">${esc(String(x.at).slice(0,16).replace('T',' '))} に閉じた</td>
    <td>${i===0?'<span class="muted">いまの状態</span>':`<button class="wzsmall" data-wzrewind="${esc(x.id)}" data-later="${esc(rw.slice(0,i).map(y=>y.id).reverse().join('・'))}">この直後に戻る</button>`}</td></tr>`).join('')}</tbody></table></details>`);
};
const origin=d=>d.origin.startsWith('migrate:')?'移行（'+d.origin.slice(8)+' から、AI の一人 Discussion）':d.origin.startsWith('continues:')?d.origin.slice(10)+' の続き':d.origin==='list'?'一覧から':d.origin.startsWith('facet:')?'Facet「'+facetLabel(d.origin.slice(6))+'」から':d.origin.startsWith('edit:')?'修正から（'+d.origin.slice(5)+'）':d.origin;

// ---- Facet のパネル（入口2）と、本文を直すボタン（C-6） ----
const _renderPanel=renderPanel;
renderPanel=function(){_renderPanel();if(!sel||view!=='F')return;
  $('#panel').insertAdjacentHTML('afterbegin',`<button class="wzprimary" data-wzfacet="${sel}">この Facet で Discussion を始める</button>`);
  const x=l1(sel);if(x)$('#panel .sec h3')?.insertAdjacentHTML('beforeend',` <button class="wzsmall" data-wzedit="${x.path}">本文を直す</button>`);
  document.querySelectorAll('#panel .item').forEach(it=>{const f=F[it.dataset.p];if(f&&f.fm.layer==='L2')it.querySelector('.t').insertAdjacentHTML('beforeend',` <button class="wzsmall" data-wzedit="${f.path}">本文を直す</button>`)});
};

// ---- チャットと要約（C-7・C-9） ----
function renderChat(){
  const d=D();if(!d){$('#chat').innerHTML='';return}
  // 渡した資料（C-14）は、チャットの中に1行で出す。実物は新しいタブで開き、会話中なら消せる
  const src=c=>(d.sources||[]).find(x=>x.card===c);
  const fileLine=m=>{const x=src(m.card);if(m.page)return `<div class="msg file">🔗 エージェントが読んだ: ${m.removed?`<s>${esc(m.text)}</s>（消した）`:esc(m.text)}${x&&x.url?` <a class="muted" href="${esc(x.url)}" target="_blank" rel="noopener">元のページ</a>`:''}（原典カードに残しました）${x&&d.state==='open'?` <button class="wzsmall" data-rmsrc="${esc(m.card)}">消す</button>`:''}</div>`;
    if(m.search){const q=(d.searches||[]).find(y=>y.id===m.search)||{};
      return `<div class="msg file">🔎 エージェントが ${esc(m.text)} <span class="muted">${q.card?'（原典カードに残しました）':'（調べた記録。原典に加えるかは要約のときに決めます）'}</span></div>`}
    if(m.web)return `<div class="msg file">🔎 エージェントが ${m.removed?`<s>${esc(m.text)}</s>（消した）`:esc(m.text)}（原典カードに残しました）${x&&d.state==='open'?` <button class="wzsmall" data-rmsrc="${esc(m.card)}">消す</button>`:''}</div>`;
    return `<div class="msg file">📎 ${m.removed?`<s>${esc(m.text)}</s> <span class="muted">（消した）</span>`
    :`${x?`<a href="${assetUrl(x.file)}" target="_blank" rel="noopener">${esc(m.text)}</a>`:esc(m.text)}${x&&x.url?` <a class="muted" href="${esc(x.url)}" target="_blank" rel="noopener">元のファイル</a>`:''}${x&&x.seen?' <span class="muted">（エージェントが読んだ）</span>':''}${x&&d.state==='open'?` <button class="wzsmall" data-rmsrc="${esc(m.card)}">消す</button>`:''}`}</div>`};
  // 最後の自分の発言には「取り消す」（返事ごと消して、本文を入力欄に戻す）。取り消せるかはサーバーが決める（can_undo）
  const lastU=d.can_undo?d.messages.map(m=>m.role).lastIndexOf('user'):-1;
  const msgs=d.messages.map((m,i)=>m.role==='file'?fileLine(m):m.role==='edit'?`<div class="msg file">✎ あなたが ${esc(m.text)}</div>`:`<div class="msg ${m.role}"><div class="who">${m.role==='user'?'あなた':WZ.real?'エージェント':'エージェント（Claude が演じています）'}${i===lastU?' <button class="wzsmall" id="wzundo" title="この発言と、それへの返事を消して、文章を入力欄に戻します">取り消す</button>':''}</div>${m.role==='agent'?md(m.text):`<div>${esc(m.text).replace(/\n/g,'<br>')}</div>`}</div>`).join('')
    +UPLOADING.filter(u=>u.d===cur).map(u=>`<div class="msg agent wait"><lattice-gem></lattice-gem><span>📎 ${esc(u.name)} を渡しています… <span class="elapsed" data-since="${u.at}"></span></span></div>`).join('')
    +(d.waiting&&d.waiting!=='drafts'?`<div class="msg agent wait"><lattice-gem></lattice-gem><span>${esc(d.progress||WAIT[d.waiting]||'保存しています…')}${since(d)}</span></div>${d.waiting==='summary'?'':feed(d)}`:'');
  const locked=d.state!=='open';
  const cands=d.candidates.map(c=>`<div class="cand"><div><b>${esc(facetLabel(c.facet))}</b> <span class="muted">${esc(c.reason||'')}</span>${c.suggest==null&&!locked?` <button class="wzsmall" data-uncand="${c.facet}">候補から外す</button>`:''}</div>
    <div class="opts">${Object.entries(CH).map(([k,v])=>`<label><input type="radio" name="c-${c.facet}" data-ch="${c.facet}" value="${k}" ${c.choice===k?'checked':''} ${locked?'disabled':''}> ${v}</label>`).join('')}</div></div>`).join('');
  const addable=facetIds().filter(i=>!d.candidates.some(c=>c.facet===i));
  // 描き直しても、入力欄の書きかけ（と、カーソルの位置）は残す。待ちの表示は1〜2秒ごとに変わるので、残さないと打った文字が消える
  const a0=$('#wzin'),keep=a0&&a0.dataset.d===String(cur)?{v:a0.value,f:document.activeElement===a0,s:a0.selectionStart,e:a0.selectionEnd}:null;
  $('#chat').innerHTML=`<div class="col left"><div class="bar"><button id="wzback">← 一覧へ</button> <b>${esc(d.id)}</b> ${esc(d.title||'')} <span class="wzst ${d.state}">${STATE[d.state]}</span>
      <span class="muted">${esc(origin(d))}</span><span style="flex:1"></span>${d.state==='review'?'<button class="wzprimary" id="wzreopen">確認の画面を開く</button>':''}
      ${d.state==='open'?'<button id="wzdiscard">廃棄する</button>':''}${d.state==='closed'?`<button class="wzprimary" data-wzcont="${esc(d.id)}">引き継いで新しい Discussion を始める</button>`:''}</div>
    <div class="msgs" id="wzmsgs">${msgs||'<div class="muted">話しかけて、議論を始めてください。</div>'}</div>
    ${d.state==='open'?`<div class="input"><textarea id="wzin" data-d="${esc(String(cur))}" rows="3" placeholder="メッセージ（Ctrl+Enter で送る）"></textarea><div class="btns"><button id="wzsend" class="wzprimary">送る</button><button id="wzsum">要約を作る</button>
      ${WZ.real?`<span style="flex:1"></span>${String(cur).startsWith('new:')?'<span class="muted">資料は、最初の発言のあとに渡せます</span>'
        :'<button id="wzpc">📎 PC から</button><button id="wzdrive">📎 ドライブから</button><input type="file" id="wzfile" multiple hidden accept=".xlsx,.csv,.pdf,.pptx,.docx,.jpg,.jpeg,.png,.heic,.txt,.md">'}`:''}</div></div>`:''}</div>
  <div class="col right"><h3>要約</h3>${d.summary?md(d.summary):`<div class="muted">${d.waiting==='summary'?'<lattice-gem></lattice-gem>'+esc(d.progress||WAIT.summary)+since(d)+feed(d):'まだありません。ある程度話がまとまったら「要約を作る」を押してください。'}</div>`}
    ${d.summary&&(d.searches||[]).length?`<h3>原典に加える Web 検索</h3><div class="muted">チェックしたものを「Facet 更新を確認する」で原典カードにします。最初のチェックはエージェントの案（要約の根拠に使ったもの）です。</div>
      ${d.searches.map(q=>`<label class="keep"><input type="checkbox" data-keep="${esc(q.id)}" ${q.keep||q.card?'checked':''} ${q.card||locked?'disabled':''}> ${esc(q.query)} <span class="muted">（出典 ${(q.links||[]).length} 件${q.card?'・原典カードにした':''}）</span></label>`).join('')}`:''}
    ${d.summary?`<h3>関係しそうな Facet</h3>${cands||'<div class="muted">候補はまだありません。</div>'}
      ${d.state==='open'?`<div class="add">候補に足す: <select id="wzadd"><option value="">Facet を選ぶ</option>${addable.map(i=>`<option value="${i}">${esc(facetLabel(i))}</option>`).join('')}</select></div>
      <button class="wzprimary big" id="wzconfirm">Facet 更新を確認する</button>`:''}`:''}</div>`;
  const a1=$('#wzin');if(a1&&keep){a1.value=keep.v;if(keep.f){a1.focus();a1.setSelectionRange(keep.s,keep.e)}}
  const m=$('#wzmsgs');if(m)m.scrollTop=m.scrollHeight;
}

// ---- 確認のモーダル（C-12）: Facet ごとのタブで、旧と新を並べて承認する ----
function renderModal(force){
  const d=D(),box=$('#wzmodal');
  if(!modalOpen||!d||d.state!=='review'&&!(d.drafts.length&&d.state==='closed')){box.style.display='none';return}
  if(!force&&box.contains(document.activeElement)&&document.activeElement.tagName==='TEXTAREA')return; // 入力中は描き直さない
  box.style.display='flex';
  if(d.waiting==='drafts'||!d.drafts.length){box.innerHTML=`<div class="dlg"><div class="dhead"><b>${esc(d.id)} の Facet の更新</b><button id="wzclose">×</button></div><div class="dbody muted"><lattice-gem></lattice-gem>${esc(d.progress||WAIT.drafts)}${since(d)}${feed(d)}</div></div>`;return}
  tab=Math.min(tab,d.drafts.length-1);const x=d.drafts[tab];
  // タブの印: ✓ / − は確定したもの、（承認予定）（取り下げ予定）はまだ確定していない予定
  const MARK={approve:'（承認予定）',withdraw:'（取り下げ予定）'};
  const tabs=d.drafts.map((y,i)=>`<button class="tab ${i===tab?'on':''} ${y.status} ${y.mark?'marked':''}" data-tab="${i}">${esc(facetLabel(y.facet))} ${y.status==='approved'?'✓':y.status==='withdrawn'?'−':MARK[y.mark]||''}</button>`).join('');
  const files=x.files.map(f=>`<div class="pair"><div class="ph">${esc(f.layer)} <span class="muted">${esc(f.path)}</span>${f.edited_by_user?' <span class="badge">あなたが修正</span>':''}</div>
    <div class="cols"><div class="o"><div class="lab">旧（いまの内容）</div>${oldOf(f)==null?'<div class="muted">（新しいファイル）</div>':marked2(oldOf(f),f.new,'del')}</div>
    <div class="o new"><div class="lab">新（表示。変わった行に色）</div><div data-preview="${esc(f.path)}" data-old-of="${esc(f.path)}">${oldOf(f)==null?md(f.new):marked2(f.new,oldOf(f),'mark')}</div></div>
    <div class="n"><div class="lab">新（直せます。左の表示にすぐ反映）</div><textarea data-draft="${esc(f.path)}" ${x.status!=='pending'?'disabled':''}>${esc(f.new)}</textarea></div></div></div>`).join('');
  const pend=x.status==='pending',open=d.drafts.filter(y=>y.status==='pending'),ready=open.length&&open.every(y=>y.mark);
  const nA=open.filter(y=>y.mark==='approve').length,nW=open.filter(y=>y.mark==='withdraw').length;
  // 各タブでは「予定」を選ぶだけ。最後の「確定する」で初めて stable になる（ウォークスルーの指摘: 続けて押して見ないまま承認できた）
  box.innerHTML=`<div class="dlg"><div class="dhead"><b>${esc(d.id)} の Facet の更新を確認</b><span class="muted">タブを順に見て、Facet ごとに承認か取り下げの予定を選び、最後に「確定する」を押してください。確定するまでは何度でも選び直せます。</span><button id="wzclose" title="閉じても確認待ちのまま残ります">×</button></div>
    <div class="tabs">${tabs}</div><div class="dbody">${files}</div>
    <div class="dfoot">${pend?`<button class="${x.mark==='approve'?'wzprimary':''}" data-act="approve">${x.mark==='approve'?'✓ ':''}この Facet を承認する予定にする</button><button class="${x.mark==='withdraw'?'wzprimary':''}" data-act="withdraw">${x.mark==='withdraw'?'✓ ':''}この Facet を取り下げる予定にする</button>`:`<span class="muted">${x.status==='approved'?'承認しました':'取り下げました'}</span>`}
      <span style="flex:1"></span>${d.state!=='review'?'<button id="wzclose2">閉じる</button>':`<button id="wzbackdisc">Discussion に戻る</button><button id="wzdiscard2">廃棄する</button>
        <button class="wzprimary" id="wzfinal" ${ready?'':'disabled'} title="${ready?'':'すべての Facet で予定を選ぶと押せます'}">確定する（承認 ${nA}・取り下げ ${nW}）</button>`}</div></div>`;
}

// ---- 本文を直す（C-6、会話の外） ----
function openEdit(p){
  const f=F[p];$('#wzedit').style.display='flex';
  $('#wzedit').innerHTML=`<div class="dlg small"><div class="dhead"><b>本文を直す</b><span class="muted">${esc(p)}</span><button id="wzeclose">×</button></div>
    <div class="dbody"><div class="muted">荷札（frontmatter）はアプリが管理します。保存すると、AI が「体裁だけか、意味が変わったか」を判断し、意味が変わったときは Discussion を始めます。</div>
    ${openTo()}<textarea id="wzetext">${esc(f.body)}</textarea></div><div class="dfoot"><span style="flex:1"></span><button id="wzeclose2">やめる</button><button class="wzprimary" id="wzesave" data-p="${esc(p)}">保存する</button></div></div>`;
}

// 会話中の Discussion があれば、修正をそこに含めるかを選べる（C-6）。最後に開いていた会話中の Discussion を最初に選んでおく
let lastChat=null;
function openTo(){const list=WZ.real?WZ.discussions.filter(d=>d.state==='open'):[];if(!list.length)return '';
  const def=list.some(d=>d.id===lastChat)?lastChat:'';
  return `<label class="muted">この修正を: <select id="wzeinto"><option value="">会話の外の修正として扱う（必要なら新しい Discussion を始める）</option>
    ${list.map(d=>`<option value="${esc(d.id)}" ${d.id===def?'selected':''}>${esc(d.id)} ${esc(d.title||'')} の会話に含める</option>`).join('')}</select></label>`}
function openDiscussion(id){cur=id;if(!String(id).startsWith('new:'))lastChat=id;view='C';setView();renderChat();const d=D();if(d&&d.state==='review'){modalOpen=true;renderModal(true)}}
// 完了した Discussion を、本番と同じように Subject の中に書かれたものとして画面に足す（ファイルは書き換えない）。
// 要約は Discussion の表（○・△）に、承認した L1・L2 は stable として Facet のボードとパネルに出る
const ORIG={};
function applyClosed(){
  for(const p in ORIG){if(ORIG[p])F[p]=ORIG[p];else delete F[p];delete ORIG[p]}
  const put=(p,f)=>{if(!(p in ORIG))ORIG[p]=F[p]||null;F[p]=f};
  for(const d of WZ.discussions.filter(d=>d.state==='closed'&&d.summary)){
    const date=(d.started||'').slice(0,10),dir=`/discussions/${date}-d${d.id.slice(2)}/`,sp=dir+'discussion_summary.md';
    put(sp,{path:sp,body:d.summary,srcs:[],fm:{type:'Discussion Summary',layer:'discussion',discussion_id:d.id,date,title:`${d.id} ${d.title||''}`}});
    for(const x of d.drafts.filter(x=>x.status==='approved'))for(const f of x.files){
      const was=F[f.path]&&!F[f.path].missing?F[f.path]:null;
      const srcs=[...new Set([...(was?.srcs||[]),...(f.layer==='L2'?[sp]:x.files.filter(y=>y.layer==='L2').map(y=>y.path))])];
      put(f.path,{path:f.path,body:f.new,srcs,fm:{...(was?.fm||{}),type:f.layer==='L1'?'Decision':'Decision Context',layer:f.layer,facet:[x.facet],
        title:was?.fm.title||(f.new.match(/^# (.+)$/m)||[])[1]||f.path,status:'stable',sources:srcs}});
    }
  }
  for(const e of [...WZ.edits].reverse().filter(e=>e.judgement==='minor')){const was=F[e.path];if(was&&!was.missing)put(e.path,{...was,body:e.text})}
  for(const p in F)F[p].rev=[];
  for(const p in F)for(const q of F[p].srcs)if(F[q])F[q].rev.push(p);
}
// いま開いているタブで直した本文を送る。ブラウザは入力欄の先頭の改行と \r を落とすので、それをそろえてから比べる
function saveEdits(){const f=D().drafts[tab],norm=v=>String(v).replace(/\r\n/g,'\n').trim();
  return Promise.all([...document.querySelectorAll('#wzmodal textarea[data-draft]')].filter(a=>norm(a.value)!==norm(f.files.find(y=>y.path===a.dataset.draft).new))
    .map(a=>ev('edit_draft',{facet:f.facet,path:a.dataset.draft,text:a.value})))}
let waitEdit=null;
function renderEditResult(){
  const e=WZ.edits.find(x=>x.id===waitEdit);if(!e)return;
  const box=$('#wzedit .dbody');if(!box)return;
  box.innerHTML=e.waiting?`<p><b>保存しました。</b></p><p class="muted"><lattice-gem></lattice-gem>${WAIT.judge}</p>`
    :e.judgement==='minor'?`<p><b>保存しました。体裁だけの修正と判断しました。</b></p><p>${esc(e.reason||'')}</p><p class="muted">Discussion は始めず、log.md に1行残します。</p>`
    :`<p><b>保存しました。意味が変わる修正と判断しました。</b></p><p>${esc(e.reason||'')}</p><p class="muted">${e.into===e.discussion?`Discussion ${esc(e.discussion||'')} の会話に含めました`:`Discussion ${esc(e.discussion||'')} を始めました`}。ほかの Facet との食い違いを、そこで確かめます。</p>`;
  $('#wzedit .dfoot').innerHTML=e.waiting?'':`<span style="flex:1"></span><button class="wzprimary" id="wzedone" data-e="${e.id}">${e.judgement==='minor'?'閉じる':'Discussion を開く'}</button>`;
}
// 索引（全ファイル）を読み直すのは、ファイルが変わりうるときだけ（Discussion の状態・draft・人の修正・巻き戻し）。
// 待ちの表示（考えごと・途中経過）が変わるたびに読み直すと、AI の作業中に重い読み込みが積み重なっていた（M4）。
// 読み直している間に次が来ても、重ねて頼まず、終わってから1回だけ読み直す。画面はまず今の索引ですぐ描く
let BKEY=null,BBUSY=null,BAGAIN=false;
const bundleKey=()=>JSON.stringify([WZ.discussions.map(d=>[d.id,d.state,d.waiting==='finishing'?1:0,(d.drafts||[]).map(x=>[x.status,x.files.map(f=>f.path+(f.edited_by_user?'*':''))]),d.summary?d.summary.length:0]),
  WZ.edits.map(e=>[e.id,e.judgement]),WZ.undo_rewind,(WZ.rewindable||[]).map(x=>x.id)]);
function reloadBundle(){if(BBUSY){BAGAIN=true;return BBUSY}
  BBUSY=loadBundle().then(after).finally(()=>{BBUSY=null;if(BAGAIN){BAGAIN=false;reloadBundle()}});return BBUSY}
function rerender(){if(typeof facetsDef==='undefined'||!facetsDef)return setTimeout(rerender,300);
  if(WZ.real){after();const k=bundleKey();if(k!==BKEY){BKEY=k;reloadBundle()}return}applyClosed();render();after()}
function after(){document.body.classList.toggle('ro',!!WZ.readonly||WZ.migration?.status==='running');renderEditResult();if(view==='D')renderDisc();if(view==='C')renderChat();renderModal()}
// 時間のかかる操作のあいだ、画面全体に経過を出す。終わったら索引を読み直すため再読み込み
function busy(text,p){document.body.insertAdjacentHTML('beforeend',`<div id="wzbusy"><div><lattice-gem></lattice-gem>${esc(text)} <span class="elapsed" data-since="${Date.now()/1000}"></span><div class="muted">ファイルの数によっては数分かかります。このままお待ちください。</div></div></div>`);
  return p.then(r=>{if(r&&r.ok)location.reload();else $('#wzbusy')?.remove()},()=>$('#wzbusy')?.remove())}
function poll(){return fetch(`/api/wiz/state?s=${encodeURIComponent(SUBJ)}`).then(r=>r.json()).then(s=>{if(s.rev===WZ.rev)return;WZ=s;rerender()}).catch(()=>{})}  // サーバーの再起動中などは、次の読み直しを待つ
setInterval(poll,1500);poll();

document.addEventListener('click',e=>{
  const t=e.target;let x;
  if(t.id==='wznew')return ev('start',{origin:'list'}).then(r=>openDiscussion(r.discussion));
  if(x=t.closest('[data-wzfacet]')){const id=x.dataset.wzfacet;return ev('start',{facet:id}).then(r=>openDiscussion(r.discussion))}
  if(x=t.closest('[data-wzcont]')){e.stopPropagation();return ev('start',{continues:x.dataset.wzcont}).then(r=>openDiscussion(r.discussion))}
  if(x=t.closest('[data-wzrewind]')){const id=x.dataset.wzrewind;
    return ui.confirm(`${x.dataset.later} で変わった Facet の本文は元に戻り、その Discussion のフォルダはゴミ箱に移ります。\n（次の Discussion を始めるまでは取り消せます）`,{title:`${id} を閉じた直後に戻しますか？`,ok:'戻す',danger:true})
      .then(ok=>ok&&busy(`${id} の直後に戻しています…`,ev('rewind',{target:id})))}
  if(t.closest('[data-wzresume]')){return ev('resume_migration').then(poll)}
  if(t.closest('[data-wzundo]'))return ui.confirm('戻したのを取り消して、戻す前の状態にします。',{title:'戻したのを取り消しますか？',ok:'取り消す'}).then(ok=>ok&&busy('戻す前の状態にしています…',ev('undo_rewind')));
  if(x=t.closest('[data-wzedit]')){e.stopPropagation();return openEdit(x.dataset.wzedit)}
  if(x=t.closest('tr.wzrow'))return openDiscussion(x.dataset.wz);
  if(t.id==='wzback'){view='D';setView();return renderDisc()}
  if(t.id==='wzundo')return ui.confirm('最後の発言と、それへのエージェントの返事を消します。発言の文章は入力欄に戻ります。',{title:'発言を取り消しますか？',ok:'取り消す'}).then(ok=>{if(!ok)return;
    t.disabled=true;return ev('undo_message').then(r=>{if(r&&r.text!=null){const a=$('#wzin');if(a){a.value=r.text+(a.value?'\n'+a.value:'');a.focus()}}})});
  if(t.id==='wzsend'||t.id==='wzsum'){const v=$('#wzin').value.trim();
    // まだ作られていない Discussion は、最初の発言で作られて番号が付く（C-7）。返ってきた番号に切り替える
    const p=v?ev('message',{text:v}).then(r=>{if(r&&r.discussion&&!String(r.discussion).startsWith('new:'))cur=r.discussion;renderChat()}):Promise.resolve();
    $('#wzin').value='';return t.id==='wzsum'?p.then(()=>ev('summarize')):p}
  if(t.id==='wzpc')return $('#wzfile').click();
  if(t.id==='wzdrive')return ui.prompt('スプレッドシート・ドキュメント・スライド・pdf などの URL を貼ってください。',{title:'ドライブから資料を渡す',ok:'渡す',
    validate:v=>!v?'URL を貼ってください':/^https:\/\/(drive|docs)\.google\.com\//.test(v)?'':'ドライブ（drive.google.com・docs.google.com）の URL を貼ってください'}).then(u=>{if(u)ev('drive',{url:u})});
  if(x=t.closest('[data-rmsrc]')){const c=x.dataset.rmsrc;return ui.confirm('ファイルと原典カードが消えます。',{title:'この資料を Discussion から消しますか？',ok:'消す',danger:true}).then(ok=>{if(ok)ev('remove_source',{card:c})})}
  if(x=t.closest('[data-uncand]'))return ev('uncandidate',{facet:x.dataset.uncand});
  if(t.id==='wzconfirm'){if(t.disabled)return;t.disabled=true;t.textContent='確認の画面を準備しています…';modalOpen=true;tab=0;return ev('confirm')}  // 2回押しで「要約を作ってから」と断られないよう、押したら止める
  if(t.id==='wzreopen'){modalOpen=true;return renderModal(true)}
  if(t.id==='wzdiscard'||t.id==='wzdiscard2')return ui.confirm('draft は取り下げ、Discussion ごと消えます。番号は使い回しません。',{title:'この Discussion を廃棄しますか？',ok:'廃棄する',danger:true})
    .then(ok=>ok&&ev('discard').then(()=>{modalOpen=false;view='D';setView();renderDisc()}));
  if(t.id==='wzclose'||t.id==='wzclose2'){modalOpen=false;return renderModal()}
  if(t.id==='wzbackdisc'){modalOpen=false;return ev('back')}
  // タブを移るときも、直した本文を失わないように先に送る
  if(x=t.closest('[data-tab]')){return saveEdits().then(()=>{tab=+x.dataset.tab;renderModal(true)})}
  // 予定を選んでも、次のタブへは自動で移らない（見ないまま次を選べないように）
  if(x=t.closest('[data-act]')){const f=D().drafts[tab];
    return saveEdits().then(()=>ev(x.dataset.act,{facet:f.facet})).then(()=>renderModal(true))}
  if(t.id==='wzfinal'){const d=D(),open=d.drafts.filter(y=>y.status==='pending');
    const list=open.map(y=>`・${facetLabel(y.facet)}: ${y.mark==='approve'?'承認（stable にする）':'取り下げ（材料として残す）'}`).join('\n');
    return ui.confirm(`確定すると元に戻せません。\n\n${list}`,{title:'次の内容で確定しますか？',ok:'確定する',careful:true}).then(ok=>{if(!ok)return;
    t.disabled=true;t.textContent='ドライブに保存しています…';
    return saveEdits().then(()=>ev('finalize')).then(()=>renderModal(true))})}
  if(t.id==='wzeclose'||t.id==='wzeclose2'){$('#wzedit').style.display='none';return}
  // 保存しても画面は移さず、編集の画面の中で AI の判断が出るまで見せる（ウォークスルーの指摘: 一覧に飛んで混乱した）
  if(t.id==='wzesave'){const p=t.dataset.p,n=WZ.edits.length;cur=null;t.disabled=true;
    ev('edit_file',{path:p,text:$('#wzetext').value,into:$('#wzeinto')?.value||null}).then(()=>{waitEdit=WZ.edits.length>n?WZ.edits[0].id:null;renderEditResult()});return}
  if(t.id==='wzedone'){$('#wzedit').style.display='none';waitEdit=null;
    const e=WZ.edits.find(x=>x.discussion&&x.id===t.dataset.e);if(e)return openDiscussion(e.discussion);return}
},true);
// PC からの資料は1つずつ送る（multipart にせず本文のまま、サーバーで30MB と形式を確かめる）
const UPLOADING=[];  // 送っている途中の資料（届くまで10秒ほどかかるので、チャットに出しておく）
async function upload(files){const mine=files.map(f=>({d:cur,name:f.name,at:Date.now()/1000}));UPLOADING.push(...mine);renderChat();
  for(const [i,f] of files.entries()){
  const r=await fetch(`/api/wiz/upload?s=${encodeURIComponent(SUBJ)}&d=${encodeURIComponent(cur)}&name=${encodeURIComponent(f.name)}`,{method:'POST',body:f});
  UPLOADING.splice(UPLOADING.indexOf(mine[i]),1);
  if(!r.ok)ui.alert((await r.json().catch(()=>({}))).detail||'渡せませんでした',{title:`${f.name} を渡せませんでした`});
  await poll();renderChat()}}
// チャットに資料を落としても渡せる（会話中で、番号の付いた Discussion のとき）
document.addEventListener('dragover',e=>{if(e.target.closest&&e.target.closest('#chat')&&$('#wzpc'))e.preventDefault()});
document.addEventListener('drop',e=>{if(e.target.closest&&e.target.closest('#chat')&&$('#wzpc')){e.preventDefault();upload([...e.dataTransfer.files])}});
document.addEventListener('change',e=>{
  if(e.target.id==='wzfile'){const fs=[...e.target.files];e.target.value='';return upload(fs)}
  if(e.target.dataset.keep)ev('keep_search',{search:e.target.dataset.keep,keep:e.target.checked});
  if(e.target.dataset.ch)ev('choice',{facet:e.target.dataset.ch,choice:e.target.value});
  if(e.target.id==='wzadd'&&e.target.value)ev('choice',{facet:e.target.value,choice:'update'});
});
// 3列の比較（旧の表示・新の表示・新の編集）: 編集すると新の表示をすぐ描き直す（ウォークスルーの指摘）
document.addEventListener('input',e=>{const p=e.target.dataset.draft;if(p){const v=document.querySelector(`#wzmodal [data-preview="${CSS.escape(p)}"]`);if(v)v.innerHTML=(p in OLD?OLD[p]:old(p))==null?md(e.target.value):marked2(e.target.value,(p in OLD?OLD[p]:old(p)),'mark')}});
document.addEventListener('keydown',e=>{if(e.target.id==='wzin'&&e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('#wzsend').click()}});
})());
