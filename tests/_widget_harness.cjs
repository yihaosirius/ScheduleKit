// 在 Node 里执行 Scriptable 小组件脚本，把元素树 dump 成 JSON。
//
// 这是给 pytest 用的**离线**驱动：用假的 Scriptable API 记录元素树，
// 用一个本地 HTTP 服务（由 pytest 起）提供固定的任务数据。
// 不访问外网，所以符合"测试不得访问网络"的约定。
//
// 用法： node tests/_widget_harness.cjs <family> <baseUrl> <apiKey>
// 输出： {"ok": true, "family": ..., "tree": {...}} 或 {"ok": false, "error": ...}

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');

const family = process.argv[2] || 'medium';
const baseUrl = process.argv[3] || 'http://127.0.0.1:1';
const apiKey = process.argv[4] || '';

// ── 假 Scriptable API ─────────────────────────────────────────
// 每个元素记录自己的类型、父节点、子元素。布局 bug 的判据就是
// "子元素挂在正确的父节点上"，以及"主轴方向有没有声明"。
let nextId = 1;

function makeNode(kind, extra = {}) {
  return { id: nextId++, kind, parent: null, children: [], layout: null, text: null, font: null, ...extra };
}

function addChild(parent, child) {
  child.parent = parent;
  parent.children.push(child);
  return child;
}

function makeText(text) {
  const node = makeNode('text', { text: String(text) });
  node.setPadding = () => {};
  node.centerAlignText = () => { node.align = 'center'; };
  node.leftAlignText = () => { node.align = 'left'; };
  node.rightAlignText = () => { node.align = 'right'; };
  // 颜色要能被断言。必须用带 setter 的属性，因为脚本直接赋值而不是调方法。
  let textColor = null;
  Object.defineProperty(node, 'textColor', {
    get: () => textColor, set: (v) => { textColor = v; }, enumerable: true,
  });
  let bg = null;
  Object.defineProperty(node, 'backgroundColor', {
    get: () => bg, set: (v) => { bg = v; }, enumerable: true,
  });
  return node;
}

function makeContainer(kind) {
  const node = makeNode(kind);
  node.addStack = () => addChild(node, makeContainer('stack'));
  // 必须复用 makeText：文本元素上有 centerAlignText 等方法
  node.addText = (text) => addChild(node, makeText(String(text)));
  node.addSpacer = (n) => addChild(node, makeNode('spacer', { size: n === undefined ? null : n }));
  node.layoutHorizontally = () => { node.layout = 'horizontal'; };
  node.layoutVertically = () => { node.layout = 'vertical'; };
  node.centerAlignContent = () => { node.alignContent = 'center'; };
  node.topAlignContent = () => { node.alignContent = 'top'; };
  node.bottomAlignContent = () => { node.alignContent = 'bottom'; };
  node.setPadding = (a, b, c, d) => { node.padding = [a, b, c, d]; };
  node.setWidget = () => {};
  node.presentSmall = async () => {};
  node.presentMedium = async () => {};
  node.presentLarge = async () => {};
  node.presentAccessoryRectangular = async () => {};
  let bg = null;
  Object.defineProperty(node, 'backgroundColor', {
    get: () => bg, set: (v) => { bg = v; }, enumerable: true,
  });
  return node;
}

// Color / Font 既是构造函数又是静态工具，所以假实现必须是
// "可被 new 调用的函数 + 挂在函数上的静态方法"。
function makeColor(hex, alpha) { return { color: hex === undefined ? null : hex, alpha }; }
function ColorCtor(hex, alpha) { return makeColor(hex, alpha); }
for (const name of ['red', 'orange', 'blue', 'gray', 'white', 'black', 'green', 'clear']) {
  ColorCtor[name] = () => makeColor(name);
}
ColorCtor.dynamic = (light, dark) => ({ dynamic: true, light, dark });

