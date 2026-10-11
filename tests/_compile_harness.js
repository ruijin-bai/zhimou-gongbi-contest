// 把 index.html 里的全部前端脚本在 vm 里加载一遍，再用 Vue 自带编译器编译每个组件的 template（不渲染，只查模板语法：标签没配对、v-else 没有紧邻的 v-if 等）。
const vm = require('vm'), fs = require('fs'), path = require('path');
const dir = process.argv[2];
const noop = () => {};
function el() {
  const o = { style: {}, setAttribute: noop, appendChild: noop, childNodes: [], textContent: '', children: [] };
  const dec = (v) => String(v).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  Object.defineProperty(o, 'innerHTML', {
    set(v) {  // Vue 的浏览器版实体解码：属性值走 <div foo="…"> + getAttribute，文本走 textContent
      const m = /^<div foo="([\s\S]*)">$/.exec(String(v));
      this.textContent = dec(v);
      this.children = m ? [{ getAttribute: () => dec(m[1]) }] : [];
    },
    get() { return this.textContent; },
  });
  return o;
}
const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, navigator: { userAgent: '' }, localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  location: { hash: '' }, addEventListener: noop, document: { addEventListener: noop, createElement: el, querySelector: () => null, documentElement: { style: {} } } };
ctx.window = ctx; ctx.self = ctx;
for (const n of ['Image', 'FormData', 'Blob', 'File', 'XMLHttpRequest', 'MediaRecorder', 'ResizeObserver', 'IntersectionObserver', 'HTMLElement', 'Element', 'Event', 'CustomEvent']) ctx[n] = function () {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
const order = fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/\/static\/[\w.\/-]+\.js/g).filter((x) => !x.includes('vendor'));
let src = '';
for (const f of order) src += fs.readFileSync(path.join(dir, f.replace(/^\/static\//, '')), 'utf8') + '\n;';
const names = [...src.matchAll(/^const ([A-Z]\w*) = \{/gm)].map((m) => m[1]);
src += '\nthis.__c = {' + [...new Set(names)].map((n) => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(',') + '};';
const errs = [];
try { vm.runInContext(src, ctx); } catch (e) { errs.push('LOAD: ' + e.message); }
// x_*.js 里 (function () { const X = {...}; EXT.route(...) })() 的内部组件由 EXT.routes 收集；EXT.slots 里的也一并编译
const comps = Object.entries(ctx.__c || {});
try { for (const [k, c] of Object.entries(ctx.EXT.routes || {})) comps.push(['route:' + k, c]); } catch (_) { /* EXT 不可用时忽略 */ }
let n = 0;
for (const [k, c] of comps) {
  if (!c || typeof c.template !== 'string') continue;
  n++;
  try { ctx.Vue.compile(c.template, { onError: (e) => { throw e; }, onWarn: noop }); }
  catch (e) { errs.push(k + ': ' + String(e.message).slice(0, 160) + (e.loc ? ' @line ' + e.loc.start.line : '')); }
}
process.stdout.write(JSON.stringify({ compiled: n, errs }), () => process.exit(0));
