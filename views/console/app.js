import { api, ago, bridge, canLeave, confirmBox, fmtSize, fmtTime, guard, h, modal, run, saveBar, select, tag } from "./lib.js";
import { contextPage, memoryPage } from "./data-pages.js";
import { routesPage } from "./routes.js";

export const S = { boot: null, cleanup: null };
const PAGES = [
  ["overview", "总览"], ["routes", "路由"], ["memory", "人格记忆"],
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
  guard.current = null;
  if (S.cleanup) { try { S.cleanup(); } catch {} S.cleanup = null; }
  const [first, ...rest] = currentPath();
  const name = PAGES.some(([key]) => key === first) ? first : "overview"; // 没有地址或地址不认识时都算总览
  const nav = document.getElementById("nav");
  nav.replaceChildren(...PAGES.map(([key, label]) =>
    h("a", { class: key === name ? "on" : "", onclick: () => go(key) }, label)));
  const page = document.getElementById("page");
  const view = { overview: overviewPage, routes: routesPage, memory: memoryPage,
    context: contextPage, logs: logsPage, settings: settingsPage }[name] || overviewPage;
  try {
    page.replaceChildren(await view(rest));
  } catch (error) {
    page.replaceChildren(h("div", { class: "card" }, h("h3", null, "加载失败"), h("pre", null, error?.message || String(error))));
  }
}
// 重新渲染当前页；页面上有没保存的修改时先问
export async function refresh() { if (await canLeave()) render(); }

// 切换页面前先检查有没有没保存的修改。hashchange 发生时地址已经变了，所以先退回原地址再问
let shownHash = location.hash, reverting = false;
async function onHashChange() {
  if (reverting) { reverting = false; return; }
  const target = location.hash;
  if (guard.current && guard.current.isDirty()) {
    reverting = true;
    location.hash = shownHash;
    if (!(await canLeave())) return;
    location.hash = target; // 守卫已清除，这次会正常进入
    return;
  }
  shownHash = target;
  render();
}


// ================= 总览 =================

const NS = "http://www.w3.org/2000/svg";
const ICON = {
  check: "M22 11.1V12a10 10 0 1 1-5.9-9.1M22 4 12 14.01l-3-3",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  stack: "M12 3 2 8l10 5 10-5zM2 13l10 5 10-5",
  chat: "M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z",
  radar: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
};
const ico = (d) => { const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "ico");
  const path = document.createElementNS(NS, "path"); path.setAttribute("d", d); svg.append(path); return svg; };
const num = (n) => Number(n || 0).toLocaleString("zh-CN");
const pill = (kind, text) => h("span", { class: "pill " + kind }, h("i"), text);

function healthBox(issues, okText) {
  if (!issues.length) return h("div", { class: "allgood" }, ico(ICON.check), h("div", null, h("b", null, "一切正常"), h("span", null, okText)));
  return issues.map((i) => h("div", { class: "issue " + i.level }, i.text));
}

function tile(icon, label, value, sub, path) {
  return h("div", { class: "tile" + (path ? "" : " static"), onclick: path ? () => go(path) : null },
    h("div", { class: "tile-ico" }, ico(icon)),
    h("div", { class: "tile-body" }, h("span", null, label), h("b", null, value), sub && h("small", null, sub)));
}

function profileCard(p) {
  const s = p.stats;
  const where = [p.platform_id === "*" ? "任意 Bot" : p.platform_id, p.bot_self_id && "账号 " + p.bot_self_id,
    (p.groups ? `${p.groups} 个群` : "全部群") + (p.private ? " + 私聊" : "")].filter(Boolean).join(" · ");
  const state = p.error ? h("span", { class: "tag err" }, "未生效") : p.active ? tag("生效", "ok") : tag("已停用");
  const due = s && s.enable_memory_summary ? Math.min(100, (s.memory_summary_progress / Math.max(1, s.summary_memory_trigger_threshold)) * 100) : 0;
  return h("div", { class: "xcard" + (p.active ? "" : " off"), onclick: () => go(p.active ? "memory/" + p.id : "routes") },
    h("div", { class: "xcard-head" },
      h("div", { class: "avatar" }, [...(p.persona || "?")][0]),
      h("div", { class: "xcard-title" }, h("b", null, p.name || p.persona), h("span", null, where)),
      state),
    s ? h("div", { class: "minis" },
      h("div", null, h("b", null, num(s.impressions)), h("span", null, "认识的人")),
      h("div", null, h("b", null, num(s.memories)), h("span", null, "全文记忆")),
      h("div", null, h("b", null, num(s.total_dialogues)), h("span", null, "对话次数")))
      : h("p", { class: "muted small", style: "margin:12px 0 0" }, p.error || "档案没有生效，暂时看不到数据。"),
    s && s.enable_memory_summary && h("div", { class: "meter" },
      h("div", { class: "meter-track", role: "img", "aria-label": `距下次全文总结 ${s.memory_summary_progress} / ${s.summary_memory_trigger_threshold} 轮` },
        h("i", { style: `width:${due}%` })),
      h("div", { class: "meter-cap" }, h("span", null, "距下次全文总结"), h("b", null, `${s.memory_summary_progress} / ${s.summary_memory_trigger_threshold} 轮`))),
    h("div", { class: "xcard-foot" },
      h("span", null, "上下文：", h("b", null, p.context_mode === "shared" ? `上下文库「${p.context_pool}」` : MODE_LABEL[p.context_mode])),
      s && h("span", { class: "right" }, "库 ", h("b", null, fmtSize(s.db_size)))));
}

