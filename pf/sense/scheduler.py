"""每群一个调度协程：逐条判断，突发或忙碌时合并成一批判断。

事件 handler 把消息 submit 进来后 await 返回的 Future：
- None：这条不需要回复；
- ReplyPlan：这条事件被选为"载体"，由它去调用主 LLM。回复发出后 handler 设置
  plan.done，worker 才开始判断下一批，保证判断时能看到 bot 刚说过的话。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .models import MsgRecord, ReplyPlan


@dataclass
class Pending:
    record: MsgRecord
    event: Any
    future: asyncio.Future
    arrived: float


@dataclass
class SchedulerConfig:
    burst_window_s: float = 10.0
    burst_threshold: int = 5
    debounce_s: float = 2.5
    max_batch_wait_s: float = 8.0
    reply_timeout_s: float = 180.0
    # 每条消息都先等的安静时长：期间又来消息就重新计时，用来把"一句话 + 紧跟的表情包"并成一批
    wait_s: float = 0.0


# (group_key, 本批待判消息) -> 回复计划；None 表示保持沉默
JudgeFn = Callable[[str, list[Pending]], Awaitable[ReplyPlan | None]]


class GroupWorker:
    def __init__(self, key: str, judge_fn: JudgeFn, cfg: SchedulerConfig) -> None:
        self.key = key
        self.cfg = cfg
        self._judge_fn = judge_fn
        self.state = "idle"
        self._pending: list[Pending] = []
        self._wakeup = asyncio.Event()
        self._arrivals: deque[float] = deque(maxlen=500)
        self._merge_next = False
        self.batches = 0
        self.merged_batches = 0
        self.replies = 0
        self._task = asyncio.create_task(self._run())

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def submit(self, record: MsgRecord, event: Any) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        now = loop.time()
        fut = loop.create_future()
        self._pending.append(Pending(record, event, fut, now))
        self._arrivals.append(now)
        if self.state != "idle":
            # 判断或主 LLM 回复进行中又来了消息：下一轮合并判断
            self._merge_next = True
        self._wakeup.set()
        return fut

    def cancel(self, alias: int) -> bool:
        """把一条还没判断的消息从待判队列里拿掉（例如它被撤回了）。

        已经在判断或回复中的消息不受影响。返回是否真的拿掉了一条。
        """
        for pending in self._pending:
            if pending.record.alias == alias:
                self._pending.remove(pending)
                if not pending.future.done():
                    pending.future.set_result(None)
                return True
        return False

    def _is_burst(self, now: float) -> bool:
        if self.cfg.burst_threshold <= 0:
            return False
        recent = sum(1 for t in self._arrivals if now - t <= self.cfg.burst_window_s)
        return recent >= self.cfg.burst_threshold

    async def _debounce(self, quiet_s: float) -> None:
        """等到安静 quiet_s 秒，但从第一条待判消息起最多等 max_batch_wait_s。

        max_batch_wait_s 比 quiet_s 还短时以 quiet_s 为准，保证每条消息至少等满 quiet_s。
        """
        loop = asyncio.get_running_loop()
        cap = max(self.cfg.max_batch_wait_s, quiet_s)
        while self._pending:
            first = min(p.arrived for p in self._pending)
            last = max(p.arrived for p in self._pending)
            until = min(last + quiet_s, first + cap)
            delay = until - loop.time()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def _sender_ready_at(self) -> dict[str, float]:
        """每个群友的待判消息何时可以判断：本人安静满 wait_s 秒，或本人第一条起满上限。"""
        cap = max(self.cfg.max_batch_wait_s, self.cfg.wait_s)
        first: dict[str, float] = {}
        last: dict[str, float] = {}
        for p in self._pending:
            sender = p.record.sender_id
            first[sender] = min(first.get(sender, p.arrived), p.arrived)
            last[sender] = max(last.get(sender, p.arrived), p.arrived)
        return {s: min(last[s] + self.cfg.wait_s, first[s] + cap) for s in first}

    async def _take_ready_senders(self) -> list[Pending]:
        """按人计时：只有本人的新消息会让本人重新计时，别人说话不影响。

        取出已经等完的群友的全部待判消息；还在等的人留在队列里，等完后单独判断。
        """
        loop = asyncio.get_running_loop()
        while self._pending:
            ready_at = self._sender_ready_at()
            now = loop.time()
            soonest = min(ready_at.values())
            if soonest > now:
                await asyncio.sleep(soonest - now)
                continue
            ready = {s for s, t in ready_at.items() if t <= now}
            batch = [p for p in self._pending if p.record.sender_id in ready]
            self._pending = [p for p in self._pending if p.record.sender_id not in ready]
            return batch
        return []

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            if not self._pending:
                continue
            try:
                merge = self._merge_next or self._is_burst(
                    asyncio.get_running_loop().time()
                )
                self._merge_next = False
                if merge and self.cfg.debounce_s > 0:
                    await self._debounce(self.cfg.debounce_s)
                if self.cfg.wait_s > 0:
                    batch = await self._take_ready_senders()
                else:
                    batch, self._pending = self._pending, []
                if not batch:
                    continue
                self.batches += 1
                if len(batch) > 1:
                    self.merged_batches += 1
                await self._process(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[GroupSense] 调度异常 {self.key}")
            finally:
                self.state = "idle"
            # 还有待判消息：判断/回复期间到达的已由 submit 标记为合并；
            # 按人计时还没等完的，继续各自的等待
            if self._pending:
                self._wakeup.set()

    async def _process(self, batch: list[Pending]) -> None:
        plan: ReplyPlan | None = None
        carrier: Pending | None = None
        self.state = "judging"
        try:
            plan = await self._judge_fn(self.key, batch)
            if plan is not None:
                carrier = next(
                    (p for p in batch if p.record.alias == plan.target.alias),
                    batch[-1],
                )
        except Exception:
            logger.exception(f"[GroupSense] 判断流程异常 {self.key}")
            plan = None
        finally:
            for p in batch:
                if p is not carrier and not p.future.done():
                    p.future.set_result(None)
        if plan is None or carrier is None or carrier.future.done():
            return
        self.state = "replying"
        self.replies += 1
        carrier.future.set_result(plan)
        try:
            await asyncio.wait_for(plan.done.wait(), timeout=self.cfg.reply_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(f"[GroupSense] 等待主 LLM 回复超时 {self.key}")

    async def close(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        for p in self._pending:
            if not p.future.done():
                p.future.set_result(None)
        self._pending.clear()


class Scheduler:
    def __init__(self, judge_fn: JudgeFn, cfg: SchedulerConfig) -> None:
        self._judge_fn = judge_fn
        self.cfg = cfg
        self.workers: dict[str, GroupWorker] = {}

    def submit(self, key: str, record: MsgRecord, event: Any) -> asyncio.Future:
        worker = self.workers.get(key)
        if worker is None:
            worker = GroupWorker(key, self._judge_fn, self.cfg)
            self.workers[key] = worker
        return worker.submit(record, event)

    def cancel(self, key: str, alias: int) -> bool:
        """取消某群一条待判消息；见 GroupWorker.cancel。"""
        worker = self.workers.get(key)
        return worker.cancel(alias) if worker else False

    async def drop(self, key: str) -> None:
        """停掉一个群的调度协程，待判消息全部按"不回复"处理。"""
        worker = self.workers.pop(key, None)
        if worker is not None:
            await worker.close()

    def update_config(self, cfg: SchedulerConfig) -> None:
        """热更新调度参数。所有群共用同一个配置对象，原地改写即可对已有协程生效。"""
        for name in (
            "burst_window_s",
            "burst_threshold",
            "debounce_s",
            "max_batch_wait_s",
            "reply_timeout_s",
            "wait_s",
        ):
            setattr(self.cfg, name, getattr(cfg, name))

    async def close(self) -> None:
        for worker in list(self.workers.values()):
            await worker.close()
        self.workers.clear()
