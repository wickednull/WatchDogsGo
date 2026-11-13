"""Wireless interface discovery and selection.

The payload used to hardcode `wlan0`, which is both the pager's own
management radio and the worst possible choice for wardriving: scanning on it
knocks the management AP off channel, and it is the *only* interface used even
when a far better one is present (issue #3 — an external Alfa AWUS036ACM
brought up as `wlan2mon`, for example).

Selection therefore has two jobs:

* enumerate what is actually available, and
* keep capture off the radio that carries the phone-GPS link.

The Pager's internal radios are `phy0` (the 2.4 GHz management/client radio,
hosting `wlan0mgmt`, `wlan0cli`, `wlan0`, `wlan0mon`, ...) and `phy1` (the
tri-band radio used by `wlan1`/`wlan1mon`).  Any vif on the management phy
competes with the AP for airtime; `iw scan` on it also parks the radio on other
channels, which drops every phone associated with the management AP.  That is
exactly the "my phone disconnects as soon as I start wardriving" report.

`pick_wifi_source()` returns only a *safe* interface (one that does not share
the management phy) when one exists, even if that monitor vif is currently
down — `bring_up()` can fix that.  If nothing safe exists it still returns the
best option rather than failing outright, but marks the reason with
``UNSAFE`` so the caller can warn before the phone link dies.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Interfaces the pager's own firmware owns. Scanning on these fights with the
# management AP / client stack, so auto-selection avoids them when it can.
# `wlan0` and `wlan0mon` are the legacy/raw names for the same internal radio.
PAGER_OWNED = (
    "wlan0", "wlan0mon",
    "wlan0mgmt", "wlan0open", "wlan0wpa", "wlan0cli", "wlan0ap",
)

# Vifs that prove a phy carries the firmware's management stack.  Their phy is
# the one we must never scan on while a phone is feeding GPS.  The legacy names
# are included too: phy0 is the internal 2.4 GHz radio even before the AP vif
# has been created/renamed.
MANAGEMENT_IFACES = (
    "wlan0", "wlan0mon",
    "wlan0mgmt", "wlan0open", "wlan0wpa", "wlan0cli", "wlan0ap",
)

_PHY_RE = re.compile(r"^phy#(\d+)")
_IFACE_RE = re.compile(r"^\s*Interface\s+(\S+)")
_TYPE_RE = re.compile(r"^\s*type\s+(\S+)")
_INDEX_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class IfaceInfo:
    name: str
    type: str          # "managed", "monitor", "AP", ...
    phy: int
    up: bool

    @property
    def is_monitor(self) -> bool:
        return self.type.lower() == "monitor"

    @property
    def is_pager_owned(self) -> bool:
        return self.name in PAGER_OWNED


def list_interfaces() -> list[IfaceInfo]:
    """Parse `iw dev` into structured interface info. Empty list if iw fails."""
    try:
        proc = subprocess.run(["iw", "dev"], capture_output=True, text=True,
                              timeout=10)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return parse_iw_dev(proc.stdout)


def parse_iw_dev(text: str) -> list[IfaceInfo]:
    """Pure parser for `iw dev` output — unit-testable without hardware."""
    out: list[IfaceInfo] = []
    phy = 0
    name: str | None = None
    itype = "managed"
    for line in text.splitlines():
        m = _PHY_RE.match(line)
        if m:
            if name:
                out.append(IfaceInfo(name, itype, phy, _is_up(name)))
                name = None
            phy = int(m.group(1))
            continue
        m = _IFACE_RE.match(line)
        if m:
            if name:
                out.append(IfaceInfo(name, itype, phy, _is_up(name)))
            name = m.group(1)
            itype = "managed"
            continue
        m = _TYPE_RE.match(line)
        if m and name:
            itype = m.group(1)
    if name:
        out.append(IfaceInfo(name, itype, phy, _is_up(name)))
    return out


def _is_up(name: str) -> bool:
    """IFF_UP from sysfs. Monitor interfaces report operstate 'unknown' even
    when live, so the flags bitmask is the only honest signal."""
    try:
        with open(f"/sys/class/net/{name}/flags") as fh:
            return bool(int(fh.read().strip(), 16) & 0x1)
    except OSError:
        return False


def bring_up(name: str, timeout: float = 5.0) -> bool:
    """`ip link set <name> up`, returning whether it worked.

    A monitor vif can exist in the kernel but be administratively down (some
    Pager firmware brings up `wlan1mon` late, or after a failed create leaves
    the vif behind).  Waking it is cheap and avoids falling back to a radio
    that would knock the phone off the management AP.
    """
    if not name:
        return False
    try:
        proc = subprocess.run(["ip", "link", "set", name, "up"],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False


def management_phys(interfaces: list[IfaceInfo]) -> set[int]:
    """Phys that carry the firmware's management/client stack.

    Derived from the interfaces actually present, so an external adapter
    promoted to management duty is protected too.  When `iw dev` does not show
    any management vif, the set is empty and only name-based filtering applies.
    """
    return {i.phy for i in interfaces if i.name in MANAGEMENT_IFACES}


def unsafe_reason(info: IfaceInfo, mgmt_phys: set[int]) -> str:
    """Human-readable reason a capture interface is unsafe, or ``""``."""
    if info.name in MANAGEMENT_IFACES:
        return f"{info.name} is the management radio"
    if info.name in PAGER_OWNED:
        return f"{info.name} is pager-owned (phy{info.phy})"
    if info.phy in mgmt_phys:
        return f"phy{info.phy} also carries the management radio"
    return ""


def _rank(info: IfaceInfo) -> tuple:
    """Sort key: prefer up, then the higher phy/index (external adapters)."""
    idx_m = _INDEX_RE.search(info.name)
    idx = int(idx_m.group(1)) if idx_m else 0
    return (info.up, info.phy, idx)


def _why(base: str, info: IfaceInfo, mgmt_phys: set[int]) -> str:
    """Attach safety/down information to a selection reason."""
    why = base
    if not info.up:
        why += " (down — bring it up)"
    reason = unsafe_reason(info, mgmt_phys)
    if reason:
        why += f" — UNSAFE: {reason}"
    return why


def _monitors(ifaces: list[IfaceInfo], mgmt: set[int],
              safe: bool) -> list[IfaceInfo]:
    return sorted((i for i in ifaces if i.is_monitor
                   and (not unsafe_reason(i, mgmt) if safe
                        else bool(unsafe_reason(i, mgmt)))),
                  key=_rank, reverse=True)


def _scanners(ifaces: list[IfaceInfo], mgmt: set[int],
              safe: bool) -> list[IfaceInfo]:
    return sorted(
        (i for i in ifaces
         if not i.is_monitor
         and i.type.lower() in ("managed", "station", "ibss")
         and (not unsafe_reason(i, mgmt) if safe
              else bool(unsafe_reason(i, mgmt)))),
        key=_rank, reverse=True,
    )


def pick_wifi_source(preference: str | None = "auto",
                     interfaces: list[IfaceInfo] | None = None,
                     ) -> tuple[str, str, str]:
    """Decide how and where to capture WiFi.

    Returns ``(mode, iface, why)`` where *mode* is ``"monitor"`` or ``"scan"``.

    *preference* accepts an explicit interface name, ``"auto"`` (prefer a
    monitor interface that does not share the management phy, else the best
    safe scan-capable one), ``"monitor"`` or ``"scan"`` to force a backend, or
    ``"wlan0"``-style legacy values.

    When only unsafe interfaces exist the function still returns one (the app
    can then ask before dropping the phone link) and puts ``UNSAFE`` in *why*.
    """
    pref = (preference or "auto").strip()
    ifaces = list_interfaces() if interfaces is None else interfaces
    mgmt = management_phys(ifaces)

    if pref not in ("auto", "monitor", "scan", ""):
        for i in ifaces:
            if i.name == pref:
                mode = "monitor" if i.is_monitor else "scan"
                return mode, i.name, _why(f"configured ({i.type})", i, mgmt)
        # Named but not enumerable — trust the user, guess from the name.
        mode = "monitor" if pref.endswith("mon") else "scan"
        why = "configured (not in `iw dev`)"
        if pref in PAGER_OWNED:
            why += " — UNSAFE: pager-owned interface"
        return mode, pref, why

    safe_monitors = _monitors(ifaces, mgmt, safe=True)
    safe_scanners = _scanners(ifaces, mgmt, safe=True)
    unsafe_monitors = _monitors(ifaces, mgmt, safe=False)
    unsafe_scanners = _scanners(ifaces, mgmt, safe=False)

    if pref == "monitor":
        if safe_monitors:
            return ("monitor", safe_monitors[0].name,
                    _why("forced monitor", safe_monitors[0], mgmt))
        if safe_scanners:
            return ("scan", safe_scanners[0].name,
                    _why("monitor requested but no safe monitor",
                         safe_scanners[0], mgmt))
        if unsafe_monitors:
            return ("monitor", unsafe_monitors[0].name,
                    _why("forced monitor", unsafe_monitors[0], mgmt))
        if unsafe_scanners:
            return ("scan", unsafe_scanners[0].name,
                    _why("monitor requested but none up",
                         unsafe_scanners[0], mgmt))
        return "scan", "wlan0", \
            "monitor requested but none up — UNSAFE: no interface enumerated"

    if pref == "scan":
        if safe_scanners:
            return ("scan", safe_scanners[0].name,
                    _why("forced scan", safe_scanners[0], mgmt))
        if unsafe_scanners:
            return ("scan", unsafe_scanners[0].name,
                    _why("forced scan", unsafe_scanners[0], mgmt))
        return "scan", "wlan0", "forced scan — UNSAFE: no scan-capable iface"

    # auto
    if safe_monitors:
        return ("monitor", safe_monitors[0].name,
                _why("safe monitor iface present", safe_monitors[0], mgmt))
    if safe_scanners:
        best = safe_scanners[0]
        why = "external adapter" if best.phy >= 2 else "safe capture radio"
        return "scan", best.name, _why(why, best, mgmt)
    # Nothing safe: prefer a live monitor for yield, else a scanner.  Both
    # share the management phy, so the caller must confirm first.
    if unsafe_monitors and unsafe_monitors[0].up:
        return ("monitor", unsafe_monitors[0].name,
                _why("no safe monitor iface", unsafe_monitors[0], mgmt))
    if unsafe_scanners:
        return ("scan", unsafe_scanners[0].name,
                _why("only pager radio available", unsafe_scanners[0], mgmt))
    if unsafe_monitors:
        return ("monitor", unsafe_monitors[0].name,
                _why("no safe monitor iface", unsafe_monitors[0], mgmt))
    return "scan", "wlan0", "no interfaces enumerated — UNSAFE: cannot verify phy"