function makeFont(family2, size) { return { family: family2, size }; }
function FontCtor() { return makeFont('system', 12); }
FontCtor.systemFont = (s) => makeFont('system', s);
FontCtor.boldSystemFont = (s) => makeFont('bold', s);
FontCtor.semiboldSystemFont = (s) => makeFont('semibold', s);
FontCtor.italicSystemFont = (s) => makeFont('italic', s);

const keychainStore = new Map();
let captured = null;

const sandbox = {
  ListWidget: function () { return makeContainer('list'); },
  Color: ColorCtor,
  Font: FontCtor,
  Keychain: {
    contains: (k) => keychainStore.has(k),
    get: (k) => keychainStore.get(k),
    set: (k, v) => { keychainStore.set(k, v); },
  },
  Request: class {
    constructor(url) { this.url = url; this.headers = {}; this.timeoutInterval = 0; }
    async loadJSON() {
      return new Promise((resolve, reject) => {
        const req = http.get(this.url, { headers: this.headers, timeout: 5000 }, (res) => {
          let body = '';
          res.setEncoding('utf8');
          res.on('data', (chunk) => { body += chunk; });
          res.on('end', () => {
            if (res.statusCode < 200 || res.statusCode >= 300) {
              reject(new Error(`HTTP ${res.statusCode}`));
              return;
            }
            try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
          });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(new Error('timeout')); });
      });
    }
  },
  args: { widgetParameter: `${baseUrl}|${apiKey}` },
  config: {
    widgetFamily: family,
    runsInWidget: true,
    runsInAccessoryWidget: family.startsWith('accessory'),
  },
  Script: { setWidget: (w) => { captured = w; }, complete: () => {} },
  console,
  Date, JSON, Math, Promise, String, Number, Boolean, Array, Object, Error, RegExp,
  setTimeout, clearTimeout, fetch,
};

const source = fs.readFileSync(
  path.resolve(__dirname, '..', 'clients', 'scriptable', 'schedulekit-widget.js'),
  'utf8',
);
vm.createContext(sandbox);

function serialize(node) {
  return {
    kind: node.kind,
    layout: node.layout,
    text: node.text,
    font: node.font ? `${node.font.family}:${node.font.size}` : null,
    size: node.size,
    color: node.textColor ? node.textColor.color : null,
    dynamicBg: node.backgroundColor ? !!node.backgroundColor.dynamic : null,
    children: node.children.map(serialize),
  };
}

/**
 * 输出一行 JSON 并退出。
 *
 * ★ 不能直接 `console.log(...); process.exit(0)`：当 stdout 是**管道**
 *   （pytest 的 capture_output=True 就是管道）时它是异步缓冲的，
 *   立刻 exit 会把还没写出去的缓冲丢掉，父进程只看到空输出。
 *   所以显式等 write 回调，再 `process.exitCode`（而不是 process.exit）。
 *   手动在终端里跑时 stdout 是 TTY、写入是同步的，所以看不出这个问题。
 */
function emit(payload, code) {
  process.stdout.write(JSON.stringify(payload) + '\n', () => {
    process.exitCode = code;
  });
}

(async () => {
  try {
    // 脚本里有顶层 await，包一层 async IIFE
    await vm.runInContext(`(async () => {\n${source}\n})()`, sandbox, { filename: 'widget.js' });
  } catch (error) {
    emit({
      ok: false,
      family,
      error: error.message,
      stack: (error.stack || '').split('\n').slice(0, 5),
    }, 1);
    return;
  }

  let tree;
  if (captured && typeof captured === 'object' && captured.kind === 'text') {
    // 脚本把**文本元素**当成 widget 返回了 —— 这是个真实的缺陷
    // （Script.setWidget 需要组件而不是元素），所以如实标出来供断言使用。
    tree = { kind: 'inline-text', text: captured.text };
  } else if (captured && typeof captured === 'object') {
    tree = serialize(captured);
  } else {
    tree = { kind: 'none' };
  }

  emit({ ok: true, family, tree }, 0);
})();
