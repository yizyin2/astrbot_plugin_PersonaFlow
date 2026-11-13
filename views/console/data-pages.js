import { api, ago, bridge, confirmBox, fmtSize, fmtTime, h, modal, promptBox, run, select, tag, toast } from "./lib.js";
import { S, activityTable, currentPath, go, profileName, progress, refresh, reloadBoot } from "./app.js";

const sideList = (items, current, onPick, empty) => h("div", { class: "card list" },
  items.length ? items.map((item) => h("a", { class: item.id === current ? "on" : "", onclick: () => onPick(item.id) }, item.label, item.sub && h("div", { class: "small muted" }, item.sub)))
    : h("p", { class: "muted" }, empty));
const subtabs = (tabs, current, onPick) => h("div", { class: "subtabs" },
  tabs.map(([key, label]) => h("a", { class: key === current ? "on" : "", onclick: () => onPick(key) }, label)));
const pager = (offset, limit, total, onGo) => h("div", { class: "row", style: "margin-top:10px" },
  h("button", { disabled: offset <= 0, onclick: () => onGo(Math.max(0, offset - limit)) }, "上一页"),
  h("span", { class: "muted" }, `${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} / ${total}`),
  h("button", { disabled: offset + limit >= total, onclick: () => onGo(offset + limit) }, "下一页"));

// ================= 人格记忆 =================

export async function memoryPage([pid, tab = "overview"]) {
  await reloadBoot();
  const profiles = S.boot.profiles;
  if (!pid && profiles.length) pid = (profiles.find((p) => p.active) || profiles[0]).id;
  const profile = profiles.find((p) => p.id === pid);
  const side = sideList(profiles.map((p) => ({ id: p.id, label: profileName(p), sub: p.error ? "未生效：" + p.error : p.active ? "" : "已停用" })),
    pid, (id) => go(`memory/${id}/${tab}`), "还没有档案，先到「路由」页添加。");
  let main = h("div", { class: "card" }, h("p", { class: "muted" }, "选择左侧的档案。"));
  if (profile && !profile.active) main = h("div", { class: "card" }, h("p", { class: "muted" }, "这个档案没有生效，无法查看数据。" + (profile.error || "")));
  else if (profile) {
    const view = { overview: memOverview, impressions: memImpressions, memories: memMemories, messages: memMessages, prompt: memPrompt }[tab] || memOverview;
    main = h("div", null,
      subtabs([["overview", "概览"], ["impressions", "人物印象"], ["memories", "全文记忆"], ["messages", "对话记录"], ["prompt", "人格预览"]], tab, (t) => go(`memory/${pid}/${t}`)),
      await view(pid));
  }
  return h("div", { class: "split" }, side, main);
}

async function memOverview(pid) {
  const s = await api.get("memory/stats", { profile: pid });
  const top = await api.get("memory/impressions", { profile: pid, limit: 21 });
  const persona = S.boot.profiles.find((p) => p.id === pid)?.personas_name;
  return h("div", null,
    h("div", { class: "card" }, h("div", { class: "grid" },
      h("div", { class: "stat" }, h("b", null, s.impressions), h("span", null, "认识的人")),
      h("div", { class: "stat" }, h("b", null, s.memories), h("span", null, `全文记忆（超过 ${s.memory_compaction_threshold} 条时压缩）`)),
      h("div", { class: "stat" }, h("b", null, s.messages), h("span", null, "保存的对话轮数")),
      h("div", { class: "stat" }, h("b", null, fmtSize(s.db_size)), h("span", null, "数据库大小"))),
      h("div", { style: "margin-top:12px" }, h("div", { class: "small muted" }, s.enable_memory_summary ? "距下次全文总结" : "全文总结已关闭"),
        s.enable_memory_summary && progress(s.memory_summary_progress, s.summary_memory_trigger_threshold)),
      h("p", { class: "muted small", style: "word-break:break-all" }, "数据库：" + s.db_path)),
    h("div", { class: "card" }, h("h3", null, "关系图 ", h("span", { class: "muted" }, "互动越多离得越近、圆越大；颜色代表关系。悬停看印象，点击看对话")), relationGraph(top.items, persona, pid)));
}

// 关系图：人格在中心，互动越多的人离得越近、圆越大、连线越粗；颜色按关系分类。
const hueOf = (text) => { let n = 7; for (const ch of text || "未知") n = (n * 31 + ch.codePointAt(0)) % 360; return n; };
const relColor = (rel, alpha = 1) => `hsla(${hueOf(rel)}, 68%, 60%, ${alpha})`;

