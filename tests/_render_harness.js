// 真渲染组件（不只查源码字符串）：把 index.html 里的全部前端脚本在 vm 里加载一遍，用 Vue 的 createRenderer 配一个内存节点树，
// 按 spec 挂载一个组件（接口返回由 spec.fetch 按 URL 提供），等异步加载完成后输出渲染出的文字。
// 用法：node _render_harness.js <static 目录> <spec.json>
//   spec = { component: "AlertDetail", props: {...}, me: {...}, fetch: { "/api/alerts/10": {...} }, wait: 30 }
const vm = require('vm'), fs = require('fs'), path = require('path');
const [dir, specFile] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'));
const noop = () => {};
function el() {
  const o = { style: {}, setAttribute: noop, appendChild: noop, childNodes: [], textContent: '', children: [] };
  const dec = (v) => String(v).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  Object.defineProperty(o, 'innerHTML', {
    set(v) {
      const m = /^<div foo="([\s\S]*)">$/.exec(String(v));
      this.textContent = dec(v);
      this.children = m ? [{ getAttribute: () => dec(m[1]) }] : [];
    },
    get() { return this.textContent; },
  });
  return o;
}
const fetched = [];
async function fakeFetch(url) {
  const key = String(url);
  fetched.push(key);
  const hit = Object.prototype.hasOwnProperty.call(spec.fetch || {}, key);
  const body = hit ? spec.fetch[key] : (key === '/api/me' ? spec.me || null : null);
  return { ok: hit || key === '/api/me', status: hit || key === '/api/me' ? 200 : 404, headers: { get: () => null }, json: async () => body };
}
const ctx = { console: { log: noop, warn: noop, error: noop, info: noop, debug: noop }, setTimeout, clearTimeout, setInterval, clearInterval, Promise,
  navigator: { userAgent: '' }, localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  location: { hash: spec.hash || '', reload: noop }, history: { back: noop }, addEventListener: noop, removeEventListener: noop, fetch: fakeFetch,
  matchMedia: () => ({ matches: true, addEventListener: noop, removeEventListener: noop }),   // 根组件按 matchMedia 区分电脑/手机顶栏（A 外壳）；按电脑端渲染
  document: { addEventListener: noop, createElement: el, querySelector: () => null, documentElement: { style: {} }, title: '' } };
ctx.window = ctx; ctx.self = ctx;
for (const n of ['Image', 'FormData', 'Blob', 'File', 'XMLHttpRequest', 'MediaRecorder', 'ResizeObserver', 'IntersectionObserver', 'HTMLElement', 'Element', 'Event', 'CustomEvent']) ctx[n] = function () {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
const order = fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/\/static\/[\w.\/-]+\.js/g).filter((x) => !x.includes('vendor'));
let src = '';
for (const f of order) src += fs.readFileSync(path.join(dir, f.replace(/^\/static\//, '')), 'utf8') + '\n;';
const names = [...src.matchAll(/^const ([A-Z]\w*) = \{/gm)].map((m) => m[1]);
src += '\nthis.__c = {' + [...new Set(names)].map((n) => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(',') + '};';
src += '\nthis.__store = store;';
vm.runInContext(src, ctx);

// ---- 内存节点树
let seq = 0;
const mk = (o) => Object.assign({ id: ++seq, parent: null, children: [] }, o);
const nodeOps = {
  // v-model 等内置指令会直接调元素的 addEventListener / focus 等：给空实现
  createElement: (tag) => mk({ tag, props: {}, style: {}, addEventListener: noop, removeEventListener: noop, focus: noop, blur: noop, setAttribute: noop, options: [], selectedIndex: -1 }),
  createText: (text) => mk({ text }),
  createComment: (text) => mk({ comment: text }),
  setText: (n, t) => { n.text = t; },
  setElementText: (n, t) => { n.children.forEach((c) => { c.parent = null; }); n.children = t ? [mk({ text: t, parent: n })] : []; },
  insert: (child, parent, anchor) => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    const i = anchor ? parent.children.indexOf(anchor) : -1;
    if (i < 0) parent.children.push(child); else parent.children.splice(i, 0, child);
    child.parent = parent;
  },
  remove: (child) => { if (child.parent) { child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; } },
  parentNode: (n) => n.parent,
  nextSibling: (n) => { if (!n.parent) return null; const s = n.parent.children; return s[s.indexOf(n) + 1] || null; },
  patchProp: (n, key, _prev, next) => { n.props[key] = next; },
  querySelector: () => null,
  setScopeId: noop,
  insertStaticContent: () => { throw new Error('static content not supported'); },
};
const text = (n) => n.comment !== undefined ? '' : n.text !== undefined ? n.text : n.children.map(text).join('');

(async () => {
  const errs = [];
  try {
    if (spec.me) ctx.__store.me = spec.me;
    const { createApp } = ctx.Vue.createRenderer(nodeOps);
    const comp = ctx.__c[spec.component];
    if (!comp) throw new Error('没有组件 ' + spec.component);
    const app = createApp(comp, spec.props || {});
    app.config.errorHandler = (e) => errs.push(String(e && e.stack || e).slice(0, 300));
    app.config.warnHandler = noop;
    for (const [tag, name] of Object.entries({ 'load-state': 'LoadState', 'page-head': 'PageHead', 'empty-state': 'EmptyState', 'ai-note': 'AiNote', ic: 'Ic', 'sim-badge': 'SimBadge', 'trust-tag': 'TrustTag', 'zb-more': 'ZbMore', 'zb-paged': 'ZbPaged' })) {   // zb-more、zb-paged 带作用域插槽，不注册会把插槽当普通子节点渲染而报错
      if (ctx.__c[name]) app.component(tag, ctx.__c[name]);
    }
    const root = mk({ tag: 'root', props: {} });
    app.mount(root);
    for (let i = 0; i < (spec.wait || 30); i++) await new Promise((r) => setTimeout(r, 5));
    process.stdout.write(JSON.stringify({ text: text(root).replace(/\s+/g, ' ').trim(), fetched, errs }), () => process.exit(0));
  } catch (e) {
    process.stdout.write(JSON.stringify({ text: '', fetched, errs: errs.concat(String(e && e.stack || e)) }), () => process.exit(0));
  }
})();
