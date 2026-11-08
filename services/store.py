"""本地消息索引：SQLite 存储与关键词检索。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..utils.keywords import tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    user_id    TEXT,
    nickname   TEXT,
    text       TEXT,
    images     TEXT,
    PRIMARY KEY (session_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages (session_id, ts);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(slots=True)
class Row:
    message_id: str
    ts: int
    user_id: str
    nickname: str
    text: str
    images: list[str]


def _row_from_record(rec: dict) -> tuple | None:
    """把一条 OneBot 消息事件转成入库元组。"""
    mid = rec.get("message_id")
    ts = rec.get("time") or rec.get("timestamp") or 0
    if mid is None or not ts:
        return None
    ts = int(ts)
    if ts > 10_000_000_000:
        ts //= 1000
    text_parts: list[str] = []
    images: list[str] = []
    for seg in rec.get("message") or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            text_parts.append(str(data.get("text") or ""))
        elif kind == "image":
            url = data.get("local") or data.get("url") or data.get("file") or ""
            if isinstance(url, str) and url:
                images.append(url)
            text_parts.append("[图片]")
        elif kind in ("face", "mface"):
            text_parts.append("[表情]")
        elif kind == "at":
            text_parts.append("@" + str(data.get("qq") or ""))
    sender = rec.get("sender") or {}
    return (
        str(mid),
        int(rec.get("group_id") or rec.get("session_id") or 0),
        ts,
        str(rec.get("user_id") or ""),
        str(sender.get("card") or sender.get("nickname") or ""),
        "".join(text_parts).strip(),
        json.dumps(images, ensure_ascii=False),
    )


class MessageStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 写入 ----

    def upsert(self, session_ids: Iterable[str], records: Iterable[dict]) -> int:
        """按会话批量入库，返回新增/更新条数。"""
        sid_map = list(session_ids)
        rows: list[tuple] = []
        for i, rec in enumerate(records):
            parsed = _row_from_record(rec)
            if not parsed:
                continue
            mid, gid, ts, uid, nick, text, imgs = parsed
            sid = sid_map[i] if i < len(sid_map) else str(gid)
            rows.append((sid, mid, ts, uid, nick, text, imgs))
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO messages (session_id, message_id, ts, user_id, nickname, text, images)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def upsert_group(self, session_id: str, records: Iterable[dict]) -> int:
        recs = list(records)
        return self.upsert([session_id] * len(recs), recs)

    # ---- 查询 ----

    def _run(self, sql: str, params: tuple) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _to_rows(self, raw: list[tuple]) -> list[Row]:
        return [
            Row(
                message_id=str(r[0]),
                ts=int(r[1]),
                user_id=str(r[2] or ""),
                nickname=str(r[3] or ""),
                text=str(r[4] or ""),
                images=json.loads(r[5] or "[]"),
            )
            for r in raw
        ]

    def search(self, session_id: str, query: str, since_ts: int, limit: int) -> tuple[list[Row], str]:
        """渐进放宽检索：整句 → 全 token AND → OR 打分。返回 (结果, 命中方式)。"""
        query = (query or "").strip()
        if not query:
            return [], "empty"
        base = "SELECT message_id, ts, user_id, nickname, text, images FROM messages WHERE session_id=? AND ts>=? AND text!='' "

        # 1) 整句
        rows = self._run(base + "AND text LIKE ? ORDER BY ts ASC LIMIT ?", (session_id, since_ts, f"%{query}%", limit))
        if rows:
            return self._to_rows(rows), "phrase"

        tokens = tokenize(query)
        if len(tokens) > 1:
            # 2) 全部 token 同时命中
            sql = base + "".join("AND text LIKE ? " for _ in tokens) + "ORDER BY ts ASC LIMIT ?"
            rows = self._run(sql, (session_id, since_ts, *[f"%{t}%" for t in tokens], limit))
            if rows:
                return self._to_rows(rows), "all"
            # 3) 任一 token 命中，按命中词权重（长词更具体）排序
            score = " + ".join(
                f"(CASE WHEN text LIKE ? THEN {min(len(t), 8)} ELSE 0 END)" for t in tokens
            )
            where = " OR ".join("text LIKE ?" for _ in tokens)
            sql = (
                "SELECT message_id, ts, user_id, nickname, text, images FROM messages "
                "WHERE session_id=? AND ts>=? AND text!='' AND (" + where + ") "
                "ORDER BY (" + score + ") DESC, ts ASC LIMIT ?"
            )
            params = (session_id, since_ts, *[f"%{t}%" for t in tokens], *[f"%{t}%" for t in tokens], limit)
            rows = self._run(sql, params)
            if rows:
                return self._to_rows(rows), "any"

        if tokens:
            rows = self._run(base + "AND text LIKE ? ORDER BY ts ASC LIMIT ?", (session_id, since_ts, f"%{tokens[-1]}%", limit))
            if rows:
                return self._to_rows(rows), "token"
        return [], "none"

    def stats(self, session_id: str) -> tuple[int, int, int]:
        rows = self._run("SELECT count(*), min(ts), max(ts) FROM messages WHERE session_id=?", (session_id,))
        n, a, b = rows[0] if rows else (0, 0, 0)
        return int(n or 0), int(a or 0), int(b or 0)

    def count_all(self) -> int:
        rows = self._run("SELECT count(*) FROM messages", ())
        return int(rows[0][0]) if rows else 0

    def prune_before(self, cutoff_ts: int) -> int:
        """删除早于 cutoff_ts 的索引，返回删除条数。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE ts < ?", (int(cutoff_ts),))
            self._conn.commit()
            return cur.rowcount

    # ---- meta ----

    def meta_get(self, key: str) -> str | None:
        rows = self._run("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
            self._conn.commit()
