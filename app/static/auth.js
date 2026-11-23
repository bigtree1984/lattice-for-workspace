// ログインが切れかけたら（API が 401 を返したら）、Google のログインへ回して、いまのページに戻ってくる（REQUIREMENTS C-4）。
(() => {
  const orig = window.fetch.bind(window);
  let going = false;
  window.fetch = async (...a) => {
    const r = await orig(...a);
    if (r.status === 401 && !going) {
      going = true;
      location.href = '/auth/login?next=' + encodeURIComponent(location.pathname + location.search);
    }
    return r;
  };
})();
