// 路由页：每个档案一条"泳道"，从左到右是 Bot → 人格 → 档案 → 上下文去向。
// 会话是可以拖动的卡片：拖到哪个去向，它的上下文就保存到哪里；点卡片有同样功能的菜单。

import { api, ago, confirmBox, h, modal, promptBox, run, select, tag, toast } from "./lib.js";
import { S, go, profileName, refresh, reloadBoot } from "./app.js";

const MODE_LABEL = { astrbot: "不接管", shared: "上下文库", isolated: "独立" };
const BUCKET_INFO = {
  shared: ["共用一份上下文", "M4 7a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-4l-4 4v-4H7a3 3 0 0 1-3-3z"],
  isolated: ["每个会话各存一份", "M5 5h6v6H5zM13 13h6v6h-6zM13 5h6v6h-6zM5 13h6v6H5z"],
  astrbot: ["对话历史由 AstrBot 保存", "M12 3l8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7z"],
};
const NS = "http://www.w3.org/2000/svg";
const icon = (d) => { const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "ico");
  const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); s.append(p); return s; };
const ICON = {
  bot: "M9 3h6v3h3a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3zM9 11v2M15 11v2",
  persona: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20c1.5-4 4.5-5 8-5s6.5 1 8 5",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
};

let drag = null;
let closeMenu = () => {};

const bucketKey = (mode, pool) => (mode === "shared" ? "shared:" + pool : mode);
const parseKey = (key) => (key.startsWith("shared:") ? { mode: "shared", pool: key.slice(7) } : { mode: key, pool: "" });
const sessionKeyOf = (s) => `${s.type === "group" ? "group" : "private"}:${s.target_id}`;
const save = async (promise, ok = "已保存") => { if (await run(promise, ok)) refresh(); };

export async function routesPage() {
  await reloadBoot();
  const boot = S.boot;
  const { sessions } = await api.get("sessions");
  closeMenu();
  const loose = sessions.filter((s) => !(s.resolved.profile_id && s.resolved.in_scope));
  const pipeline = boot.profiles.length ? h("div", { class: "card pipe" },
    h("div", { class: "pipe-scroll" }, h("div", { class: "pipe-inner" },
      h("div", { class: "pipe-cols" }, ["Bot", "人格", "记忆库 · 档案", "上下文", "群聊 · 私聊"].map((t, i) => h("div", { class: "col-label lv" + i }, t))),
      pipelineTree(boot, sessions), wiresSvg())))
    : h("div", { class: "card" }, h("p", { class: "muted" }, "还没有档案。点右上角「添加档案」，选一个 Bot 和人格就能开始记忆。"));
  const root = h("div", { class: "rt" },
    h("div", { class: "card rt-head" },
      h("div", null, h("h3", null, "路由"),
        h("p", { class: "muted", style: "margin:0" }, "一条消息从左走到右：Bot 收到它，按会话选用的人格找到记忆库，再决定上下文存到哪里。",
          h("b", null, "点最右边的会话"), "可以改它的上下文去向；图上已有的去向之间也可以直接拖。")),
      h("div", { class: "row right" },
        h("button", { onclick: async () => { const r = await run(api.post("sessions/refresh")); if (r) { toast(`新增 ${r.added} 个群`); refresh(); } } }, "从 QQ 拉取群列表"),
        h("button", { class: "primary", onclick: () => editProfile(null) }, "＋ 添加档案"))),
    pipeline,
    looseLane(loose, boot),
    routeTester(boot));
  const redraw = () => root.querySelectorAll(".pipe-inner").forEach(drawWires);
  requestAnimationFrame(redraw);
  setTimeout(redraw, 350);
  window.addEventListener("resize", redraw);
  S.cleanup = () => { window.removeEventListener("resize", redraw); closeMenu(); };
  return root;
}

// ================= 管线 =================
// 一棵从左到右的树：Bot → 人格 → 记忆库（档案）→ 上下文 → 会话。同一个 Bot、同一个人格只画一次。