function relationGraph(items, persona, pid) {
  if (!items.length) return h("p", { class: "muted" }, "还没有人物印象。和她聊过几轮之后，这里会画出她认识的人。");
  // 窄屏用一套更紧凑、接近正圆的布局，缩放后文字仍然看得清
  const compact = matchMedia("(max-width: 640px)").matches;
  const touch = matchMedia("(hover: none)").matches;
  const NS = "http://www.w3.org/2000/svg", W = compact ? 440 : 860, H = compact ? 600 : 620, cx = W / 2, cy = H / 2 - 14;
  const [R1, R2] = compact ? [112, 196] : [146, 236];
  const [SX, SY] = compact ? [0.84, 1.08] : [1.38, 0.96];
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "rg-svg");
  const el = (name, attrs, parent = svg, text) => { const e = document.createElementNS(NS, name); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text !== undefined) e.textContent = text; parent.append(e); return e; };
  const defs = el("defs", {});
  const grad = el("radialGradient", { id: "rg-core", cx: "35%", cy: "30%", r: "80%" }, defs);
  el("stop", { offset: "0%", "stop-color": "#b9a8ff" }, grad); el("stop", { offset: "55%", "stop-color": "#7a6cf5" }, grad); el("stop", { offset: "100%", "stop-color": "#4c3fd6" }, grad);
  const glow = el("filter", { id: "rg-glow", x: "-60%", y: "-60%", width: "220%", height: "220%" }, defs);
  el("feGaussianBlur", { stdDeviation: "7" }, glow);

  // 同心参考圈
  [R1, R2].forEach((r) => el("ellipse", { cx, cy, rx: r * SX, ry: r * SY, class: "rg-orbit" }));

  const max = Math.max(...items.map((i) => i.dialogue_count), 1);
  const inner = items.slice(0, Math.min(7, items.length)), outer = items.slice(inner.length, inner.length + 14);
  const placed = [];
  const place = (list, radius, offset) => list.forEach((item, i) => {
    const angle = offset + (i / list.length) * Math.PI * 2;
    placed.push({ item, x: cx + Math.cos(angle) * radius * SX, y: cy + Math.sin(angle) * radius * SY, angle });
  });
  place(inner, R1, -Math.PI / 2);
  place(outer, R2, -Math.PI / 2 + Math.PI / Math.max(outer.length, 1));

  const edges = el("g", {}), nodes = el("g", {});
  const tip = h("div", { class: "rg-tip" });
  placed.forEach(({ item, x, y, angle }, index) => {
    const weight = Math.sqrt(item.dialogue_count / max), r = (compact ? 12 : 13) + weight * (compact ? 12 : 15), color = relColor(item.relationship);
    // 连线略带弧度，从中心向外由实到虚
    const mx = (cx + x) / 2 - Math.sin(angle) * 22, my = (cy + y) / 2 + Math.cos(angle) * 22;
    const edge = el("path", { d: `M${cx} ${cy} Q${mx} ${my} ${x} ${y}`, class: "rg-edge", stroke: color, "stroke-width": (1.2 + weight * 4.5).toFixed(1),
      style: `animation-delay:${index * 45}ms` }, edges);
    const g = el("g", { class: "rg-node", style: `animation-delay:${120 + index * 45}ms;transform-origin:${x}px ${y}px` }, nodes);
    el("circle", { cx: x, cy: y, r: r + 5, fill: relColor(item.relationship, 0.18) }, g);
    el("circle", { cx: x, cy: y, r, fill: color, stroke: "rgba(255,255,255,.55)", "stroke-width": 1.5 }, g);
    el("text", { x, y: y + r * 0.36, "text-anchor": "middle", class: "rg-initial", "font-size": Math.max(11, r * 0.95) }, g, [...(item.name || item.qq_number)][0]);
    const name = [...(item.name || item.qq_number)].slice(0, compact ? 5 : 7).join("");
    el("text", { x, y: y + r + 17, "text-anchor": "middle", class: "rg-name" }, g, name);
    if (item.relationship) el("text", { x, y: y + r + 31, "text-anchor": "middle", class: "rg-rel", fill: color }, g, [...item.relationship].slice(0, 8).join(""));
    const show = () => {
      svg.querySelectorAll(".rg-edge.hot").forEach((e) => e.classList.remove("hot"));
      edge.classList.add("hot");
      tip.replaceChildren(
        h("div", { class: "row" }, h("b", null, item.name || item.qq_number), h("span", { class: "tag", style: `background:${relColor(item.relationship, 0.2)};color:${color}` }, item.relationship || "未知关系"),
          h("span", { class: "muted small right" }, `${item.dialogue_count} 次对话`)),
        h("div", { class: "small muted" }, item.qq_number),
        h("div", { style: "margin-top:6px" }, item.impression || "还没有形成印象。"));
      tip.classList.add("on");
      tip.style.left = `${Math.min(Math.max((x / W) * 100, 22), 78)}%`;
      tip.style.top = y > cy ? `${((y - r) / H) * 100}%` : `${((y + r + 36) / H) * 100}%`;
      tip.style.transform = y > cy ? "translate(-50%, calc(-100% - 10px))" : "translate(-50%, 6px)";
      shown = g;
    };
    const hide = () => { edge.classList.remove("hot"); tip.classList.remove("on"); shown = null; };
    g.addEventListener("mouseenter", () => { if (!touch) show(); });
    g.addEventListener("mouseleave", () => { if (!touch) hide(); });
    // 触屏没有悬停：第一次点显示印象，再点同一个人才跳到对话
    g.addEventListener("click", (e) => {
      e.stopPropagation();
      if (touch && shown !== g) return show();
      go(`memory/${pid}/messages/${encodeURIComponent(item.qq_number)}`);
    });
  });
  let shown = null;
  svg.addEventListener("click", () => { if (shown) { svg.querySelectorAll(".rg-edge.hot").forEach((e) => e.classList.remove("hot")); tip.classList.remove("on"); shown = null; } });

  // 中心：人格
  el("circle", { cx, cy, r: 46, fill: "#7a6cf5", filter: "url(#rg-glow)", opacity: 0.55, class: "rg-pulse" });
  el("circle", { cx, cy, r: 40, fill: "url(#rg-core)", stroke: "rgba(255,255,255,.5)", "stroke-width": 1.5 });
  const label = [...(persona || "人格")].slice(0, 6).join("");
  el("text", { x: cx, y: cy + 5, "text-anchor": "middle", class: "rg-core-text", "font-size": label.length > 4 ? 12 : 15 }, svg, label);

  // 图例：按关系汇总
  const groups = {};
  items.forEach((i) => { const key = i.relationship || "未知关系"; (groups[key] = groups[key] || { n: 0, talks: 0 }); groups[key].n += 1; groups[key].talks += i.dialogue_count; });
  const legend = h("div", { class: "rg-legend" }, Object.entries(groups).sort((a, b) => b[1].talks - a[1].talks).slice(0, 12).map(([rel, g]) =>
    h("span", { class: "rg-chip" }, h("i", { style: `background:${relColor(rel === "未知关系" ? "" : rel)}` }), rel, h("em", null, `${g.n} 人`))));
  return h("div", { class: "rg" }, svg, tip, legend);
}

