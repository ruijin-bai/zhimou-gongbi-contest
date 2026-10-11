// 二维码生成器自检（由 tests/test_quick.py 调用）：取出 x_quick.js 里的 QRGen，用独立写的解码流程校验。
// 校验点：规范里的 RS 纠错标准向量、格式/版本信息标准值、校正图形位置，再把生成的矩阵按规范反向解码（格式信息两份一致且合法 →
// 去掩码 → 蛇形读码字 → 去交织 → 纠错码字验证 → 还原文本）。
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "app", "static", "x_quick.js"), "utf8");
const cut = src.indexOf("window.QRGen = QRGen;");
const QRGen = new Function(src.slice(0, cut) + "; return QRGen;")();

let fails = 0;
const ok = (c, m) => { if (!c) { fails++; console.log("FAIL: " + m); } };

// ---- 独立的 GF(256) 与 RS（对数表实现）
const EXP = new Array(512), LOG = new Array(256);
for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
const mul = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);
function rsEnc(data, n) {  // 生成多项式 ∏(x - α^i)，长除法求余
  let g = [1];
  for (let i = 0; i < n; i++) { const ng = new Array(g.length + 1).fill(0); g.forEach((c, j) => { ng[j] ^= c; ng[j + 1] ^= mul(c, EXP[i]); }); g = ng; }
  const r = data.concat(new Array(n).fill(0));
  for (let i = 0; i < data.length; i++) { const f = r[i]; if (f) for (let j = 0; j < g.length; j++) r[i + j] ^= mul(g[j], f); }
  return r.slice(data.length);
}
const synd = (cw, n) => { const s = []; for (let i = 0; i < n; i++) { let v = 0; for (const c of cw) v = mul(v, EXP[i]) ^ c; s.push(v); } return s; };

// 规范/教程里的标准向量：01234567（1-M，10 个纠错码字）；HELLO WORLD（1-M）
const eq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
ok(eq(rsEnc([16, 32, 12, 86, 97, 128, 236, 17, 236, 17, 236, 17, 236, 17, 236, 17], 10), [165, 36, 212, 193, 237, 54, 199, 135, 44, 85]), "RS 标准向量 01234567");
ok(eq(rsEnc([32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17], 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]), "RS 标准向量 HELLO WORLD");

// 数据码字容量（L/M，版本 1~14）：与规范表一致
const capL = [19, 34, 55, 80, 108, 136, 156, 194, 232, 274, 324, 370, 428, 461], capM = [16, 28, 44, 64, 86, 108, 124, 154, 182, 216, 254, 290, 334, 365];
for (let v = 1; v <= 14; v++) { ok(QRGen.dataWords(v, "L") === capL[v - 1], `容量 L v${v}`); ok(QRGen.dataWords(v, "M") === capM[v - 1], `容量 M v${v}`); }

// ---- 反向解码
function bchFormat(d) { let r = d; for (let i = 0; i < 10; i++) r = (r << 1) ^ ((r >> 9) * 0x537); return ((d << 10) | r) ^ 0x5412; }
ok(bchFormat((1 << 3) | 0) === 0x77c4 && bchFormat((1 << 3) | 1) === 0x72f3 && bchFormat(0) === 0x5412, "格式信息标准值");

function align(v) { if (v === 1) return []; const n = Math.floor(v / 7) + 2, size = v * 4 + 17, step = Math.ceil((v * 4 + 4) / (n * 2 - 2)) * 2, a = [6]; for (let p = size - 7; a.length < n; p -= step) a.splice(1, 0, p); return a; }
ok(eq(align(2), [6, 18]) && eq(align(7), [6, 22, 38]) && eq(align(10), [6, 28, 50]) && eq(align(14), [6, 26, 46, 66]), "校正图形位置");

