"""群聊感知各模块共享的数据结构。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field

# 消息正文里媒体占位符的定界符。用 Unicode 私用区字符，正常聊天内容不会出现，
# 渲染群聊记录时再替换成 [图片#k] 等文字。
MEDIA_OPEN = ""
MEDIA_CLOSE = ""


def media_token(index: int) -> str:
    """返回指向 MsgRecord.media[index] 的占位符。"""
    return f"{MEDIA_OPEN}{index}{MEDIA_CLOSE}"


@dataclass
class MediaRef:
    """消息里的一张图片或表情包。

    kind: image（普通图片）| sticker（收藏表情）| mface（商城表情）
    path 为空表示没能落盘，只能显示占位符或 summary。
    """

    kind: str
    md5: str = ""
    path: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MediaRef:
        return cls(
            kind=str(data.get("kind", "image")),
            md5=str(data.get("md5", "")),
            path=str(data.get("path", "")),
            summary=str(data.get("summary", "")),
        )


@dataclass
class MsgRecord:
    """群聊记录里的一条消息（群友或 bot 自己）。"""

    group_key: str
    msg_id: str
    sender_id: str
    nickname: str
    card: str
    is_bot: bool
    ts: float
    content: str
    at_bot: bool = False
    reply_bot: bool = False
    media: list[MediaRef] = field(default_factory=list)
    alias: int = 0
    # msg：聊天消息；notice：群事件（禁言、戳一戳、撤回等），正文是事件描述
    kind: str = "msg"
    # 发言人的群身份：owner / admin / member，未知为空串
    role: str = ""
    # 发言人的专属头衔
    title: str = ""
    recalled: bool = False
    # 群事件的结构化信息，例如 {"notice": "poke", "actor_id": "…", "target_id": "…"}
    extra: dict = field(default_factory=dict)

    @property
    def is_notice(self) -> bool:
        return self.kind == "notice"

    @property
    def addressed(self) -> bool:
        """这条记录是否在叫 bot（@了 bot、引用了 bot 的消息，或戳了 bot）。"""
        return self.at_bot or self.reply_bot or bool(self.extra.get("poke_bot"))


@dataclass
class JudgeDecision:
    """判断 LLM 的结论。"""

    action: str = "silent"
    target: int | None = None
    reason: str = ""
    failed: bool = False

    @property
    def should_reply(self) -> bool:
        return self.action == "reply"


@dataclass
class ReplyPlan:
    """交给主 LLM 的交接包：只说明要回应哪条消息。引用和 @ 由主 LLM 用工具自己决定。"""

    group_key: str
    target: MsgRecord
    batch_aliases: list[int]
    done: asyncio.Event = field(default_factory=asyncio.Event)
