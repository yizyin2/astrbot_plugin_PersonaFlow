"""/drh：删除当前会话最近 N 轮对话，并撤销上一次删除。

一轮 = 一条用户消息到下一条用户消息之前的全部消息（含工具调用），删除后不会留下半截上下文。
- 上下文库接管的会话删上下文库里本会话的轮次，其余会话删 AstrBot 自己的对话历史；
- 同步删除记忆库 Message 表里这几轮的对话记录（总结印象和全文记忆时读的就是它）；
- 可选在 QQ 上撤回机器人这几轮发出的回复；
- 删掉的内容原样存进撤销栈，/drh undo 放回原处（QQ 上撤回的消息恢复不了）；
  会话里一收到新消息（/drh 本身除外），就不能再撤销，免得放回的轮次和新的对话交错。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from astrbot.api import logger

from ..context.service import Binding, resolve_binding
from ..keys import CTX_INFO_KEY, MEMORY_ROW_KEY, ROUND_KEY
from .store import DrhStore, reply_hash

PREVIEW_CHARS = 1200
UNDO_WORDS = ("undo", "u", "撤销", "撤回", "恢复")
USAGE = "用法：/drh [N] 删除当前对话最近 N 轮（默认 1）；/drh undo 撤销上一次删除"
# QQ 消息时间和本机时间、发送前后的误差（秒）
SLACK_S = 3.0
_NORM = re.compile(r"[\s，。！？、；：,.!?;:~～…\"'“”‘’（）()\[\]【】<>《》「」『』\-—_*`#|]+")
_CQ = re.compile(r"\[CQ:[^\]]*\]")


class DrhError(Exception):
    """要直接回给用户的错误。"""


@dataclass
class Removed:
    """删掉的一轮。"""

    reply: str
    ctx_turn: int = 0
    sender_id: str = ""
    # 这一轮所有助手消息的文字。调用工具的轮次里，工具前说的话也会单独发成一条 QQ 消息
    texts: list[str] = field(default_factory=list)

    @property
    def sent_text(self) -> str:
        return "\n".join(self.texts) or self.reply


# ================= 纯函数 =================


def content_to_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p.strip() for p in parts if p.strip())
    return "" if content is None else str(content).strip()


def drop_recent_turns(history: list, count: int) -> tuple[list, int]:
    """去掉最近 count 轮，返回 (保留的部分, 实际去掉的轮数)。"""
    starts = [i for i, m in enumerate(history) if isinstance(m, dict) and m.get("role") == "user"]
    if not starts:
        return history, 0
    n = min(count, len(starts))
    return history[:starts[-n]], n


def split_turns(items: list) -> list[list]:
    turns: list[list] = []
    for item in items:
        if not turns or (isinstance(item, dict) and item.get("role") == "user"):
            turns.append([])
        turns[-1].append(item)
    return turns


def last_reply(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            text = content_to_text(m.get("content"))
            if text:
                return text
    return ""


def assistant_texts(messages: list) -> list[str]:
    return [t for t in (content_to_text(m.get("content")) for m in messages
                        if isinstance(m, dict) and m.get("role") == "assistant") if t]


def history_digest(history: list) -> str:
    return hashlib.sha1(json.dumps(history, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def truncate(text: str) -> str:
    if len(text) <= PREVIEW_CHARS:
        return text
    return f"{text[:PREVIEW_CHARS].rstrip()}\n...（已截断，原文 {len(text)} 字）"


def _norm(text: str) -> str:
    return _NORM.sub("", text or "")


def onebot_text(message: dict) -> str:
    """OneBot 历史消息里的文字部分。"""
    body = message.get("message")
    if isinstance(body, list):
        return "".join(str((seg.get("data") or {}).get("text") or "") for seg in body
                       if isinstance(seg, dict) and seg.get("type") == "text")
    if isinstance(body, str):
        return _CQ.sub("", body)
    return _CQ.sub("", str(message.get("raw_message") or ""))


def pick_bot_messages(history: list[dict], windows: list[dict], self_id: str,
                      recalled: set[str] | frozenset = frozenset()) -> tuple[list, int, int]:
    """在聊天记录里找出各轮机器人发出的回复。

    windows 每项含 ts_user（用户消息到达）、ts_done（模型回复完成）、ts_sent（回复发完）和 reply
    （这一轮所有助手消息的原文，包括调用工具前说的话）。
    带文字的消息要落在 [ts_user, ts_sent] 里且文字出自这一轮的回复；没有文字的（图片、语音、转发）
    只认 [ts_done, ts_sent] 这段发送时间。recalled 里的消息之前已经撤回过，算找到但不再撤回。
    返回 (要撤回的消息 ID, 一条都没找到的轮数, 之前已经撤回过的条数)。
    """
    picked: list = []
    seen: set = set()
    missing = 0
    for w in windows:
        target = _norm(w.get("reply") or "")
        hit = False
        for m in history:
            sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
            if str(sender.get("user_id") or m.get("user_id") or "") != str(self_id):
                continue
            mid = m.get("message_id")
            if mid is None or mid in seen:
                continue
            t = float(m.get("time") or 0)
            if not (w["ts_user"] - SLACK_S <= t <= w["ts_sent"] + SLACK_S):
                continue
            text = _norm(onebot_text(m))
            if text:
                if not target or text not in target:
                    continue
            elif t < w["ts_done"] - SLACK_S:
                continue
            seen.add(mid)
            hit = True
            if str(mid) not in recalled:
                picked.append(mid)
        if not hit:
            missing += 1
    return picked, missing, len(seen) - len(picked)


# ================= 服务 =================


class DrhService:
    def __init__(self, plugin, store: DrhStore) -> None:
        self.p = plugin
        self.store = store
        self._lock = asyncio.Lock()
        # 有撤销记录的会话，第一次收到消息时从库里读；每条消息都查库太浪费
        self._undo_umos: set[str] | None = None
        # 因为收到新消息而清空了撤销栈的会话，/drh undo 时据此说明原因
        self._expired: set[str] = set()

    @property
    def settings(self) -> dict:
        return self.p.settings.data.get("drh") or {}

    def _emit(self, message: str, level: str = "INFO", **data) -> None:
        try:
            self.p.bus.event("context", message, level=level, **data)
        except Exception:
            pass

    # ---------- 轮次账 ----------

    async def record(self, event, resp) -> None:
        """模型回复完成时记一轮。定时任务到点时的运行不算：它不进对话历史，提醒也不走正常发送。"""
        if resp is None or getattr(resp, "role", "") != "assistant":
            return
        if event.get_platform_name() == "cron":
            return
        mem = event.get_extra(MEMORY_ROW_KEY) or {}
        event.set_extra(MEMORY_ROW_KEY, None)
        info = event.get_extra(CTX_INFO_KEY)
        ctx_pool, ctx_turn = "", 0
        if isinstance(info, dict) and info.get("turn_id"):
            ctx_pool, ctx_turn = info["binding"].pool, int(info.pop("turn_id"))
        ts_user = float(getattr(getattr(event, "message_obj", None), "timestamp", 0) or 0)
        now = time.time()
        try:
            round_id = await self.store.add_round(
                str(event.unified_msg_origin), ts_user=ts_user or now, ts_done=now,
                profile_id=str(mem.get("profile_id") or ""), message_row=int(mem.get("row") or 0),
                ctx_pool=ctx_pool, ctx_turn=ctx_turn, reply=getattr(resp, "completion_text", "") or "")
        except Exception as e:
            logger.warning(f"[PersonaFlow] 记录 /drh 轮次失败: {e}")
            return
        event.set_extra(ROUND_KEY, [*(event.get_extra(ROUND_KEY) or []), round_id])

    async def on_sent(self, event) -> None:
        """回复发完时记下时间，撤回 QQ 消息时按它找。"""
        round_ids = event.get_extra(ROUND_KEY) or []
        for round_id in round_ids:
            try:
                await self.store.mark_sent(round_id)
            except Exception as e:
                logger.warning(f"[PersonaFlow] 记录回复发送时间失败: {e}")

    # ---------- 新消息 ----------

    @staticmethod
    def is_drh_command(event) -> bool:
        words = (event.get_message_str() or "").strip().split()
        return bool(event.is_at_or_wake_command and words and words[0].lstrip("/") == "drh")

    async def on_message(self, event) -> None:
        """会话里收到新消息（/drh 本身除外）：之前的删除不能再撤销。"""
        if self.is_drh_command(event):
            return
        umo = str(event.unified_msg_origin)
        if self._undo_umos is None:
            self._undo_umos = await self.store.undo_sessions()
        if umo not in self._undo_umos:
            return
        async with self._lock:
            removed = await self.store.clear_undo(umo)
            self._undo_umos.discard(umo)
            if removed:
                self._expired.add(umo)

    # ---------- 指令 ----------

    def denied(self, event) -> str | None:
        """共享的群聊会话里只有管理员能删。"""
        if not event.get_group_id() or event.is_admin():
            return None
        try:
            cfg = self.p.context.get_config(umo=event.unified_msg_origin)
            unique = bool(cfg["platform_settings"]["unique_session"])
        except Exception as e:
            logger.warning(f"[PersonaFlow] 读取会话隔离配置失败，按共享群聊处理: {e}")
            unique = False
        if unique:
            return None
        return f"当前是共享群聊会话，只有管理员可以删除或恢复聊天记录。你的 ID：{event.get_sender_id()}"

    async def handle(self, event, arg: str = "1") -> str:
        arg = str(arg if arg is not None else "1").strip().lower() or "1"
        refused = self.denied(event)
        if refused:
            return refused
        try:
            if arg in UNDO_WORDS:
                return await self.undo(event)
            try:
                count = int(arg)
            except ValueError:
                return USAGE
            if count <= 0:
                return f"参数错误：N 必须是大于 0 的整数。{USAGE}"
            return await self.delete(event, count)
        except DrhError as e:
            return str(e)
        except Exception as e:
            logger.error(f"[PersonaFlow] /drh {arg} 失败: {e}", exc_info=True)
            return f"❌ /drh 执行失败：{e}"

    async def _binding(self, event):
        """这个会话的对话历史存在哪里：返回 (记忆库引擎, 上下文库绑定)。"""
        memory = await self.p.router._select_memory(event)
        if memory is None or not self.p.enabled:
            return memory, None
        profile = self.p.settings.profile(str(memory.config.get("profile_id") or ""))
        binding = resolve_binding(profile, str(event.get_group_id() or ""),
                                  str(event.get_sender_id() or ""), str(event.unified_msg_origin))
        return memory, binding

    # ---------- 删除 ----------

    async def delete(self, event, count: int) -> str:
        umo = str(event.unified_msg_origin)
        async with self._lock:
            memory, binding = await self._binding(event)
            if binding is not None:
                removed, part, summary, preview = await self._drop_ctx(binding, umo, count)
            else:
                removed, part, summary, preview = await self._drop_astr(umo, count)
            payload: dict = {"history": part, "messages": [], "rounds": [], "recalled": 0}
            notes: list[str] = []
            try:
                rounds = await self._match_rounds(umo, binding, removed)
            except Exception as e:
                logger.error(f"[PersonaFlow] 读取 /drh 轮次账失败: {e}", exc_info=True)
                rounds = {}
            try:
                payload["messages"] = await self._drop_messages(event, memory, removed, rounds)
                n = sum(len(item["rows"]) for item in payload["messages"])
                notes.append(f"记忆库同步删除 {n} 条对话记录。" if n else "记忆库里没有这几轮的对话记录。")
            except Exception as e:
                logger.error(f"[PersonaFlow] /drh 删除记忆库对话记录失败: {e}", exc_info=True)
                notes.append(f"⚠️ 删除记忆库对话记录失败：{e}")
            if self.settings.get("recall_qq", True):
                note, payload["recalled"] = await self._recall(event, removed, rounds)
                if note:
                    notes.append(note)
            payload["rounds"] = list(rounds.values())
            try:
                await self.store.delete_rounds([r["id"] for r in payload["rounds"]])
            except Exception as e:
                logger.warning(f"[PersonaFlow] 清理 /drh 轮次账失败: {e}")
            who = f"{event.get_sender_name()}({event.get_sender_id()})"
            await self.store.push_undo(umo, who, payload, int(self.settings.get("undo_depth") or 10))
            if self._undo_umos is not None:
                self._undo_umos.add(umo)
            self._expired.discard(umo)
        self._emit(f"/drh 删除最近 {len(removed)} 轮 by {who}", session=umo,
                   turns=len(removed), messages=sum(len(i["rows"]) for i in payload["messages"]),
                   recalled=payload["recalled"])
        text = "\n".join([summary, *notes, "在这个会话收到新消息之前，发送 /drh undo 可以撤销这次删除。"])
        text += f"\n\n删除后最后一条 AI 输出：\n{truncate(preview)}" if preview else "\n\n删除后没有剩余 AI 输出。"
        return text

    async def _drop_ctx(self, binding: Binding, umo: str, count: int):
        store = self.p.ctx_store
        turns = await store.session_turns(binding.pool, umo, count)
        if not turns:
            raise DrhError("上下文库里当前会话没有可删除的对话（/ctx reset 之前的不算）。")
        await store.delete_turns([t.id for t in turns])
        left = await store.session_turns(binding.pool, umo, 1)
        removed = [Removed(t.assistant_text, t.id, t.sender_id, assistant_texts(t.messages)) for t in turns]
        where = "共享上下文库" if binding.mode == "shared" else "上下文库"
        summary = (f"已从{where}删除当前会话最近 {len(turns)} 轮对话"
                   f"（共 {sum(len(t.messages) for t in turns)} 条消息）。")
        part = {"kind": "ctx", "pool": binding.pool, "turns": [t.to_dict() for t in turns]}
        return removed, part, summary, left[0].assistant_text if left else ""

    async def _drop_astr(self, umo: str, count: int):
        conv_mgr = self.p.context.conversation_manager
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            raise DrhError("当前没有正在使用的对话，无法删除聊天记录。")
        conversation = await conv_mgr.get_conversation(umo, cid)
        if not conversation:
            raise DrhError("当前对话不存在，无法删除聊天记录。")
        try:
            history = json.loads(conversation.history or "[]")
        except (TypeError, ValueError) as e:
            logger.error(f"[PersonaFlow] 读取当前对话历史失败: {e}")
            raise DrhError("当前对话历史不是有效 JSON，已停止删除。") from e
        if not isinstance(history, list):
            raise DrhError("当前对话历史格式异常，已停止删除。")
        if not history:
            raise DrhError("当前对话没有可删除的聊天记录。")
        kept, n = drop_recent_turns(history, count)
        if n == 0:
            raise DrhError("当前对话没有可删除的用户对话轮次。")
        items = history[len(kept):]
        await conv_mgr.update_conversation(umo, cid, history=kept, token_usage=0)
        removed = [Removed(last_reply(t), texts=assistant_texts(t)) for t in reversed(split_turns(items))]
        summary = (f"已删除当前对话最近 {n} 轮聊天记录（共 {len(items)} 条 history 消息），"
                   f"剩余 {len(kept)} 条 history 消息。")
        part = {"kind": "astr", "cid": cid, "cut": len(kept), "prefix": history_digest(kept),
                "items": items}
        return removed, part, summary, last_reply(kept)

    async def _match_rounds(self, umo: str, binding: Binding | None, removed: list[Removed]) -> dict[int, dict]:
        """给每个删掉的轮次找到轮次账里的记录：{序号: 记录}。先按上下文库轮次 ID，再按回复摘要。"""
        found: dict[int, dict] = {}
        if binding is not None:
            by_turn = await self.store.rounds_for_turns(binding.pool, [r.ctx_turn for r in removed])
            for i, r in enumerate(removed):
                if r.ctx_turn in by_turn:
                    found[i] = by_turn[r.ctx_turn]
        used = {row["id"] for row in found.values()}
        recent = await self.store.recent_rounds(umo, len(removed) * 3 + 20)
        for i, r in enumerate(removed):
            if i in found or not r.reply:
                continue
            digest = reply_hash(r.reply)
            row = next((x for x in recent if x["id"] not in used and x["reply_hash"] == digest), None)
            if row is not None:
                found[i] = row
                used.add(row["id"])
        return found

    async def _drop_messages(self, event, memory, removed: list[Removed], rounds: dict[int, dict]) -> list[dict]:
        """删除这几轮在记忆库 Message 表里的记录，返回 [{"profile_id", "rows"}]。"""
        by_profile: dict[str, list[int]] = {}
        for row in rounds.values():
            if row["message_row"]:
                by_profile.setdefault(row["profile_id"], []).append(row["message_row"])
        # 轮次账里没有的（/drh 上线前的对话），在当前记忆库里按回复原文找
        orphans = [r for i, r in enumerate(removed) if i not in rounds]
        if orphans and memory is not None:
            pid = str(memory.config.get("profile_id") or "")
            private_sender = "" if event.get_group_id() else str(event.get_sender_id() or "")
            for qq in dict.fromkeys(r.sender_id or private_sender for r in orphans):
                replies = [r.reply for r in orphans if (r.sender_id or private_sender) == qq]
                ids = await memory.find_message_ids(replies, qq)
                by_profile.setdefault(pid, []).extend(ids)
        out = []
        for pid, ids in by_profile.items():
            engine = self._engine(pid, memory)
            if engine is None:
                logger.warning(f"[PersonaFlow] /drh：档案 {pid} 已不在使用，跳过它的 {len(ids)} 条对话记录")
                continue
            rows = await engine.pop_messages(ids)
            if rows:
                out.append({"profile_id": pid, "rows": rows})
        return out

    def _engine(self, profile_id: str, fallback=None):
        if fallback is not None and str(fallback.config.get("profile_id") or "") == profile_id:
            return fallback
        return self.p.router.engine_by_profile_id(profile_id)

    async def _recall(self, event, removed: list[Removed], rounds: dict[int, dict]) -> tuple[str, int]:
        """在 QQ 上撤回这几轮机器人发出的回复。返回 (说明, 撤回条数)。"""
        windows = [{**rounds[i], "reply": r.sent_text} for i, r in enumerate(removed)
                   if i in rounds and rounds[i]["ts_sent"]]
        bot = getattr(event, "bot", None)
        if event.get_platform_name() != "aiocqhttp" or bot is None:
            return "", 0
        if not windows:
            return "QQ 上没有找到这几轮的发送记录（/drh 上线前的对话或回复没发出去），未撤回消息。", 0
        try:
            history = await self._chat_history(bot, event, len(removed))
        except Exception as e:
            logger.warning(f"[PersonaFlow] /drh 读取 QQ 聊天记录失败: {e}")
            return f"⚠️ 读取 QQ 聊天记录失败，未撤回消息：{e}", 0
        try:
            recalled = await self.store.recalled_among([m.get("message_id") for m in history])
        except Exception as e:
            logger.warning(f"[PersonaFlow] /drh 读取已撤回记录失败: {e}")
            recalled = set()
        ids, missing, already = pick_bot_messages(history, windows, str(event.get_self_id() or ""), recalled)
        done, failed = [], 0
        for mid in ids:
            try:
                await bot.call_action("delete_msg", message_id=mid)
                done.append(mid)
            except Exception as e:
                failed += 1
                logger.info(f"[PersonaFlow] /drh 撤回消息 {mid} 失败: {e}")
        try:
            await self.store.add_recalled(done)
        except Exception as e:
            logger.warning(f"[PersonaFlow] /drh 记录已撤回消息失败: {e}")
        ok = len(done)
        parts = [f"QQ 上撤回了 {ok} 条机器人消息"]
        if already:
            parts.append(f"{already} 条之前已经撤回过")
        if failed:
            parts.append(f"{failed} 条撤回失败（可能超过了 2 分钟）")
        if missing:
            parts.append(f"{missing} 轮在聊天记录里没找到")
        if len(windows) < len(removed):
            parts.append(f"{len(removed) - len(windows)} 轮没有发送记录")
        return "，".join(parts) + "。", ok

    @staticmethod
    async def _chat_history(bot, event, turns: int) -> list[dict]:
        count = max(30, min(200, turns * 20))
        group_id = str(event.get_group_id() or "")
        if group_id:
            data = await bot.call_action("get_group_msg_history", group_id=int(group_id), count=count)
        else:
            data = await bot.call_action("get_friend_msg_history",
                                         user_id=int(event.get_sender_id()), count=count)
        messages = data.get("messages") if isinstance(data, dict) else None
        return [m for m in messages or [] if isinstance(m, dict)]

    # ---------- 撤销 ----------

    async def undo(self, event) -> str:
        umo = str(event.unified_msg_origin)
        async with self._lock:
            item = await self.store.peek_undo(umo)
            if item is None:
                if umo in self._expired:
                    return "删除之后这个会话又收到了新消息，之前的 /drh 删除已经不能撤销了。"
                return "当前会话没有可以撤销的 /drh 删除。"
            payload = item["payload"] or {}
            lines: list[str] = []
            part = payload.get("history") or {}
            if part.get("kind") == "ctx":
                n = await self.p.ctx_store.restore_turns(part.get("turns") or [])
                lines.append(f"上下文库恢复了 {n} 轮对话。")
            elif part.get("kind") == "astr":
                lines.append(await self._restore_astr(umo, part))
            preview = await self._undo_preview(umo, part)
            restored, lost = 0, 0
            for chunk in payload.get("messages") or []:
                engine = self._engine(str(chunk.get("profile_id") or ""))
                if engine is None:
                    lost += len(chunk.get("rows") or [])
                    continue
                restored += await engine.restore_messages(chunk.get("rows") or [])
            if restored or lost:
                lines.append(f"记忆库恢复了 {restored} 条对话记录。"
                             + (f"另有 {lost} 条所属的档案已不在使用，没法放回。" if lost else ""))
            try:
                await self.store.restore_rounds(payload.get("rounds") or [])
            except Exception as e:
                logger.warning(f"[PersonaFlow] 恢复 /drh 轮次账失败: {e}")
            if payload.get("recalled"):
                lines.append(f"QQ 上已撤回的 {payload['recalled']} 条消息没法恢复。")
            await self.store.drop_undo(item["id"])
            left = await self.store.count_undo(umo)
            if not left and self._undo_umos is not None:
                self._undo_umos.discard(umo)
        lines.append(f"还可以再撤销 {left} 次。" if left else "没有更早的删除可以撤销了。")
        self._emit(f"/drh undo by {event.get_sender_name()}({event.get_sender_id()})", session=umo)
        text = "已撤销上一次 /drh 删除。\n" + "\n".join(lines)
        text += f"\n\n撤销后最后一条 AI 输出：\n{truncate(preview)}" if preview else "\n\n撤销后没有 AI 输出。"
        return text

    async def _undo_preview(self, umo: str, part: dict) -> str:
        """撤销后这段对话里最后一条 AI 输出：上下文库看本会话断点之后的最后一轮，AstrBot 历史看放回的那个对话。"""
        try:
            if part.get("kind") == "ctx":
                last = await self.p.ctx_store.session_turns(part.get("pool") or "", umo, 1)
                return last[0].assistant_text if last else ""
            if part.get("kind") == "astr" and part.get("cid"):
                conversation = await self.p.context.conversation_manager.get_conversation(umo, part["cid"])
                history = json.loads(conversation.history or "[]") if conversation else []
                return last_reply(history) if isinstance(history, list) else ""
        except Exception as e:
            logger.warning(f"[PersonaFlow] 读取撤销后的最后一条 AI 输出失败: {e}")
        return ""

    async def _restore_astr(self, umo: str, part: dict) -> str:
        conv_mgr = self.p.context.conversation_manager
        cid = part.get("cid")
        conversation = await conv_mgr.get_conversation(umo, cid) if cid else None
        if not conversation:
            return "⚠️ 原来的对话已经不存在，对话历史没法恢复。"
        try:
            history = json.loads(conversation.history or "[]")
        except (TypeError, ValueError):
            history = None
        cut = int(part.get("cut") or 0)
        if not isinstance(history, list) or len(history) < cut or history_digest(history[:cut]) != part.get("prefix"):
            return "⚠️ 删除之后这段对话历史被改动过（例如被重置、压缩或又删过更早的轮次），对话历史没法放回原位。"
        items = part.get("items") or []
        await conv_mgr.update_conversation(umo, cid, history=history[:cut] + items + history[cut:])
        tail = len(history) - cut
        where = "" if await conv_mgr.get_curr_conversation_id(umo) == cid else "（放回的是原来那个对话，不是当前对话）"
        after = f"，排在删除后新聊的 {len(split_turns(history[cut:]))} 轮之前" if tail else ""
        return f"对话历史恢复了 {len(split_turns(items))} 轮（{len(items)} 条消息）{after}{where}。"
