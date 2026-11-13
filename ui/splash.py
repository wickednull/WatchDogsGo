"""Splash screen: ASCII logo, charging bars animation + boot beep."""

from __future__ import annotations

from .theme import Palette, clear_bg, draw_corner, draw_divider, FONT_HINT

LOGO = [
    "W   W  DDD   GGG  W   W  AAA  RRRR   SSS",
    "W   W  D  D G     W   W A   A R   R S    ",
    "W W W  D  D G  GG W W W AAAAA RRRR   SSS ",
    "WW WW  D  D G   G WW WW A   A R  R     S",
    "W   W  DDD   GGG  W   W A   A R   R SSSS ",
]

TAGLINE = "// hak5 pager wardriver  ::  wdgwars.pl"
VERSION = "v1.2.3"

# charging-bar phase in modules lit per frame
_CHARGE = (1, 2, 3, 4, 3, 2)


def _bars(p, pal: Palette, y: int, lit: int) -> None:
    n = 8
    pitch, w, h = 14, 8, 7
    total = n * w + (n - 1) * (pitch - w)
    x0 = (p.width - total) // 2
    for i in range(n):
        x = x0 + i * pitch
        if i < lit:
            p.fill_rect(x, y, w, h, pal.green)
            p.hline(x, y + h, w, pal.green)
        else:
            p.rect(x, y, w, h, pal.cyan_dim)
            p.fill_rect(x, y, w, 2, pal.cyan_dim)


def show(p, pal: Palette, hold_ms: int = 1100) -> None:
    frames = max(1, len(_CHARGE))
    step = max(30, hold_ms // frames)

    y0 = 16
    for f in range(frames):
        clear_bg(p, pal)
        draw_corner(p, pal, 4, 4, p.width - 8, p.height - 8, 6)

        for i, line in enumerate(LOGO):
            p.draw_text_centered(y0 + i * 7, line, pal.cyan, 1)
        y_logo = y0 + len(LOGO) * 7
        draw_divider(p, pal, 16, y_logo + 8, p.width - 32, pal.cyan_dim, 10)
        p.draw_text_centered(y_logo + 16, TAGLINE, pal.fg_dim, FONT_HINT)
        p.draw_text_centered(y_logo + 26, VERSION, pal.magenta, FONT_HINT)
        p.draw_text_centered(y_logo + 36, "fork by wickednull  /  orig. LOCOSP",
                             pal.fg_dim, FONT_HINT)

        _bars(p, pal, p.height - 52, _CHARGE[f % len(_CHARGE)])

        p.draw_text_centered(p.height - 24, "[ booting ]", pal.cyan, FONT_HINT)
        p.flip()

        if f == 0:
            try:
                p.vibrate(40)
                p.play_rtttl_sync("wd:d=8,o=5,b=200:c6,e6,g6", with_vibration=False)
            except Exception:
                pass
        p.delay(step)