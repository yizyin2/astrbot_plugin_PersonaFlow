"""把群聊记录拼成给判断 LLM / 主 LLM 看的文本。两边共用同一份格式。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

from .members import role_label
from .models import MEDIA_CLOSE, MEDIA_OPEN, MediaRef, MsgRecord
from .notice import NOTICE_MARK

BOT_MARK = "【你】"
PENDING_MARK = "【新】"
RECALLED_MARK = "（已撤回）"
RECALLED_HIDDEN = "〔此消息已撤回〕"

_TOKEN_RE = re.compile(f"{MEDIA_OPEN}(\\d+){MEDIA_CLOSE}")


@dataclass
class TranscriptImage:
    index: int
    path: str


@dataclass
class Transcript:
    text: str
    images: list[TranscriptImage]


def sender_label(
    rec: MsgRecord, *, show_roles: bool = True, show_titles: bool = False
) -> str:
    """发言人格式：QQ昵称(群名片)QQ号[群身份]，bot 自己前面加【你】。

    群主和管理员才带身份标记，普通群员不标，省 token。
    """
    label = f"{rec.nickname or rec.sender_id}({rec.card}){rec.sender_id}"
    if show_roles and role_label(rec.role):
        label += f"[{role_label(rec.role)}]"
    if show_titles and rec.title:
        label += f"[头衔:{rec.title}]"
    return BOT_MARK + label if rec.is_bot else label


def media_placeholder(ref: MediaRef, index: int | None = None, caption: str = "") -> str:
    label = "图片" if ref.kind == "image" else "表情包"
    num = f"#{index}" if index else ""
    desc = (ref.summary if ref.kind == "mface" else "") or caption
    return f"[{label}{num}:{desc}]" if desc else f"[{label}{num}]"


def expand_content(
    rec: MsgRecord,
    captions: dict[str, str] | None = None,
    numbering: dict[int, int] | None = None,
) -> str:
    """把正文里的媒体占位符替换成文字。numbering: media 下标 -> 图片编号。"""
    captions = captions or {}
    numbering = numbering or {}

    def repl(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx >= len(rec.media):
            return "[图片]"
        ref = rec.media[idx]
        return media_placeholder(ref, numbering.get(idx), captions.get(ref.md5, ""))

    return _TOKEN_RE.sub(repl, rec.content)


def record_line(
    rec: MsgRecord,
    content: str,
    *,
    show_roles: bool = True,
    show_titles: bool = False,
    hide_recalled: bool = False,
) -> str:
    """一条记录在群聊记录里编号和时间之后的部分。

    群事件写成"〔系统〕事件说明"，聊天消息写成"发言人: 正文"。
    """
    if rec.is_notice:
        return f"{NOTICE_MARK}{content}"
    if rec.recalled:
        content = RECALLED_HIDDEN if hide_recalled else f"{content}{RECALLED_MARK}"
    label = sender_label(rec, show_roles=show_roles, show_titles=show_titles)
    return f"{label}: {content}"


def build_transcript(
    records: list[MsgRecord],
    *,
    captions: dict[str, str] | None = None,
    image_slots: int = 0,
    image_aliases: set[int] | None = None,
    pending: set[int] | None = None,
    show_roles: bool = True,
    show_titles: bool = False,
    hide_recalled: bool = False,
) -> Transcript:
    """生成群聊记录文本。

    image_slots: 最多附带几张原图（取最新的）；附带的图在正文里标 [图片#k]。
    image_aliases: 只从这些消息里挑原图；None 表示不限。
    pending: 需要标【新】的消息编号（给判断 LLM 用）。
    hide_recalled: 已撤回的消息不显示原文，也不附带它的图片。
    """
    candidates: list[tuple[int, int, MediaRef]] = []
    for i, rec in enumerate(records):
        if image_aliases is not None and rec.alias not in image_aliases:
            continue
        if rec.recalled and hide_recalled:
            continue
        for j, ref in enumerate(rec.media):
            if ref.path and os.path.isfile(ref.path):
                candidates.append((i, j, ref))
    chosen = candidates[-image_slots:] if image_slots > 0 else []
    numbering: dict[int, dict[int, int]] = {}
    images: list[TranscriptImage] = []
    for k, (i, j, ref) in enumerate(chosen, start=1):
        numbering.setdefault(i, {})[j] = k
        images.append(TranscriptImage(index=k, path=ref.path))

    lines: list[str] = []
    last_date = ""
    for i, rec in enumerate(records):
        dt = datetime.fromtimestamp(rec.ts)
        date = dt.strftime("%Y-%m-%d")
        if date != last_date:
            lines.append(f"—— {date} ——")
            last_date = date
        mark = PENDING_MARK if pending and rec.alias in pending else ""
        content = expand_content(rec, captions, numbering.get(i))
        body = record_line(
            rec,
            content,
            show_roles=show_roles,
            show_titles=show_titles,
            hide_recalled=hide_recalled,
        )
        lines.append(f"{mark}#{rec.alias} [{dt.strftime('%H:%M:%S')}] {body}")
    return Transcript(text="\n".join(lines), images=images)
