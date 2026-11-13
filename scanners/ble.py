"""BLE scanner using `bluetoothctl` over a pseudo-tty.

bluetoothctl only emits live `[CHG] Device ... RSSI:` events when it thinks
its stdout is a terminal — when piped through a regular subprocess pipe it
silently drops async events. So we wrap it in a pty.
"""

from __future__ import annotations

import errno
import os
import pty
import queue
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class BleObs:
    mac: str
    name: str
    rssi: int
    first_seen: float


_DEVICE_RE = re.compile(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)?")
# bluez 5.72 prints `RSSI: 0xffffffb1 (-79)`; older versions print `RSSI: -79`.
# Prefer the parenthesised signed decimal, fall back to the first signed int.
_RSSI_RE = re.compile(r"RSSI:[^(\n]*\((-?\d+)\)|RSSI:\s*(-?\d+)")
_NAME_RE = re.compile(r"Name:\s*(.+)")
_CONTROLLER_RE = re.compile(r"Controller\s+([0-9A-Fa-f:]{17})")


def _list_adapters() -> list[str]:
    """hciN adapters present in sysfs, lowest index first."""
    try:
        names = os.listdir("/sys/class/bluetooth")
    except OSError:
        return []
    out = [n for n in names if n.startswith("hci")]
    return sorted(out, key=lambda n: int(n[3:]) if n[3:].isdigit() else 99)


