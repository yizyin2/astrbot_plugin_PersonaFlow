"""群事件：把 OneBot v11 的 notice 解析成统一结构，并渲染成群聊记录里的一行说明。

支持：禁言 / 解禁（含全员）、戳一戳、消息撤回、进群 / 退群 / 被踢、管理员变动、
群名片和专属头衔变更。其余 notice（精华消息、文件上传等）不处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .members import Member, role_label

# 群事件在群聊记录里的行首标记
NOTICE_MARK = "〔系统〕"

# 事件类别 → 默认规则。record：记入群聊记录；trigger：交给判断模型决定要不要回应。
# 戳一戳的 trigger 只对"戳机器人"生效，其余类别对任何人的事件都生效。
DEFAULT_NOTICE_RULES: dict[str, dict[str, bool]] = {
    "ban": {"record": True, "trigger": False},
    "poke": {"record": True, "trigger": True},
    "recall": {"record": True, "trigger": False},
    "member": {"record": True, "trigger": False},
    "admin": {"record": True, "trigger": False},
    "card": {"record": True, "trigger": False},
}

NOTICE_CATEGORY_LABELS = {
    "ban": "禁言 / 解禁",
    "poke": "戳一戳",
    "recall": "消息撤回",
    "member": "进群 / 退群 / 被踢",
    "admin": "管理员变动",
    "card": "改群名片 / 头衔",
}

_KIND_CATEGORY = {
    "ban": "ban",
    "lift_ban": "ban",
    "poke": "poke",
    "recall": "recall",
    "join": "member",
    "leave": "member",
    "kick": "member",
    "admin_set": "admin",
    "admin_unset": "admin",
    "card": "card",
    "title": "card",
}


@dataclass
class Notice:
    """一次群事件。actor 是做动作的人，target 是被作用的人；没有的一方为空串。"""

    kind: str
    group_id: str
    actor_id: str = ""
    target_id: str = ""
    ts: float = 0.0
    data: dict = field(default_factory=dict)

    @property
    def category(self) -> str:
        return _KIND_CATEGORY.get(self.kind, self.kind)

    def to_extra(self) -> dict:
        """写进 MsgRecord.extra 的结构化信息。"""
        return {
            "notice": self.kind,
            "category": self.category,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            **self.data,
        }


def normalize_rules(raw) -> dict[str, dict[str, bool]]:
    """把设置里的规则表补全成每个类别都有 record / trigger 两个布尔值。"""
    rules = {key: dict(value) for key, value in DEFAULT_NOTICE_RULES.items()}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in rules and isinstance(value, dict):
                for flag in ("record", "trigger"):
                    if flag in value:
                        rules[key][flag] = bool(value[flag])
    return rules


def _id(value) -> str:
    """OneBot 的 ID 可能是 int 或 str；0 和空值都当作没有。"""
    if value in (None, "", 0, "0"):
        return ""
    return str(value)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _poke_texts(raw: dict) -> tuple[str, str]:
    """从戳一戳事件里取动作文字和后缀，例如 ("拍了拍", "的肩膀")。

    NapCat 把它们放在 raw_info 的文字片段里；Lagrange 用 action / suffix 字段。
    """
    action = str(raw.get("action") or "").strip()
    suffix = str(raw.get("suffix") or "").strip()
    info = raw.get("raw_info")
    if isinstance(info, list):
        # 片段顺序是：发起者、动作文字、被戳者、后缀。按出现在第几个人之后来区分动作和后缀，
        # 自定义戳一戳可能只有后缀没有动作文字。
        people = 0
        for item in info:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "qq":
                people += 1
            elif item.get("type") == "nor":
                text = str(item.get("txt") or "").strip()
                if not text:
                    continue
                if people <= 1 and not action:
                    action = text
                elif people >= 2 and not suffix:
                    suffix = text
    return action or "戳了戳", suffix


def parse_notice(raw, self_id: str) -> Notice | None:
    """解析一条 OneBot notice 事件；不是群事件或不支持的类型返回 None。"""
    if not isinstance(raw, dict) or raw.get("post_type") != "notice":
        return None
    group_id = _id(raw.get("group_id"))
    if not group_id:
        return None
    notice_type = str(raw.get("notice_type") or "")
    sub_type = str(raw.get("sub_type") or "")
    user_id = _id(raw.get("user_id"))
    operator_id = _id(raw.get("operator_id"))
    try:
        ts = float(raw.get("time") or 0) or time.time()
    except (TypeError, ValueError):
        ts = time.time()

    def make(kind: str, actor: str, target: str, **data) -> Notice:
        return Notice(
            kind=kind, group_id=group_id, actor_id=actor, target_id=target, ts=ts, data=data
        )

    if notice_type == "group_ban":
        kind = "lift_ban" if sub_type == "lift_ban" else "ban"
        return make(
            kind,
            operator_id,
            user_id,
            duration=max(0, _int(raw.get("duration"))),
            whole=not user_id,
        )
    if notice_type == "notify" and sub_type == "poke":
        target = _id(raw.get("target_id"))
        if not user_id or not target:
            return None
        action, suffix = _poke_texts(raw)
        return make(
            "poke",
            user_id,
            target,
            action=action,
            suffix=suffix,
            poke_bot=target == str(self_id) and user_id != str(self_id),
        )
    if notice_type == "notify" and sub_type == "title":
        if not user_id:
            return None
        return make("title", "", user_id, title=str(raw.get("title") or ""))
    if notice_type == "group_recall":
        return make(
            "recall",
            operator_id or user_id,
            user_id,
            message_id=_id(raw.get("message_id")),
        )
    if notice_type == "group_increase":
        if not user_id:
            return None
        return make("join", operator_id, user_id, invited=sub_type == "invite")
    if notice_type == "group_decrease":
        if not user_id:
            return None
        if sub_type in ("kick", "kick_me"):
            return make("kick", operator_id, user_id)
        return make("leave", "", user_id)
    if notice_type == "group_admin":
        if not user_id:
            return None
        return make("admin_unset" if sub_type == "unset" else "admin_set", "", user_id)
    if notice_type == "group_card":
        if not user_id:
            return None
        return make(
            "card",
            "",
            user_id,
            card_old=str(raw.get("card_old") or ""),
            card_new=str(raw.get("card_new") or ""),
        )
    return None


def format_duration(seconds: int) -> str:
    """把秒数写成"10 分钟""1 小时 30 分钟""2 天"这样的说法。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts)


