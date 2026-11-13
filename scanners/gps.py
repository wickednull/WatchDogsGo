"""Shared GPS state and position history, feed-agnostic.

The phone-GPS bridge (`scanners.phone_gps`) feeds fixes into the same
`GpsState` that the old gpsd reader used, so every consumer — the live HUD,
the pairing screen and — most importantly — `GpsState.at()` — keeps working
unchanged.

Beyond the plain "where am I *now*" snapshot, the history lets an observation
be geo-tagged with the position that was true *when it was seen* rather than
when it was written.  That matters a lot at driving speed: `iw scan` hands us
BSSes that the kernel cached up to ~30 s ago, which is 400+ m of error at
50 km/h.  See `GpsState.at()`.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass, field

# How many fixes to keep in the rolling history. Phones report at ~1 Hz, so
# 240 samples is ~4 minutes — comfortably longer than the kernel BSS cache
# (30 s) plus any plausible scan cycle.
_HIST_MAX = 240
_HIST_TRIM = 80

# A history sample further away than this from the requested timestamp is not
# trusted to geo-tag an observation.
DEFAULT_MAX_GAP_S = 6.0


# ── public data classes ────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpsSnapshot:
    fix_3d: bool
    fix_quality: int
    lat: float
    lon: float
    alt_m: float
    accuracy_m: float
    sats: int
    utc_iso: str
    last_update: float
    device: str
    # Extras — defaulted so existing call sites and tests keep working.
    speed_mps: float = 0.0
    track_deg: float = 0.0
    age_s: float = 0.0           # how stale the underlying fix is, seconds
    interpolated: bool = False   # True when lat/lon came from interpolation


@dataclass
class GpsState:
    fix_3d: bool = False
    fix_quality: int = 0
    lat: float = 0.0
    lon: float = 0.0
    alt_m: float = 0.0
    accuracy_m: float = 0.0
    sats: int = 0
    utc_iso: str = ""
    last_update: float = 0.0
    device: str = ""
    speed_mps: float = 0.0
    track_deg: float = 0.0
    # Freshness limit (seconds). When non-zero, a snapshot older than this is
    # reported as *no fix* even though lat/lon are still held for display.
    # Set by the phone bridge; 0 disables (live fix never expires on its own).
    max_age_s: float = 0.0

    lock: threading.Lock = field(default_factory=threading.Lock,
                                 repr=False, compare=False)
    # Parallel arrays so we can bisect on time. A deque would make the
    # middle-of-buffer lookup O(n) with slow indexing; two lists keep the
    # binary search genuinely O(log n).
    _hist_t: list = field(default_factory=list, repr=False, compare=False)
    _hist_p: list = field(default_factory=list, repr=False, compare=False)

    # ── reads ──────────────────────────────────────────────────────────────

    def snapshot(self) -> GpsSnapshot:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> GpsSnapshot:
        has_fix = self.fix_3d
        if has_fix and self.max_age_s > 0 and self.last_update:
            if time.time() - self.last_update > self.max_age_s:
                has_fix = False
        return GpsSnapshot(
            fix_3d=has_fix,
            fix_quality=self.fix_quality,
            lat=self.lat,
            lon=self.lon,
            alt_m=self.alt_m,
            accuracy_m=self.accuracy_m,
            sats=self.sats,
            utc_iso=self.utc_iso,
            last_update=self.last_update,
            device=self.device,
            speed_mps=self.speed_mps,
            track_deg=self.track_deg,
            age_s=max(0.0, time.time() - self.last_update) if self.last_update else 0.0,
            interpolated=False,
        )

    def at(self, t: float, max_gap_s: float = DEFAULT_MAX_GAP_S) -> GpsSnapshot:
        """Position as of wall-clock time *t*.

        Interpolates between the two bracketing fixes. If *t* falls outside the
        history (older than the buffer, or in the future), the nearest fix is
        returned and `fix_3d` is cleared once the gap exceeds *max_gap_s* — so
        a caller that refuses to write without a fix automatically refuses to
        write observations it cannot honestly place.
        """
        with self.lock:
            hist_t = self._hist_t
            if not hist_t:
                snap = self._snapshot_locked()
                # No history yet: trust the live fix only if it is fresh
                # relative to the moment being asked about. `last_update` of 0
                # means no feed (phone) has ever reported yet.
                if not self.last_update or abs(t - self.last_update) > max_gap_s:
                    return _clear_fix(snap)
                return snap

            i = bisect.bisect_right(hist_t, t)

            if i == 0:                       # older than anything we remember
                gap = hist_t[0] - t
                return self._at_index(0, gap, max_gap_s, interpolated=False)
            if i >= len(hist_t):             # at or after the newest fix
                gap = t - hist_t[-1]
                return self._at_index(len(hist_t) - 1, gap, max_gap_s,
                                      interpolated=False)

            t0, t1 = hist_t[i - 1], hist_t[i]
            p0, p1 = self._hist_p[i - 1], self._hist_p[i]
            span = t1 - t0
            # Both ends must be real fixes for the interpolation to mean
            # anything; a gap across a fix dropout is not a straight line.
            if span <= 0 or not p0[4] or not p1[4] or span > max_gap_s * 4:
                near = i - 1 if (t - t0) <= (t1 - t) else i
                gap = abs(t - hist_t[near])
                return self._at_index(near, gap, max_gap_s, interpolated=False)

            f = (t - t0) / span
            lat = p0[0] + (p1[0] - p0[0]) * f
            lon = p0[1] + (p1[1] - p0[1]) * f
            alt = p0[2] + (p1[2] - p0[2]) * f
            acc = max(p0[3], p1[3])
            return GpsSnapshot(
                fix_3d=True, fix_quality=self.fix_quality,
                lat=lat, lon=lon, alt_m=alt, accuracy_m=acc,
                sats=self.sats, utc_iso=self.utc_iso,
                last_update=self.last_update, device=self.device,
                speed_mps=self.speed_mps, track_deg=self.track_deg,
                age_s=0.0, interpolated=True,
            )

    def _at_index(self, i: int, gap: float, max_gap_s: float,
                  interpolated: bool) -> GpsSnapshot:
        """Build a snapshot from history slot *i*. Caller holds the lock."""
        lat, lon, alt, acc, had_fix = self._hist_p[i]
        snap = GpsSnapshot(
            fix_3d=bool(had_fix) and gap <= max_gap_s,
            fix_quality=self.fix_quality,
            lat=lat, lon=lon, alt_m=alt, accuracy_m=acc,
            sats=self.sats, utc_iso=self.utc_iso,
            last_update=self.last_update, device=self.device,
            speed_mps=self.speed_mps, track_deg=self.track_deg,
            age_s=gap, interpolated=interpolated,
        )
        return snap

    # ── writes (reader thread only) ────────────────────────────────────────

    def _record(self, t: float, lat: float, lon: float, alt: float,
                acc: float, has_fix: bool) -> None:
        """Append a history sample. Caller holds the lock."""
        # A feed can replay a timestamp on reconnect; keep the arrays sorted.
        if self._hist_t and t < self._hist_t[-1]:
            t = self._hist_t[-1]
        self._hist_t.append(t)
        self._hist_p.append((lat, lon, alt, acc, has_fix))
        if len(self._hist_t) > _HIST_MAX:
            del self._hist_t[:_HIST_TRIM]
            del self._hist_p[:_HIST_TRIM]


def _clear_fix(snap: GpsSnapshot) -> GpsSnapshot:
    return GpsSnapshot(
        fix_3d=False, fix_quality=snap.fix_quality, lat=snap.lat, lon=snap.lon,
        alt_m=snap.alt_m, accuracy_m=snap.accuracy_m, sats=snap.sats,
        utc_iso=snap.utc_iso, last_update=snap.last_update, device=snap.device,
        speed_mps=snap.speed_mps, track_deg=snap.track_deg,
        age_s=snap.age_s, interpolated=snap.interpolated,
    )


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _accuracy_from(obj: dict, mode: int) -> float:
    """Horizontal position error in metres.

    Used for feed-style reports: prefers `eph`, falls back to combining
    `epx`/`epy`, and finally to a mode-derived guess. Never returns 0 — WiGLE
    treats AccuracyMeters=0 as a bogus "perfect" fix and downweights the row.
    """
    eph = _as_float(obj.get("eph"))
    if eph > 0:
        return eph
    epx, epy = _as_float(obj.get("epx")), _as_float(obj.get("epy"))
    if epx > 0 or epy > 0:
        return max(1.0, (epx * epx + epy * epy) ** 0.5)
    return 5.0 if mode == 3 else 15.0