const branch = (level, node, kids, extra = "") => h("div", { class: "branch " + extra },
  h("div", { class: "bcell lv" + level }, node), kids && h("div", { class: "kids" }, kids));
const rnode = (cls, ico, title, sub, extra) => h("div", { class: "rnode wnode " + cls }, h("div", { class: "rnode-ico" }, icon(ico)),
  h("div", { class: "grow" }, h("div", { class: "rnode-title" }, title), sub && h("div", { class: "small muted" }, sub), extra));

function pipelineTree(boot, sessions) {
  const bots = new Map();
  boot.profiles.forEach((p) => {
    const key = `${p.platform_id}|${p.bot_self_id}`;
    if (!bots.has(key)) bots.set(key, { profile: p, personas: new Map() });
    const personas = bots.get(key).personas;
    if (!personas.has(p.personas_name)) personas.set(p.personas_name, []);
    personas.get(p.personas_name).push(p);
  });
  return h("div", { class: "tree" }, [...bots.values()].map(({ profile: first, personas }) => {
    const platform = boot.platforms.find((x) => x.id === first.platform_id);
    const sub = [platform ? platform.type : first.platform_id === "*" ? "按人格匹配" : "当前不存在", first.bot_self_id && "账号 " + first.bot_self_id].filter(Boolean).join(" · ");
    return branch(0, rnode("n-bot", ICON.bot, first.platform_id === "*" ? "任意 Bot" : first.platform_id, sub),
      [...personas.entries()].map(([name, profiles]) => branch(1,
        rnode("n-persona", ICON.persona, name, boot.personas.includes(name) ? "会话选用它时走这条线" : "AstrBot 里找不到这个人格"),
        profiles.map((p) => profileBranch(p, sessions.filter((s) => s.resolved.profile_id === p.id && s.resolved.in_scope), boot)))));
  }));
}

function profileBranch(profile, sessions, boot) {
  const live = profile.active && !profile.error;
  const ctx = profile.context;
  const defaultKey = bucketKey(ctx.mode, ctx.pool);
  const where = (s) => (s.resolved.context_mode === "shared" ? "shared:" + s.resolved.context_pool.split("/").slice(1).join("/") : s.resolved.context_mode);
  // 可选的去向：这个档案下已创建的上下文库，加上"不接管"。
  // "独立"不再作为选项提供，只有已经在用它的档案或会话才继续显示。
  const usesIsolated = ctx.mode === "isolated" || sessions.some((s) => where(s) === "isolated");
  const keys = [...(ctx.pools || [ctx.pool]).map((p) => "shared:" + p), ...(usesIsolated ? ["isolated"] : []), "astrbot"];
  const scope = (profile.apply_to_group_chat.length ? `${profile.apply_to_group_chat.length} 个指定的群` : "全部群") + (profile.enable_private_chat ? " + 私聊" : "");

  const buckets = keys.filter((key) => key === defaultKey || sessions.some((s) => where(s) === key)).map((key) => {
    const { mode, pool } = parseKey(key);
    const members = sessions.filter((s) => where(s) === key);
    const isDefault = key === defaultKey;
    const node = h("div", { class: `bucket wnode b-${mode}` + (isDefault ? " is-default" : "") + (members.length ? "" : " empty"), "data-key": key },
      icon(BUCKET_INFO[mode][1]),
      h("div", { class: "grow" }, h("b", null, mode === "shared" ? `上下文库 · ${pool}` : MODE_LABEL[mode]), h("div", { class: "small muted" }, BUCKET_INFO[mode][0])),
      h("div", { class: "bucket-acts" },
        isDefault ? h("span", { class: "tag acc", title: "没有单独指定的会话都走这里" }, "默认")
          : live && h("button", { class: "link", title: "让这个档案里没有单独指定的会话都走这里", onclick: () => save(api.post("ctx/bind", { profile: profile.id, mode, pool })) }, "设为默认"),
        isDefault && live && h("button", { class: "link", title: "换一种默认的上下文保存方式", onclick: (e) => { e.stopPropagation(); openMenu(e.currentTarget, defaultMenu(profile, keys, key)); } }, "切换 ▾"),
        mode !== "astrbot" && members.length > 0 && h("button", { class: "link", onclick: () => go("context/" + encodeURIComponent(members[0].resolved.context_pool)) }, "查看")));
    const el = branch(3, node, members.length ? members.map((s) => branch(4, chip(s, profile, keys, boot), null, "leaf"))
      : [h("div", { class: "drop-hint" }, live ? "把会话拖到这里" : "")],
      "b-row" + (isDefault ? " w-default" : mode === "shared" ? " w-shared" : " w-dim"));
    if (live) dropTarget(el, (s) => moveTo(s, profile, key));
    return el;
  });
  const card = rnode("n-profile", ICON.db, profileName(profile), `生效范围：${scope}`,
    h("div", { class: "row", style: "margin-top:8px;gap:6px" },
      profile.error ? h("span", { class: "tag err" }, profile.error) : live ? tag("生效", "ok") : tag("已停用"),
      h("button", { class: "link", onclick: () => editProfile(profile) }, "编辑"),
      live && h("button", { class: "link", onclick: () => go("memory/" + profile.id) }, "记忆"),
      h("button", { class: "link danger", onclick: async () => {
        if (await confirmBox(`删除档案「${profileName(profile)}」？数据库文件会保留，重新添加同样的档案可以继续使用。`, "删除")) save(api.post("profiles/delete", { id: profile.id }), "已删除"); } }, "删除")));
  const el = branch(2, card, buckets, live ? "p-drop" : "off");
  if (live) dropTarget(el, (s) => { if (s.resolved.profile_id !== profile.id || !s.resolved.in_scope) joinProfile(s, profile); });
  return el;
}