async function memImpressions(pid) {
  const state = { q: "", offset: 0, limit: 30 };
  const box = h("div", null);
  const edit = (item) => {
    const d = item ? { ...item } : { qq_number: "", name: "", relationship: "", impression: "", dialogue_count: 0 };
    const f = (label, key, area) => h("label", { class: "field" }, h("span", { class: "name" }, label),
      area ? h("textarea", { value: d[key], oninput: (e) => (d[key] = e.target.value) })
        : h("input", { value: d[key], disabled: key === "qq_number" && !!item, type: key === "dialogue_count" ? "number" : "text", oninput: (e) => (d[key] = e.target.value) }));
    modal({ title: item ? "编辑人物印象" : "添加人物", okText: "保存",
      body: h("div", null, f("用户 ID（QQ 号）", "qq_number"), f("称呼", "name"), f("关系", "relationship"), f("印象", "impression", true), f("对话次数", "dialogue_count")),
      onOk: async () => (await run(api.post("memory/impression/save", { profile: pid, ...d }), "已保存")) ? true : false }).then((ok) => ok && load());
  };
  async function load() {
    const data = await run(api.get("memory/impressions", { profile: pid, ...state }));
    if (!data) return;
    // 只有"印象"一列吃掉剩余宽度，其余列按内容收紧
    box.replaceChildren(h("div", { class: "wrap" }, h("table", { class: "imp-table" },
      h("tr", null, [["用户", "imp-user"], ["关系", "imp-rel"], ["印象", "imp-text"], ["对话", "fit num"], ["", "fit"]].map(([t, c]) => h("th", { class: c }, t))),
      data.items.map((i) => h("tr", null,
        h("td", { class: "imp-user" }, h("div", { class: "imp-name" }, i.name || h("span", { class: "muted" }, "—")), h("div", { class: "muted small mono" }, i.qq_number)),
        h("td", { class: "imp-rel" }, i.relationship || h("span", { class: "muted" }, "—")),
        h("td", { class: "imp-text" }, i.impression || h("span", { class: "muted" }, "—")),
        h("td", { class: "fit num" }, i.dialogue_count),
        h("td", { class: "fit" }, h("button", { class: "link", onclick: () => edit(i) }, "编辑"),
          h("button", { class: "link", onclick: () => go(`memory/${pid}/messages/${i.qq_number}`) }, "对话"),
          h("button", { class: "link danger", onclick: async () => {
            if (await confirmBox(`忘掉 ${i.name}(${i.qq_number})？会删除他的印象和保存的对话记录；全文记忆里已经写进去的内容不会改。`, "删除")) {
              await run(api.post("memory/impression/delete", { profile: pid, qq_number: i.qq_number }), "已删除"); load(); } } }, "删除")))))),
      pager(state.offset, state.limit, data.total, (o) => { state.offset = o; load(); }));
  }
  await load();
  return h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("input", { placeholder: "搜索 QQ 号、称呼、关系、印象", onchange: (e) => { state.q = e.target.value.trim(); state.offset = 0; load(); } }),
      h("button", { class: "primary right", onclick: () => edit(null) }, "添加")), box);
}

