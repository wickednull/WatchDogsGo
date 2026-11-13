"""Cyber-cyan palette and reusable drawing helpers for the WDGoWars payload."""

from __future__ import annotations

import os

# Path is relative to PAYLOAD_DIR (set in payload.sh). Falls back to the module
# directory if the env var is missing so local unit tests still work.
_BG_PATH = os.path.join(
    os.environ.get("WDGWARS_PAYLOAD_DIR") or
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "background.png",
)
_bg_handle = None
_bg_tried = False

# Bitmap font scale (pagerctl FONT_SMALL=1, FONT_MEDIUM=2, FONT_LARGE=3).
# Uniform scale — everything except small footer hints / chips renders at
# FONT_BODY=2 so the whole UI reads at one density.
FONT_HINT = 1    # footer hints, chip labels — must stay dense
FONT_BODY = 2    # menu items, dialog body, alerts, progress log
FONT_OUTPUT = 2  # alias kept so existing `size=FONT_OUTPUT` call-sites compile
FONT_TITLE = 2   # screen-title text in the header bar
FONT_HUGE = 3    # splash logo, big "%" in progress (the only size-3 surface)

CHAR_W = 6       # width of a single bitmap glyph at size=1
CHAR_H = 7


class Palette:
    def __init__(self, p):
        self.bg        = p.hex_color(0x05060A)
        self.bg_dim    = p.hex_color(0x0A0E14)
        self.surface   = p.hex_color(0x0B111A)   # panel fill
        self.surface_hi = p.hex_color(0x131C28)  # selected row / chip backing
        self.cyan      = p.hex_color(0x00FFE0)
        self.cyan_glow = p.hex_color(0x077C74)   # soft edges / glows
        self.cyan_dim  = p.hex_color(0x0A4F4A)
        self.magenta   = p.hex_color(0xFF2E88)
        self.green     = p.hex_color(0x39FF14)
        self.green_dim = p.hex_color(0x0E3D16)
        self.amber     = p.hex_color(0xFFB000)
        self.red       = p.hex_color(0xFF2A2A)
        self.fg        = p.hex_color(0xE6FFFB)
        self.fg_dim    = p.hex_color(0x8AA4A0)
        self.grid      = p.hex_color(0x031A19)


def clear_bg(p, pal: Palette) -> None:
    """Clear to background colour, blit the cyberpunk bg image if available,
    and lay the neon scan-lines over the top."""
    p.clear(pal.bg)
    global _bg_handle, _bg_tried
    if _bg_handle is None and not _bg_tried:
        _bg_tried = True
        if os.path.isfile(_BG_PATH):
            try:
                _bg_handle = p.load_image(_BG_PATH)
            except Exception:
                _bg_handle = None
    if _bg_handle is not None:
        try:
            # If the image dims match the screen we blit 1:1; otherwise scale.
            p.draw_image_scaled(0, 0, p.width, p.height, _bg_handle)
        except Exception:
            pass
    draw_scanlines(p, pal)


def draw_scanlines(p, pal: Palette, step: int = 4) -> None:
    for y in range(0, p.height, step):
        p.hline(0, y, p.width, pal.bg_dim)


def draw_led(p, pal: Palette, x: int, y: int, color: int, r: int = 2) -> None:
    """Small filled status LED (square pixel blob, colour default cyan)."""
    p.fill_rect(x - r, y - r, 2 * r + 1, 2 * r + 1, color)


def draw_divider(p, pal: Palette, x: int, y: int, w: int,
                 color: int | None = None, step: int = 8) -> None:
    """Dashed horizontal divider for separating content blocks."""
    color = color if color is not None else pal.cyan_dim
    i = 0
    while i < w:
        p.hline(x + i, y, min(4, w - i), color)
        i += step


def draw_chip(p, pal: Palette, x: int, y: int, text: str,
              color: int | None = None, fill: int | None = None) -> int:
    """Backed label pill. Returns its width so callers can chain items."""
    color = color if color is not None else pal.cyan
    w = p.text_width(text, FONT_HINT)
    p.fill_rect(x, y, w + 6, 12, fill if fill is not None else pal.surface_hi)
    p.draw_text(x + 3, y + 2, text, color, FONT_HINT)
    return w + 6


def draw_bar(p, pal: Palette, x: int, y: int, w: int, value: float,
             color: int | None = None, height: int = 6) -> None:
    """Straight-edged value bar 0..1 with a leading "tip" pixel."""
    color = color if color is not None else pal.cyan
    value = max(0.0, min(1.0, value))
    p.rect(x, y, w, height, pal.cyan_dim)
    fill = int((w - 2) * value)
    if fill > 0:
        p.fill_rect(x + 1, y + 1, fill, height - 2, color)
    p.hline(x + 1 + fill, y + 1, 1, pal.fg)  # leading tip


