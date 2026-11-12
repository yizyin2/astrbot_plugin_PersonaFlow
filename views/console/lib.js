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
