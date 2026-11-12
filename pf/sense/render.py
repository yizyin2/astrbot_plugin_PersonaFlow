"""把一条 QQ 群消息渲染成群聊记录里的一行正文。

aiocqhttp 平台优先读 raw_message 里 NapCat 的原始消息段：AstrBot 的组件会丢掉
图片的 sub_type/summary，也不会把 face、商城表情放进 message_str。
At 名字、引用内容这类适配器已经额外查询过的信息仍从 AstrBot 组件取。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from astrbot.api.message_components import (
    At,
    AtAll,
    Face,
    File,
    Forward,
    Image,
    Json,
    Plain,
    Record,
    Reply,
    Video,
)

from .models import MediaRef, media_token

# 引用摘要、消息正文的最大长度
REPLY_SUMMARY_LEN = 60
CONTENT_MAX_LEN = 600

_MULTI_SPACE = re.compile(r"(?<=\S)[ \t]{2,}")

# 常见 QQ 小黄脸 id → 名称。NapCat 的 face 段通常带 raw.faceText，优先用那个。
FACE_NAMES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞",
    7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙",
    14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐", 20: "偷笑", 21: "可爱",
    22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 35: "折磨",
    36: "衰", 37: "骷髅", 38: "敲打", 39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳",
    46: "猪头", 49: "拥抱", 53: "蛋糕", 54: "闪电", 55: "炸弹", 56: "刀", 57: "足球",
    59: "便便", 60: "咖啡", 61: "饭", 63: "玫瑰", 64: "凋谢", 66: "爱心", 67: "心碎",
    69: "礼物", 74: "太阳", 75: "月亮", 76: "赞", 77: "踩", 78: "握手", 79: "胜利",
    85: "飞吻", 86: "怄火", 89: "西瓜", 96: "冷汗", 97: "擦汗", 98: "抠鼻", 99: "鼓掌",
    100: "糗大了", 101: "坏笑", 102: "左哼哼", 103: "右哼哼", 104: "哈欠", 105: "鄙视",
    106: "委屈", 107: "快哭了", 108: "阴险", 109: "亲亲", 110: "吓", 111: "可怜",
    112: "菜刀", 113: "啤酒", 114: "篮球", 115: "乒乓", 116: "示爱", 117: "瓢虫",
    118: "抱拳", 119: "勾引", 120: "拳头", 121: "差劲", 122: "爱你", 123: "NO",
    124: "OK", 125: "转圈", 126: "磕头", 127: "回头", 128: "跳绳", 129: "挥手",
    171: "茶", 172: "眨眼睛", 173: "泪奔", 174: "无奈", 175: "卖萌", 176: "小纠结",
    177: "喷血", 178: "斜眼笑", 179: "doge", 180: "惊喜", 181: "骚扰", 182: "笑哭",
    183: "我最美", 201: "点赞", 212: "托腮", 262: "脑阔疼", 263: "沧桑", 264: "捂脸",
    265: "辣眼睛", 266: "哦哟", 267: "头秃", 268: "问号脸", 269: "暗中观察",
    270: "emm", 271: "吃瓜", 272: "呵呵哒", 273: "我酸了", 277: "汪汪", 281: "无眼笑",
    282: "敬礼", 283: "狂笑", 284: "面无表情", 285: "摸鱼", 286: "魔鬼笑", 287: "哦",
    288: "请", 289: "睁眼", 293: "摸锦鲤", 294: "期待", 297: "拜谢", 299: "牛啊",
    305: "右亲亲", 306: "牛气冲天", 307: "喵喵", 311: "打call", 314: "仔细分析",
    318: "崇拜", 319: "比心", 320: "庆祝", 323: "嫌弃", 324: "吃糖", 325: "惊吓",
    326: "生气", 337: "花朵脸", 338: "我想开了", 339: "舔屏", 341: "打招呼",
    342: "酸Q", 343: "我方了", 344: "大怨种", 346: "你真棒棒", 349: "坚强",
    350: "贴贴", 351: "敲敲", 352: "咦", 353: "拜托", 354: "尊嘟假嘟", 355: "耶",
    356: "666", 357: "裂开",
}

# (msg_id, sender_id, quoted_text) -> 群内编号；找不到返回 None
AliasResolver = Callable[[str, str, str], Awaitable[int | None]]
# (kind, segment_data, Image 组件或 None) -> MediaRef
MediaIngestor = Callable[[str, dict, Image | None], Awaitable[MediaRef]]


@dataclass
class RenderResult:
    content: str
    at_bot: bool = False
    reply_bot: bool = False
    media: list[MediaRef] = field(default_factory=list)


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def face_name(data: dict) -> str:
    raw = data.get("raw")
    text = ""
    if isinstance(raw, dict):
        text = str(raw.get("faceText") or "")
    text = text or str(data.get("faceText") or "")
    text = text.strip().lstrip("/")
    if text and text != "[表情]":
        return text
    try:
        return FACE_NAMES.get(int(data.get("id")), f"表情{data.get('id')}")
    except (TypeError, ValueError):
        return "表情"


def classify_image(seg_type: str, data: dict) -> str:
    if seg_type == "mface" or data.get("emoji_id") or data.get("emoji_package_id"):
        return "mface"
    if str(data.get("sub_type", "0")) == "1":
        return "sticker"
    return "image"


def clean_summary(summary) -> str:
    """NapCat 的 summary 形如 "[动画表情]"、"[狗头]"，去掉方括号；通用占位词丢弃。"""
    text = str(summary or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if text in ("", "图片", "动画表情", "表情"):
        return ""
    return text


def card_text(payload) -> str:
    """QQ 分享卡片（json 段）提取标题/描述，同内置群聊上下文的做法。"""
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return "[卡片消息]"
    if isinstance(data, dict) and isinstance(data.get("data"), str):
        try:
            nested = json.loads(data["data"])
            if isinstance(nested, dict):
                data = nested
        except json.JSONDecodeError:
            pass
    detail = {}
    if isinstance(data, dict):
        meta = data.get("meta")
        if isinstance(meta, dict):
            for value in meta.values():
                if isinstance(value, dict) and (value.get("title") or value.get("desc")):
                    detail = value
                    break
    fields = [
        truncate(str(v), 80)
        for v in (detail.get("title"), detail.get("desc"))
        if isinstance(v, str) and v.strip()
    ]
    if not fields and isinstance(data, dict) and isinstance(data.get("prompt"), str):
        fields = [truncate(data["prompt"], 80)]
    return f"[分享卡片:{' | '.join(fields)}]" if fields else "[分享卡片]"


def describe_chain(chain) -> str:
    """把 AstrBot 组件链概括成一段纯文本，用于引用摘要。"""
    parts = []
    for comp in chain or []:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif isinstance(comp, Image):
            parts.append("[图片]")
        elif isinstance(comp, AtAll) or (isinstance(comp, At) and str(comp.qq) == "all"):
            parts.append("@全体成员")
        elif isinstance(comp, At):
            parts.append(f"@{comp.name or comp.qq}")
        elif isinstance(comp, Face):
            parts.append(f"[表情:{FACE_NAMES.get(comp.id, comp.id)}]")
        elif isinstance(comp, Record):
            parts.append("[语音]")
        elif isinstance(comp, Video):
            parts.append("[视频]")
        elif isinstance(comp, File):
            parts.append(f"[文件:{comp.name or ''}]")
        elif isinstance(comp, Forward):
            parts.append("[合并转发]")
        elif isinstance(comp, Json):
            parts.append(card_text(comp.data))
        elif isinstance(comp, Reply):
            continue
        else:
            parts.append(f"[{type(comp).__name__}]")
    return "".join(parts)


def _components_to_segments(components) -> list[dict]:
    """非 aiocqhttp 平台没有 raw 消息段时，用 AstrBot 组件拼出等价的段。"""
    segs = []
    for comp in components:
        if isinstance(comp, Plain):
            segs.append({"type": "text", "data": {"text": comp.text}})
        elif isinstance(comp, At):
            segs.append({"type": "at", "data": {"qq": str(comp.qq)}})
        elif isinstance(comp, Image):
            segs.append({"type": "image", "data": {}})
        elif isinstance(comp, Face):
            segs.append({"type": "face", "data": {"id": comp.id}})
        elif isinstance(comp, Reply):
            segs.append({"type": "reply", "data": {"id": str(comp.id)}})
        elif isinstance(comp, Record):
            segs.append({"type": "record", "data": {}})
        elif isinstance(comp, Video):
            segs.append({"type": "video", "data": {}})
        elif isinstance(comp, File):
            segs.append({"type": "file", "data": {"name": comp.name}})
        elif isinstance(comp, Forward):
            segs.append({"type": "forward", "data": {}})
        elif isinstance(comp, Json):
            segs.append({"type": "json", "data": {"data": comp.data}})
    return segs


async def render_incoming(
    segments: list[dict] | None,
    components: list,
    self_id: str,
    *,
    resolve_alias: AliasResolver,
    ingest_media: MediaIngestor,
) -> RenderResult:
    """渲染一条群友发来的消息。"""
    self_id = str(self_id)
    if not segments:
        segments = _components_to_segments(components)
    images = [c for c in components if isinstance(c, Image)]
    at_names = {str(c.qq): (c.name or "") for c in components if isinstance(c, At)}
    reply_comp = next((c for c in components if isinstance(c, Reply)), None)

    result = RenderResult(content="")
    parts: list[str] = []
    prefix = ""
    image_idx = 0

    for seg in segments:
        seg_type = str(seg.get("type", ""))
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq == "all":
                parts.append("@全体成员 ")
            elif qq == self_id:
                result.at_bot = True
                parts.append("@你 ")
            else:
                name = at_names.get(qq) or str(data.get("name") or "")
                parts.append(f"@{name}({qq}) " if name else f"@{qq} ")
        elif seg_type == "reply":
            prefix = await _render_reply(data, reply_comp, self_id, resolve_alias, result)
        elif seg_type in ("image", "mface"):
            comp = None
            if seg_type == "image":
                comp = images[image_idx] if image_idx < len(images) else None
                image_idx += 1
            kind = classify_image(seg_type, data)
            ref = await ingest_media(kind, data, comp)
            result.media.append(ref)
            parts.append(media_token(len(result.media) - 1))
        elif seg_type == "face":
            parts.append(f"[表情:{face_name(data)}]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "file":
            parts.append(f"[文件:{data.get('name') or data.get('file') or ''}]")
        elif seg_type in ("forward", "node"):
            parts.append("[合并转发]")
        elif seg_type == "json":
            parts.append(card_text(data.get("data")))
        elif seg_type == "xml":
            parts.append("[卡片消息]")
        elif seg_type == "poke":
            parts.append("[戳一戳]")
        elif seg_type == "dice":
            parts.append(f"[骰子:{data.get('result', '?')}]")
        elif seg_type == "rps":
            parts.append("[猜拳]")
        elif seg_type == "markdown":
            parts.append(str(data.get("markdown") or data.get("content") or ""))
        elif seg_type in ("share", "music", "location", "contact"):
            title = data.get("title") or ""
            parts.append(f"[分享:{title}]" if title else "[分享]")
        elif seg_type:
            parts.append(f"[{seg_type}]")

    body = "".join(parts).strip()
    # 多行消息的续行缩进，保证群聊记录里"一条消息一行起头"；@ 后自带的空格会叠成两个，合并掉
    body = "\n    ".join(
        _MULTI_SPACE.sub(" ", line.rstrip()) for line in body.splitlines()
    )
    if len(body) > CONTENT_MAX_LEN:
        body = body[:CONTENT_MAX_LEN] + "…（过长截断）"
    result.content = prefix + body
    return result


async def _render_reply(
    data: dict,
    reply_comp: Reply | None,
    self_id: str,
    resolve_alias: AliasResolver,
    result: RenderResult,
) -> str:
    reply_id = str(data.get("id") or (reply_comp.id if reply_comp else ""))
    if reply_comp is None or not (reply_comp.chain or reply_comp.message_str):
        alias = await resolve_alias(reply_id, "", "")
        return f"[引用 #{alias}] " if alias else "[引用了一条消息] "

    quoted = reply_comp.message_str or describe_chain(reply_comp.chain)
    summary = truncate(quoted, REPLY_SUMMARY_LEN)
    sender_id = str(reply_comp.sender_id or "")
    alias = await resolve_alias(reply_id, sender_id, quoted)
    ref = f" #{alias}" if alias else ""
    if sender_id == self_id:
        result.reply_bot = True
        return f"[回复了你的消息{ref}「{summary}」] "
    name = reply_comp.sender_nickname or sender_id
    return f"[引用{ref} {name}:「{summary}」] "


def render_bot_chain(
    chain, at_name: Callable[[str], str], reply_prefix: str = ""
) -> str:
    """渲染 bot 自己发出的消息链。bot 的图片只记占位符，不落盘。

    reply_prefix: 链里有 Reply 时由调用方给出的引用说明，例如 "[引用 #12] "。
    """
    parts = [reply_prefix] if reply_prefix else []
    for comp in chain or []:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif isinstance(comp, Reply):
            continue
        elif isinstance(comp, At):
            qq = str(comp.qq)
            parts.append("@全体成员 " if qq == "all" else f"@{at_name(qq)}({qq}) ")
        elif isinstance(comp, Image):
            parts.append("[图片]")
        elif isinstance(comp, Face):
            parts.append(f"[表情:{FACE_NAMES.get(comp.id, comp.id)}]")
        elif isinstance(comp, Record):
            parts.append("[语音]")
        elif isinstance(comp, Video):
            parts.append("[视频]")
        elif isinstance(comp, File):
            parts.append(f"[文件:{comp.name or ''}]")
        else:
            nested = getattr(comp, "content", None)
            if isinstance(nested, list):  # 长消息被包成转发节点
                parts.append(render_bot_chain(nested, at_name))
            else:
                parts.append(f"[{type(comp).__name__}]")
    body = "".join(parts).strip()
    body = "\n    ".join(line.rstrip() for line in body.splitlines())
    if len(body) > CONTENT_MAX_LEN:
        body = body[:CONTENT_MAX_LEN] + "…（过长截断）"
    return body