// 把一个还没进档案的会话加进档案的生效范围；key 是它要去的上下文去向（可选）
async function joinProfile(s, profile, key) {
  if (profile.platform_id !== "*" && profile.platform_id !== s.platform_id)
    return toast(`${s.name || s.target_id} 是 Bot「${s.platform_id}」的会话，不能加入 Bot「${profile.platform_id}」的档案。`, true);
  // 只有确实记录到了这个会话的人格才拦；没触发过回复的会话人格未知，允许先加进来
  if (s.persona && s.persona !== profile.personas_name) {
    const name = s.name || s.target_id;
    if (!S.boot.personas.includes(profile.personas_name))
      return toast(`AstrBot 里找不到人格「${profile.personas_name}」，${name} 没法切换过去。`, true);
    const ok = await confirmBox(`${name} 现在用的人格是「${personaText(s.persona)}」${s.astr?.source_label ? `（来自${s.astr.source_label}）` : ""}，而这个档案是人格「${profile.personas_name}」。\n\n要把它的人格改成「${profile.personas_name}」并加入档案吗？这会修改 AstrBot 的会话规则，给这个会话强制指定人格；其他会话和配置文件不受影响，之后可以在这里取消。`, "改人格并加入", false);
    if (!ok || !(await run(api.post("astr/persona", { key: s.key, persona: profile.personas_name })))) return;
  }
  const next = JSON.parse(JSON.stringify(profile));
  const sk = sessionKeyOf(s);
  next.exclude_sessions = (next.exclude_sessions || []).filter((k) => k !== sk);
  if (s.type === "group") { if (next.apply_to_group_chat.length && !next.apply_to_group_chat.includes(s.target_id)) next.apply_to_group_chat.push(s.target_id); }
  else next.enable_private_chat = true;
  if (key && key !== bucketKey(profile.context.mode, profile.context.pool)) next.context.sessions[sk] = parseKey(key);
  save(api.post("profiles/save", { profile: next }), `${s.name || s.target_id} 已加入「${profileName(profile)}」`);
}

// 默认去向节点上的"切换"菜单
function defaultMenu(profile, keys, currentKey) {
  return [{ label: "档案的默认去向", head: true },
    ...ctxOptions(keys, currentKey, (k) => { const { mode, pool } = parseKey(k); save(api.post("ctx/bind", { profile: profile.id, mode, pool })); }),
    { label: "新建上下文库", add: true, act: () => go(`context/_new/${profile.id}`) }];
}