def _cmd(cmd: list[str], timeout: float = 4.0) -> tuple[int, str]:
    """Best-effort helper for the preflight commands."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


class BleScanner:
    """Run bluetoothctl with a pty-less pipe and parse live updates."""

    def __init__(self, hci: str = "auto", interval_s: float = 12.0,
                 emit_interval_s: float = 1.0, queue_max: int = 256) -> None:
        self.hci = hci
        self.interval = interval_s
        self.emit_interval_s = emit_interval_s
        self._q: queue.Queue[list[BleObs]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pending: dict[str, BleObs] = {}
        self._controller: str | None = None
        self.last_error: str | None = None
        self.available: bool = False
        self.events: int = 0
        self.dropped_batches: int = 0

    def _preflight(self) -> str | None:
        """Get the Bluetooth stack into a usable state.

        Returns None when an adapter is powered and bluetoothd answered, or a
        short human-readable error otherwise. Without this, `bluetoothctl`
        fails silently and the HUD just shows zero BLE hits: the daemon is not
        always running on the Pager, a USB dongle needs its kernel module, and
        rfkill soft-blocks survive reboots.
        """
        adapters = _list_adapters()
        if not adapters:
            for mod in ("bluetooth", "btusb"):
                _cmd(["modprobe", mod])
            time.sleep(0.6)
            adapters = _list_adapters()
        if not adapters:
            return "no adapter (kmod-bluetooth/kmod-btusb?)"

        rc, out = _cmd(["rfkill", "list", "bluetooth"])
        if rc == 0 and "blocked: yes" in out.lower():
            _cmd(["rfkill", "unblock", "bluetooth"])
            time.sleep(0.3)

        if _cmd(["pidof", "bluetoothd"])[0] != 0:
            for cmd in (["service", "bluetoothd", "restart"],
                        ["service", "bluetoothd", "start"],
                        ["bluetoothd"]):
                _cmd(cmd, timeout=4)
                if _cmd(["pidof", "bluetoothd"])[0] == 0:
                    break
                time.sleep(0.3)
            if _cmd(["pidof", "bluetoothd"])[0] != 0:
                return "bluetoothd not running"

        want = (self.hci or "auto").strip()
        if want in ("", "auto") or want not in adapters:
            want = adapters[0]
        self.hci = want
        if shutil.which("hciconfig"):
            _cmd(["hciconfig", want, "up"])

        rc, out = _cmd(["bluetoothctl", "list"], timeout=6)
        m = _CONTROLLER_RE.search(out)
        if rc != 0 or not m:
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            return (f"no controller ({lines[-1][:44]})" if lines
                    else "no controller")
        self._controller = m.group(1)
        return None

    def start(self) -> None:
        self.last_error = None
        if not shutil.which("bluetoothctl"):
            self.last_error = "`bluetoothctl` not installed (opkg install bluez-utils)"
            return
        self.last_error = self._preflight()
        if self.last_error:
            return
        self.available = True
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="ble-scan", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b"scan off\nexit\n")
            except Exception:
                pass
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except Exception:
                pass
            self._master_fd = None
        if self._thr:
            self._thr.join(timeout=2)
            self._thr = None

    def drain(self) -> list[BleObs]:
        out: list[BleObs] = []
        while True:
            try:
                out.extend(self._q.get_nowait())
            except queue.Empty:
                return out

    def _flush(self) -> None:
        """Hand the current window's strongest sighting per MAC to the queue.

        `duplicate-data on` makes bluez re-emit RSSI for every advertisement,
        which in a station concourse is thousands of events a second. Emitting
        one object per MAC per second instead carries the same information —
        the closest approach — for a fraction of the allocation churn on a
        580 MHz core.
        """
        if not self._pending:
            return
        batch = list(self._pending.values())
        self._pending.clear()
        try:
            self._q.put_nowait(batch)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(batch)
            except (queue.Empty, queue.Full):
                pass
            self.dropped_batches += 1

    def _run(self) -> None:
        # Spawn bluetoothctl under a pty so it emits async [CHG]/[NEW] events.
        try:
            master_fd, slave_fd = pty.openpty()
            self._master_fd = master_fd
            self._proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
        except Exception as e:
            self.last_error = f"spawn: {e}"
            return
        try:
            if self._controller:
                os.write(master_fd,
                         f"select {self._controller}\n".encode())
                time.sleep(0.15)
            # Disable every bluez discovery filter so we see raw-like LE
            # advertising traffic instead of the heavily-deduped default.
            # Without these, bluetoothctl only emits RSSI for a fresh or
            # strongly-moving device — which is why handhelds doing raw HCI
            # (ESP32 / Marauder / Bruce) see 10× more than we do.
            for cmd in (b"power on\n",
                        b"agent off\n",
                        b"menu scan\n",
                        b"clear\n",                # reset any leftover filter
                        b"transport le\n",         # LE-only
                        b"duplicate-data on\n",    # re-emit RSSI on repeats
                        b"rssi 0\n",               # no RSSI floor
                        b"pathloss 0\n",           # no pathloss floor
                        b"back\n",
                        b"scan on\n"):
                os.write(master_fd, cmd)
                time.sleep(0.07)
        except Exception as e:
            self.last_error = f"setup: {e}"
            return

        current_mac: str | None = None
        names: dict[str, str] = {}
        buf = b""
        last_emit = time.monotonic()
        while not self._stop.is_set() and self._proc.poll() is None:
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break
            now = time.monotonic()
            if now - last_emit >= self.emit_interval_s:
                self._flush()
                last_emit = now
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                line = _strip_ansi(raw.decode(errors="replace")).strip()
                if not line:
                    continue
                low = line.lower()
                if ("no default controller" in low
                        or "failed to set power" in low
                        or "org.bluez.error" in low
                        or "unable to open" in low
                        or "blocked" in low):
                    self.last_error = line[:70]
                d = _DEVICE_RE.search(line)
                if d:
                    current_mac = d.group(1).lower()
                    trailing = (d.group(2) or "").strip()
                    if trailing and not trailing.lower().startswith(("rssi", "txpower", "uuid", "manufacturer")):
                        names.setdefault(current_mac, trailing)
                n = _NAME_RE.search(line)
                if n and current_mac:
                    names[current_mac] = n.group(1).strip()
                r = _RSSI_RE.search(line)
                if r and current_mac:
                    rssi = int(r.group(1) or r.group(2))
                    self.events += 1
                    prev = self._pending.get(current_mac)
                    # Keep the strongest sighting in the window — that is the
                    # closest approach, which is what geo-locating a device
                    # actually wants.
                    if prev is None or rssi > prev.rssi:
                        self._pending[current_mac] = BleObs(
                            mac=current_mac,
                            name=names.get(current_mac, ""),
                            rssi=rssi,
                            first_seen=time.time(),
                        )
                    elif not prev.name:
                        names_now = names.get(current_mac, "")
                        if names_now:
                            self._pending[current_mac] = BleObs(
                                mac=prev.mac, name=names_now,
                                rssi=prev.rssi, first_seen=prev.first_seen,
                            )
        if not self._stop.is_set() and self._proc is not None:
            rc = self._proc.poll()
            if rc:
                self.last_error = self.last_error or f"bluetoothctl exited ({rc})"
        # Hand over whatever the last partial window collected.
        self._flush()


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def parse_bluetoothctl_lines(lines: list[str], now: float) -> list[BleObs]:
    """Pure-python parser for unit tests — fed a list of bluetoothctl output lines."""
    cache: dict[str, BleObs] = {}
    names: dict[str, str] = {}
    current_mac: str | None = None
    for raw in lines:
        line = _strip_ansi(raw).strip()
        d = _DEVICE_RE.search(line)
        if d:
            current_mac = d.group(1).lower()
            trailing = (d.group(2) or "").strip()
            if trailing and not trailing.lower().startswith(("rssi", "txpower", "uuid", "manufacturer")):
                names.setdefault(current_mac, trailing)
        n = _NAME_RE.search(line)
        if n and current_mac:
            names[current_mac] = n.group(1).strip()
        r = _RSSI_RE.search(line)
        if r and current_mac:
            cache[current_mac] = BleObs(
                mac=current_mac,
                name=names.get(current_mac, ""),
                rssi=int(r.group(1) or r.group(2)),
                first_seen=now,
            )
    return list(cache.values())
