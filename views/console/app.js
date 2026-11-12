import { api, ago, bridge, confirmBox, fmtTime, h, modal, promptBox, run, select, tag, toast } from "./lib.js";
import { contextPage, memoryPage, sensePage } from "./data-pages.js";
import { routesPage } from "./routes.js";

export const S = { boot: null, cleanup: null };
const PAGES = [
  ["overview", "总览"], ["routes", "路由"], ["memory", "人格记忆"], ["sense", "群聊感知"],
  ["context", "上下文库"], ["logs", "日志"], ["settings", "设置"],
];
const MODE_LABEL = { astrbot: "不接管", shared: "上下文库", isolated: "独立" };
export const profileName = (p) => p.name || `${p.platform_id === "*" ? "任意 Bot" : p.platform_id} / ${p.personas_name}`;

export async function reloadBoot() {
  S.boot = await api.get("bootstrap");
  document.getElementById("version").textContent = "v" + S.boot.version;
  return S.boot;
}

export function go(path) { location.hash = "#/" + path; }
export function currentPath() { return location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent); }

async function render() {
  if (S.cleanup) { try { S.cleanup(); } catch {} S.cleanup = null; }
  const [name = "overview", ...rest] = currentPath();
  const nav = document.getElementById("nav");
  nav.replaceChildren(...PAGES.map(([key, label]) =>
    h("a", { class: key === name ? "on" : "", onclick: () => go(key) }, label)));
  const page = document.getElementById("page");
  const view = { overview: overviewPage, routes: routesPage, memory: memoryPage, sense: sensePage,
    context: contextPage, logs: logsPage, settings: settingsPage }[name] || overviewPage;
  try {
    page.replaceChildren(await view(rest));
  } catch (error) {
    page.replaceChildren(h("div", { class: "card" }, h("h3", null, "加载失败"), h("pre", null, error?.message || String(error))));
  }
}
export const refresh = render;

// ================= 总览 =================

async function overviewPage() {
  const data = await api.get("overview");
  const boot = S.boot;
  const issues = data.health.length
    ? data.health.map((i) => h("div", { class: "issue " + i.level }, i.text))
    : h("div", { class: "issue info" }, "没有发现问题。");
  return h("div", null,
    h("div", { class: "card" }, h("h3", null, "状态检查"), issues),
    boot.migration && h("div", { class: "card" }, h("h3", null, "迁移报告 ", h("span", { class: "muted" }, fmtTime(boot.migration.ts))),
      h("ul", null, boot.migration.notes.map((n) => h("li", null, n)))),
    h("div", { class: "card" }, h("h3", null, "概况"), h("div", { class: "grid" },
      stat(data.profiles.filter((p) => p.active).length, "生效的档案", "routes"),
      stat(data.groups.filter((g) => g.managed).length, data.sense_enabled ? "受管群" : "受管群（群聊感知未运行）", "sense"),
      stat(data.pools.length, "上下文池", "context"),
      stat(data.sessions, "见过的会话", "routes"))),
    h("div", { class: "card" }, h("h3", null, "人格记忆档案"),
      data.profiles.length ? h("div", { class: "wrap" }, h("table", null,
        h("tr", null, ["档案", "状态", "印象", "全文记忆", "对话轮数", "下次全文总结", "上下文"].map((t) => h("th", null, t))),
        data.profiles.map((p) => h("tr", null,
          h("td", null, h("a", { onclick: () => go("memory/" + p.id) }, p.name || `${p.platform_id} / ${p.persona}`)),
          h("td", null, p.error ? tag("未生效", "err") : p.active ? tag("生效", "ok") : tag("已停用")),
          h("td", null, p.stats ? p.stats.impressions : "-"),
          h("td", null, p.stats ? p.stats.memories : "-"),
          h("td", null, p.stats ? p.stats.total_dialogues : "-"),
          h("td", null, p.stats && p.stats.enable_memory_summary
            ? progress(p.stats.memory_summary_progress, p.stats.summary_memory_trigger_threshold) : "-"),
          h("td", null, tag(MODE_LABEL[p.context_mode], p.context_mode === "astrbot" ? "" : "acc"))))))
        : h("p", { class: "muted" }, "还没有档案。")),
    h("div", { class: "card" }, h("h3", null, "最近活动"), activityTable(data.recent)));
}

