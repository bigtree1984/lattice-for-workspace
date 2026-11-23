// 待ち時間の宝石（<lattice-gem>）。ブリリアントカット風の宝石が、手前に30度傾けた縦の軸のまわりを回る。
// 面ごとに光の当たり方で色を変え、サファイアからエメラルドへ移るように塗る（tokens.css の --gem と同じ色）。
// 画面が数秒ごとに描き直されても回転が飛ばないよう、角度はページを開いてからの時間で決める。
(() => {
  const N = 8, TILT = 30 * Math.PI / 180, PERIOD = 2400;  // 8角形、傾き30度、1周 2.4 秒
  const SAPPHIRE = [22, 53, 127], BLUE = [31, 79, 184], EMERALD = [19, 137, 106];
  const pts = [], faces = [];
  const ring = (y, r, off) => { const s = pts.length; for (let i = 0; i < N; i++) { const a = (i + off) / N * 2 * Math.PI; pts.push([r * Math.cos(a), y, r * Math.sin(a)]); } return s; };
  const table = ring(0.42, 0.52, 0.5), girdle = ring(0.12, 1.0, 0);
  pts.push([0, -1.05, 0]); const culet = pts.length - 1;
  faces.push([...Array(N).keys()].map(i => table + i).reverse());  // 上の平らな面
  for (let i = 0; i < N; i++) {
    const j = (i + 1) % N;
    faces.push([table + i, girdle + j, girdle + i]);               // 上側の三角
    faces.push([table + i, table + j, girdle + j]);                // 上側の三角（もう一方）
    faces.push([girdle + i, girdle + j, culet]);                   // 下側（パビリオン）
  }
  const mix = (a, b, t) => a.map((v, k) => Math.round(v + (b[k] - v) * t));
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');

  function draw(cv, t) {
    const dpr = devicePixelRatio || 1, px = cv.clientWidth || 18;
    if (cv.width !== px * dpr) { cv.width = cv.height = px * dpr; }
    const g = cv.getContext('2d'), h = cv.width / 2, sc = h * 0.82;
    g.clearRect(0, 0, cv.width, cv.height);
    const th = reduce.matches ? 0.6 : (t / PERIOD) * 2 * Math.PI;
    const cy = Math.cos(th), sy = Math.sin(th), ct = Math.cos(TILT), st = Math.sin(TILT);
    const P = pts.map(([x, y, z]) => {             // 縦の軸で回し、手前に傾ける
      const x1 = x * cy + z * sy, z1 = -x * sy + z * cy;
      return [x1, y * ct - z1 * st, y * st + z1 * ct];
    });
    const out = [];
    for (const f of faces) {
      const [a, b, c] = f.map(i => P[i]);
      const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
      let n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
      const len = Math.hypot(...n) || 1; n = n.map(q => q / len);
      if (n[2] <= 0) continue;                      // 向こう向きの面は描かない
      const z = f.reduce((s, i) => s + P[i][2], 0) / f.length;
      const light = Math.max(0, -0.45 * n[0] + 0.6 * n[1] + 0.66 * n[2]);  // 左上手前からの光
      const hue = Math.min(1, Math.max(0, (n[0] + n[2] * 0.3 + 0.6) / 1.6));                   // 面の向きでサファイア → エメラルド
      let col = hue < 0.5 ? mix(SAPPHIRE, BLUE, hue * 2) : mix(BLUE, EMERALD, (hue - 0.5) * 2);
      col = mix(col, [255, 255, 255], Math.pow(light, 10) * 0.6);  // きらめき（強い光の面だけ）
      col = mix([8, 20, 45], col, 0.45 + 0.55 * light);
      out.push({ f, z, col });
    }
    out.sort((p, q) => p.z - q.z);
    for (const { f, col } of out) {
      g.beginPath();
      f.forEach((i, k) => { const x = h + P[i][0] * sc, y = h - P[i][1] * sc; k ? g.lineTo(x, y) : g.moveTo(x, y); });
      g.closePath();
      g.fillStyle = `rgb(${col})`; g.fill();
      g.strokeStyle = 'rgba(255,255,255,0.28)'; g.lineWidth = Math.max(0.5, dpr * 0.4); g.stroke();
    }
  }

  class LatticeGem extends HTMLElement {
    connectedCallback() {
      if (!this.cv) {
        this.cv = document.createElement('canvas');
        this.cv.setAttribute('aria-hidden', 'true');
        this.appendChild(this.cv);
        this.setAttribute('role', 'img');
        this.setAttribute('aria-label', '処理中');
      }
      const loop = t => { draw(this.cv, t); if (!reduce.matches) this.raf = requestAnimationFrame(loop); };
      this.raf = requestAnimationFrame(loop);
    }
    disconnectedCallback() { cancelAnimationFrame(this.raf); }
  }
  if (!customElements.get('lattice-gem')) customElements.define('lattice-gem', LatticeGem);
})();