const MASK = [(r, c) => (r + c) % 2 === 0, (r, c) => r % 2 === 0, (r, c) => c % 3 === 0, (r, c) => (r + c) % 3 === 0, (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0, (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0, (r, c) => ((((r + c) % 2) + ((r * c) % 3)) % 2) === 0];
const EPB = { L: [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30], M: [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24] };
const NBK = { L: [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4], M: [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9] };

function decode(q, level) {
  const m = q.m, n = q.size, v = q.version, g = (r, c) => (m[r][c] ? 1 : 0);
  // 格式信息：按 zxing 的读法逐位取（先读到的是高位）。get(x, y) 即第 y 行第 x 列
  const get = (x, y) => g(y, x);
  const A = []; for (let i = 0; i <= 5; i++) A.push(get(i, 8)); A.push(get(7, 8), get(8, 8), get(8, 7)); for (let j = 5; j >= 0; j--) A.push(get(8, j));
  const B = []; for (let j = n - 1; j >= n - 7; j--) B.push(get(8, j)); for (let i = n - 8; i < n; i++) B.push(get(i, 8));
  A.reverse(); B.reverse();  // 转成 bit0..bit14
  const w = (bits) => bits.reduce((a, b, i) => a | (b << i), 0);
  const fa = w(A), fb = w(B);
  if (fa !== fb) throw new Error("格式信息两份不一致");
  let found = -1;
  for (let d = 0; d < 32; d++) if (bchFormat(d) === fa) found = d;
  if (found < 0) throw new Error("格式信息非法");
  const lvl = (found >> 3) === 1 ? "L" : (found >> 3) === 0 ? "M" : "?", mask = found & 7;
  if (lvl !== level) throw new Error("纠错等级位不符");
  // 功能区标记
  const fn = Array.from({ length: n }, () => new Array(n).fill(false));
  const mark = (r0, r1, c0, c1) => { for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) if (r >= 0 && r < n && c >= 0 && c < n) fn[r][c] = true; };
  mark(0, 8, 0, 8); mark(0, 8, n - 8, n - 1); mark(n - 8, n - 1, 0, 8);
  for (let i = 0; i < n; i++) { fn[6][i] = true; fn[i][6] = true; }
  const ap = align(v);
  ap.forEach((a, i) => ap.forEach((b, j) => { if ((i === 0 && j === 0) || (i === 0 && j === ap.length - 1) || (i === ap.length - 1 && j === 0)) return; mark(a - 2, a + 2, b - 2, b + 2); }));
  if (v >= 7) { mark(0, 5, n - 11, n - 9); mark(n - 11, n - 9, 0, 5); }
  // 版本信息
  if (v >= 7) {
    let r = v; for (let i = 0; i < 12; i++) r = (r << 1) ^ ((r >> 11) * 0x1f25);
    const want = (v << 12) | r; let got = 0;
    for (let i = 0; i < 18; i++) got |= g(Math.floor(i / 3), n - 11 + (i % 3)) << i;
    if (got !== want) throw new Error("版本信息不符");
    if (v === 7 && want !== 0x07c94) throw new Error("v7 版本信息标准值不符");
  }
  // 蛇形读码字（右下角起，两列一组，上下交替，跳过第 6 列）
  const bits = [];
  for (let col = n - 1, up = true; col > 0; col -= 2, up = !up) {
    if (col === 6) col--;
    for (let k = 0; k < n; k++) {
      const r = up ? n - 1 - k : k;
      for (const c of [col, col - 1]) if (!fn[r][c]) bits.push(g(r, c) ^ (MASK[mask](r, c) ? 1 : 0));
    }
  }
  const raw = Math.floor(bits.length / 8), cw = [];
  for (let i = 0; i < raw; i++) cw.push(bits.slice(i * 8, i * 8 + 8).reduce((a, b) => (a << 1) | b, 0));
  // 去交织
  const nb = NBK[lvl][v], ecc = EPB[lvl][v], nShort = nb - (raw % nb), shortLen = Math.floor(raw / nb);
  const blocks = Array.from({ length: nb }, () => []);
  let p = 0;
  for (let i = 0; i < shortLen - ecc + 1; i++) for (let b = 0; b < nb; b++) { if (i === shortLen - ecc && b < nShort) continue; blocks[b].push(cw[p++]); }
  for (let i = 0; i < ecc; i++) for (let b = 0; b < nb; b++) blocks[b].push(cw[p++]);
  const data = [];
  blocks.forEach((blk, b) => {
    if (!synd(blk, ecc).every((s) => s === 0)) throw new Error("块 " + b + " 纠错校验失败");
    data.push(...blk.slice(0, blk.length - ecc));
  });
  // 解析字节模式
  const bs = data.flatMap((x) => [7, 6, 5, 4, 3, 2, 1, 0].map((i) => (x >> i) & 1));
  const rd = (k, len) => { let r = 0; for (let i = 0; i < len; i++) r = (r << 1) | bs[k + i]; return r; };
  if (rd(0, 4) !== 4) throw new Error("不是字节模式");
  const cl = v < 10 ? 8 : 16, len = rd(4, cl), out = [];
  for (let i = 0; i < len; i++) out.push(rd(4 + cl + i * 8, 8));
  return Buffer.from(out).toString("utf8");
}

const samples = ["A", "HELLO WORLD", "http://192.168.1.23:8600/q/AbCdEfGhIjKlMnOpQrStUv", "https://zhimou.example.com/q/abcdefghijklmnopqrstuvwxyz0123456789_-",
  "现场随手拍 3号墩承台", "x".repeat(100), "y".repeat(180), "z".repeat(260), "w".repeat(330), "u".repeat(360)];
for (const t of samples) for (const lv of ["L", "M"]) {
  let q;
  try { q = QRGen.build(t, lv); } catch (e) { if (!(t.length > 360)) ok(false, `${lv} len=${t.length} 生成失败 ${e.message}`); continue; }
  try { ok(decode(q, lv) === t, `${lv} v${q.version} len=${t.length} 解码不一致`); } catch (e) { ok(false, `${lv} v${q.version} len=${t.length} ${e.message}`); }
}
let tooLong = false; try { QRGen.build("a".repeat(400), "M"); } catch (_) { tooLong = true; }
ok(tooLong, "超长内容应报错");
ok(/^<svg[^>]*viewBox="0 0 \d+ \d+"/.test(QRGen.svg("http://x/q/abc")) && QRGen.svg("hello").includes("<path"), "svg 输出");

if (fails) { console.log(fails + " 项失败"); process.exit(1); }
console.log("QR OK");
