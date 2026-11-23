// 面板公共工具：DOM 构造、接口调用、弹窗、提示。受限 iframe 里不能用 confirm()/localStorage。

export const bridge = window.AstrBotPluginView;

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : String(child));
  }
}

export const api = {
  get: (endpoint, params) => bridge.apiGet(endpoint, clean(params)),
  post: (endpoint, body) => bridge.apiPost(endpoint, body || {}),
};

function clean(params) {
  const out = {};
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== null && v !== undefined && v !== "") out[k] = v;
  }
  return out;
}

export function toast(message, isError) {
  const el = h("div", { class: "toast" + (isError ? " err" : "") }, message);
  document.getElementById("toast-root").append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 2500);
}

// 包一层：失败时弹提示并返回 undefined
export async function run(promise, okMessage) {
  try {
    const result = await promise;
    if (okMessage) toast(okMessage);
    return result === undefined ? true : result;
  } catch (error) {
    toast(error?.message || String(error), true);
    return undefined;
  }
}

// body 可以是节点或返回节点的函数；onOk 返回 false 时不关闭
export function modal({ title, body, okText = "确定", danger = false, onOk, cancelText = "取消" }) {
  return new Promise((resolve) => {
    const root = document.getElementById("modal-root");
    const close = (value) => { mask.remove(); resolve(value); };
    const ok = h("button", { class: danger ? "danger" : "primary", onclick: async () => {
      ok.disabled = true;
      const result = onOk ? await onOk() : true;
      ok.disabled = false;
      if (result !== false) close(result === undefined ? true : result);
    } }, okText);
    const mask = h("div", { class: "mask", onmousedown: (e) => { if (e.target === mask) close(null); } },
      h("div", { class: "modal" },
        h("h3", null, title),
        typeof body === "function" ? body() : body,
        h("div", { class: "foot" },
          cancelText && h("button", { onclick: () => close(null) }, cancelText), ok)));
    root.append(mask);
  });
}

export const confirmBox = (text, okText = "确定", danger = true) =>
  modal({ title: "请确认", body: h("p", null, text), okText, danger });

export function promptBox(title, label, value = "") {
  const input = h("input", { value, style: "width:100%" });
  return modal({ title, body: h("div", null, h("p", { class: "muted" }, label), input),
    onOk: () => input.value.trim() || false });
}

const pad = (n) => String(n).padStart(2, "0");
export function fmtTime(ts, withDate = true) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return withDate ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}` : time;
}
export function ago(ts) {
  if (!ts) return "从未";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}
export function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}
export const tag = (text, kind = "") => h("span", { class: "tag " + kind }, text);
export const select = (options, value, onchange, attrs = {}) =>
  h("select", { ...attrs, onchange: (e) => onchange && onchange(e.target.value) },
    options.map(([v, label]) => h("option", { value: v, selected: String(v) === String(value) }, label)));

// ---------- 未保存的修改 ----------
// 有表单的页面在 guard.current 登记 { isDirty, save }；切换页面、刷新页面前先问一声。
export const guard = { current: null };

// 返回 "save"（保存并离开）、"discard"（不保存）或 null（留在这里）
export function leaveBox() {
  return new Promise((resolve) => {
    const close = (value) => { mask.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    const mask = h("div", { class: "mask", onmousedown: (e) => { if (e.target === mask) close(null); } },
      h("div", { class: "modal" },
        h("h3", null, "修改还没保存"),
        h("p", null, "这一页改过的设置还没有保存，离开后这些修改会丢失。"),
        h("div", { class: "foot" },
          h("button", { onclick: () => close(null) }, "留在这里"),
          h("button", { class: "danger", onclick: () => close("discard") }, "不保存"),
          h("button", { class: "primary", onclick: () => close("save") }, "保存并离开"))));
    document.addEventListener("keydown", onKey);
    document.getElementById("modal-root").append(mask);
  });
}

// 可以离开当前页面时返回 true（没有修改、选了不保存，或者保存成功）
export async function canLeave() {
  const current = guard.current;
  if (!current || !current.isDirty()) return true;
  const choice = await leaveBox();
  if (choice === "save" && await current.save()) { guard.current = null; return true; }
  if (choice === "discard") { guard.current = null; return true; }
  return false;
}

// 表单底部的保存栏：有修改时变成醒目的提示条并标出改过的项，同时登记离开前的提醒。
// root 里带 data-key 的元素会在对应的值改过时加上 changed 样式。
export function saveBar({ root, draft, onSave, onDone, idleText = "保存后立即生效，不需要重载插件。", normalize = (d) => d }) {
  const snap = (d) => JSON.parse(JSON.stringify(normalize(d)));
  const original = snap(draft);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const isDirty = () => !same(snap(draft), original);
  const state = h("span", { class: "state" }, idleText);
  const save = async () => {
    saveBtn.disabled = true;
    const ok = !!(await onSave());
    saveBtn.disabled = false;
    return ok;
  };
  const saveBtn = h("button", { class: "primary", onclick: async () => { if (await save()) { guard.current = null; onDone(); } } }, "保存");
  const undo = h("button", { class: "link", hidden: true, onclick: () => { guard.current = null; onDone(); } }, "撤销修改");
  const bar = h("div", { class: "savebar" }, saveBtn, state, undo);
  const update = () => {
    const now = snap(draft);
    const dirty = !same(now, original);
    bar.classList.toggle("dirty", dirty);
    state.textContent = dirty ? "有修改还没保存" : idleText;
    undo.hidden = !dirty;
    root.querySelectorAll("[data-key]").forEach((el) => el.classList.toggle("changed", !same(now[el.dataset.key], original[el.dataset.key])));
  };
  root.addEventListener("input", update);
  root.addEventListener("change", update);
  guard.current = { isDirty, save };
  return bar;
}
