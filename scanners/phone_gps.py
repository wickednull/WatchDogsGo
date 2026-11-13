"""Phone-as-GPS bridge: HTTPS server + QR pairing screen.

The Pager shows a QR code; the user scans it with their phone, which opens
this module's served page. The page streams Geolocation API fixes (lat / lon /
accuracy / timestamp) back to a POST endpoint, and each fix is pushed into the
same `GpsState` the rest of the payload reads.

Layout decisions (all MIPS/OpenWrt-friendly):

  * stdlib `http.server` + `ssl` only — no Flask/CherryPy/eventlet.
  * Self-signed certificate, generated once and cached. Generated with the
    `cryptography` package when present, otherwise by shelling out to
    `openssl` (both land on the device; neither is a new dependency for a
    payload that already uploads over HTTPS).
  * HTTPS (not plain HTTP) because browsers need a secure context for the
    Geolocation API. The certificate is self-signed, so the user taps
    "Advanced > proceed" once when the page opens.
"""

from __future__ import annotations

import base64
import datetime as _dt
import ipaddress
import os
import ssl
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .gps import GpsState

# Browsers cap request bodies far below this; a reasonableness limit stops a
# runaway phone queue from pinning the MIPS CPU.
_MAX_BODY = 128 * 1024

_DEFAULT_INDEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "index.html",
)


# ── helpers ────────────────────────────────────────────────────────────────

def _ip_score(iface: str, addr: str) -> int:
    """Rough reachability ranking for the IP embedded in the QR code.

    The phone camps on the management LAN, so the management bridge and the
    classic Pineapple subnet (172.16.42.0/24) must beat client-uplink,
    tunnel and container interfaces the phone can never reach.
    """
    name = iface.split("@")[0].lower()
    if name.startswith(("br-lan", "brlan", "lan")):
        base = 100                      # OpenWrt management bridge
    elif name.startswith("br"):
        base = 80                       # any other bridge
    elif name.startswith(("wl", "ra", "radio", "ath", "phy")):
        base = 40                       # radio interface (AP or client)
    elif name.startswith(("eth", "usb", "enx", "en")):
        base = 30                       # wired / USB
    else:
        base = 10                       # tun/tap/veth/wg/ppp/... last resort
    try:
        if ipaddress.ip_address(addr) in ipaddress.ip_network("172.16.42.0/24"):
            base += 50
    except ValueError:
        pass
    return base


def detect_ip() -> str:
    """Pick the IPv4 a phone on the management LAN should reach.

    Parses `ip -o -4 addr show` and keeps the best-scoring non-loopback,
    non-link-local address (`_ip_score`). Empty string if none exists.
    """
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    best: tuple[int, str] = (-1, "")
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        addr = parts[3].split("/")[0]
        if addr.startswith("127.") or addr.startswith("169.254."):
            continue
        score = _ip_score(parts[1].rstrip(":"), addr)
        if score > best[0]:
            best = (score, addr)
    return best[1]