// 上下文去向的选项：每个上下文库一项，加上"不接管"；正在用的那项打勾。"独立"只在正在用时才列出
const ctxOptions = (keys, current, pick) => keys.filter((k) => k !== "isolated" || k === current).map((k) => {
  const { mode, pool } = parseKey(k);
  return { label: mode === "shared" ? pool : MODE_LABEL[mode], desc: mode === "shared" ? "上下文库 · 共用一份上下文" : BUCKET_INFO[mode][0],
    ico: BUCKET_INFO[mode][1], kind: mode, on: k === current, act: () => pick(k) };
});

function looseLane(sessions, boot) {
  if (!sessions.length) return null;
  return h("div", { class: "card lane loose" },
    h("h3", null, "还没进入任何档案的会话 ", h("span", { class: "muted" }, "它们不记录记忆，上下文由 AstrBot 保存。把卡片拖到上面的档案里就能加入；鼠标停在卡片上能看到没进去的原因")),
    h("div", { class: "bucket-body" }, sessions.map((s) => chip(s, null, [], boot))));
}

// ================= 会话卡片 =================

function chip(s, profile, keys, boot) {
  const isGroup = s.type === "group";
  const r = s.resolved;
  const canSense = isGroup && (!s.platform_name || s.platform_name === "aiocqhttp");
  const el = h("div", { class: "chip" + (isGroup ? " c-group" : " c-private"), draggable: true, title: (profile ? "点击可以改上下文去向和群聊感知" : (r.reason || "没有进入任何档案") + "。拖到上面的档案里就能加入，点击有更多操作")
      + (s.astr?.ok ? `\nAstrBot：人格「${personaText(s.astr.persona)}」来自${s.astr.source_label}，配置文件「${s.astr.conf_name}」` : ""),
    ondragstart: (e) => { drag = s; el.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", s.key); document.body.classList.add("is-dragging"); },
    ondragend: () => { drag = null; el.classList.remove("dragging"); document.body.classList.remove("is-dragging"); document.querySelectorAll(".over").forEach((b) => b.classList.remove("over")); },
    onclick: (e) => { e.stopPropagation(); openMenu(el, menuItems(s, profile, keys, boot, canSense)); } },
    h("span", { class: "chip-kind" }, isGroup ? "群" : "私"),
    h("span", { class: "chip-name" }, s.name || s.target_id),
    h("span", { class: "chip-id" }, s.target_id),
    canSense && h("span", { class: "chip-sense" + (r.managed ? " on" : ""), title: r.managed ? "群聊感知已开启：判断模型决定她要不要开口" : "群聊感知未开启" }, r.managed ? "感知" : "未感知"),
    r.context_override && h("span", { class: "chip-pin", title: "这个会话单独指定了上下文去向" }, "●"));
  return el;
}

function menuItems(s, profile, keys, boot, canSense) {
  const items = [];
  const r = s.resolved;
  if (profile) {
    const current = r.context_mode === "shared" ? "shared:" + r.context_pool.split("/").slice(1).join("/") : r.context_mode;
    items.push({ label: "上下文去向", head: true });
    items.push(...ctxOptions(keys, current, (k) => moveTo(s, profile, k)));
    items.push({ label: "新建上下文库", add: true, act: () => go(`context/_new/${profile.id}`) });
    if (r.context_override) items.push({ label: "恢复为档案的默认去向", act: () => save(api.post("ctx/bind", { profile: profile.id, session: sessionKeyOf(s), mode: null })) });
    if (r.context_mode !== "astrbot") items.push({ label: "查看这份上下文", act: () => go("context/" + encodeURIComponent(r.context_pool)) });
    items.push({ label: "档案", head: true });
    items.push({ label: `移出「${profileName(profile)}」`, danger: true, act: async () => {
      if (!(await confirmBox(`把 ${s.name || s.target_id} 移出档案「${profileName(profile)}」？之后它不再记录和使用这个档案的记忆，上下文改由 AstrBot 保存。已有的记忆不会删除，随时可以加回来。`, "移出"))) return;
      const next = JSON.parse(JSON.stringify(profile));
      const key = sessionKeyOf(s);
      next.apply_to_group_chat = next.apply_to_group_chat.filter((g) => !(s.type === "group" && g === s.target_id));
      // 名单留空表示"全部群"，所以单独记一笔"排除"，否则移出最后一个群反而变成全部生效
      if (!next.exclude_sessions.includes(key)) next.exclude_sessions.push(key);
      delete next.context.sessions[key];
      save(api.post("profiles/save", { profile: next }), "已移出");
    } });
  } else {
    items.push({ label: r.reason || (r.profile_id ? "不在档案的生效范围内" : "没有匹配的档案"), head: true });
    boot.profiles.filter((p) => p.active && (p.platform_id === s.platform_id || p.platform_id === "*")).forEach((p) => {
      const swap = s.persona && p.personas_name !== s.persona;
      items.push({ label: swap ? `改用人格「${p.personas_name}」并加入「${profileName(p)}」` : `加入「${profileName(p)}」的生效范围`, act: () => joinProfile(s, p) });
    });
    items.push({ label: "为它新建一个档案…", act: () => editProfile(null, { platform_id: s.platform_id, personas_name: r.persona || S.boot.personas[0] || "",
      apply_to_group_chat: s.type === "group" ? [s.target_id] : [], enable_private_chat: s.type !== "group" }) });
  }
  if (s.astr?.ok) {
    const a = s.astr;
    items.push({ label: "AstrBot 路由", head: true });
    items.push({ label: `人格「${personaText(a.persona)}」· 来自${a.source_label}，点击修改…`, act: () => editAstrPersona(s) });
    items.push({ label: `配置文件「${a.conf_name}」${a.conf_route ? (a.conf_exact ? " · 单独指定" : " · 按规则匹配") : ""}，点击修改…`, act: () => editAstrConf(s) });
  }
  if (canSense) {
    items.push({ label: "群聊感知", head: true });
    items.push({ label: r.managed ? "关闭群聊感知" : "开启群聊感知", act: () => save(api.post("sense/toggle", { platform_id: s.platform_id, group_id: s.target_id, managed: !r.managed })) });
    items.push({ label: "查看群聊记录", act: () => go("sense/" + encodeURIComponent(`${s.platform_id}:${s.target_id}`)) });
  }
  items.push({ label: `最近活动：${ago(s.last_seen)}`, head: true });
  items.push({ label: "不在这里显示（下次有消息会再出现）", act: () => save(api.post("sessions/remove", { key: s.key }), "已隐藏（不影响任何数据）") });
  return items;
}

// ================= AstrBot 自己的路由 =================

const personaText = (name) => (!name ? "无" : name === "[%None]" ? "不使用人格" : name);

function astrFacts(a) {
  const row = (label, value, on) => h("div", { class: "row small" + (on ? "" : " muted"), style: "gap:8px" }, h("span", { style: "min-width:7em" }, label), h("b", null, value), on && tag("生效", "ok"));
  return h("div", { class: "col", style: "gap:4px;margin:8px 0 12px" },
    h("div", { class: "small muted" }, "AstrBot 按下面的顺序决定人格，排在前面的优先："),
    row("① 会话规则", a.rule_persona ? personaText(a.rule_persona) : "未指定", a.source === "rule"),
    row("② 当前对话", a.conv_persona ? personaText(a.conv_persona) : "未指定", a.source === "conversation"),
    row("③ 配置文件", `${personaText(a.conf_persona)}（${a.conf_name}）`, a.source === "config"));
}

function editAstrPersona(s) {
  const a = s.astr;
  let value = a.rule_persona;
  const options = [["", "不强制（跟随当前对话和配置文件）"], ...S.boot.personas.map((p) => [p, p])];
  modal({ title: `${s.name || s.target_id} 的人格`, okText: "保存到 AstrBot",
    body: h("div", null, astrFacts(a),
      h("label", { class: "col", style: "gap:6px" }, h("span", null, "在会话规则里强制指定"), select(options, value, (v) => (value = v))),
      h("p", { class: "small muted" }, "保存后会修改 AstrBot「会话管理」里这个会话的规则，只影响这一个会话。改完后它会按新的人格重新匹配档案。")),
    onOk: async () => {
      if (value === a.rule_persona) return true;
      if (!(await run(api.post("astr/persona", { key: s.key, persona: value }), "已修改 AstrBot 的会话规则"))) return false;
      refresh();
    } });
}

function editAstrConf(s) {
  const a = s.astr;
  let value = a.conf_exact ? a.conf_id : "";
  const options = [["", a.conf_exact ? "不单独指定（删掉这条路由）" : "不单独指定（保持现状）"], ...S.boot.confs.map((c) => [c.id, c.name])];
  modal({ title: `${s.name || s.target_id} 的配置文件`, okText: "保存到 AstrBot",
    body: h("div", null,
      h("p", { class: "small" }, "现在走配置文件 ", h("b", null, a.conf_name), a.conf_route ? (a.conf_exact ? "，是给这个会话单独指定的。" : `，由路由规则 ${a.conf_route} 匹配到。`) : "，没有任何路由规则匹配它。"),
      h("label", { class: "col", style: "gap:6px" }, h("span", null, "给这个会话单独指定配置文件"), select(options, value, (v) => (value = v))),
      h("p", { class: "small muted" }, "保存后会修改 AstrBot「配置文件」页里的路由表。配置文件不只决定默认人格，还决定模型、回复方式等全部设置，换之前请确认那份配置是你想要的。")),
    onOk: async () => {
      if (value === (a.conf_exact ? a.conf_id : "")) return true;
      if (!(await run(api.post("astr/conf", { key: s.key, conf_id: value }), "已修改 AstrBot 的配置文件路由"))) return false;
      refresh();
    } });
}

function openMenu(anchor, items) {
  closeMenu();
  const menu = h("div", { class: "pop" }, items.map((it) => it.head ? h("div", { class: "pop-head" }, it.label)
    : it.ico ? h("a", { class: `opt k-${it.kind}` + (it.on ? " on" : ""), onclick: () => { closeMenu(); if (!it.on) it.act(); } },
        h("span", { class: "opt-ico" }, icon(it.ico)),
        h("span", { class: "grow" }, h("b", null, it.label), h("small", null, it.desc)),
        it.on && h("span", { class: "opt-check", title: "正在使用" }, icon("M5 12.5l4.5 4.5L19 7.5")))
    : h("a", { class: (it.danger ? "danger" : "") + (it.add ? " add" : ""), onclick: () => { closeMenu(); it.act(); } }, it.add && h("span", { class: "add-plus" }, "＋"), it.label)));
  document.body.append(menu);
  const a = anchor.getBoundingClientRect(), m = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(a.left + scrollX, innerWidth - m.width - 12 + scrollX)) + "px";
  menu.style.top = (a.bottom + m.height + 8 > innerHeight ? a.top - m.height - 6 : a.bottom + 6) + scrollY + "px";
  const off = (e) => { if (!menu.contains(e.target)) closeMenu(); };
  closeMenu = () => { menu.remove(); document.removeEventListener("click", off); closeMenu = () => {}; };
  setTimeout(() => document.addEventListener("click", off), 0);
}

