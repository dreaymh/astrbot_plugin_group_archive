"""索引维护：被动记录、图片本地化、历史回填。"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..utils.net import is_public_url
from .onebot import history_from_bot
from .store import MessageStore

UA = {"User-Agent": "Mozilla/5.0"}
IMAGE_TTL = 3 * 86400  # 只对近期消息尝试下载图片，老链接已失效


def _session_of(event: Any) -> str | None:
    gid = None
    try:
        gid = event.get_group_id()
    except Exception:  # noqa: BLE001
        gid = None
    if not gid:
        mo = getattr(event, "message_obj", None)
        gid = getattr(mo, "group_id", None)
    return str(gid) if gid else None


def _raw_event(event: Any) -> dict | None:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw, dict) and isinstance(raw.get("message"), list):
        return raw
    return None


class Indexer:
    def __init__(self, store: MessageStore, image_dir: Path, cache_images: bool, max_cache_mb: int) -> None:
        self.store = store
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.cache_images = cache_images
        self.max_bytes = max_cache_mb * 1024 * 1024
        self._sem = asyncio.Semaphore(2)
        self._bytes: int | None = None

    # ---- 图片 ----

    def _dir_bytes(self) -> int:
        if self._bytes is None:
            total = 0
            for p in self.image_dir.glob("*"):
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
            self._bytes = total
        return self._bytes

    def _save_blocking(self, url: str) -> str | None:
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        if not is_public_url(url):
            logger.debug(f"[Archive] 跳过非公网图片地址: {url[:80]}")
            return None
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as resp:
                blob = resp.read()
            if not blob or len(blob) > 8 * 1024 * 1024:
                return None
            ext = ".png" if blob[:4] == b"\x89PNG" else ".gif" if blob[:3] == b"GIF" else ".jpg"
            path = self.image_dir / f"{name}{ext}"
            path.write_bytes(blob)
            if self._bytes is not None:
                self._bytes += len(blob)
            return str(path)
        except Exception:  # noqa: BLE001
            return None

    async def cache_url(self, url: str) -> str | None:
        """下载一张图到本地，返回本地路径；超预算或失败返回 None。"""
        if not self.cache_images or not url.startswith("http"):
            return None
        if self._dir_bytes() > self.max_bytes:
            return None
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        for ext in (".png", ".gif", ".jpg"):
            p = self.image_dir / f"{name}{ext}"
            if p.exists():
                return str(p)
        async with self._sem:
            return await asyncio.to_thread(self._save_blocking, url)

    async def localize(self, records: list[dict], only_recent: bool = True) -> list[dict]:
        """把消息里的图片链接换成本地缓存路径，原地修改并返回同一列表。"""
        cutoff = time.time() - IMAGE_TTL
        for rec in records:
            ts = rec.get("time") or 0
            if ts > 10_000_000_000:
                ts //= 1000
            if only_recent and ts and ts < cutoff:
                continue
            for seg in rec.get("message") or []:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                url = data.get("url") or ""
                if not isinstance(url, str) or not url.startswith("http"):
                    continue
                local = await self.cache_url(url)
                if local:
                    data["local"] = local
        return records

    # ---- 记录 ----

    async def record_event(self, event: Any) -> int:
        """被动记录一条群消息。"""
        sid = _session_of(event)
        raw = _raw_event(event)
        if not sid or not raw:
            return 0
        try:
            await self.localize([raw])
            return self.store.upsert_group(sid, [raw])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Recall] 记录消息失败: {exc}")
            return 0

    # ---- 回填 ----

    async def backfill(self, bot: Any, session_id: str, days: int, max_pages: int = 220) -> int:
        """沿历史接口向前翻页，把消息写入本地索引，返回新增条数。"""
        fetch = history_from_bot(bot)
        if fetch is None:
            logger.warning("[Recall] 当前适配器不支持 get_group_msg_history，跳过回填")
            return 0
        try:
            gid = int(session_id)
        except (TypeError, ValueError):
            return 0

        # 已有大索引时只做增量，不必每次把整个历史再翻一遍
        have, _, _ = self.store.stats(session_id)
        if have >= 1000:
            days = min(days, 7)
        deadline = int(time.time()) - days * 86400
        floor = deadline
        mid = 0
        total = 0
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                msgs = await fetch(gid, mid, 200)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Recall] 回填第 {page} 页失败: {exc}")
                break
            if not msgs:
                break
            msgs.sort(key=lambda m: m.get("time") or 0)
            fresh = [m for m in msgs if str(m.get("message_id")) not in seen]
            for m in fresh:
                seen.add(str(m.get("message_id")))
            if not fresh:
                break
            await self.localize(fresh)
            total += self.store.upsert_group(session_id, fresh)
            oldest = msgs[0]
            oldest_ts = oldest.get("time") or 0
            if oldest_ts > 10_000_000_000:
                oldest_ts //= 1000
            if oldest_ts < floor:
                logger.info(f"[Recall] 回填到达时间下界（{page} 页）")
                break
            new_mid = oldest.get("message_id")
            if new_mid in (None, mid):
                break
            mid = new_mid
            await asyncio.sleep(0.35)
        else:
            logger.info(f"[Recall] 回填达到页数上限 {max_pages}")

        self.store.meta_set(f"backfill:{session_id}", int(time.time()))
        if total:
            logger.info(f"[Recall] 回填完成，写入 {total} 条（会话 {session_id}）")
        return total

    async def live_fetch(self, bot: Any, session_id: str, since_ts: int, max_pages: int = 12) -> int:
        """查询前的实时补页：只翻到 since_ts 为止，数量受 max_pages 限制。"""
        fetch = history_from_bot(bot)
        if fetch is None:
            return 0
        try:
            gid = int(session_id)
        except (TypeError, ValueError):
            return 0
        mid = 0
        total = 0
        for _ in range(max(1, max_pages)):
            try:
                msgs = await fetch(gid, mid, 200)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Recall] 实时补页失败: {exc}")
                break
            if not msgs:
                break
            msgs.sort(key=lambda m: m.get("time") or 0)
            await self.localize(msgs)
            total += self.store.upsert_group(session_id, msgs)
            oldest = msgs[0]
            old_ts = oldest.get("time") or 0
            if old_ts > 10_000_000_000:
                old_ts //= 1000
            if old_ts < since_ts:
                break
            new_mid = oldest.get("message_id")
            if new_mid in (None, mid):
                break
            mid = new_mid
            await asyncio.sleep(0.3)
        return total

    def should_backfill(self, session_id: str, cooldown: int = 21600) -> bool:
        last = self.store.meta_get(f"backfill:{session_id}")
        try:
            return not last or int(last) + cooldown < int(time.time())
        except (TypeError, ValueError):
            return True
