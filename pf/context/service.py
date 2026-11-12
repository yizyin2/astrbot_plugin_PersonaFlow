"""独立上下文库的接管流程。

请求前用上下文库里的最近几轮替换 AstrBot 的对话历史；agent 开始时让 AstrBot 放弃保存
这次对话；agent 结束后把这一轮写进上下文库。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from astrbot.api import logger

from ..keys import CTX_INFO_KEY, CTX_TAKEOVER_KEY
from .store import ContextStore, Turn

GUARD_PROMPT = (
    "你的对话历史来自多个场合（不同的群聊和私聊），每一轮开头都标了来源。"
    "回应时注意现在是在哪个场合；不要主动把私聊里的内容透露到群里。"
)


@dataclass
class Binding:
    pool: str
    mode: str
    profile_id: str
    private_visible_in_groups: bool = True


def session_key_of(group_id: str, sender_id: str) -> str:
    return f"group:{group_id}" if group_id else f"private:{sender_id}"


def resolve_binding(profile: dict | None, group_id: str, sender_id: str, umo: str) -> Binding | None:
    """按档案的上下文设置决定这个会话用哪个池；不接管返回 None。"""
    if not profile:
        return None
    ctx = profile.get("context") or {}
    key = session_key_of(group_id, sender_id)
    override = (ctx.get("sessions") or {}).get(key) or {}
    mode = override.get("mode") or ctx.get("mode") or "astrbot"
    if mode == "shared":
        pool = f"{profile['id']}/{override.get('pool') or ctx.get('pool') or 'main'}"
    elif mode == "isolated":
        pool = f"{profile['id']}/@{umo or key}"
    else:
        return None
    return Binding(pool, mode, profile["id"], bool(ctx.get("private_visible_in_groups", True)))


def select_window(turns: list[Turn], *, max_turns: int, max_chars: int, current_umo: str = "",
                  is_group: bool = False, private_visible: bool = True,
                  managed_since: float | None = None, keep_same_group_turns: int = 0) -> list[Turn]:
    """从候选轮次里挑出这次请求要带上的窗口。"""
    if is_group and not private_visible:
        turns = [t for t in turns if t.source_type != "private"]
    if managed_since is not None:
        # 受管群：落在群聊记录时间窗口内的本群轮次已经能在群聊记录里看到，只留最近几轮
        dup = [t for t in turns if t.source_umo == current_umo and t.ts >= managed_since]
        keep = {t.id for t in dup[-keep_same_group_turns:]} if keep_same_group_turns > 0 else set()
        drop = {t.id for t in dup} - keep
        turns = [t for t in turns if t.id not in drop]
    turns = turns[-max_turns:]
    total, start = 0, len(turns)
    for index in range(len(turns) - 1, -1, -1):
        total += turns[index].chars
        if total > max_chars and start < len(turns):
            break
        start = index
    return turns[start:]


def sanitize_messages(messages: list[dict], strip_images: bool) -> list[dict]:
    """去掉不该进库的内容：检查点、图片和语音数据。"""
    out = []
    for message in messages:
        if message.get("role") in ("_checkpoint", "system"):
            continue
        message = dict(message)
        message.pop("_no_save", None)
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "image_url" and strip_images:
                    parts.append({"type": "text", "text": "[图片]"})
                elif kind == "audio_url":
                    parts.append({"type": "text", "text": "[语音]"})
                else:
                    parts.append({k: v for k, v in part.items() if k != "_no_save"})
            if not parts and message.get("role") != "assistant":
                continue
            message["content"] = parts
        out.append(message)
    return out


class ContextService:
    def __init__(self, store: ContextStore, bus=None) -> None:
        self.store = store
        self.bus = bus
        self.settings: dict = {}

    def configure(self, settings: dict) -> None:
        self.settings = dict(settings or {})

    def _emit(self, message: str, level: str = "INFO", **data) -> None:
        if self.bus is not None:
            try:
                self.bus.event("context", message, level=level, **data)
            except Exception:
                pass

    async def preview(self, binding: Binding, *, current_umo: str = "", is_group: bool = False,
                      managed_since: float | None = None, keep_same_group_turns: int = 0) -> list[Turn]:
        max_turns = int(self.settings.get("window_turns") or 30)
        candidates = await self.store.recent(binding.pool, max_turns * 4 + 20)
        return select_window(
            candidates, max_turns=max_turns,
            max_chars=int(self.settings.get("window_chars") or 24000),
            current_umo=current_umo, is_group=is_group,
            private_visible=binding.private_visible_in_groups,
            managed_since=managed_since, keep_same_group_turns=keep_same_group_turns)

    async def before_request(self, event, req, profile: dict | None, *, source: dict,
                             managed: bool = False, managed_since: float | None = None,
                             keep_same_group_turns: int = 0) -> bool:
        """接管这次请求的对话历史。返回是否接管。"""
        if event.get_extra(CTX_INFO_KEY):
            return True
        group_id = str(event.get_group_id() or "")
        umo = str(event.unified_msg_origin)
        binding = resolve_binding(profile, group_id, str(event.get_sender_id() or ""), umo)
        if binding is None:
            return False
        try:
            turns = await self.preview(
                binding, current_umo=umo, is_group=bool(group_id),
                managed_since=managed_since if managed else None,
                keep_same_group_turns=keep_same_group_turns)
        except Exception as e:
            logger.error(f"[PersonaFlow] 读取上下文库失败，本次不接管: {e}", exc_info=True)
            self._emit(f"读取上下文库失败：{e}", level="ERROR", session=umo)
            return False
        contexts = req.contexts if isinstance(req.contexts, list) else []
        n_begin = 0
        if req.conversation is not None:
            try:
                history = json.loads(req.conversation.history or "[]")
                if isinstance(history, list):
                    n_begin = max(0, len(contexts) - len(history))
            except (TypeError, ValueError):
                n_begin = 0
        req.contexts = list(contexts[:n_begin]) + [m for t in turns for m in t.messages]
        shared = binding.mode == "shared"
        if shared and self.settings.get("source_prefix", True) and not managed and source.get("label"):
            req.prompt = f"{source['label']}\n{req.prompt or ''}"
        if shared and self.settings.get("guard_prompt", True):
            if GUARD_PROMPT not in (req.system_prompt or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{GUARD_PROMPT}"
        event.set_extra(CTX_TAKEOVER_KEY, True)
        event.set_extra(CTX_INFO_KEY, {"binding": binding, "req": req, "source": source,
                                       "window": len(turns)})
        self._emit(
            f"接管上下文：池 {binding.pool}，带上 {len(turns)} 轮",
            session=umo, pool=binding.pool, mode=binding.mode, turns=len(turns),
            chars=sum(t.chars for t in turns))
        return True

    def on_agent_begin(self, event, run_context) -> None:
        """记下本轮的起点，并让 AstrBot 不再把这次对话写进自己的库。"""
        info = event.get_extra(CTX_INFO_KEY)
        if not isinstance(info, dict) or "run_context" in info:
            return
        messages = getattr(run_context, "messages", None) or []
        info["run_context"] = run_context
        info["marker"] = messages[-1] if messages else None
        req = info.get("req")
        if req is not None:
            req.conversation = None

    async def on_agent_done(self, event, run_context, resp) -> None:
        """把这一轮写进上下文库。模型出错的轮次不保存。"""
        info = event.get_extra(CTX_INFO_KEY)
        if not isinstance(info, dict) or info.get("saved") or info.get("run_context") is not run_context:
            return
        if resp is None or getattr(resp, "role", "") != "assistant":
            return
        binding: Binding = info["binding"]
        source = info.get("source") or {}
        umo = str(event.unified_msg_origin)
        try:
            from astrbot.core.agent.message import dump_messages_with_checkpoints

            messages = list(run_context.messages)
            marker = info.get("marker")
            start = next((i for i, m in enumerate(messages) if m is marker), None)
            if start is None:
                start = next((i for i in range(len(messages) - 1, -1, -1)
                              if messages[i].role == "user"), None)
            if start is None:
                return
            turn = [m for m in messages[start:]
                    if not (m.role in ("assistant", "user") and getattr(m, "_no_save", False))]
            dumped = sanitize_messages(
                dump_messages_with_checkpoints(turn), bool(self.settings.get("strip_images", True)))
            if not any(m.get("role") == "assistant" for m in dumped):
                return
            turn_id = await self.store.append_turn(
                binding.pool, dumped, ts=time.time(), source_umo=umo,
                source_type=source.get("type", ""), source_id=source.get("id", ""),
                source_name=source.get("name", ""), platform_id=source.get("platform_id", ""),
                sender_id=source.get("sender_id", ""), sender_name=source.get("sender_name", ""))
            info["saved"] = True
            self._emit(f"保存一轮对话到池 {binding.pool}", level="DEBUG", session=umo,
                       pool=binding.pool, turn_id=turn_id, messages=len(dumped))
        except Exception as e:
            logger.error(f"[PersonaFlow] 写入上下文库失败: {e}", exc_info=True)
            self._emit(f"写入上下文库失败：{e}", level="ERROR", session=umo)

    async def reset(self, pool: str, reason: str) -> dict:
        mark = await self.store.add_mark(pool, reason)
        self._emit(f"池 {pool} 打了断点：{reason}", pool=pool)
        return mark