const stat = (value, label, path) => h("div", { class: "stat", style: "cursor:pointer", onclick: () => go(path) }, h("b", null, value), h("span", null, label));
export const progress = (value, max) => h("div", null, h("div", { class: "small muted" }, `${value} / ${max}`),
  h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, (value / Math.max(1, max)) * 100)}%` })));

export function activityTable(items) {
  if (!items.length) return h("p", { class: "muted" }, "暂无记录。");
  const cats = S.boot.log_categories;
  return h("div", { class: "wrap" }, h("table", { class: "log" }, items.map((e) => h("tr", { class: "lv-" + e.level, style: "cursor:pointer", onclick: () => showLog(e) },
    h("td", { style: "white-space:nowrap" }, fmtTime(e.ts)),
    h("td", null, tag(cats[e.category] || e.category)),
    h("td", { class: "muted", style: "white-space:nowrap" }, e.session),
    h("td", null, e.message)))));
}

function showLog(entry) {
  modal({ title: `${fmtTime(entry.ts)} · ${S.boot.log_categories[entry.category] || entry.category}`, cancelText: "", okText: "关闭",
    body: h("div", null, h("p", null, entry.message), entry.session && h("p", { class: "muted" }, "会话：" + entry.session),
      h("pre", null, JSON.stringify(entry.data, null, 2))) });
}

// ================= 日志 =================

async function logsPage() {
  const boot = S.boot;
  const f = { level: "", category: "", session: "", q: "" };
  const body = h("tbody", null);
  const table = h("table", { class: "log" }, body);
  let oldest = null, subscription = null, live = false;
  const row = (e) => h("tr", { class: "lv-" + e.level, style: "cursor:pointer", onclick: () => showLog(e) },
    h("td", { style: "white-space:nowrap" }, fmtTime(e.ts)), h("td", null, e.level),
    h("td", null, tag(boot.log_categories[e.category] || e.category)),
    h("td", { class: "muted", style: "white-space:nowrap" }, e.session), h("td", null, e.message));
  const matches = (e) => (!f.category || e.category === f.category) && (!f.session || e.session === f.session)
    && (!f.q || e.message.includes(f.q)) && (!f.level || ["DEBUG", "INFO", "WARNING", "ERROR"].indexOf(e.level) >= ["DEBUG", "INFO", "WARNING", "ERROR"].indexOf(f.level));
  async function load(more) {
    const data = await run(api.get("logs", { ...f, before: more ? oldest : null, limit: 150 }));
    if (!data) return;
    if (!more) body.replaceChildren();
    data.items.forEach((e) => body.append(row(e)));
    if (data.items.length) oldest = data.items[data.items.length - 1].id;
    moreBtn.style.display = data.items.length < 150 ? "none" : "";
  }
  const moreBtn = h("button", { onclick: () => load(true) }, "加载更早的");
  const liveBtn = h("button", { onclick: async () => {
    if (live) { live = false; if (subscription) bridge.unsubscribeSSE(subscription); subscription = null; }
    else {
      live = true;
      subscription = await run(bridge.subscribeSSE("logs/stream", { onMessage: (ev) => {
        const e = ev.parsed; if (e && typeof e === "object" && matches(e)) body.prepend(row(e)); } }));
    }
    liveBtn.textContent = live ? "停止实时" : "实时滚动"; liveBtn.className = live ? "primary" : "";
  } }, "实时滚动");
  S.cleanup = () => { if (subscription) bridge.unsubscribeSSE(subscription); };
  const [, session] = currentPath();
  if (session) f.session = session;
  await load(false);
  const change = (key) => (v) => { f[key] = v; load(false); };
  return h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      select([["", "全部级别"], ["DEBUG", "DEBUG 及以上"], ["INFO", "INFO 及以上"], ["WARNING", "WARNING 及以上"], ["ERROR", "ERROR"]], "", change("level")),
      select([["", "全部类别"], ...Object.entries(boot.log_categories)], "", change("category")),
      h("input", { placeholder: "会话，如 林然:123456", value: f.session, onchange: (e) => change("session")(e.target.value.trim()) }),
      h("input", { placeholder: "关键字", onchange: (e) => change("q")(e.target.value.trim()) }),
      h("button", { onclick: () => load(false) }, "刷新"), liveBtn,
      h("button", { class: "danger right", onclick: async () => { if (await confirmBox("清空全部日志？")) { await run(api.post("logs/clear"), "已清空"); load(false); } } }, "清空")),
    h("p", { class: "muted small" }, `记录级别：${boot.settings.logs.level}（在「设置 → 日志」里修改）。点一行查看详情。`),
    h("div", { class: "wrap" }, table), h("div", { style: "margin-top:10px" }, moreBtn));
}

// ================= 设置 =================

async function settingsPage([section = "memory"]) {
  await reloadBoot();
  const { meta, settings, providers } = S.boot;
  const draft = JSON.parse(JSON.stringify(settings[section]));
  const fields = meta.fields.filter((f) => f.section === section);
  const control = (f) => {
    const value = draft[f.key];
    const set = (v) => (draft[f.key] = v);
    if (f.type === "bool") return h("input", { type: "checkbox", checked: !!value, onchange: (e) => set(e.target.checked) });
    if (f.type === "select") return select(f.options, value, set);
    if (f.type === "provider") {
      const options = [["", "（未选择）"], ...providers.map((p) => [p.id, p.model ? `${p.id}（${p.model}）` : p.id])];
      if (value && !providers.some((p) => p.id === value)) options.push([value, value + "（当前不存在）"]);
      return select(options, value, set);
    }
    if (f.type === "text") return h("textarea", { value: value || "", oninput: (e) => set(e.target.value) });
    if (f.type === "int" || f.type === "float") return h("input", { type: "number", value, min: f.min, max: f.max, step: f.type === "int" ? 1 : "any",
      oninput: (e) => set(e.target.value === "" ? f.default : Number(e.target.value)) });
    return h("input", { value: value || "", oninput: (e) => set(e.target.value) });
  };
  const extra = section === "sense" ? noticeRules(draft, meta) : null;
  return h("div", null,
    h("div", { class: "subtabs" }, Object.entries(meta.sections).map(([key, label]) =>
      h("a", { class: key === section ? "on" : "", onclick: () => go("settings/" + key) }, label))),
    h("div", { class: "card" },
      fields.map((f) => h("label", { class: "field" }, h("span", { class: "name" }, f.label), control(f),
        (f.hint || f.min !== undefined) && h("span", { class: "hint" }, [f.hint, f.min !== undefined ? ` 范围 ${f.min}～${f.max}。` : ""].join("")))),
      extra,
      section === "sense" && h("p", { class: "muted small" }, "哪些群受管、每个群的单独设置，在「路由」和「群聊感知」页里改。"),
      h("div", { class: "row", style: "margin-top:14px" },
        h("button", { class: "primary", onclick: async () => {
          if (await run(api.post("settings/save", { [section]: draft }), "已保存，立即生效")) refresh(); } }, "保存"),
        h("span", { class: "muted small" }, "保存后立即生效，不需要重载插件。"))));
}

function noticeRules(draft, meta) {
  const rules = draft.notice_rules;
  const box = (key, flag) => h("input", { type: "checkbox", checked: rules[key][flag], onchange: (e) => (rules[key][flag] = e.target.checked) });
  return h("div", { style: "margin-top:14px" }, h("h3", null, "群事件"),
    h("table", null, h("tr", null, h("th", null, "事件"), h("th", null, "记入群聊记录"), h("th", null, "交给判断模型决定是否回应")),
      Object.entries(meta.notice_categories).map(([key, label]) => h("tr", null, h("td", null, label), h("td", null, box(key, "record")), h("td", null, box(key, "trigger"))))),
    h("p", { class: "muted small" }, "戳一戳的「交给判断模型」只对戳机器人生效。机器人被禁言期间一律不调用判断模型。"));
}

// ================= 启动 =================

(async function start() {
  if (!bridge) {
    document.getElementById("page").replaceChildren(h("p", { class: "pad muted" }, "请在 AstrBot 的插件页面里打开这个面板。"));
    return;
  }
  await bridge.ready();
  try {
    await reloadBoot();
  } catch (error) {
    document.getElementById("page").replaceChildren(h("div", { class: "card" }, h("h3", null, "无法连接插件后端"), h("pre", null, error?.message || String(error))));
    return;
  }
  // 主题：Dashboard 是浅色、但浏览器或插件在强制把页面变暗时（Dark Reader、Chrome 自动深色），
  // 改用面板自己的深色色板并让它们别再处理这个页面，否则浅色色板会被压成一片暗色。
  const forcedDark = () => {
    if (document.querySelector('meta[name="darkreader"], style.darkreader, style[class*="darkreader"]')) return "Dark Reader";
    const probe = document.createElement("div");
    probe.style.cssText = "position:absolute;width:0;height:0;color-scheme:light;background-color:canvas";
    document.body.append(probe);
    const color = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return color && color !== "rgb(255, 255, 255)" ? "浏览器自动深色" : "";
  };
  const applyTheme = () => {
    const dashboardDark = !!bridge.getContext()?.isDark;
    const forced = forcedDark();
    const dark = dashboardDark || !!forced;
    const root = document.documentElement;
    if (root.getAttribute("data-theme") !== (dark ? "dark" : "light")) root.setAttribute("data-theme", dark ? "dark" : "light");
    let scheme = document.querySelector('meta[name="color-scheme"]');
    if (!scheme) { scheme = document.createElement("meta"); scheme.name = "color-scheme"; document.head.append(scheme); }
    scheme.content = dark ? "dark" : "light";
    if (forced && !document.querySelector('meta[name="darkreader-lock"]')) {
      const lock = document.createElement("meta"); lock.name = "darkreader-lock"; document.head.append(lock);
    }
    document.getElementById("version").title = `Dashboard 深色: ${dashboardDark} / 强制变暗: ${forced || "无"} / 面板主题: ${dark ? "深色" : "浅色"}`;
  };
  applyTheme();
  bridge.onContext(applyTheme);
  // 深色插件可能比面板晚一步注入，稍后再检查两次
  setTimeout(applyTheme, 600);
  setTimeout(applyTheme, 2500);
  window.addEventListener("hashchange", render);
  render();
})();