async function memMemories(pid) {
  const box = h("div", null);
  const edit = (item) => {
    const area = h("textarea", { value: item ? item.memory : "", style: "min-height:180px" });
    modal({ title: item ? "编辑记忆" : "添加记忆", okText: "保存", body: area,
      onOk: async () => (await run(api.post("memory/memory/save", { profile: pid, id: item?.id, memory: area.value }), "已保存")) ? true : false }).then((ok) => ok && load());
  };
  async function load() {
    const data = await run(api.get("memory/memories", { profile: pid }));
    if (!data) return;
    // 最新的排在最上面；编号仍按生成顺序，#1 是最早的一条
    const items = data.items.map((m, i) => ({ m, i })).reverse();
    box.replaceChildren(items.length ? h("div", null, items.map(({ m, i }) => h("div", { class: "turn" },
      h("div", { class: "row small muted" }, h("b", null, `#${i + 1}`), h("span", null, m.created_at), h("span", { class: "right" },
        h("button", { class: "link", onclick: () => edit(m) }, "编辑"),
        h("button", { class: "link danger", onclick: async () => { if (await confirmBox("删除这条记忆？", "删除")) { await run(api.post("memory/memory/delete", { profile: pid, ids: [m.id] }), "已删除"); load(); } } }, "删除"))),
      h("div", { style: "white-space:pre-wrap" }, m.memory)))) : h("p", { class: "muted" }, "还没有全文记忆。"));
  }
  const act = (label, endpoint, body, hint) => h("button", { onclick: async (e) => {
    e.target.disabled = true; toast(hint);
    if (await run(api.post(endpoint, { profile: pid, ...body }), "完成")) load();
    e.target.disabled = false; } }, label);
  await load();
  return h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("span", { class: "muted small" }, "最新的在最上面，编号越大越新。全部记忆都会按从旧到新的顺序写进人格提示词的 {Memory} 位置。"),
      h("span", { class: "right row" },
        act("立即总结", "memory/summarize", {}, "正在调用模型总结最近的对话…"),
        act("立即压缩", "memory/compact", { force: true }, "正在调用模型合并最旧的一批记忆…"),
        h("button", { class: "primary", onclick: () => edit(null) }, "添加"))), box);
}

async function memMessages(pid) {
  const [, , , user = ""] = currentPath();
  const state = { user, offset: 0, limit: 30 };
  const box = h("div", null);
  async function load() {
    const data = await run(api.get("memory/messages", { profile: pid, ...state }));
    if (!data) return;
    box.replaceChildren(h("div", null, data.items.map((m) => h("div", { class: "turn" },
      h("div", { class: "row small muted" }, h("span", null, m.chat_time), h("span", null, "用户 " + m.qq_number), h("button", { class: "link danger right", onclick: async () => {
        await run(api.post("memory/message/delete", { profile: pid, ids: [m.id] })); load(); } }, "删除")),
      h("div", { style: "white-space:pre-wrap" }, m.message))),
      !data.items.length && h("p", { class: "muted" }, "没有记录。"),
      pager(state.offset, state.limit, data.total, (o) => { state.offset = o; load(); })));
  }
  await load();
  return h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("input", { placeholder: "只看某个 QQ 号", value: user, onchange: (e) => { state.user = e.target.value.trim(); state.offset = 0; load(); } }),
      h("span", { class: "muted small" }, "最新的在前。这些是用来生成印象和全文记忆的原始对话。")), box);
}

async function memPrompt(pid) {
  const p = await api.get("memory/prompt", { profile: pid });
  return h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:8px" }, h("b", null, "人格：" + p.persona),
      tag("{Impression} " + (p.has_impression_slot ? "已指定位置" : "未写，追加到末尾"), p.has_impression_slot ? "ok" : "warn"),
      tag("{Memory} " + (p.has_memory_slot ? "已指定位置" : "未写，追加到末尾"), p.has_memory_slot ? "ok" : "warn")),
    p.template ? h("div", { class: "grid cols2" },
      h("div", null, h("div", { class: "small muted" }, `原始模板（${p.template.length} 字）`), h("pre", null, p.template)),
      h("div", null, h("div", { class: "small muted" }, `实际发给模型的人格（${p.rendered.length} 字）`), h("pre", null, p.rendered)))
      : h("p", { class: "muted" }, "AstrBot 里找不到这个人格。"));
}