function dropTarget(el, onDrop) {
  el.addEventListener("dragover", (e) => { if (drag) { e.preventDefault(); el.classList.add("over"); } });
  el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("over"); });
  el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("over"); const s = drag; drag = null; if (s) onDrop(s); });
}

function moveTo(s, profile, key) {
  if (s.resolved.profile_id !== profile.id || !s.resolved.in_scope) return joinProfile(s, profile, key);
  const { mode, pool } = parseKey(key);
  const isDefault = key === bucketKey(profile.context.mode, profile.context.pool);
  save(api.post("ctx/bind", { profile: profile.id, session: sessionKeyOf(s), mode: isDefault ? null : mode, pool }),
    `${s.name || s.target_id} → ${mode === "shared" ? "上下文库「" + pool + "」" : MODE_LABEL[mode]}`);
}

// ================= 连线 =================

function wiresSvg() {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "wires");
  return svg;
}

function drawWires(inner) {
  const svg = inner.querySelector(".wires");
  if (!svg) return;
  const base = inner.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`);
  svg.replaceChildren();
  const rect = (el) => { const r = el.getBoundingClientRect(); return { l: r.left - base.left, r: r.right - base.left, y: r.top - base.top + r.height / 2 }; };
  inner.querySelectorAll(".branch").forEach((parent) => {
    const from = parent.querySelector(":scope > .bcell > .wnode");
    if (!from) return;
    const a = rect(from);
    parent.querySelectorAll(":scope > .kids > .branch:not(.no-wire)").forEach((child) => {
      const to = child.querySelector(":scope > .bcell > .wnode, :scope > .bcell > .chip");
      if (!to || !to.getClientRects().length) return; // 隐藏的去向不画线
      const b = rect(to);
      const cls = child.classList.contains("leaf")
        ? (parent.classList.contains("w-default") ? "w-default" : parent.classList.contains("w-shared") ? "w-shared" : "w-dim")
        : child.classList.contains("w-default") ? "w-default" : child.classList.contains("w-shared") ? "w-shared" : child.classList.contains("w-dim") ? "w-dim" : "w-main";
      const mid = (a.r + b.l) / 2;
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", `M${a.r} ${a.y} C${mid} ${a.y} ${mid} ${b.y} ${b.l} ${b.y}`);
      path.setAttribute("class", "wire " + cls);
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", b.l); dot.setAttribute("cy", b.y); dot.setAttribute("r", 3.2); dot.setAttribute("class", "wire-dot " + cls);
      svg.append(path, dot);
    });
  });
}

// ================= 路由测试 =================

function routeTester(boot) {
  const f = { platform_id: boot.platforms[0]?.id || "", persona: boot.personas[0] || "", self_id: "", group_id: "", user_id: "" };
  const out = h("div", null);
  const input = (key, placeholder) => h("input", { placeholder, oninput: (e) => (f[key] = e.target.value.trim()) });
  return h("div", { class: "card" }, h("h3", null, "路由测试 ", h("span", { class: "muted" }, "输入一个假想的会话，看它会走到哪个档案、哪份上下文")),
    h("div", { class: "row" },
      select(boot.platforms.map((p) => [p.id, p.id]), f.platform_id, (v) => (f.platform_id = v)),
      select(boot.personas.map((p) => [p, p]), f.persona, (v) => (f.persona = v)),
      input("self_id", "机器人账号（可空）"), input("group_id", "群号（私聊留空）"), input("user_id", "对方 QQ（私聊时填）"),
      h("button", { class: "primary", onclick: async () => {
        const r = await run(api.get("route/test", f));
        if (!r) return;
        out.replaceChildren(h("div", { class: "rt-result" },
          h("div", { class: "row" },
            tag(r.profile_id ? "档案：" + r.profile_name : "没有档案", r.profile_id && r.in_scope ? "ok" : "warn"),
            tag("上下文：" + MODE_LABEL[r.context_mode] + (r.context_pool ? "（" + r.context_pool.split("/").slice(1).join("/") + "）" : ""), r.context_mode === "astrbot" ? "" : "acc"),
            f.group_id && tag("群聊感知：" + (r.managed ? "受管" : "不受管"), r.managed ? "ok" : "")),
          h("ol", { class: "muted small" }, r.steps.map((s) => h("li", null, s)))));
      } }, "测试")), out);
}

// ================= 档案编辑 =================

export function editProfile(profile, preset = {}) {
  const boot = S.boot;
  const p = profile ? JSON.parse(JSON.stringify(profile)) : { name: "", enabled: true, platform_id: boot.platforms[0]?.id || "", bot_self_id: "",
    personas_name: boot.personas[0] || "", database_path: "", apply_to_group_chat: [], enable_private_chat: true, exclude_sessions: [],
    context: { mode: "astrbot", pool: "main", sessions: {}, private_visible_in_groups: true }, ...preset };
  const platforms = [...boot.platforms.map((x) => [x.id, `${x.id}（${x.type}${x.enabled ? "" : "，未启用"}）`]), ["*", "任意 Bot（按人格匹配，兼容旧版单库）"]];
  if (p.platform_id && !platforms.some(([v]) => v === p.platform_id)) platforms.push([p.platform_id, p.platform_id + "（当前不存在）"]);
  const personas = boot.personas.map((x) => [x, x]);
  if (p.personas_name && !boot.personas.includes(p.personas_name)) personas.push([p.personas_name, p.personas_name + "（当前不存在）"]);
  const field = (name, control, hint) => h("label", { class: "field" }, h("span", { class: "name" }, name), control, hint && h("span", { class: "hint" }, hint));
  const text = (key, placeholder) => h("input", { value: p[key], placeholder, oninput: (e) => (p[key] = e.target.value) });
  const poolRow = h("div", { class: "row", style: (p.context.mode === "shared" ? "" : "display:none;") + "margin-top:8px" },
    select((p.context.pools?.length ? p.context.pools : [p.context.pool]).map((x) => [x, "上下文库 · " + x]), p.context.pool, (v) => (p.context.pool = v)),
    h("label", { class: "row", style: "gap:6px" }, h("input", { type: "checkbox", checked: p.context.private_visible_in_groups, onchange: (e) => (p.context.private_visible_in_groups = e.target.checked) }), "私聊内容对群聊可见"));
  const modes = [["astrbot", "不接管", "对话历史仍由 AstrBot 保存"], ["shared", "使用上下文库", "范围内的群和私聊共用库里的一份上下文"]];
  if (p.context.mode === "isolated") modes.push(["isolated", "独立（旧选项）", "每个会话各一份，存在插件库里"]);
  const seg = h("div", { class: "seg" }, modes.map(([value, label, desc]) => h("a", { class: p.context.mode === value ? "on" : "", onclick: (e) => {
    p.context.mode = value; seg.querySelectorAll("a").forEach((a) => a.classList.remove("on")); e.currentTarget.classList.add("on"); poolRow.style.display = value === "shared" ? "" : "none";
  } }, h("b", null, label), h("span", null, desc))));
  modal({ title: profile ? "编辑档案" : "添加档案", okText: "保存", body: h("div", null,
    field("Bot", select(platforms, p.platform_id, (v) => (p.platform_id = v))),
    field("人格", select(personas, p.personas_name, (v) => (p.personas_name = v)), "会话必须实际选用这个人格才会命中。人格提示词里可以写 {Impression} 和 {Memory} 指定插入位置。"),
    field("生效的群", h("input", { value: p.apply_to_group_chat.join(","), placeholder: "留空 = 全部群",
      oninput: (e) => (p.apply_to_group_chat = e.target.value.split(/[,，\s]+/).filter(Boolean)) }), "多个群号用逗号分隔。范围内的会话共享这个档案的记忆。"),
    field("私聊也生效", h("input", { type: "checkbox", checked: p.enable_private_chat, onchange: (e) => (p.enable_private_chat = e.target.checked) })),
    field("上下文默认去向", h("div", null, seg, poolRow), "个别会话可以在路由页里单独改。更多上下文库到「上下文库」页新建。"),
    h("details", { class: "more" }, h("summary", null, "更多选项"),
      field("名称", text("name", "留空显示为 Bot / 人格")),
      field("机器人账号", text("bot_self_id", "例如机器人的 QQ 号"), "多个账号共用一个 Bot 适配器时才需要填，用来分别隔离记忆。"),
      field("数据库路径", text("database_path", "留空自动生成"), "要沿用某个旧库时填它的绝对路径。"),
      field("启用", h("input", { type: "checkbox", checked: p.enabled, onchange: (e) => (p.enabled = e.target.checked) })))),
    onOk: async () => (await run(api.post("profiles/save", { profile: p }), "已保存")) ? true : false,
  }).then((ok) => ok && refresh());
}