def member_label(member: Member | None, user_id: str, self_id: str, show_roles: bool = True) -> str:
    """群事件里的人名：机器人自己写成"你"，其余写成"名字(QQ号)[身份]"。"""
    if not user_id:
        return "有人"
    if str(user_id) == str(self_id):
        return "你"
    name = member.display_name if member else str(user_id)
    label = f"{name}({user_id})" if name != str(user_id) else str(user_id)
    if show_roles and member and role_label(member.role):
        label += f"[{role_label(member.role)}]"
    return label


def render_notice(
    notice: Notice,
    *,
    actor: str,
    target: str,
    recalled_alias: int | None = None,
) -> str:
    """生成群聊记录里的事件说明（不含行首的〔系统〕标记）。

    actor / target 是已经用 member_label 渲染好的人名。
    """
    data = notice.data
    kind = notice.kind
    if kind == "ban":
        duration = format_duration(data.get("duration", 0))
        if data.get("whole"):
            return f"{actor} 开启了全员禁言"
        if target == "你":
            return f"{actor} 把你禁言了 {duration}"
        return f"{actor} 把 {target} 禁言了 {duration}"
    if kind == "lift_ban":
        if data.get("whole"):
            return f"{actor} 关闭了全员禁言"
        if target == "你":
            return f"{actor} 解除了你的禁言"
        return f"{actor} 解除了 {target} 的禁言"
    if kind == "poke":
        action = data.get("action") or "戳了戳"
        suffix = data.get("suffix") or ""
        who = "自己" if notice.actor_id == notice.target_id else target
        text = f"{actor} {action} {who}{suffix}"
        count = int(data.get("count") or 1)
        return f"{text} ×{count}" if count > 1 else text
    if kind == "recall":
        ref = f" #{recalled_alias}" if recalled_alias else ""
        if notice.actor_id == notice.target_id or not notice.target_id:
            return f"{actor} 撤回了一条消息{ref}"
        whose = "你的" if target == "你" else f"{target} 的"
        return f"{actor} 撤回了{whose}一条消息{ref}"
    if kind == "join":
        if data.get("invited") and notice.actor_id:
            return f"{actor} 邀请 {target} 加入了群聊"
        return f"{target} 加入了群聊"
    if kind == "leave":
        return f"{target} 退出了群聊"
    if kind == "kick":
        if notice.actor_id:
            return f"{target} 被 {actor} 移出了群聊"
        return f"{target} 被移出了群聊"
    if kind == "admin_set":
        return f"{target} 被设为管理员"
    if kind == "admin_unset":
        return f"{target} 被取消了管理员"
    if kind == "card":
        old, new = data.get("card_old") or "", data.get("card_new") or ""
        if old and new:
            return f"{target} 把群名片从「{old}」改成了「{new}」"
        if new:
            return f"{target} 把群名片设为「{new}」"
        return f"{target} 清空了群名片"
    if kind == "title":
        title = data.get("title") or ""
        if title:
            return f"{target} 获得了专属头衔「{title}」"
        return f"{target} 的专属头衔被取消了"
    return f"群事件：{kind}"