// ================= 群聊感知 =================

export async function sensePage([key, tab = "records"]) {
  await reloadBoot();
  const { groups } = await api.get("sense/groups");
  if (!key && groups.length) key = groups[0].group_key;
  const group = groups.find((g) => g.group_key === key);
  const side = sideList(groups.map((g) => ({ id: g.group_key, label: g.name || g.group_id,
    sub: `${g.group_id} · ${g.count} 条 · ${g.managed ? "受管" : "未受管"}${g.muted ? " · 禁言中" : ""}` })),
    key, (id) => go(`sense/${encodeURIComponent(id)}/${tab}`), "还没有群聊记录。到「路由」页给群打开群聊感知。");
  let main = h("div", { class: "card" }, h("p", { class: "muted" }, "选择左侧的群。"));
  if (group) {
    const view = { records: senseRecords, members: senseMembers, judge: senseJudge, config: senseConfig }[tab] || senseRecords;
    main = h("div", null,
      h("div", { class: "card row" }, h("b", null, group.name || group.group_id), tag(group.managed ? "受管" : "未受管", group.managed ? "ok" : ""),
        group.bot_role && tag("机器人：" + ({ owner: "群主", admin: "管理员" }[group.bot_role] || "普通群员")),
        group.muted && tag(group.mute_until ? `禁言到 ${fmtTime(group.mute_until, false)}` : "全员禁言中", "warn"),
        group.worker && h("span", { class: "muted small" }, `已判 ${group.worker.batches} 批，回复 ${group.worker.replies} 次，待判 ${group.worker.pending}`),
        h("span", { class: "muted small right" }, group.last_decision)),
      subtabs([["records", "群聊记录"], ["members", "成员身份"], ["judge", "判断历史"], ["config", "本群设置"]], tab, (t) => go(`sense/${encodeURIComponent(key)}/${t}`)),
      await view(group));
  }
  return h("div", { class: "split" }, side, main);
}

async function senseRecords(group) {
  const key = group.group_key;
  const chat = h("div", { class: "chat" });
  let oldest = null;
  const bubble = (r) => {
    if (r.kind === "notice") return h("div", { class: "msg sys" }, `#${r.alias} ${fmtTime(r.ts, false)}  ${r.content}`);
    const who = [r.card || r.nickname || r.sender_id, r.role_label && tag(r.role_label, "acc"), r.title && tag(r.title),
      r.at_bot && tag("@机器人", "warn"), r.reply_bot && tag("回复机器人", "warn"), r.recalled && tag("已撤回", "err")];
    const el = h("div", { class: "msg" + (r.is_bot ? " bot" : "") + (r.recalled ? " recalled" : "") },
      h("div", { class: "meta" }, `#${r.alias} ${fmtTime(r.ts)} `, who, " ", h("span", null, r.sender_id)), r.content);
    r.media.filter((m) => m.has_file).forEach((m) => {
      const img = h("img", { alt: m.caption || "图片", title: m.caption || "" });
      el.append(img);
      api.get("sense/thumb", { md5: m.md5 }).then((d) => d.url ? (img.src = d.url) : img.remove()).catch(() => img.remove());
    });
    return el;
  };
  async function load(older) {
    const data = await run(api.get("sense/records", { key, before: older ? oldest : null, limit: 80 }));
    if (!data) return;
    const nodes = data.items.map(bubble);
    if (older) { const height = chat.scrollHeight; chat.prepend(...nodes); chat.scrollTop = chat.scrollHeight - height; }
    else { chat.replaceChildren(...nodes); chat.scrollTop = chat.scrollHeight; }
    if (data.items.length) oldest = data.items[0].alias;
    more.style.display = data.items.length < 80 ? "none" : "";
  }
  const more = h("button", { onclick: () => load(true) }, "加载更早的");
  const card = h("div", { class: "card" }, h("div", { class: "row", style: "margin-bottom:8px" }, more,
    h("button", { onclick: () => load(false) }, "刷新"),
    h("span", { class: "muted small" }, "这就是判断模型和主模型看到的群聊记录；黄色的是群事件。"),
    h("button", { class: "danger right", onclick: async () => {
      if (await confirmBox("清除这个群在插件里保存的全部聊天记录？编号会继续递增。", "清除")) { await run(api.post("sense/clear", { key }), "已清除"); refresh(); } } }, "清除记录")), chat);
  await load(false);
  setTimeout(() => (chat.scrollTop = chat.scrollHeight), 0);
  return card;
}

