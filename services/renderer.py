"""召回结果图片渲染：Pillow 绘制，2 倍超采样保证 QQ 内清晰。"""

from __future__ import annotations

import datetime
import hashlib
import io
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..utils.net import is_public_url

FONT_REGULAR = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_EMOJI = "C:/Windows/Fonts/seguiemj.ttf"

S = 2  # 超采样倍数

W = 760
PAD = 26
AVATAR = 36
NICK_H = 20
BODY = 15
LINE_H = 26
BODY_GAP = 4
BLOCK_GAP = 22
HEAD_H = 96
FOOT_H = 54
MAX_LINES = 6
AVATAR_TIMEOUT = 3
IMAGE_TIMEOUT = 2
IMAGE_BUDGET = 3.0  # 一张卡片里远程图片的总耗时上限（秒）

HIT_BG = "#5A1F28"
HIT_FG = "#FF8A8A"
INK = "#E6E6EC"
INK2 = "#9C9CA8"
INK3 = "#6B6B78"
DIVIDER = "#26262E"
BRAND = "#5B9BFF"
PAGE = "#0E0E12"
PANEL = "#17171D"
PANEL_LINE = "#23232B"

AVATAR_COLORS = ["#5B8FF9", "#5AD8A6", "#F6BD16", "#E8684A", "#6DC8EC", "#9270CA", "#FF9D4D", "#269A99"]


@dataclass(slots=True)
class RecallItem:
    """一条可被渲染的召回命中。"""

    timestamp: int
    nickname: str
    user_id: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)


_FONT_CACHE: dict[tuple, ImageFont.FreeTypeFont] = {}

# 微软雅黑没有彩色 emoji 字形，这些区间的字符改用 Segoe UI Emoji
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x1F1E6, 0x1F1FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x2190, 0x21FF),
    (0x2300, 0x23FF),
    (0x25A0, 0x25FF),
    (0x2900, 0x297F),
)
_ZERO_WIDTH = "\ufe0f\u200b\u200c\u200d\ufeff"


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = ("b" if bold else "r", size)
    f = _FONT_CACHE.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size * S, index=0)
        except OSError:
            f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return f


def _emoji_font(size: int) -> ImageFont.FreeTypeFont:
    key = ("e", size)
    f = _FONT_CACHE.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(FONT_EMOJI, size * S, index=0)
        except OSError:
            f = _font(size)
        _FONT_CACHE[key] = f
    return f


