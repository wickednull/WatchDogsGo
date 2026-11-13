"""Modal dialogs: alert / confirm / progress / wait."""

from __future__ import annotations

from typing import Callable

from . import idle
from .theme import (
    Palette, clear_bg, draw_header, draw_footer, draw_marquee, draw_corner,
    draw_divider, FONT_BODY, FONT_HINT, FONT_HUGE, FONT_OUTPUT,
    HEADER_H, FOOTER_H, CHAR_W, CHAR_H,
)


def _box(p, pal, box_y: int, box_h: int, accent: int) -> None:
    """Shared modal card: accent bar, surface fill, layered border."""
    p.fill_rect(12, box_y, p.width - 24, box_h, pal.surface)
    p.fill_rect(12, box_y, 4, box_h, accent)
    p.rect(12, box_y, p.width - 24, box_h, pal.cyan_dim)
    p.rect(13, box_y + 1, p.width - 26, box_h - 2, pal.bg)


def _icon(p, pal, x: int, y: int, glyph: str, color: int) -> int:
    """Square status icon tile; returns its width so callers stack text beside."""
    w = 16
    p.fill_rect(x, y, w, w, pal.surface_hi)
    p.rect(x, y, w, w, pal.cyan_dim)
    p.draw_text(x + (w - p.text_width(glyph, FONT_BODY)) // 2,
                y + (w - (CHAR_H * FONT_BODY)) // 2, glyph, color, FONT_BODY)
    return w


def alert(p, pal: Palette, title: str, message: str, accent: int | None = None) -> None:
    color = accent if accent is not None else pal.magenta
    clear_bg(p, pal)
    draw_header(p, pal, title)
    box_y = HEADER_H + 14
    box_h = p.height - box_y - FOOTER_H - 14
    _box(p, pal, box_y, box_h, color)
    iw = _icon(p, pal, 22, box_y + 14, "!", color)
    _multiline(p, pal, message, 22 + iw + 10, box_y + 16,
               p.width - 24 - iw - 12, color, size=FONT_OUTPUT)
    draw_footer(p, pal, [("A", "ok")])
    p.flip()
    while True:
        btn = idle.wait_button(p)
        if btn is None:
            continue
        if btn in (p.BTN_A, p.BTN_B):
            return


def confirm(p, pal: Palette, title: str, message: str) -> bool:
    clear_bg(p, pal)
    draw_header(p, pal, title)
    box_y = HEADER_H + 14
    box_h = p.height - box_y - FOOTER_H - 14
    _box(p, pal, box_y, box_h, pal.cyan)
    iw = _icon(p, pal, 22, box_y + 14, "?", pal.cyan)
    _multiline(p, pal, message, 22 + iw + 10, box_y + 16,
               p.width - 24 - iw - 12, pal.fg, size=FONT_OUTPUT)
    draw_footer(p, pal, [("A", "yes"), ("B", "no")])
    p.flip()
    while True:
        ev = idle.wait_button(p)
        if ev is None:
            continue
        if ev == p.BTN_A:
            return True
        if ev == p.BTN_B:
            return False


class Progress:
    def __init__(self, p, pal: Palette, title: str) -> None:
        self.p = p
        self.pal = pal
        self.title = title
        self.lines: list[tuple[str, int]] = []
        self.pct = 0.0

    def set(self, pct: float, line: str | None = None, color: int | None = None) -> None:
        self.pct = pct
        if line is not None:
            self.lines.append((line, color if color is not None else self.pal.fg_dim))
            # Trim to the last N lines we can actually fit on screen at FONT_BODY.
            self.lines = self.lines[-6:]
        self._render()

    def _render(self) -> None:
        p, pal = self.p, self.pal
        clear_bg(p, pal)
        draw_header(p, pal, self.title, dot="ok")
        bar_y = HEADER_H + 10
        draw_marquee(p, pal, 16, bar_y, p.width - 32, self.pct,
                     pal.green if self.pct >= 1 else pal.cyan)
        # hero %
        pct_txt = f"{int(self.pct * 100):>3d}%"
        p.draw_text_centered(bar_y + 12, pct_txt, pal.cyan, FONT_HUGE)
        draw_divider(p, pal, 16, bar_y + 46, p.width - 32)
        # Log lines at FONT_OUTPUT so they match alert body size — this is the
        # data the user actively reads (TEST CONNECTION responses, SYNC status).
        font = FONT_OUTPUT
        y = bar_y + 54
        line_h = (CHAR_H + 3) * font
        max_w = (p.width - 24) // (CHAR_W * font)
        avail = p.height - FOOTER_H - y - 4
        max_lines = max(1, avail // line_h)
        for line, color in self.lines[-max_lines:]:
            p.fill_rect(12, y + 2, 3, 12, color)
            p.draw_text(21, y, line[:max_w], color, font)
            y += line_h
        draw_footer(p, pal, [("B", "close")])
        p.flip()

    def wait_dismiss(self) -> None:
        while True:
            ev = idle.wait_button(self.p)
            if ev is None:
                continue
            if ev in (self.p.BTN_A, self.p.BTN_B):
                return


def wait_with(p, pal: Palette, title: str, message: str,
              poll: Callable[[], bool], timeout_ms: int = 0,
              tick_ms: int = 250,
              live_message: Callable[[], str] | None = None) -> bool:
    """Show animated waiting screen until poll() returns True. B aborts.

    timeout_ms <= 0 means wait indefinitely.
    live_message, if given, replaces `message` on every tick (lets caller show
    changing telemetry like sat count).
    """
    elapsed = 0
    dot = 0
    while True:
        clear_bg(p, pal)
        draw_header(p, pal, title)
        box_y = HEADER_H + 14
        box_h = p.height - box_y - FOOTER_H - 32
        _box(p, pal, box_y, box_h, pal.cyan)
        msg = live_message() if live_message else message
        _multiline(p, pal, msg, 22 + 22, box_y + 12, p.width - 24 - 40,
                   pal.fg, size=FONT_OUTPUT)
        spinner = "[" + "." * (dot + 1) + " " * (3 - dot) + "]"
        p.draw_text_centered(box_y + box_h - 22, spinner, pal.cyan, FONT_BODY)
        elapsed_s = elapsed // 1000
        p.draw_text_centered(p.height - FOOTER_H - 12,
                             f"elapsed {elapsed_s}s", pal.fg_dim, FONT_HINT)
        draw_footer(p, pal, [("B", "abort")])
        p.flip()
        if poll():
            return True
        mgr = idle.get()
        if mgr:
            mgr.tick()
        if p.has_input_events():
            ev = p.get_input_event()
            if ev:
                # First press just wakes; don't abort the wait
                if mgr and mgr.wake_consume():
                    while p.has_input_events():
                        p.get_input_event()
                elif ev[0] == p.BTN_B:
                    return False
        p.delay(tick_ms)
        elapsed += tick_ms
        dot = (dot + 1) % 4
        if 0 < timeout_ms <= elapsed:
            return False
    return False


def _multiline(p, pal: Palette, text: str, x: int, y: int, w: int, color: int,
               size: int = FONT_BODY) -> None:
    max_chars = max(6, w // (CHAR_W * size))
    line_h = (CHAR_H + 4) * size
    cy = y
    for raw in text.split("\n"):
        words = raw.split(" ")
        cur = ""
        for word in words:
            tentative = (cur + " " + word).strip()
            if len(tentative) > max_chars and cur:
                p.draw_text(x, cy, cur, color, size)
                cy += line_h
                cur = word
            else:
                cur = tentative
        if cur:
            p.draw_text(x, cy, cur, color, size)
            cy += line_h