async function senseMembers(group) {
  const { items } = await api.get("sense/members", { key: group.group_key });
  return h("div", { class: "card" }, h("p", { class: "muted small" }, "从群消息和群事件里看到的成员信息。群主和管理员会在群聊记录里带上身份标记。"),
    items.length ? h("div", { class: "wrap" }, h("table", null, h("tr", null, ["QQ 号", "昵称", "群名片", "身份", "头衔", "更新"].map((t) => h("th", null, t))),
      items.map((m) => h("tr", null, h("td", null, m.user_id), h("td", null, m.nickname), h("td", null, m.card),
        h("td", null, m.role_label ? tag(m.role_label, "acc") : h("span", { class: "muted" }, m.role === "member" ? "普通群员" : "未知")),
        h("td", null, m.title), h("td", { class: "muted" }, ago(m.updated_at)))))) : h("p", { class: "muted" }, "还没有记录到成员。"));
}

async function senseJudge(group) {
  const { items } = await api.get("logs", { category: "judge", session: group.group_key, limit: 200 });
  return h("div", { class: "card" }, h("p", { class: "muted small" }, "每次判断的结论、理由和耗时。点一行看详情；更多筛选在「日志」页。"), activityTable(items));
}

async function senseConfig(group) {
  const { settings, providers, override_keys } = S.boot;
  const draft = { ...group.override };
  const labels = { judge_provider_id: "判断模型", judge_extra_prompt: "额外判断准则", judge_context_count: "判断时参考的记录条数",
    judge_with_images: "判断时附带图片", main_context_count: "注入主模型的记录条数", main_max_images: "注入主模型的原图数量" };
  const control = (k) => {
    const global = settings.sense[k];
    if (k === "judge_provider_id") return select([["", `跟随全局（${global || "未配置"}）`], ...providers.map((p) => [p.id, p.id])], draft[k] || "", (v) => (draft[k] = v));
    if (k === "judge_with_images") return select([["", `跟随全局（${global ? "是" : "否"}）`], ["true", "是"], ["false", "否"]], draft[k] === undefined ? "" : String(draft[k]), (v) => (draft[k] = v === "" ? "" : v === "true"));
    if (k === "judge_extra_prompt") return h("textarea", { value: draft[k] || "", placeholder: global ? "跟随全局：" + global : "只对这个群生效的判断准则", oninput: (e) => (draft[k] = e.target.value) });
    return h("input", { type: "number", value: draft[k] ?? "", placeholder: `跟随全局（${global}）`, oninput: (e) => (draft[k] = e.target.value === "" ? "" : Number(e.target.value)) });
  };
  return h("div", { class: "card" },
    h("label", { class: "field" }, h("span", { class: "name" }, "群聊感知"), h("input", { type: "checkbox", checked: group.managed, onchange: async (e) => {
      if (await run(api.post("sense/toggle", { platform_id: group.platform_id, group_id: group.group_id, managed: e.target.checked }), "已保存")) refresh(); } }),
      h("span", { class: "hint" }, "打开后，这个群的每条消息都先由判断模型决定机器人要不要开口。")),
    override_keys.map((k) => h("label", { class: "field" }, h("span", { class: "name" }, labels[k] || k), control(k))),
    h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "primary", onclick: async () => {
      if (await run(api.post("sense/override", { key: group.group_key, values: draft }), "已保存")) refresh(); } }, "保存本群设置"),
      h("span", { class: "muted small" }, "留空的项跟随「设置 → 群聊感知」里的全局值。")));
}

// ================= 上下文库 =================

function createPool(presetProfile) {
  const profiles = S.boot.profiles.filter((p) => p.active);
  if (!profiles.length) return toast("先到「路由」页添加一个生效的档案", true);
  let profile = profiles.some((p) => p.id === presetProfile) ? presetProfile : profiles[0].id;
  const input = h("input", { placeholder: "例如：日常、工作群", style: "width:100%" });
  modal({ title: "新建上下文库", okText: "创建", body: h("div", null,
    h("p", { class: "muted" }, "上下文库是一个共享池：指到同一个库的群和私聊共用一份对话上下文。创建后到「路由」页把会话或档案的默认去向选到它。"),
    h("label", { class: "field" }, h("span", { class: "name" }, "属于哪个档案"), select(profiles.map((p) => [p.id, profileName(p)]), profile, (v) => (profile = v)),
      h("span", { class: "hint" }, "上下文库跟着档案走：只有这个 Bot + 人格的会话能用它。")),
    h("label", { class: "field" }, h("span", { class: "name" }, "名称"), input)),
    onOk: async () => {
      const result = await run(api.post("ctx/pool/create", { profile, name: input.value.trim() }), "已创建");
      return result ? result.pool : false;
    } }).then((created) => { if (created) go("context/" + encodeURIComponent(created)); else if (currentPath()[1] === "_new") go("context"); });
  setTimeout(() => input.focus(), 50);
}

