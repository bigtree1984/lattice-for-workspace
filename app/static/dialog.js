// 画面の中に出す確認・知らせ・入力のウィンドウ（ブラウザの confirm・alert・prompt の代わり）。
// どれも Promise を返す: ui.confirm → true/false、ui.alert → 閉じたら終わり、ui.prompt → 入れた文字列か null。
// Esc でやめる、Enter で決める。取り消せない操作は danger: true で決めるボタンを赤くする。
// careful: true は、赤くはしないが最初に「キャンセル」を選んでおく（確定のように、ふつうの操作だが元に戻せないもの）。
(() => {
  const css = `
.uidlg-back{position:fixed;inset:0;background:rgba(15,30,51,.38);display:flex;align-items:center;justify-content:center;z-index:9999;padding:16px}
.uidlg{background:var(--color-base,#fff);color:var(--color-ink,#0F1E33);width:min(520px,100%);max-height:calc(100vh - 32px);display:flex;flex-direction:column;
  border-top:3px solid transparent;border-image:var(--gem,linear-gradient(90deg,#1F4FB8,#0B7A5A)) 1;box-shadow:0 12px 40px rgba(15,30,51,.25);font:14px/1.7 sans-serif}
.uidlg h3{margin:0;padding:14px 18px 4px;font-size:15px}
.uidlg .uibody{padding:6px 18px 4px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.uidlg input{display:block;width:100%;box-sizing:border-box;margin:10px 0 2px;font:inherit;border:1px solid #b8c1cc;padding:6px 8px}
.uidlg input:focus{outline:2px solid var(--color-contrast-a,#1F4FB8);outline-offset:-1px}
.uidlg .uierr{color:#b3261e;font-size:12px;min-height:1.4em;white-space:normal}
.uidlg .uifoot{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px 16px}
.uidlg button{font:inherit;font-size:13px;border:1px solid var(--color-muted,#5A6577);background:#fff;color:inherit;padding:6px 16px;cursor:pointer}
.uidlg button.ok{background:var(--color-contrast-a,#1F4FB8);border-color:var(--color-contrast-a,#1F4FB8);color:#fff}
.uidlg button.ok.danger{background:#b3261e;border-color:#b3261e}
.uidlg button:focus-visible{outline:2px solid var(--color-contrast-a,#1F4FB8);outline-offset:2px}`;
  const style = document.createElement('style'); style.textContent = css; document.head.appendChild(style);

  let current = null;  // いま開いているウィンドウを「やめた」で閉じる関数

  function open({ title = '', message = '', ok = 'OK', cancel = 'キャンセル', danger = false, input = null, validate = null, careful = false }) {
    // 前のウィンドウが開いたままなら、やめた扱いで閉じてから開く（2つ重なって、下の古いほうを押してしまわないように。M4 の本番確認）
    if (current) current();
    return new Promise(resolve => {
      const back = document.createElement('div');
      back.className = 'uidlg-back';
      back.innerHTML = `<div class="uidlg" role="dialog" aria-modal="true">
        ${title ? '<h3></h3>' : ''}<div class="uibody"></div>
        ${input !== null ? '<div style="padding:0 18px"><input type="text"><div class="uierr"></div></div>' : ''}
        <div class="uifoot">${cancel ? '<button class="cancel"></button>' : ''}<button class="ok${danger ? ' danger' : ''}"></button></div></div>`;
      if (title) back.querySelector('h3').textContent = title;
      back.querySelector('.uibody').textContent = message;
      back.querySelector('.ok').textContent = ok;
      if (cancel) back.querySelector('.cancel').textContent = cancel;
      const inp = back.querySelector('input'), err = back.querySelector('.uierr');
      if (inp) inp.value = input;
      const before = document.activeElement;
      const done = v => { if (current === cancelMe) current = null; document.removeEventListener('keydown', key, true); back.remove(); before?.focus?.(); resolve(v); };
      const yes = () => {
        if (inp) { const v = inp.value.trim(), e = validate ? validate(v) : ''; if (e) { err.textContent = e; inp.focus(); return } return done(v) }
        done(true);
      };
      const no = () => done(inp ? null : cancel ? false : true);
      const cancelMe = () => no();
      current = cancelMe;
      const key = e => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); no() }
        // 入力欄では Enter で決める。ボタンでは、選んでいるボタンが押される（ブラウザのふつうの動き）
        else if (e.key === 'Enter' && !e.isComposing && inp && document.activeElement === inp) { e.preventDefault(); e.stopPropagation(); yes() }
      };
      document.addEventListener('keydown', key, true);
      back.querySelector('.ok').onclick = yes;
      if (cancel) back.querySelector('.cancel').onclick = no;
      back.addEventListener('mousedown', e => { if (e.target === back) no() });  // 外を押したらやめる
      if (inp) inp.addEventListener('input', () => { err.textContent = '' });
      document.body.appendChild(back);
      (inp || back.querySelector((danger || careful) && cancel ? '.cancel' : '.ok')).focus();  // 取り消せない操作は、うっかり Enter で決まらないよう「やめる」に置く
      if (inp) inp.select();
    });
  }

  window.ui = {
    confirm: (message, o = {}) => open({ message, ok: 'OK', ...o }),
    alert: (message, o = {}) => open({ message, ok: '閉じる', cancel: '', ...o }),
    prompt: (message, o = {}) => open({ message, ok: 'OK', input: o.value ?? '', ...o }),
  };
})();