def _write_pem_files(cert_path: Path, key_path: Path,
                     cert_pem: bytes, key_pem: bytes) -> None:
    cert_path.write_bytes(cert_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    key_path.write_bytes(key_pem)


def _make_cert_crypto(cert_path: Path, key_path: Path,
                      ips: list[str], hostname: str) -> bool:
    """Attempt cert generation with the optional `cryptography` package."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    sans = []
    seen = set()
    for ip in ips + ["127.0.0.1"]:
        if ip not in seen:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
            seen.add(ip)
    sans.append(x509.DNSName("localhost"))
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write_pem_files(cert_path, key_path, cert_pem, key_pem)
    return True


def _make_cert_openssl(cert_path: Path, key_path: Path,
                       ips: list[str], hostname: str) -> bool:
    """Fallback: `openssl req -x509`. Prefers SAN from `-addext` (>= 1.1.1);
    retries without it if the build predates the flag."""
    san = ",".join([f"IP:{ip}" for ip in ips] + ["IP:127.0.0.1", "DNS:localhost"])
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "3650", "-nodes", "-subj", f"/CN={hostname}"]
    try:
        subprocess.run(base + ["-addext", f"subjectAltName={san}"],
                       check=True, capture_output=True, timeout=60)
        return True
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        subprocess.run(base, check=True, capture_output=True, timeout=60)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def ensure_cert(cert_dir: str | os.PathLike, ips: list[str],
                hostname: str = "wdgwars") -> tuple[str, str]:
    """Return (cert_path, key_path), (re)generating a cached pair if needed."""
    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert = cert_dir / "cert.pem"
    key = cert_dir / "key.pem"
    if cert.exists() and key.exists():
        return str(cert), str(key)
    if not (_make_cert_crypto(cert, key, ips, hostname)
            or _make_cert_openssl(cert, key, ips, hostname)):
        raise RuntimeError("certificate generation failed "
                           "(need cryptography or openssl)")
    return str(cert), str(key)


# ── reader ─────────────────────────────────────────────────────────────────

def _pair_handler(reader: "PhoneGpsReader") -> type[BaseHTTPRequestHandler]:
    class PairHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/plain") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, reader.index_html, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found\n")

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            length = max(0, min(length, _MAX_BODY))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = parse_qs(body, keep_blank_values=True)
            lats = fields.get("Lat", [])
            lons = fields.get("Lon", [])
            if not lats or not lons:
                self._send(400, b"missing Lat/Lon\n")
                return
            accs = fields.get("Acc", ["-1"] * len(lats))
            times = fields.get("Time", [])
            n = 0
            for i, (lat, lon) in enumerate(zip(lats, lons)):
                acc = _num(accs[i] if i < len(accs) else "", -1.0)
                ts_ms = _num(times[i] if i < len(times) else "", 0.0)
                if reader.apply_payload(lat, lon, acc, ts_ms):
                    n += 1
            self._send(200, f"ok {n}\n".encode())

    return PairHandler


def _num(raw: str, fallback: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


# ── iOS trust bootstrap ────────────────────────────────────────────────────
#
# iOS Safari has no "proceed" past a self-signed certificate, so the pairing
# page never loads there and the geolocation prompt never fires. The fix is
# to install + fully trust the Pager's certificate as a root. These endpoints
# run on a plain-HTTP sibling port (port + 1) so the phone can reach them
# before any TLS trust exists.

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>PayloadIdentifier</key><string>pl.wdgwars.pager.gpsroot</string>
  <key>PayloadUUID</key><string>%CFG_UUID%</string>
  <key>PayloadDisplayName</key><string>WDGoWars Phone GPS Trust</string>
  <key>PayloadDescription</key><string>Adds the WDGoWars phone-GPS certificate to the trusted root store so iPhones can load the pairing page over HTTPS.</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadDisplayName</key><string>WDGoWars Phone GPS Root</string>
      <key>PayloadIdentifier</key><string>pl.wdgwars.pager.gpsroot.cert</string>
      <key>PayloadUUID</key><string>%CERT_UUID%</string>
      <key>PayloadType</key><string>com.apple.security.root</string>
      <key>PayloadVersion</key><integer>1</integer>
      <key>PayloadCertificateFileName</key><string>wdgwars-gps-root.pem</string>
      <key>PayloadContent</key>
      <data>%CERT_B64%</data>
    </dict>
  </array>
</dict>
</plist>
"""


def _to_der(cert_path: str | os.PathLike) -> bytes:
    """DER form of the PEM certificate (openssl first, cryptography second)."""
    try:
        run = subprocess.run(["openssl", "x509", "-in", str(cert_path),
                              "-outform", "DER"],
                             capture_output=True, timeout=30)
        if run.returncode == 0 and run.stdout:
            return run.stdout
    except Exception:
        pass
    try:
        from cryptography import x509 as _x509
        from cryptography.hazmat.primitives import serialization
        return _x509.load_pem_x509_certificate(
            Path(cert_path).read_bytes()
        ).public_bytes(serialization.Encoding.DER)
    except Exception:
        return b""


def _mobileconfig(cert_path: str | os.PathLike) -> bytes:
    """Apple configuration profile embedding the Pager cert as a root."""
    der = _to_der(cert_path)
    if not der:
        raise RuntimeError("certificate unavailable for mobileconfig")
    return (_PLIST
            .replace("%CERT_B64%", base64.b64encode(der).decode())
            .replace("%CERT_UUID%", str(uuid.uuid4()).upper())
            .replace("%CFG_UUID%", str(uuid.uuid4()).upper())
            .encode())


def _bootstrap_index(reader: "PhoneGpsReader") -> bytes:
    cert = reader.bootstrap_url.rstrip("/") + "/pair.mobileconfig"
    https = reader.url
    return (f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WDGoWars · phone GPS trust</title>
<style>
body{{background:#05060a;color:#e6fffb;font-family:monospace;padding:15px}}
h1{{color:#00ffe5;letter-spacing:2px;font-size:20px;font-weight:normal}}
h2{{color:#39ff14;font-size:13px;letter-spacing:1px}}
ol{{margin:0 0 20px 18px;padding:0}}
li{{margin:6px 0;font-size:14px}}
a{{color:#00ffe5}}
code{{background:#0b111a;padding:1px 6px}}
</style></head><body>
<h1>WDGoWars PHONE GPS</h1>
<p>iPhone/Safari blocks the self-signed pairing page and never asks for
location. Fix it once by trusting the Pager's certificate:</p>
<ol>
<li><a href="{cert}">Download &amp; install the trust profile</a>
(opens Settings → "Profile Downloaded" → Install).</li>
<li>Settings → General → About → <b>Certificate Trust Settings</b> →
enable <b>Full Trust</b> for "WDGoWars Phone GPS Root".</li>
<li>If asked, allow Safari to connect to this device on the local network.</li>
<li>Go back to the Pager and scan the QR again, or open <a href="{https}">
{https}</a>. The location prompt will appear now.</li>
</ol>
<h2>Why</h2>
<p>Safari only calls the GPS for pages it can <b>verify over HTTPS</b>.
"Might not be secure" pages get no location popup.</p>
</body></html>
""").encode()


def _bootstrap_handler(reader: "PhoneGpsReader") -> type[BaseHTTPRequestHandler]:
    class BootstrapHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str,
                  attach: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if attach:
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="wdgwars-gps.mobileconfig"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/pair.mobileconfig":
                try:
                    body = _mobileconfig(reader._cert_path)
                except (RuntimeError, OSError):
                    self._send(503, b"certificate not ready\n")
                    return
                self._send(200, body, "application/x-apple-aspen-config",
                           attach=True)
            elif path == "/cert.pem":
                try:
                    body = Path(reader._cert_path).read_bytes()
                except OSError:
                    self._send(503, b"certificate not ready\n")
                    return
                self._send(200, body, "application/x-pem-file")
            elif path in ("/", "/index.html"):
                self._send(200, _bootstrap_index(reader),
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found\n")

    return BootstrapHandler


class PhoneGpsReader:
    """Runs the HTTPS pairing server and feeds fixes into a `GpsState`.

    Public shape mirrors the old serial reader: `.state`, `.start()`, `.stop()`.
    Once `start()` has run, `.url` holds the QR target and `.ip` the address
    the phone must reach.
    """

    def __init__(self, port: int = 1993, host: str = "",
                 max_accuracy_m: float = 100.0, fix_timeout_s: float = 90.0,
                 cert_dir: str | os.PathLike | None = None,
                 index_path: str | os.PathLike | None = None,
                 clock=None) -> None:
        self.port = int(port)
        self.host = host.strip()
        self.max_accuracy_m = max(1.0, float(max_accuracy_m))
        self.fix_timeout_s = max(1.0, float(fix_timeout_s))
        self._clock = clock or time.time
        self.cert_dir = Path(cert_dir) if cert_dir else \
            Path(os.getenv("WDGWARS_PAYLOAD_DIR",
                           Path(os.path.abspath(__file__)).parent.parent
                           .parent)) / "data" / "phone-gps"
        self.state = GpsState()
        self.state.max_age_s = self.fix_timeout_s
        self.url = ""
        self.ip = ""
        # Diagnostics surfaced on the pairing screen: how many valid payloads
        # arrived, and the most recent one — accepted or not.
        self.feeds = 0
        self.last_feed: tuple[float, float, float, bool] | None = None

        self._index_path = Path(index_path) if index_path else Path(_DEFAULT_INDEX)
        self.index_html = self._index_path.read_bytes() \
            if self._index_path.exists() else b"phone gps\n"
        # Plain-HTTP sibling that serves the iOS trust profile. Port + 1 so
        # the phone can reach it before any TLS trust exists.
        self.bootstrap_port = int(port) + 1
        self.bootstrap_url = ""
        self._cert_path = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thr: threading.Thread | None = None
        self._bhttpd: ThreadingHTTPServer | None = None
        self._bthr: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self.ip = self.host or detect_ip() or "127.0.0.1"
        self.url = f"https://{self.ip}:{self.port}/"
        self.bootstrap_url = f"http://{self.ip}:{self.bootstrap_port}/"
        cert, key = ensure_cert(self.cert_dir, [self.ip])
        self._cert_path = cert

        self._httpd = ThreadingHTTPServer(
            ("0.0.0.0", self.port), _pair_handler(self),
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self._httpd.socket = ctx.wrap_socket(self._httpd.socket,
                                             server_side=True)
        self._thr = threading.Thread(target=self._httpd.serve_forever,
                                     name="phone-gps", daemon=True)
        self._thr.start()

        self._bhttpd = ThreadingHTTPServer(
            ("0.0.0.0", self.bootstrap_port), _bootstrap_handler(self),
        )
        self._bthr = threading.Thread(target=self._bhttpd.serve_forever,
                                      name="phone-gps-boot", daemon=True)
        self._bthr.start()

    def stop(self) -> None:
        for httpd in (self._httpd, self._bhttpd):
            if httpd:
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                try:
                    httpd.server_close()
                except Exception:
                    pass
        self._httpd = None
        self._bhttpd = None

    # ── fix ingestion ──────────────────────────────────────────────────────

    def apply_payload(self, lat_raw: str | float, lon_raw: str | float,
                      acc: float = -1.0, ts_ms: float = 0.0) -> bool:
        """Validate one phone fix and push it into `self.state`."""
        lat = _num(lat_raw, 999.0)
        lon = _num(lon_raw, 999.0)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return False

        acc = max(0.0, acc)
        accepted = acc <= self.max_accuracy_m
        self.feeds += 1
        self.last_feed = (lat, lon, acc, accepted)
        now = self._clock()
        iso = ""
        if ts_ms > 0:
            try:
                iso = _dt.datetime.fromtimestamp(
                    ts_ms / 1000.0, tz=_dt.timezone.utc
                ).isoformat()
            except (ValueError, OSError, OverflowError):
                iso = ""

        with self.state.lock:
            self.state.lat = lat
            self.state.lon = lon
            self.state.alt_m = 0.0
            self.state.accuracy_m = acc
            self.state.sats = 1
            self.state.fix_quality = 2
            self.state.fix_3d = acc <= self.max_accuracy_m
            self.state.utc_iso = iso or self.state.utc_iso
            self.state.last_update = now
            self.state.device = "phone"
            self.state._record(now, lat, lon, 0.0, acc, self.state.fix_3d)
        return True


# ── QR pairing screen ──────────────────────────────────────────────────────

_CARD_X = 10
_CARD_Y = 30
_CARD_W = 200
_CARD_H = 174
_QR_PAD = 12          # light "scanner" panel inset inside the card
_URL_X = 222


def show_pairing(p, pal, reader: PhoneGpsReader) -> bool:
    """Draw the QR + status screen until a fix arrives. Returns True on fix,
    False if the user cancels with B."""
    from .qr import QrCode, fit_scale
    from ui import theme, idle
    from ui.dialog import _multiline

    qr = QrCode(reader.url.rstrip("/"))

    o_x = _CARD_X + _QR_PAD
    o_y = _CARD_Y + _QR_PAD
    o_w = _CARD_W - _QR_PAD * 2
    o_h = _CARD_H - _QR_PAD * 2
    px, cx, cy = fit_scale(qr, o_w, o_h)
    text_w = p.width - _URL_X - 10

    title_y = _CARD_Y + 4
    url_y = _CARD_Y + 20
    note_y = url_y + 24
    hdr2_y = note_y + 8
    step1_y = hdr2_y + 8
    step_gap = 18
    card_y = step1_y + 3 * step_gap + 8
    card_h = p.height - card_y - theme.FOOTER_H - 2

    dot = 0
    steps = [
        ("1", "connect to pager Wi-Fi"),
        ("2", "scan the code with camera"),
        ("3", "allow location sharing"),
    ]
    while True:
        snap = reader.state.snapshot()
        has_fix = snap.fix_3d

        theme.clear_bg(p, pal)
        theme.draw_header(p, pal, "PHONE GPS", "scan code with phone",
                          dot="ok" if has_fix else "warn")

        # ── QR card ─────────────────────────────────────────────────────
        p.fill_rect(_CARD_X, _CARD_Y, _CARD_W, _CARD_H, pal.surface)
        p.rect(_CARD_X, _CARD_Y, _CARD_W, _CARD_H, pal.cyan_dim)
        p.rect(_CARD_X + 1, _CARD_Y + 1, _CARD_W - 2, _CARD_H - 2, pal.bg)
        # light scanner panel — the phone camera locks onto this rectangle
        p.fill_rect(o_x, o_y, o_w, o_h, p.hex_color(0xF6F8FA))
        # reticle ticks
        c = pal.fg_dim
        p.hline(o_x - 2, o_y, 8, c); p.vline(o_x, o_y - 2, 8, c)
        p.hline(o_x + o_w - 6, o_y, 8, c); p.vline(o_x + o_w - 1, o_y - 2, 8, c)
        p.hline(o_x - 2, o_y + o_h - 1, 8, c); p.vline(o_x, o_y + o_h - 7, 8, c)
        p.hline(o_x + o_w - 6, o_y + o_h - 1, 8, c); p.vline(o_x + o_w - 1, o_y + o_h - 7, 8, c)
        qr.draw(p, o_x + cx + 4 * px, o_y + cy + 4 * px, px, p.hex_color(0x05060A))

        # scan beam sweeping the panel
        span = o_h - 2
        beam = o_y + (dot % span)
        p.fill_rect(o_x, beam, o_w, 1, pal.cyan)
        p.fill_rect(o_x, max(o_y, beam - 1), o_w, 1, pal.cyan_glow)

        # ── right column ────────────────────────────────────────────────
        p.draw_text(_URL_X, title_y, "LINK THIS PHONE", pal.cyan, theme.FONT_BODY)
        p.fill_rect(_URL_X, url_y, text_w, 22, pal.bg_dim)
        p.rect(_URL_X, url_y, text_w, 22, pal.cyan_dim)
        _multiline(p, pal, reader.url, _URL_X + 4, url_y + 2,
                   text_w - 8, pal.magenta, size=theme.FONT_HINT)
        p.draw_text(_URL_X + 4, note_y,
                    "self-signed cert - tap 'proceed'", pal.fg_dim,
                    theme.FONT_HINT)

        p.draw_text(_URL_X, hdr2_y, "ON YOUR PHONE", pal.fg_dim,
                    theme.FONT_HINT)
        for i, (num, label) in enumerate(steps):
            sy = step1_y + i * step_gap
            p.fill_rect(_URL_X, sy, 14, 14, pal.surface_hi)
            p.rect(_URL_X, sy, 14, 14, pal.cyan_dim)
            p.draw_text(_URL_X + 4, sy + 3, num, pal.cyan, theme.FONT_HINT)
            p.draw_text(_URL_X + 20, sy + 3, label, pal.fg, theme.FONT_HINT)

        # status card
        theme.draw_divider(p, pal, _URL_X, card_y - 5, text_w)
        p.fill_rect(_URL_X, card_y, text_w, card_h, pal.surface)
        p.rect(_URL_X, card_y, text_w, card_h, pal.cyan_dim)
        if has_fix:
            p.draw_text(_URL_X + 4, card_y + 4, "FIX LOCKED", pal.green,
                        theme.FONT_BODY)
            p.draw_text(_URL_X + 4, card_y + 22,
                        f"acc {snap.accuracy_m:.0f} m", pal.green,
                        theme.FONT_BODY)
            p.draw_text(_URL_X + 4, card_y + 36,
                        f"{snap.lat:.5f} {snap.lon:.5f}", pal.fg,
                        theme.FONT_HINT)
        else:
            p.draw_text(_URL_X + 4, card_y + 4, "AWAITING FIX", pal.amber,
                        theme.FONT_BODY)
            if reader.feeds == 0:
                body2 = "no signal from phone"
                hint = (f"iPhone? {reader.bootstrap_url} first"
                        if reader.bootstrap_url else "open the page on the phone")
            else:
                _lat, _lon, acc, ok = reader.last_feed
                if not ok:
                    body2 = f"acc {acc:.0f} m too poor"
                    hint = (f"needs <= {reader.max_accuracy_m:.0f} m "
                            "- go outside")
                else:
                    body2 = f"last fix {snap.age_s:.0f}s ago"
                    hint = "stale - restart the stream"
            p.draw_text(_URL_X + 4, card_y + 22, body2, pal.amber,
                        theme.FONT_BODY)
            p.draw_text(_URL_X + 4, card_y + 36, hint, pal.fg_dim,
                        theme.FONT_HINT)
            p.draw_text(_URL_X + 4, card_y + 44,
                        f"ok when acc <= {reader.max_accuracy_m:.0f} m",
                        pal.fg_dim, theme.FONT_HINT)

        theme.draw_footer(p, pal, [("B", "cancel")])
        p.flip()

        if has_fix:
            return True

        mgr = idle.get()
        if mgr:
            mgr.tick()
        if p.has_input_events():
            ev = p.get_input_event()
            if ev:
                if mgr and mgr.wake_consume():
                    while p.has_input_events():
                        p.get_input_event()
                elif ev[0] == p.BTN_B:
                    return False
        p.delay(200)
        dot += 1