export async function contextPage([pool, presetProfile]) {
  await reloadBoot();
  const { pools } = await api.get("ctx/pools");
  if (pool === "_new") { pool = undefined; setTimeout(() => createPool(presetProfile), 0); }
  if (pool === undefined && pools.length) pool = pools[0].pool;
  const info = pools.find((p) => p.pool === pool);
  const usage = (p) => p.kind !== "shared" ? "" : p.users.default ? " · 默认去向" : p.users.sessions ? ` · ${p.users.sessions} 个会话在用` : " · 未使用";
  const side = h("div", null,
    h("button", { class: "primary", style: "width:100%;margin-bottom:10px", onclick: () => createPool() }, "＋ 新建上下文库"),
    sideList(pools.map((p) => ({ id: p.pool, label: (p.kind === "shared" ? "" : "独立 · ") + p.name,
      sub: `${p.profile_name} · ${p.turns} 轮${usage(p)}` })), pool, (id) => go("context/" + encodeURIComponent(id)),
      "还没有上下文库。"),
    h("div", { class: "card small muted" }, "在这里新建上下文库，再到「路由」页把会话或档案的默认去向选到它。指到库里的对话不再写入 AstrBot 的对话数据。"));
  return h("div", { class: "split" }, side, info ? await poolView(info) : h("div", { class: "card" }, h("p", { class: "muted" }, "选择左侧的池。")));
}

