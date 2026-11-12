"""插件设置：存放在数据目录的 settings.json，由管理面板读写，保存即生效。

FIELDS 是所有标量设置项的说明表，既用来校验和补默认值，也发给面板生成表单。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

from .sense.notice import NOTICE_CATEGORY_LABELS, normalize_rules

SETTINGS_VERSION = 1
CONTEXT_MODES = ("astrbot", "shared", "isolated")


def _f(section, key, type_, default, label, hint="", **extra) -> dict:
    return {"section": section, "key": key, "type": type_, "default": default,
            "label": label, "hint": hint, **extra}


FIELDS: list[dict] = [
    # ---- 人格记忆 ----
    _f("memory", "summary_trigger_threshold", "int", 5, "每位用户对话多少轮后总结印象", min=1, max=100000),
    _f("memory", "summary_history_count", "int", 20, "总结印象时读取该用户最近多少轮对话", min=1, max=500),
    _f("memory", "summary_max_retries", "int", 3, "总结失败时的最大尝试次数", min=1, max=10),
    _f("memory", "summary_provider_id", "provider", "", "总结模型",
       "留空时沿用当前会话的聊天模型。可以选一个便宜的模型专门做总结和压缩。"),
    _f("memory", "enable_memory_summary", "bool", True, "启用全文总结与记忆注入",
       "关闭后停止生成和使用全文记忆，已有记忆保留，人物印象不受影响。"),
    _f("memory", "summary_memory_trigger_threshold", "int", 20, "累计多少轮对话后生成一次全文记忆", min=1, max=100000),
    _f("memory", "summary_memory_history_count", "int", 30, "生成全文记忆时读取最近多少轮对话", min=1, max=500),
    _f("memory", "enable_memory_compaction", "bool", True, "启用全文记忆自动压缩"),
    _f("memory", "memory_compaction_threshold", "int", 30, "记忆超过多少条时压缩", min=2, max=10000),
    _f("memory", "memory_compaction_batch_size", "int", 20, "每次把最旧的多少条合并成一条", min=2, max=10000),
    # ---- 群聊感知 ----
    _f("sense", "enable", "bool", True, "启用群聊感知",
       "受管群里的每条消息（包括 @ 机器人的）都先由判断模型决定主模型是否回复。"),
    _f("sense", "judge_provider_id", "provider", "", "判断模型",
       "决定要不要发言的小模型。未配置时：有人 @ 或引用机器人就回复，其余沉默。"),
    _f("sense", "judge_context_count", "int", 20, "判断时参考的群聊记录条数", min=3, max=200),
    _f("sense", "judge_timeout_s", "float", 20, "判断超时（秒）", min=3, max=120),
    _f("sense", "judge_max_retries", "int", 1, "判断输出格式错误时的重试次数", min=0, max=5),
    _f("sense", "judge_fail_policy", "select", "reply_if_addressed", "判断失败策略",
       options=[["reply_if_addressed", "有人叫机器人就回复"], ["silent", "一律沉默"]]),
    _f("sense", "judge_with_images", "bool", False, "判断时附带新消息里的图片", "需要判断模型支持图片输入。"),
    _f("sense", "judge_persona_max_chars", "int", 2000, "给判断模型的人格设定最大长度", "0 表示不截断。", min=0, max=50000),
    _f("sense", "judge_extra_prompt", "text", "", "额外判断准则", "例如「群主说话一定要理」。"),
    _f("sense", "wait_before_judge_s", "float", 3, "每条消息判断前的等待（秒）",
       "按人计时：某人发消息后先等这么久，期间他又发消息就重新计时。0 表示立即判断。", min=0, max=60),
    _f("sense", "burst_window_s", "float", 10, "突发检测窗口（秒）", min=1, max=120),
    _f("sense", "burst_threshold", "int", 5, "突发阈值（条）", "窗口内消息数达到这个值就改为合并判断。0 表示关闭。", min=0, max=100),
    _f("sense", "debounce_s", "float", 2.5, "合并判断的安静等待（秒）", min=0, max=30),
    _f("sense", "max_batch_wait_s", "float", 8, "合并判断最长等待（秒）", min=0, max=120),
    _f("sense", "reply_timeout_s", "float", 180, "等待主模型回复的上限（秒）", min=10, max=1800),
    _f("sense", "main_context_count", "int", 30, "注入主模型的群聊记录条数", min=3, max=300),
    _f("sense", "main_max_images", "int", 3, "注入主模型的原图数量", min=0, max=10),
    _f("sense", "main_keep_history_turns", "int", 0, "发给主模型的本群对话历史轮数",
       "群聊记录里已有机器人自己的发言，默认不再重复发送对话历史。需要工具调用连续性时可调大。", min=0, max=100),
    _f("sense", "caption_provider_id", "provider", "", "图片转述模型（可选）"),
    _f("sense", "caption_prompt", "text", "", "图片转述提示词", "留空使用内置提示词。"),
    _f("sense", "media_retention_days", "float", 7, "图片缓存保留天数", min=1, max=365),
    _f("sense", "db_max_messages_per_group", "int", 3000, "每个群最多保存的消息条数", min=100, max=1000000),
    _f("sense", "show_roles", "bool", True, "显示群身份",
       "在群聊记录里给群主、管理员加上身份标记，并告诉模型机器人自己的群身份。"),
    _f("sense", "show_titles", "bool", False, "显示专属头衔"),
    _f("sense", "recall_mode", "select", "mark", "撤回的消息怎么显示",
       options=[["mark", "保留原文并标记已撤回"], ["hide", "隐藏原文"]]),
    _f("sense", "poke_merge_s", "float", 10, "连续戳一戳合并窗口（秒）", "同一人在这段时间内连续戳会合并成一条。", min=0, max=600),
    _f("sense", "poke_cooldown_s", "float", 30, "戳机器人的触发冷却（秒）", "同一人在冷却内再戳只记录，不再交给判断模型。", min=0, max=3600),
    _f("sense", "enable_poke_tool", "bool", True, "允许机器人戳回去", "给主模型一个 poke_user 工具。"),
    # ---- 独立上下文库 ----
    _f("context", "window_turns", "int", 30, "每次请求最多带上最近多少轮", min=1, max=500),
    _f("context", "window_chars", "int", 24000, "上下文窗口的字符预算", "超出时从最旧的一轮开始丢弃。", min=1000, max=400000),
    _f("context", "strip_images", "bool", True, "图片不存入上下文库", "存成「[图片]」占位文字，避免库体积暴涨。"),
    _f("context", "source_prefix", "bool", True, "给消息标注来源", "共享池里，非受管会话的消息前加上「【私聊】昵称(QQ)：」这样的来源。"),
    _f("context", "guard_prompt", "bool", True, "提醒模型区分场合", "共享池时在系统提示词末尾加一句：历史来自多个场合，不要把私聊内容主动说到群里。"),
    _f("context", "sync_builtin_reset", "bool", True, "/reset、/new 同步打断点", "接管的会话里使用内置重置指令时，在上下文库里打一个断点。"),
    # ---- 日志 ----
    _f("logs", "level", "select", "INFO", "记录级别",
       options=[["DEBUG", "DEBUG"], ["INFO", "INFO"], ["WARNING", "WARNING"], ["ERROR", "ERROR"]]),
    _f("logs", "retention_days", "int", 7, "日志保留天数", min=1, max=365),
    _f("logs", "max_rows", "int", 50000, "最多保留多少条", min=1000, max=2000000),
    _f("logs", "save_prompts", "bool", False, "调试模式：保存完整提示词", "会让日志库明显变大，排查问题时再打开。"),
]

SECTION_LABELS = {"memory": "人格记忆", "sense": "群聊感知", "context": "独立上下文库", "logs": "日志"}


def _coerce(field: dict, value):
    type_ = field["type"]
    default = field["default"]
    if type_ == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value) if value is not None else default
    if type_ in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default)
        number = min(max(number, field.get("min", number)), field.get("max", number))
        return int(number) if type_ == "int" else number
    if type_ == "select":
        allowed = [option[0] for option in field.get("options", [])]
        return value if value in allowed else default
    return "" if value is None else str(value)


def _str_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def default_settings() -> dict:
    data: dict = {"version": SETTINGS_VERSION, "profiles": []}
    for field in FIELDS:
        data.setdefault(field["section"], {})[field["key"]] = field["default"]
    data["sense"].update(
        scope="all", enabled_groups=[], excluded_groups=[], group_overrides={},
        notice_rules=normalize_rules(None),
    )
    return data


def normalize_profile(raw: dict) -> dict:
    """补全一个档案的字段。"""
    raw = raw if isinstance(raw, dict) else {}
    ctx = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    sessions = {}
    for key, value in (ctx.get("sessions") or {}).items() if isinstance(ctx.get("sessions"), dict) else []:
        if isinstance(value, dict) and value.get("mode") in CONTEXT_MODES:
            sessions[str(key)] = {"mode": value["mode"], "pool": str(value.get("pool") or "").strip()}
    default_pool = str(ctx.get("pool") or "main").strip() or "main"
    # 已创建的共享池：显式创建的，加上正在被引用的（默认池和各会话指定的池）
    pools = _str_list(ctx.get("pools"))
    for name in [default_pool, *(o["pool"] for o in sessions.values() if o["mode"] == "shared" and o["pool"])]:
        if name not in pools:
            pools.append(name)
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": str(raw.get("name") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "platform_id": str(raw.get("platform_id") or "").strip(),
        "bot_self_id": str(raw.get("bot_self_id") or "").strip(),
        "personas_name": str(raw.get("personas_name") or "").strip(),
        "database_path": str(raw.get("database_path") or "").strip(),
        "apply_to_group_chat": _str_list(raw.get("apply_to_group_chat")),
        "enable_private_chat": bool(raw.get("enable_private_chat", False)),
        # 被单独移出这个档案的会话，写成 group:群号 / private:QQ号
        "exclude_sessions": _str_list(raw.get("exclude_sessions")),
        "context": {
            "mode": ctx.get("mode") if ctx.get("mode") in CONTEXT_MODES else "astrbot",
            "pool": default_pool,
            "pools": pools,
            "sessions": sessions,
            "private_visible_in_groups": bool(ctx.get("private_visible_in_groups", True)),
        },
    }


def normalize(raw: dict | None) -> dict:
    """把任意输入整理成完整、合法的设置。"""
    raw = raw if isinstance(raw, dict) else {}
    data = default_settings()
    for field in FIELDS:
        section = raw.get(field["section"])
        if isinstance(section, dict) and field["key"] in section:
            data[field["section"]][field["key"]] = _coerce(field, section[field["key"]])
    memory = data["memory"]
    if memory["memory_compaction_batch_size"] > memory["memory_compaction_threshold"]:
        memory["memory_compaction_batch_size"] = memory["memory_compaction_threshold"]
    sense_raw = raw.get("sense") if isinstance(raw.get("sense"), dict) else {}
    sense = data["sense"]
    sense["enabled_groups"] = _str_list(sense_raw.get("enabled_groups"))
    sense["excluded_groups"] = _str_list(sense_raw.get("excluded_groups"))
    scope = sense_raw.get("scope")
    sense["scope"] = scope if scope in ("all", "list") else ("list" if sense["enabled_groups"] else "all")
    overrides = sense_raw.get("group_overrides")
    sense["group_overrides"] = {
        str(k): {kk: vv for kk, vv in v.items() if vv not in (None, "")}
        for k, v in (overrides.items() if isinstance(overrides, dict) else [])
        if isinstance(v, dict)
    }
    sense["group_overrides"] = {k: v for k, v in sense["group_overrides"].items() if v}
    sense["notice_rules"] = normalize_rules(sense_raw.get("notice_rules"))
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), list) else []
    seen: set[str] = set()
    for item in profiles:
        profile = normalize_profile(item)
        if profile["id"] in seen:
            profile["id"] = uuid.uuid4().hex[:8]
        seen.add(profile["id"])
        data["profiles"].append(profile)
    return data


def auto_db_path(data_dir: Path, profile: dict) -> Path:
    """与记忆路由相同的自动路径规则。"""
    key = (profile["platform_id"], profile["personas_name"], profile["bot_self_id"])
    digest = hashlib.sha256(
        json.dumps(key if profile["bot_self_id"] else key[:2], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return data_dir / "profiles" / f"{digest}.db"


def profile_db_path(data_dir: Path, profile: dict) -> Path:
    path = profile["database_path"]
    return Path(path).expanduser().resolve() if path else auto_db_path(data_dir, profile).resolve()


def validate_profiles(profiles: list[dict], data_dir: Path) -> dict[str, str]:
    """检查档案冲突，返回 {档案 ID: 问题说明}。有问题的档案不会生效。"""
    errors: dict[str, str] = {}
    keys: dict[tuple, str] = {}
    paths: dict[Path, str] = {}
    for profile in profiles:
        if not profile["enabled"]:
            continue
        pid = profile["id"]
        if not profile["platform_id"] or not profile["personas_name"]:
            errors[pid] = "需要选择 Bot 和人格"
            continue
        key = (profile["platform_id"], profile["personas_name"], profile["bot_self_id"])
        if key in keys:
            errors[pid] = "Bot + 人格 + 账号 与另一个档案重复"
            continue
        if any(k[:2] == key[:2] and (not k[2] or not key[2]) for k in keys):
            errors[pid] = "同一 Bot 和人格不能同时存在「账号留空」和「指定账号」的档案"
            continue
        path = profile_db_path(data_dir, profile)
        if path in paths:
            errors[pid] = "数据库文件与另一个档案相同"
            continue
        if path.is_dir():
            errors[pid] = "数据库路径是一个目录"
            continue
        keys[key] = pid
        paths[path] = pid
    return errors


def memory_router_config(settings: dict, errors: dict[str, str]) -> dict:
    """生成记忆路由使用的配置（旧版 PersonaFlow 的配置格式）。"""
    config = dict(settings["memory"])
    config["disable_legacy"] = True
    config["memory_profiles"] = [
        {
            "id": p["id"],
            "platform_id": p["platform_id"],
            "personas_name": p["personas_name"],
            "bot_self_id": p["bot_self_id"],
            "database_path": p["database_path"],
            "apply_to_group_chat": list(p["apply_to_group_chat"]),
            "enable_private_chat": p["enable_private_chat"],
            "exclude_sessions": list(p["exclude_sessions"]),
        }
        for p in settings["profiles"]
        if p["enabled"] and p["id"] not in errors
    ]
    return config


def fields_for_panel() -> dict:
    return {"fields": FIELDS, "sections": SECTION_LABELS, "notice_categories": NOTICE_CATEGORY_LABELS}


class SettingsStore:
    def __init__(self, path: Path, data_dir: Path) -> None:
        self.path = Path(path)
        self.data_dir = Path(data_dir)
        self.data: dict = default_settings()
        self.profile_errors: dict[str, str] = {}

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
        self._set(raw)

    def _set(self, raw: dict) -> None:
        self.data = normalize(raw)
        self.profile_errors = validate_profiles(self.data["profiles"], self.data_dir)

    def replace(self, raw: dict) -> dict:
        """整理、校验并保存一份完整设置。"""
        self._set(raw)
        self.save()
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    def profile(self, profile_id: str) -> dict | None:
        return next((p for p in self.data["profiles"] if p["id"] == profile_id), None)

    def router_config(self) -> dict:
        return memory_router_config(self.data, self.profile_errors)

    def sense_config(self) -> dict:
        return dict(self.data["sense"])
