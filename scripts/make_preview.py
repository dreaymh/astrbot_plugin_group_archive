"""生成 README 用的示例图（假数据，不联网）。

用法：python scripts/make_preview.py
输出：docs/preview.png
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_group_archive.services.renderer import RecallItem, render  # noqa: E402

NOW = int(time.time())
FAKE = [
    (6 * 3600, "阿飞", "旷宿这个词是谁先叫起来的啊，我记得最早是在群里说的"),
    (5 * 3600 + 1800, "小满", "旷宿 = 旷课回宿舍，我们宿舍发明的"),
    (4 * 3600, "老王", "所以旷宿和早退的区别是啥，早退还要打卡"),
    (3 * 3600, "阿飞", "区别是旷宿的人根本没去，早退的人去了又跑了"),
    (2 * 3600, "小满", "上周三整个宿舍旷宿，导员点名叫到我们的时候全在睡"),
    (3600, "老王", "别说了，再旷宿绩点就没了"),
    (600, "群友", "旷宿一时爽，期末火葬场 🔥"),
]

items = [
    RecallItem(timestamp=NOW - delta, nickname=nick, text=t)
    for delta, nick, t in FAKE
]

png = render("旷宿", items, "示例群聊", 1, ROOT / "avatars")
out = ROOT / "docs" / "preview.png"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(png)
print(f"{out} {len(png)} bytes")