async function poolView(info) {
  const pool = info.pool;
  const state = { source: "", q: "" };
  const list = h("div", null);
  let oldest = null, windowIds = new Set(), windowChars = 0, turns = [], marks = [];
  const srcName = (t) => (t.source_type === "group" ? "群 " : t.source_type === "private" ? "私聊 " : "") + (t.source_name || t.source_id || "未知");

  function turnNode(t, lastMark) {
    const tools = t.messages.filter((m) => m.role === "tool" || (m.role === "assistant" && m.tool_calls)).length;
    const inWindow = windowIds.has(t.id);
    return h("div", { class: "turn" + (t.id <= lastMark ? " out" : "") },
      h("div", { class: "row small muted" }, h("b", null, `#${t.id}`), h("span", null, fmtTime(t.ts)), tag(srcName(t), t.source_type === "private" ? "" : "acc"),
        t.sender_name && h("span", null, `${t.sender_name}${t.sender_id ? "(" + t.sender_id + ")" : ""}`), inWindow && tag("下次会带上", "ok"),
        tools > 0 && h("button", { class: "link", onclick: () => modal({ title: `第 ${t.id} 轮的完整消息`, okText: "关闭", cancelText: "", body: h("pre", null, JSON.stringify(t.messages, null, 2)) }) }, `含工具调用（${tools}）`),
        h("span", { class: "right" },
          h("button", { class: "link", onclick: () => editTurn(t) }, "编辑"),
          h("button", { class: "link", title: "这一轮及之前的内容不再发给模型", onclick: async () => { if (await run(api.post("ctx/mark/add", { pool, after: t.id }), "已打断点")) load(false); } }, "从这之后开始"),
          h("button", { class: "link danger", onclick: async () => { if (await confirmBox("删除这一轮？", "删除")) { await run(api.post("ctx/turn/delete", { ids: [t.id] })); load(false); } } }, "删除"))),
      h("div", { class: "u" }, t.user_text || "（空）"), h("div", { class: "a" }, t.assistant_text || "（只有工具调用）"));
  }
  function editTurn(t) {
    const u = h("textarea", { value: t.user_text }), a = h("textarea", { value: t.assistant_text });
    modal({ title: `编辑第 ${t.id} 轮`, okText: "保存", body: h("div", null, h("div", { class: "small muted" }, "用户消息"), u, h("div", { class: "small muted", style: "margin-top:8px" }, "机器人回复"), a),
      onOk: async () => (await run(api.post("ctx/turn/save", { id: t.id, user_text: u.value, assistant_text: a.value }), "已保存")) ? true : false }).then((ok) => ok && load(false));
  }
  function draw(lastMark) {
    const nodes = [];
    const byAfter = {};
    marks.forEach((m) => (byAfter[m.after_turn_id] = byAfter[m.after_turn_id] || []).push(m));
    const markNode = (m) => h("div", { class: "mark" }, `断点 · ${fmtTime(m.ts)} · ${m.reason || ""} `,
      h("button", { class: "link", onclick: async () => { if (await run(api.post("ctx/mark/remove", { id: m.id }), "已撤销断点")) load(false); } }, "撤销"));
    turns.forEach((t) => { nodes.push(turnNode(t, lastMark)); (byAfter[t.id] || []).forEach((m) => nodes.push(markNode(m))); });
    if (!turns.length) { marks.forEach((m) => nodes.push(markNode(m))); nodes.push(h("p", { class: "muted" }, "这个库里还没有对话。")); }
    list.replaceChildren(...nodes);
    summary.textContent = `下次请求会带上 ${windowIds.size} 轮，约 ${windowChars} 字符`;
  }
  // 对话从旧到新排，最新的在最下面。stick 为 true 时滚到最下面；加载更早的内容时保持眼前的位置不动
  const toBottom = () => { const go = () => (scroller.scrollTop = scroller.scrollHeight); go(); requestAnimationFrame(go); setTimeout(go, 120); };
  async function load(older, stick = false) {
    const [data, win] = await Promise.all([
      run(api.get("ctx/turns", { pool, before: older ? oldest : null, limit: 40, ...state })),
      run(api.get("ctx/window", { pool, source: state.source }))]);
    if (!data) return;
    turns = older ? [...data.items, ...turns] : data.items;
    marks = data.marks;
    if (win) { windowIds = new Set(win.ids); windowChars = win.chars; }
    if (turns.length) oldest = turns[0].id;
    more.style.display = data.items.length < 40 ? "none" : "";
    const fromBottom = scroller.scrollHeight - scroller.scrollTop;
    draw(data.last_mark);
    if (stick) toBottom();
    else if (older) scroller.scrollTop = scroller.scrollHeight - fromBottom;
  }
  const summary = h("span", { class: "muted small" });
  const more = h("button", { onclick: () => load(true) }, "加载更早的");
  const scroller = h("div", { class: "card turns-scroll" }, more, list);
  const refreshBtn = h("button", { title: "重新读取这个库，并跳到最新的对话", onclick: async () => {
    refreshBtn.disabled = true; await load(false, true); refreshBtn.disabled = false; toast("已刷新"); } }, "刷新");
  const sources = [["", "全部来源"], ...info.source_list.map((s) => [s.umo, `${s.type === "group" ? "群" : "私聊"} ${s.name || s.id}（${s.turns}）`])];
  const sessions = (await api.get("sessions")).sessions.filter((s) => s.umo);
  const importBtn = h("button", { onclick: async () => {
    if (!sessions.length) return toast("还没有可导入的会话", true);
    let umo = sessions[0].umo;
    const ok = await modal({ title: "从 AstrBot 导入对话历史", okText: "导入",
      body: h("div", null, h("p", { class: "muted" }, "把某个会话在 AstrBot 里现有的对话历史复制进这个池（AstrBot 里的原记录不动）。图片会换成占位文字。"),
        select(sessions.map((s) => [s.umo, `${s.type === "group" ? "群" : "私聊"} ${s.name || s.target_id}`]), umo, (v) => (umo = v))) });
    if (!ok) return;
    const result = await run(api.post("ctx/import", { pool, umo }));
    if (result) { toast(`导入 ${result.imported} 轮`); load(false); }
  } }, "导入历史");
  await load(false, true);
  setTimeout(toBottom, 0); // 这时列表还没挂到页面上，挂上之后再滚一次
  return h("div", null,
    h("div", { class: "card" }, h("div", { class: "row" }, h("b", null, (info.kind === "shared" ? "上下文库 · " : "独立上下文 · ") + info.name), tag(info.profile_name), summary,
      h("span", { class: "right row" }, refreshBtn, importBtn,
        h("button", { onclick: () => run(bridge.download("ctx/export", { pool }, "context.json")) }, "导出"),
        h("button", { onclick: async () => { if (await run(api.post("ctx/mark/add", { pool }), "已打断点，之后的请求从这里重新开始")) load(false); } }, "从现在重新开始"),
        h("button", { class: "danger", onclick: async () => { if (await confirmBox("清空这个库的全部对话和断点？无法恢复。", "清空")) { await run(api.post("ctx/pool/clear", { pool }), "已清空"); refresh(); } } }, "清空"),
        info.kind === "shared" && !info.users.default && !info.users.sessions && h("button", { class: "danger", onclick: async () => {
          if (await confirmBox(`删除上下文库「${info.name}」和里面的全部对话？无法恢复。`, "删除")) { if (await run(api.post("ctx/pool/remove", { pool }), "已删除")) go("context"); } } }, "删除这个库"))),
      info.kind === "shared" && !info.users.default && !info.users.sessions && h("div", { class: "issue info", style: "margin:12px 0 0" },
        "这个上下文库还没有被使用。到「路由」页点会话选它，或在默认去向上点「切换」选它。"),
      h("div", { class: "row", style: "margin-top:10px" },
        select(sources, "", (v) => { state.source = v; load(false, true); }),
        h("input", { placeholder: "搜索内容或发言人", onchange: (e) => { state.q = e.target.value.trim(); load(false, true); } }),
        h("span", { class: "muted small" }, "选一个来源可以预览「在那个会话里发起请求时」会带上哪些轮次。变灰的是断点之前的内容。"))),
    scroller);
}