def draw_panel(p, pal: Palette, x: int, y: int, w: int, h: int,
               title: str | None = None, active: bool = True,
               accent: int | None = None) -> None:
    """Filled HUD panel: surface fill, layered border, optional title chip."""
    color = accent if accent is not None else (pal.cyan if active else pal.cyan_dim)
    p.fill_rect(x, y, w, h, pal.surface)
    p.rect(x, y, w, h, pal.cyan_dim)
    p.rect(x + 1, y + 1, w - 2, h - 2, pal.bg)
    p.hline(x + 2, y + 2, w - 4, pal.surface_hi)
    p.vline(x + 3, y + 3, h - 6, pal.bg_dim)
    if title:
        chip = f"[ {title} ]"
        cw = p.text_width(chip, FONT_HINT)
        cx = x + 8
        p.fill_rect(cx - 2, y - 4, cw + 6, 10, pal.surface_hi)
        p.draw_text(cx, y - 3, chip, color, FONT_HINT)
    # corner ticks
    c = pal.cyan_glow if active else pal.cyan_dim
    p.hline(x, y, 3, c); p.vline(x, y, 3, c)
    p.hline(x + w - 3, y, 3, c); p.vline(x + w - 1, y, 3, c)
    p.hline(x, y + h - 1, 3, c); p.vline(x, y + h - 3, 3, c)
    p.hline(x + w - 3, y + h - 1, 3, c); p.vline(x + w - 1, y + h - 3, 3, c)


HEADER_H = 28   # taller to fit FONT_TITLE=2
FOOTER_H = 14


def draw_header(p, pal: Palette, title: str, sub: str | None = None,
                dot: str | None = None) -> None:
    """Top bar: brand, right-aligned screen title, optional live LED."""
    p.fill_rect(0, 0, p.width, HEADER_H, pal.bg_dim)
    p.hline(0, HEADER_H, p.width, pal.cyan)
    p.draw_text(6, 6, "// WDGWARS", pal.cyan, FONT_HINT)
    if dot:
        color = {"ok": pal.green, "warn": pal.amber, "bad": pal.red}.get(dot)
        draw_led(p, pal, p.width - 10, 14, color, r=3)
    tw = p.text_width(title, FONT_TITLE)
    tx = p.width - tw - (16 if dot else 6)
    p.draw_text(tx, 6, title, pal.fg, FONT_TITLE)
    if sub:
        p.draw_text(6, 17, sub, pal.fg_dim, FONT_HINT)


def draw_footer(p, pal: Palette, hints: list[tuple[str, str]],
                status: str | None = None) -> None:
    y = p.height - FOOTER_H + 2
    p.hline(0, y - 3, p.width, pal.cyan_dim)
    x = 6
    if status:
        p.fill_rect(x - 2, y - 1, 12, 10, pal.surface_hi)
        p.draw_text(x, y, status, pal.amber, FONT_HINT)
        x += 18
        p.hline(x, y + 4, 4, pal.cyan_dim)
        x += 8
    for key, label in hints:
        kw = p.text_width(key, FONT_HINT)
        p.fill_rect(x - 2, y - 1, kw + 4, 10, pal.cyan)
        p.draw_text(x, y, key, pal.bg, FONT_HINT)
        x += kw + 6
        lw = p.text_width(label, FONT_HINT)
        p.draw_text(x, y, label, pal.fg_dim, FONT_HINT)
        x += lw + 10


def draw_marquee(p, pal: Palette, x: int, y: int, w: int, value: float, color: int | None = None) -> None:
    """Draw a horizontal progress bar 0..1."""
    color = color if color is not None else pal.cyan
    draw_bar(p, pal, x, y, w, value, color)
    p.rect(x, y, w, 7, pal.cyan_dim)


def draw_corner(p, pal: Palette, x: int, y: int, w: int, h: int, size: int = 4) -> None:
    """Decorative corner brackets like [ ]."""
    c = pal.cyan
    p.hline(x, y, size, c); p.vline(x, y, size, c)
    p.hline(x + w - size, y, size, c); p.vline(x + w - 1, y, size, c)
    p.hline(x, y + h - 1, size, c); p.vline(x, y + h - size, size, c)
    p.hline(x + w - size, y + h - 1, size, c); p.vline(x + w - 1, y + h - size, size, c)
