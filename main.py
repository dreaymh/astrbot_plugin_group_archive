"""群聊考古：被动索引群消息，按关键词把历史拼成图片发出。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .services.indexer import Indexer
from .services.onebot import find_reply, plain_text, quoted_text
from .services.renderer import RecallItem, render
from .services.store import MessageStore
from .utils.keywords import clean_query

PLUGIN_NAME = "astrbot_plugin_group_archive"
COOLDOWN_SEC = 8  # 同一用户两次召回的最小间隔
SEPARATORS = " :：,，、-—\t"


class GroupArchivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.data_root = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.store = MessageStore(self.data_root / "index.sqlite3")
        self.indexer = Indexer(
            self.store,
            self.data_root / "images",
            bool(config.get("cache_images", True)),
            int(config.get("max_cache_mb", 800)),
        )
        self._bg: set[asyncio.Task] = set()
        self._fetch_sem = asyncio.Semaphore(1)
        self._last: dict[str, float] = {}
        self._group_last: dict[str, float] = {}
        self._running: set[str] = set()
        self._backfilling: set[str] = set()

    async def initialize(self) -> None:
        self._prune()
        logger.info(f"[Archive] 已加载，本地索引 {self.store.count_all()} 条")

    async def terminate(self) -> None:
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 工具 ----

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    @staticmethod
    def _session_id(event: Any) -> str | None:
        try:
            gid = event.get_group_id()
        except Exception:  # noqa: BLE001
            gid = None
        if not gid:
            gid = getattr(getattr(event, "message_obj", None), "group_id", None)
        return str(gid) if gid else None

    @staticmethod
    def _group_name(event: Any) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("group_name") or "")
        return ""

    def _triggers(self) -> list[str]:
        raw = self.config.get("keywords") or ["召回"]
        return [str(k).strip() for k in raw if str(k).strip()]

    def _is_status(self, text: str) -> bool:
        """「召回状态」这类查询，允许带或不带唤醒前缀。"""
        t = (text or "").strip().lstrip("/").strip()
        return any(t == kw + "状态" for kw in self._triggers())

    def _match(self, text: str) -> tuple[str | None, str]:
        """命中触发词则返回 (触发词, 剩余参数)。"""
        text = (text or "").strip()
        strict = bool(self.config.get("strict_trigger", True))
        for kw in self._triggers():
            if not text.startswith(kw):
                continue
            rest = text[len(kw) :]
            if rest == "":
                return kw, ""
            if rest[0] in SEPARATORS:
                return kw, rest.lstrip(SEPARATORS + "\u3000").strip()
            # 非严格模式才接受「召回旷宿」这种不带分隔符的写法
            if not strict:
                return kw, rest.strip()
        return None, ""

    def _prune(self) -> None:
        days = int(self.config.get("retention_days", 0) or 0)
        if days <= 0:
            return
        cutoff = int(time.time()) - days * 86400
        try:
            removed = self.store.prune_before(cutoff)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Archive] 清理旧索引失败: {exc}")
            return
        if removed:
            logger.info(f"[Archive] 已清理 {removed} 条超过 {days} 天的索引")

    def _maybe_backfill(self, event: AstrMessageEvent, sid: str) -> None:
        """首次见到某个会话时，后台把可得的历史拉进本地索引。"""
        if not self.config.get("backfill_enabled", True):
            return
        if sid in self._backfilling:
            return
        if not self.indexer.should_backfill(sid):
            return
        self._backfilling.add(sid)
        days = int(self.config.get("backfill_days", 90))

        async def _run() -> None:
            try:
                await self.indexer.backfill(event.bot, sid, days)
                self._prune()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Archive] 回填失败: {exc}")
            finally:
                self._backfilling.discard(sid)

        self._spawn(_run())

    def _allowed(self, event: AstrMessageEvent) -> bool:
        # 默认对所有人开放；关掉后回落到白名单 / 群管理员
        if self.config.get("allow_all", True):
            return True
        owners = {str(x).strip() for x in (self.config.get("owner_ids") or []) if str(x).strip()}
        uid = str(event.get_sender_id() or "")
        if owners:
            return uid in owners
        return bool(event.is_admin())

    # ---- 主流程 ----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        sid = self._session_id(event)
        if not sid:
            return

        # 被动索引：不阻塞主流程
        self._spawn(self.indexer.record_event(event))
        self._maybe_backfill(event, sid)

        if not self.config.get("enabled", True):
            return
        text = plain_text(event)
        if not text:
            return

        if self._is_status(text):
            if self._allowed(event):
                await event.send(MessageChain([Plain(self._status_text(sid))]))
                event.stop_event()
            return

        trigger, arg = self._match(text)
        if trigger is None:
            return

        # 权限：非名单用户静默忽略
        if not self._allowed(event):
            logger.debug(f"[Archive] 用户 {event.get_sender_id()} 无权限，忽略")
            return

        uid = str(event.get_sender_id() or "")
        now = time.time()
        if now - self._last.get(uid, 0) < COOLDOWN_SEC:
            # 冷却期也要吃掉事件，否则消息会漏给下游 LLM 被别的插件瞎回答
            event.stop_event()
            return
        # 群级冷却：防止多人同时刷召回把机器人占满
        group_gap = float(self.config.get("group_cooldown_sec", 10) or 0)
        if group_gap > 0 and now - self._group_last.get(sid, 0) < group_gap:
            event.stop_event()
            return
        self._last[uid] = now
        self._group_last[sid] = now

        if sid in self._running:
            await event.send(MessageChain([Plain("还在检索上一单，稍等一下")]))
            event.stop_event()
            return

        reply = find_reply(event)
        if not arg and reply is not None:
            arg = clean_query(await quoted_text(event, reply))
            if not arg:
                await event.send(MessageChain([Plain("引用里没读到文字，换个说法或直接写关键词")]))
                event.stop_event()
                return
        if not arg:
            kw = trigger or (self._triggers() or ["召回"])[0]
            await event.send(
                MessageChain([Plain(f"用法：{kw} 关键词\n或者引用一条消息后只发「{kw}」")])
            )
            event.stop_event()
            return

        self._running.add(sid)
        try:
            await self._do_recall(event, sid, arg)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Archive] 召回失败: {exc}", exc_info=True)
            await event.send(MessageChain([Plain("检索出错了，具体原因见机器人日志")]))
        finally:
            self._running.discard(sid)
        event.stop_event()

    async def _do_recall(self, event: AstrMessageEvent, sid: str, query: str) -> None:
        days = int(self.config.get("default_days", 60))
        max_items = int(self.config.get("max_items", 30))
        per_image = max(1, int(self.config.get("items_per_image", 15)))
        max_pages = int(self.config.get("max_search_pages", 12))
        since = int(time.time()) - days * 86400

        rows, mode = self.store.search(sid, query, since, max_items)
        if not rows:
            async with self._fetch_sem:
                added = await self.indexer.live_fetch(event.bot, sid, since, max_pages)
            if added:
                rows, mode = self.store.search(sid, query, since, max_items)
        logger.debug(f"[Archive] 查询「{query}」命中方式 {mode}，{len(rows)} 条")

        triggers = self._triggers()
        rows = [r for r in rows if not any(r.text.strip().startswith(t) for t in triggers)]

        if not rows:
            total, earliest, _ = self.store.stats(sid)
            span = ""
            if earliest:
                span = f"（本地索引最早 {time.strftime('%m-%d', time.localtime(earliest))}）"
            await event.send(
                MessageChain([Plain(f"没找到「{query}」相关记录，往回翻了 {days} 天{span}")])
            )
            return

        items = [
            RecallItem(
                timestamp=r.ts,
                nickname=r.nickname or r.user_id or "群友",
                user_id=r.user_id,
                text=r.text,
                images=[p for p in r.images if p],
            )
            for r in rows
        ]

        group_name = self._group_name(event)
        avatar_dir = self.data_root / "avatars"
        for i in range(0, len(items), per_image):
            chunk = items[i : i + per_image]
            png = await asyncio.to_thread(render, query, chunk, group_name, 1, avatar_dir)
            await event.send(MessageChain([Image.fromBytes(png)]))
            if i + per_image < len(items):
                await asyncio.sleep(0.8)

    def _status_text(self, sid: str) -> str:
        total, earliest, latest = self.store.stats(sid)
        if not total:
            return "本地还没有索引，先说几句话或发一次召回试试"
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        return f"本地索引 {total} 条\n范围 {fmt(earliest)} ~ {fmt(latest)}"

    @filter.command("考古状态", alias={"群聊考古状态"})
    async def archive_status(self, event: AstrMessageEvent):
        """查看本地索引覆盖范围。"""
        if not self._allowed(event):
            yield event.plain_result("无权限")
            return
        sid = self._session_id(event)
        if not sid:
            yield event.plain_result("只在群里可用")
            return
        yield event.plain_result(self._status_text(sid))
