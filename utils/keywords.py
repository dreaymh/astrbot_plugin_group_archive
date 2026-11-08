"""查询词切分：英文按词，中文按串 + 二元组。"""

from __future__ import annotations

import re

RE_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*")
RE_CJK = re.compile(r"[\u4e00-\u9fa5]+")
STOP = {"的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "吗", "呢", "吧", "啊", "和", "就", "都", "也", "有", "这", "那"}


def tokenize(query: str) -> list[str]:
    """把查询串切成检索用 token，长中文串补二元组。"""
    query = (query or "").strip()
    if not query:
        return []
    out: list[str] = []
    for m in RE_ASCII.finditer(query):
        w = m.group(0)
        if len(w) >= 2:
            out.append(w)
    for m in RE_CJK.finditer(query):
        s = m.group(0)
        if len(s) <= 2:
            if s not in STOP:
                out.append(s)
        else:
            out.append(s)
            out.extend(s[i : i + 2] for i in range(len(s) - 1))
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        key = t.lower()
        if key in seen or not t:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq


def clean_query(query: str) -> str:
    """去掉首尾标点与多余空白，保留内部空格。"""
    q = re.sub(r"[\s\u3000]+", " ", (query or "").strip())
    return q.strip(" ,，。.！!？?、;；:：\"'“”‘’()（）[]【】")
