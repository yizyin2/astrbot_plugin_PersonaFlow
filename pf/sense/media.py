"""图片 / 表情包落盘（按 md5 去重）与可选的图片转述缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.api.provider import Provider
from astrbot.core.utils.media_utils import (
    ImageInputTooLargeError,
    normalize_model_image_max_size,
    prepare_model_image,
)

from .models import MediaRef
from .render import clean_summary
from .store import MessageStore

MAX_MEDIA_BYTES = 10 * 1024 * 1024
CAPTION_TIMEOUT_S = 60

DEFAULT_CAPTION_PROMPT = (
    "用一句简短的中文描述这张图片。如果是表情包，说明它表达的情绪或梗；"
    "如果图中有文字，带上关键文字。不超过40字，只输出描述本身。"
)


@dataclass
class PreparedImage:
    path: str
    montage: bool  # 动图已转成 3x3 帧拼图


def model_image_max_size(context, umo: str | None = None) -> int:
    """读取 AstrBot 给主 LLM 用的图片最长边限制，判断/转述模型用同一个值。"""
    try:
        options = context.get_config(umo=umo).get("provider_settings", {}).get(
            "image_compress_options", {}
        )
        raw = options.get("max_size") if isinstance(options, dict) else None
    except Exception:
        raw = None
    return normalize_model_image_max_size(raw)


@asynccontextmanager
async def prepared_images(paths: list[str], max_size: int, work_dir: Path):
    """用 AstrBot 主 LLM 同款预处理压缩图片：缩到最长边、动图转帧拼图。

    产出与输入一一对应（处理失败或过大的图片为 None），退出时删除临时文件。
    """
    results: list[PreparedImage | None] = []
    cleanup: list[str] = []
    try:
        for path in paths:
            try:
                prepared = await prepare_model_image(
                    path, max_size=max_size, output_dir=work_dir
                )
            except ImageInputTooLargeError:
                prepared = None
            except Exception as e:
                logger.debug(f"[GroupSense] 图片预处理失败 {path}: {e}")
                prepared = None
            if prepared is None:
                results.append(None)
                continue
            out_path, is_montage, needs_cleanup, _ = prepared
            if needs_cleanup:
                cleanup.append(out_path)
            results.append(PreparedImage(path=out_path, montage=is_montage))
        yield results
    finally:
        for path in cleanup:
            try:
                os.remove(path)
            except OSError:
                pass


def _sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    return ".img"


def _local_file(ref: str | None) -> str:
    if not ref:
        return ""
    if ref.startswith("file:///"):
        ref = ref[len("file://") :]
    if ref.startswith(("http://", "https://", "base64://", "data:")):
        return ""
    return ref if os.path.isfile(ref) else ""


class MediaManager:
    def __init__(
        self,
        store: MessageStore,
        media_dir: Path,
        context,
        caption_provider_id: str,
        caption_prompt: str,
    ) -> None:
        self.store = store
        self.media_dir = media_dir
        self.context = context
        self.caption_provider_id = caption_provider_id
        self.caption_prompt = caption_prompt or DEFAULT_CAPTION_PROMPT
        self.work_dir = media_dir.parent / "tmp"
        self._sem = asyncio.Semaphore(2)
        self._captioning: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    async def ingest(self, kind: str, seg_data: dict, comp: Image | None) -> MediaRef:
        """把一张图片落盘到插件目录，失败时返回只有占位信息的 MediaRef。"""
        summary = clean_summary(seg_data.get("summary"))
        ref = MediaRef(kind=kind, summary=summary)
        try:
            path = await self._resolve_local(seg_data, comp)
            if not path:
                return ref
            if os.path.getsize(path) > MAX_MEDIA_BYTES:
                return ref
            data = await asyncio.to_thread(Path(path).read_bytes)
            md5 = hashlib.md5(data).hexdigest()
            ref.md5 = md5
            existing = await self.store.get_media(md5)
            if existing and existing["path"] and os.path.isfile(existing["path"]):
                await self.store.touch_media(md5)
                ref.path = existing["path"]
                ref.summary = summary or existing["summary"]
                if not existing["caption"]:
                    self.schedule_caption(md5, ref.path)
                return ref
            dest = self.media_dir / f"{md5}{_sniff_ext(data)}"
            if not dest.exists():
                await asyncio.to_thread(dest.write_bytes, data)
            await self.store.put_media(md5, str(dest), kind, summary)
            ref.path = str(dest)
            self.schedule_caption(md5, ref.path)
        except Exception as e:
            logger.warning(f"[GroupSense] 图片落盘失败，只保留占位符: {e}")
        return ref

    async def _resolve_local(self, seg_data: dict, comp: Image | None) -> str:
        # PreProcess 阶段通常已经把图片下载到本地，path/file/url 都是本地路径
        if comp is not None:
            for cand in (comp.path, comp.file, comp.url):
                if path := _local_file(cand):
                    return path
            try:
                return _local_file(await comp.convert_to_file_path())
            except Exception:
                pass
        url = str(seg_data.get("url") or "")
        if url.startswith(("http://", "https://")):
            return _local_file(await Image.fromURL(url).convert_to_file_path())
        return ""

    def schedule_caption(self, md5: str, path: str) -> None:
        if not self.caption_provider_id or md5 in self._captioning:
            return
        self._captioning.add(md5)
        task = asyncio.create_task(self._caption(md5, path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _caption(self, md5: str, path: str) -> None:
        try:
            async with self._sem:
                provider = self.context.get_provider_by_id(self.caption_provider_id)
                if not isinstance(provider, Provider):
                    logger.warning(
                        f"[GroupSense] 图片转述模型 {self.caption_provider_id} 不可用"
                    )
                    return
                async with prepared_images(
                    [path], model_image_max_size(self.context), self.work_dir
                ) as (image,):
                    if image is None:
                        return
                    prompt = self.caption_prompt
                    if image.montage:
                        prompt += "\n（这是一张动图，已按阅读顺序拼成 3×3 的帧图，请当作动图描述。）"
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            session_id=uuid.uuid4().hex,
                            image_urls=[image.path],
                        ),
                        timeout=CAPTION_TIMEOUT_S,
                    )
                caption = " ".join((resp.completion_text or "").split())[:120]
                if caption and resp.role != "err":
                    await self.store.set_caption(md5, caption)
        except Exception as e:
            logger.debug(f"[GroupSense] 图片转述失败 {md5}: {e}")
        finally:
            self._captioning.discard(md5)

    async def cleanup(self, retention_days: float) -> int:
        paths = await self.store.expire_media(time.time() - retention_days * 86400)
        removed = 0
        for path in paths:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        return removed

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()


def _thumbnail_bytes(path: str, max_size: int) -> tuple[bytes, str]:
    """生成缩略图（动图取第一帧）；Pillow 不可用或处理失败时退回原文件。"""
    try:
        import io

        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            img.seek(0)
            frame = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") else img.convert("RGB")
            frame.thumbnail((max_size, max_size))
            buffer = io.BytesIO()
            if frame.mode == "RGBA":
                frame.save(buffer, format="PNG")
                return buffer.getvalue(), "image/png"
            frame.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        data = Path(path).read_bytes()
        mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(
            Path(path).suffix.lower(), "image/jpeg"
        )
        return data, mime


async def thumbnail_data_url(path: str, max_size: int = 320) -> str:
    """把一张缓存图片转成 data URL 缩略图，给管理面板显示；文件不存在返回空串。"""
    import base64

    if not path or not os.path.isfile(path):
        return ""
    if os.path.getsize(path) > MAX_MEDIA_BYTES:
        return ""
    data, mime = await asyncio.to_thread(_thumbnail_bytes, path, max_size)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