def _char_font(ch: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return _emoji_font(size) if _is_emoji(ch) else _font(size, bold)


def _avatar_color(seed: str) -> str:
    return AVATAR_COLORS[hashlib.md5(seed.encode("utf-8")).digest()[0] % len(AVATAR_COLORS)]


def _norm_ts(ts: int) -> int:
    return ts // 1000 if ts > 10_000_000_000 else ts


def _fmt_clock(ts: int) -> str:
    return datetime.datetime.fromtimestamp(_norm_ts(ts)).strftime("%m-%d %H:%M")


def _fmt_span(items: list[RecallItem]) -> str:
    if not items:
        return ""
    stamps = sorted(_norm_ts(i.timestamp) for i in items)
    a = datetime.datetime.fromtimestamp(stamps[0])
    b = datetime.datetime.fromtimestamp(stamps[-1])
    if a.date() == b.date():
        return a.strftime("%m-%d %H:%M") + " ~ " + b.strftime("%H:%M")
    return a.strftime("%m-%d") + " ~ " + b.strftime("%m-%d")


def _split_hits(text: str, keywords: list[str]) -> list[tuple[str, bool]]:
    keys = [k for k in keywords if k]
    if not keys:
        return [(text, False)]
    pattern = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        if m.start() > pos:
            out.append((text[pos : m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


def _layout(
    text: str, keywords: list[str], size: int, max_w: float, bold: bool = False
) -> list[list[tuple[str, bool, float]]]:
    """逐字符折行，返回每行的 (字符, 是否命中, 宽度)。"""
    lines: list[list[tuple[str, bool, float]]] = []
    cur: list[tuple[str, bool, float]] = []
    cur_w = 0.0
    for seg, hit in _split_hits(text, keywords):
        for ch in seg:
            if ch == "\n":
                lines.append(cur)
                cur, cur_w = [], 0.0
                continue
            if ch in _ZERO_WIDTH:
                continue
            w = _char_font(ch, size, bold).getlength(ch)
            if cur_w + w > max_w and cur:
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur.append((ch, hit, w))
            cur_w += w
    lines.append(cur)
    return lines


def _ellipsis_line(
    line: list[tuple[str, bool, float]], size: int, bold: bool = False
) -> list[tuple[str, bool, float]]:
    """把末行裁到不超过原宽度，并补省略号。"""
    dot_w = _font(size, bold).getlength("…")
    limit = sum(w for _, _, w in line) - dot_w
    out: list[tuple[str, bool, float]] = []
    used = 0.0
    for ch, hit, w in line:
        if used + w > limit:
            break
        out.append((ch, hit, w))
        used += w
    return out + [("…", False, dot_w)]


_AVATAR_CACHE: dict[str, Image.Image | None] = {}


def _fetch_avatar(user_id: str, cache_dir: Path | None) -> Image.Image | None:
    """取 QQ 头像，落盘缓存；失败返回 None 由调用方回退首字色块。"""
    if not user_id.isdigit():
        return None
    if user_id in _AVATAR_CACHE:
        return _AVATAR_CACHE[user_id]
    d = AVATAR * S
    path = cache_dir / f"{user_id}.png" if cache_dir else None
    im: Image.Image | None = None
    try:
        if path and path.exists():
            im = Image.open(path).convert("RGB")
        else:
            import urllib.request

            url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=AVATAR_TIMEOUT) as resp:
                im = Image.open(io.BytesIO(resp.read())).convert("RGB")
            if path and im is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                im.save(path, format="PNG")
    except Exception:
        im = None
    if im is not None:
        im = im.resize((d, d), Image.LANCZOS)
    _AVATAR_CACHE[user_id] = im
    return im


def _draw_avatar(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, item: RecallItem, cache_dir: Path | None) -> None:
    d = AVATAR * S
    photo = _fetch_avatar(str(item.user_id), cache_dir)
    if photo is not None:
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, d, d], fill=255)
        img.paste(photo, (x, y), mask)
        return
    nick = (item.nickname or str(item.user_id) or "?").strip()
    label = nick[:1] if len(nick) >= 1 else "?"
    if label in "—-":
        label = "?"
    draw.ellipse([x, y, x + d, y + d], fill=_avatar_color(item.user_id or nick))
    draw.text((x + d / 2, y + d / 2 - 1 * S), label, font=_font(14, True), fill="#FFFFFF", anchor="mm")


_IMAGE_FAILED: set[str] = set()


def _remember_failure(src: str) -> None:
    if len(_IMAGE_FAILED) > 800:
        _IMAGE_FAILED.clear()
    _IMAGE_FAILED.add(src)


def _open_image(src: str, deadline: float | None = None) -> Image.Image | None:
    """读一张图；远程地址受总耗时预算限制，避免整页卡在失效链接上。"""
    if src in _IMAGE_FAILED:
        return None
    try:
        if src.startswith(("http://", "https://")):
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if not is_public_url(src):
                _remember_failure(src)
                return None
            import urllib.request

            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
                return Image.open(io.BytesIO(resp.read())).convert("RGB")
        p = Path(src)
        if p.exists():
            return Image.open(p).convert("RGB")
    except Exception:
        _remember_failure(src)
        return None
    _remember_failure(src)
    return None


def render(
    keyword: str,
    items: list[RecallItem],
    group_name: str = "",
    max_images_per_msg: int = 1,
    avatar_dir: Path | None = None,
) -> bytes:
    """渲染召回结果卡片，返回 PNG 字节。"""
    keywords = [k.strip() for k in keyword.replace("，", ",").split(",") if k.strip()] or [keyword]
    if isinstance(avatar_dir, str):
        avatar_dir = Path(avatar_dir)

    title_f = _font(18, True)
    sub_f = _font(12)
    nick_f = _font(13, True)
    time_f = _font(12)
    foot_f = _font(12)

    text_x = PAD + AVATAR + 14
    text_w = (W - text_x - PAD) * S

    blocks: list[dict] = []
    for it in items:
        body = (it.text or "").strip() or ("[图片]" if it.images else "[空消息]")
        lines = _layout(body, keywords, BODY, text_w)
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            lines[-1] = _ellipsis_line(lines[-1], BODY)
        thumbs: list[Image.Image] = []
        deadline = time.monotonic() + IMAGE_BUDGET
        for src in it.images[:max_images_per_msg]:
            im = _open_image(src, deadline)
            if im is not None:
                im.thumbnail((110 * S, 110 * S))
                thumbs.append(im)
        thumbs_h = (max(t.height for t in thumbs) / S + 10) if thumbs else 0
        h = NICK_H + BODY_GAP + len(lines) * LINE_H + thumbs_h + BLOCK_GAP
        blocks.append({"item": it, "lines": lines, "thumbs": thumbs, "h": h})

    total_h = HEAD_H + sum(b["h"] for b in blocks) + FOOT_H
    img = Image.new("RGB", (W * S, total_h * S), PAGE)
    draw = ImageDraw.Draw(img)

    # 顶部
    draw.rectangle([0, 0, W * S, 5 * S], fill=BRAND)
    draw.text((PAD * S, 22 * S), "召回", font=title_f, fill=INK)
    draw.text((PAD * S + title_f.getlength("召回") + 10 * S, 22 * S), f"「{keyword}」", font=title_f, fill=BRAND)

    meta = f"命中 {len(items)} 条"
    if items:
        meta += f" · {_fmt_span(items)}"
    draw.text((PAD * S, 56 * S), meta, font=sub_f, fill=INK2)
    if group_name:
        draw.text((W * S - PAD * S, 56 * S), group_name, font=sub_f, fill=INK3, anchor="ra")
    draw.line([PAD * S, (HEAD_H - 14) * S, (W - PAD) * S, (HEAD_H - 14) * S], fill=DIVIDER, width=S)

    # 消息块
    y = HEAD_H * S
    for idx, b in enumerate(blocks):
        it: RecallItem = b["item"]

        panel_top = y - 7 * S
        panel_bottom = y + (b["h"] - BLOCK_GAP + 7) * S
        draw.rounded_rectangle(
            [PAD * S - 14 * S, panel_top, (W - PAD + 14) * S, panel_bottom],
            radius=10 * S,
            fill=PANEL,
            outline=PANEL_LINE,
            width=S,
        )

        _draw_avatar(img, draw, PAD * S, y, it, avatar_dir)

        tx = text_x * S
        draw.text((tx, y), it.nickname or str(it.user_id), font=nick_f, fill=INK2)
        draw.text((W * S - PAD * S, y), _fmt_clock(it.timestamp), font=time_f, fill=INK3, anchor="ra")

        ty = y + (NICK_H + BODY_GAP) * S
        for line in b["lines"]:
            cx = tx
            for ch, hit, w in line:
                if hit:
                    draw.rounded_rectangle(
                        [cx - 1 * S, ty + 2 * S, cx + w + 1 * S, ty + LINE_H * S - 6 * S],
                        radius=3 * S,
                        fill=HIT_BG,
                    )
                draw.text((cx, ty), ch, font=_char_font(ch, BODY), fill=HIT_FG if hit else INK)
                cx += w
            ty += LINE_H * S

        for th in b["thumbs"]:
            draw.rounded_rectangle(
                [tx, ty + 4 * S, tx + th.width, ty + 4 * S + th.height], radius=6 * S, fill="#22222A"
            )
            img.paste(th, (tx, ty + 4 * S))
            ty += th.height + 10 * S

        y += b["h"] * S

    # 页脚
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    fy = (total_h - FOOT_H) * S
    draw.line([PAD * S, fy, (W - PAD) * S, fy], fill=DIVIDER, width=S)
    draw.text((PAD * S, fy + 20 * S), f"共 {len(items)} 条 · 生成于 {stamp}", font=foot_f, fill=INK3)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