function timeline(items) {
  if (!items.length) return h("p", { class: "muted", style: "margin:4px 0" }, "暂无记录。");
  const cats = S.boot.log_categories;
  const levelTag = { WARNING: ["警告", "warn"], ERROR: ["错误", "err"] };
  return h("div", { class: "tl" }, items.map((e) => h("div", { class: "tl-item lv-" + e.level, onclick: () => showLog(e) },
    h("i", { class: "tl-dot" }),
    h("div", null,
      h("div", { class: "tl-meta" }, levelTag[e.level] && tag(...levelTag[e.level]), tag(cats[e.category] || e.category),
        e.session && h("span", null, e.session), h("span", { class: "when", title: fmtTime(e.ts) }, ago(e.ts))),
      h("div", { class: "tl-msg" }, e.message)))));
}

async function overviewPage() {
  const data = await api.get("overview");
  const boot = S.boot;
  const live = data.profiles.filter((p) => p.active && p.stats);
  const sum = (key) => live.reduce((n, p) => n + (p.stats[key] || 0), 0);
  const [kind, state] = !data.enabled ? ["warn", "总开关已关闭"] : live.length ? ["ok", "运行中"] : ["", "还没有生效的档案"];
  const turns = data.pools.reduce((n, p) => n + p.turns, 0);
  const gs = data.groupsense;

  const hero = h("div", { class: "card hero" },
    h("div", null,
      h("div", { class: "hero-kicker" }, pill(kind, state), h("span", null, `v${boot.version}`)),
      h("div", { class: "hero-fig" }, h("b", null, num(sum("impressions"))), h("span", null, "位认识的人")),
      h("div", { class: "hero-sub" }, live.length
        ? `${live.length} 个档案里记着 ${num(sum("memories"))} 条全文记忆，一共聊过 ${num(sum("total_dialogues"))} 次。`
        : "到「路由」页添加一个档案：选 Bot 和人格，就开始记人了。")),
    h("div", { class: "hero-side" }, h("div", { class: "cap" }, "状态检查"), healthBox(data.health, "档案都已生效，没有冲突。")));

  const tiles = h("div", { class: "tiles" },
    tile(ICON.db, "生效的档案", `${live.length} / ${data.profiles.length}`, "Bot + 人格 + 会话", "routes"),
    tile(ICON.stack, "上下文库", num(data.pools.length), `共 ${num(turns)} 轮对话`, "context"),
    tile(ICON.chat, "见过的会话", num(data.sessions), "群和私聊，在路由页分配", "routes"),
    tile(ICON.radar, "群聊感知", gs.active ? "已连接" : "未安装",
      gs.active ? `GroupSense ${gs.version ? "v" + gs.version : ""}，会读取这里的印象` : "独立插件，装上后由它决定在群里何时开口", null));

  const profiles = data.profiles.length
    ? [h("div", { class: "ov-head" }, h("h3", null, "人格记忆档案"), h("span", { class: "muted" }, "点卡片查看印象、记忆和关系图"),
        h("a", { class: "right", onclick: () => go("routes") }, "管理档案 →")),
      h("div", { class: "cards" }, data.profiles.map(profileCard))]
    : [h("div", { class: "card empty-state" }, h("p", null, "还没有档案。一个档案 = Bot + 人格 + 生效的群和私聊，决定用哪个记忆库。"),
        h("button", { class: "primary", onclick: () => go("routes") }, "去添加档案"))];

  const usage = (p) => p.kind !== "shared" ? "独立" : p.users.default ? "默认去向" : p.users.sessions ? `${p.users.sessions} 个会话在用` : "未使用";
  const pools = data.pools.length > 0 && h("div", { class: "card" },
    h("h3", null, "上下文库 ", h("span", { class: "muted" }, "按最近使用排序")),
    h("div", { class: "plist" }, data.pools.slice(0, 6).map((p) => h("a", { onclick: () => go("context/" + encodeURIComponent(p.pool)) },
      h("div", { class: "tile-ico" }, ico(ICON.stack)),
      h("div", { class: "grow" }, h("b", null, p.name), h("span", null, `${p.profile_name} · ${usage(p)} · ${p.sources} 个来源`)),
      h("div", { class: "num" }, h("b", null, `${num(p.turns)} 轮`), p.last_ts ? ago(p.last_ts) : "还没有对话")))));

  return h("div", { class: "ov" }, hero, tiles, ...profiles, pools,
    h("div", { class: "card" }, h("h3", null, "最近活动 ", h("span", { class: "muted" }, "点一条看详情")), timeline(data.recent)),
    boot.migration && h("details", { class: "card report" },
      h("summary", null, `迁移报告 · ${fmtTime(boot.migration.ts)}`),
      h("ul", null, boot.migration.notes.map((n) => h("li", null, n)))));
}

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
  const card = h("div", { class: "card" },
    fields.map((f) => h("div", { class: "field", "data-key": f.key }, h("span", { class: "name" }, f.label), control(f),
      (f.hint || f.rec) && h("span", { class: "hint" }, f.hint, f.rec && h("span", { class: "rec" + (f.hint ? "" : " solo") }, `推荐 ${f.rec}。`)))));
  card.append(saveBar({ root: card, draft, onDone: render,
    onSave: () => run(api.post("settings/save", { [section]: draft }), "已保存，立即生效") }));
  return h("div", null,
    h("div", { class: "subtabs" }, Object.entries(meta.sections).map(([key, label]) =>
      h("a", { class: key === section ? "on" : "", onclick: () => go("settings/" + key) }, label))),
    card);
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
  window.addEventListener("hashchange", onHashChange);
  // 关掉或刷新整个页面时，浏览器允许的话也提醒一下
  window.addEventListener("beforeunload", (e) => { if (guard.current && guard.current.isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  render();
})();
