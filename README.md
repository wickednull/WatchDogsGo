# WDGoWars Wardriver — WiFi Pineapple Pager payload

Fork of [LOCOSP's WDGoWars](https://github.com/LOCOSP/pineapple_pager_wdgwars),
turning a [Hak5 WiFi Pineapple Pager](https://docs.hak5.org/wifi-pineapple-pager/)
into an offline-first **WiFi + BLE wardriver** that feeds
[wdgwars.pl](https://wdgwars.pl).

The fork's main difference is GPS: **no USB dongle required**. Fixes can come
from the phone through the [Mobile2GPS](https://github.com/ryanpohlner/mobile2gps)
payload, or from any gpsd-backed USB receiver — the app just reads gpsd either
way. It also refuses to scan on the radio that carries the phone link without
asking first.

- **Phone GPS or USB GPS.** Mobile2GPS feeds gpsd from the phone's browser over
  HTTPS; a u-blox/Quectel serial stick works exactly as before.
- **2D or 3D fix accepted.** Altitude is not used for wardriving, and phone
  feeds report no satellite count — a 2D fix with unknown sats is enough.
- **Two WiFi capture backends.** Passive monitor-mode beacon capture when a
  monitor interface is available, `iw dev … scan` with rotating band passes
  otherwise.
- **BLE LE capture** via `bluetoothctl` under a pty (async `[CHG] RSSI` events),
  auto-selecting an adapter and starting `bluetoothd` / clearing rfkill blocks
  when they get in the way.
- **Movement-aware deduplication.** A row is written for a new BSSID, after you
  have moved far enough for the sighting to add information, or when the signal
  got materially stronger.
- **WigleWifi-1.6 CSV** in `/mmc/root/loot/wdgwars/sessions/`.
- **Manual SYNC NOW** uploads pending CSVs to `POST /api/upload-csv`, switching
  to the async `POST /api/v2/upload-csv` queue (gzipped) for large files.
- **UPLOAD LOG** reads `GET /api/upload-history` so the server's `imported` /
  `captured` / `no_gps` / `bad_rows` counts are visible on the device.
- **Management-radio protection.** Auto-selection prefers a monitor interface on
  a radio that is not carrying the management AP; using that radio requires
  confirmation because it drops the phone GPS link.
- **App handoff** to Loki / PagerGotchi / WiFMan / Bjorn via the
  `exit 42 + data/.next_payload` protocol.
- **Idle dim** after 20 s (configurable) to keep the Pager cool.

## Requirements

| | |
|---|---|
| Hardware | WiFi Pineapple Pager |
| Firmware | 1.x runs WDGoWars; **Mobile2GPS needs 2.0.0+** (`GPS_CONFIGURE`) |
| Python | `python3` + `python3-ctypes` (the payload offers to install them) |
| pagerctl | `pagerctl.py` + `libpagerctl.so` (bootstrap copies them from WiFMan or downloads them) |
| GPS | `gpsd` + `gpsd-clients` (bootstrap installs them) |
| WiFi | `iw`; `tcpdump-mini` for the monitor-mode backend |
| BLE | `bluez-utils` + `bluez-daemon`; `kmod-bluetooth` / `kmod-btusb` for the radio and USB dongles |
| Network | Internet only for uploads and first-time package/GPS-binary downloads |

## Install

The conventional path is `wdgwars`, but any folder name works — the launcher
and `payload.sh` locate the app by `wdgwars.py`. On the Pager `/root` and
`/mmc/root` are the same tree, so `/root/payloads/...` in the UI equals
`/mmc/root/payloads/...` over SSH/SCP.

```sh
git clone https://github.com/wickednull/WatchDogsGo.git \
  /mmc/root/payloads/user/reconnaissance/wdgwars
cd /mmc/root/payloads/user/reconnaissance/wdgwars
sh bootstrap.sh
chmod +x payload.sh bootstrap.sh launch_*.sh launchers/*.sh
```

If the folder already exists, update it instead of cloning:

```sh
git -C /mmc/root/payloads/user/reconnaissance/wdgwars pull
```

`bootstrap.sh` verifies the tree is complete, copies pagerctl from an installed
WiFMan payload (or downloads it), opkg-installs `iw`, `bluez-utils`,
`kmod-usb-acm`, `gpsd`, `gpsd-clients` and `tcpdump-mini` (best-effort),
creates the loot directory, and pushes `launch_wdgwars.sh` into every peer
payload it finds (Loki / PagerGotchi / WiFMan / Bjorn).

**Copy the whole folder**, not just the shell scripts. If only `payload.sh`
lands on the Pager, it stops with `=== WDGoWars FILES MISSING ===`.

Then launch **Payloads → User → Reconnaissance → WDGoWars Wardriver**.

### API key

Get one at <https://wdgwars.pl/profile> → *Generate API key*, then either edit
`config.json` on the Pager (`api_key`) or use **CONFIG → EDIT API KEY**. The key
lives only on the device.

## GPS setup

### Phone GPS (no dongle)

Mobile2GPS is a separate third-party payload and is **not in the Pager Portal
yet** — copy it from its repository:

```sh
git clone --depth 1 https://github.com/ryanpohlner/wifipineapplepager-payloads.git /tmp/wpp
mkdir -p /root/payloads/user/general/mobile2gps
cp -a /tmp/wpp/library/user/general/mobile2gps/. \
      /root/payloads/user/general/mobile2gps/
```

The payload script is only half of it — the server binary downloads on first
run. Either launch **Mobile2GPS** once from the Pager's payload menu (General)
and let it download, or fetch it directly:

```sh
cd /root/payloads/user/general/mobile2gps
curl -k -L -o Mobile2GPS \
  https://github.com/ryanpohlner/mobile2gps/releases/latest/download/Mobile2GPS
chmod +x Mobile2GPS
```

Then, on the Pager:

1. **CONFIG → PHONE GPS** → *Start the Mobile2GPS server*.
2. Join the Pager's **Management AP** from the phone.
3. Open **`https://<pager-ip>:1993`** — the server is HTTPS-only, so `http://`
   fails with *"client sent an http request to a https server"*. Accept the
   self-signed certificate warning.
4. Press **Start**, allow location, and select **Precise Location** if offered.
5. Keep the page in the foreground. WDGoWars' GPS wait screen should move from
   `no fix` to `2D`/`3D` and the scan starts.

Mobile2GPS converts browser coordinates to NMEA `GPRMC`, points gpsd at its
virtual serial device via `GPS_CONFIGURE`, and WDGoWars reads gpsd. Because
`GPRMC` carries no satellite data, `sats` shows `n/a`; that is expected.

### USB GPS

Plug in the receiver. gpsd auto-detects `/dev/ttyACM*` / `/dev/ttyUSB*`; the
Pager's own **Settings → GPS** page (or `GPS_CONFIGURE`) owns port and baud
selection. WDGoWars never opens the serial device itself, so it does not
duplicate those settings — use **CONFIG → GPS STATUS** to see what gpsd
currently reports. The wait screen shows the live device, satellite count and
fix mode.

`gps.min_sats` only blocks fixes when gpsd actually reports a satellite count,
so a real dongle with a weak constellation is still refused, while a phone feed
is not.

### Checking gpsd directly

```sh
gpspipe -w -n 5
```

A `"class":"TPV","mode":2` (or `3`) line with `lat`/`lon` means the feed is good.
`mode:0`/`1` or no TPV means gpsd has no position — check `pidof gpsd` and, for
phone GPS, that Mobile2GPS is running on firmware 2.0.0+.

## UI map

```
MAIN MENU
├── WARDRIVE BOTH      WiFi + BLE
├── WARDRIVE WIFI      WiFi only
├── WARDRIVE BT        BLE only
│      └── GPS WAIT    dev / sats / 2D-3D, B = cancel
│          └── LIVE HUD (A = pause/resume, B = end, ↑↓ = brightness)
├── SYNC NOW           upload queued CSVs
├── SESSIONS           CSV list (v = uploaded, ^ = pending, x = error)
├── UPLOAD LOG         server-side import history
├── CONFIG
│   ├── API KEY        masked view
│   ├── EDIT API KEY   on-screen hex keyboard
│   ├── TEST CONNECTION
│   ├── SCAN SETUP
│   │   ├── WIFI SOURCE       auto / force monitor / force iw / pick interface
│   │   ├── BLE SOURCE        auto / hci0 / hci1 / ...
│   │   ├── BAND PLAN         rotate / 2.4 only / 2.4+5 / full sweep
│   │   ├── MONITOR HOP
│   │   ├── MOVE FILTER +/-
│   │   ├── REFRESH TTL +/-
│   │   └── REQUIRE GPS FIX
│   ├── GPS STATUS     live gpsd device, fix mode, sats, accuracy
│   ├── PHONE GPS      start Mobile2GPS / show pairing URL
│   ├── BRIGHTNESS +/-
│   ├── IDLE TIMEOUT +/-
│   └── DIM LEVEL +/-
├── JUMP TO ...        Loki / PagerGotchi / WiFMan / Bjorn
└── POWER OFF          quit the payload back to the Pager UI
```

The big HUD number is rows **written to the CSV**; the small "seen" number is
raw sightings, which climbs faster because APs are re-seen every pass.

## Capture backends

| | Monitor mode | `iw scan` |
|---|---|---|
| Needs | monitor interface + `tcpdump` | `iw` only |
| Sees | every beacon | one snapshot per pass |
| Position samples | continuous | one per band pass |
| Coverage | hops channels itself | rotating band passes |

Auto-selection prefers a monitor interface that is **not** on the management
phy. `wlan0*` carries the management AP; scanning there drops the phone link,
so WDGoWars asks before using it. An external adapter staged as `wlan2mon` is
used automatically.

## `config.json`

| Key | Default | Meaning |
|---|---|---|
| `api_key` | `""` | wdgwars.pl API key |
| `gps.min_sats` | `4` | Minimum satellites **when reported**; ignored for phone feeds |
| `scan.wifi_iface` | `"auto"` | `auto`, `monitor`, `scan`, or an interface name |
| `scan.ble_iface` | `"auto"` | BLE adapter: `auto` or `hci0`/`hci1`/… |
| `scan.band_plan` | rotate plan | Band sequence used by `iw scan` |
| `scan.wifi_interval_s` | `0` | Pause between band passes; `0` = as fast as possible |
| `scan.flush_bss_cache` | `true` | Purge the kernel BSS cache so results are fresh |
| `scan.monitor_hop` | `true` | Let WDGoWars hop channels in monitor mode |
| `scan.monitor_dwell_ms` | `250` | Dwell per channel |
| `scan.ble_interval_s` | `12` | BLE scan cycle |
| `scan.emit_interval_s` | `1.0` | Observation emit cadence |
| `scan.min_move_m` | `30` | Movement before an AP is logged again |
| `scan.rssi_delta_db` | `6` | Signal change that re-logs an AP without moving |
| `scan.refresh_ttl_s` | `300` | Slow re-log while standing still |
| `scan.require_fix` | `true` | Refuse to write rows without a GPS fix |
| `scan.hud_interval_ms` | `500` | HUD refresh |
| `scan.poll_interval_ms` | `150` | Input/queue poll |
| `storage.max_file_mb` | `30` | Session CSV rotation size |
| `storage.loot_dir` | `/mmc/root/loot/wdgwars` | Loot root |
| `storage.flush_interval_s` | `2.0` | Buffered write flush |
| `upload.mode` | `"auto"` | `auto`, `v1`, or `v2` |
| `ui.brightness` | `70` | Backlight % |
| `ui.rotation` | `270` | Panel rotation |
| `ui.idle_timeout_s` | `20` | Dim after N seconds |
| `ui.auto_dim_level` | `10` | Dim backlight % |

## Files, uploads and handoff

- Sessions: `/mmc/root/loot/wdgwars/sessions/` (`WigleWifi-1.6` CSV).
- `SYNC NOW` probes `GET /api/me`, then uploads pending files. Successful files
  get a `.uploaded` marker; failures get `.error` with the reason.
- `UPLOAD LOG` shows the server's own import counters.
- JUMP TO writes `data/.next_payload` and exits 42; `payload.sh` execs the
  chosen launcher without restarting the whole Pager UI.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `=== MISSING DEPENDENCY: pagerctl ===` | Run `bootstrap.sh`, or install WiFMan; the payload also searches peer payload `lib/` dirs |
| `=== WDGoWars FILES MISSING ===` | The folder is a partial copy — copy `wdgwars.py`, `scanners/`, `ui/`, `storage/`, `uploader/`, `config.json` |
| Payload exits immediately | The last 8 lines of `/tmp/wdgwars.err` are printed on screen; the full file stays for SSH |
| Endless `Waiting for GPS fix` | `gpspipe -w -n 5`: no TPV → gpsd/Mobile2GPS; `mode:2` with lat/lon → feed is fine |
| BLE sees 0 devices | `hciconfig -a`, `pidof bluetoothd`; `service bluetoothd restart`; USB dongle → `opkg install kmod-btusb`; `rfkill unblock bluetooth`; pick the adapter in **SCAN SETUP → BLE SOURCE** |
| `client sent an http request to a https server` | Use `https://<pager-ip>:1993`, not `http://` |
| Mobile2GPS "not found" but installed | It is discovered by folder name anywhere under `/root/payloads` or `/mmc/root/payloads`; check with `find / -iname '*mobile2gps*'` |
| Mobile2GPS exits at start | Needs Pager firmware 2.0.0+ (`GPS_CONFIGURE`) |
| Phone drops off Wi-Fi while scanning | You are scanning the management radio; use an external monitor adapter or confirm the warning |
| `set: Illegal option -` / `: not foundh` | CRLF line endings: `sed -i 's/\r$//' *.sh` |

## Layout

```
.
├── payload.sh          # pager manifest + dependency checks + launcher loop
├── bootstrap.sh        # one-time: tree check, pagerctl, opkg packages, launchers
├── config.json         # settings (see table above)
├── wdgwars.py          # entry point, menus, scan loop, actions
├── handoff.py          # APP_HANDOFF discovery / exit 42
├── scanners/           # iface, wifi, monitor, ble, gps (gpsd), wigle_auth
├── storage/            # WigleWifi-1.6 writer + movement-aware dedup
├── uploader/           # multipart v1 / async v2 upload + history
├── ui/                 # theme, splash, menu, HUD, dialogs, keyboard, idle
├── launchers/          # launch_wdgwars.sh pushed into peer payloads
├── launch_*.sh         # jump-to scripts for Loki / WiFMan / PagerGotchi / Bjorn
├── assets/             # background image
└── lib/                # pagerctl.py + libpagerctl.so (created by bootstrap)
```

## Credits

- Original WDGoWars: [LOCOSP](https://github.com/LOCOSP/pineapple_pager_wdgwars)
- Phone-as-GPS server: [Mobile2GPS](https://github.com/ryanpohlner/mobile2gps)
  by Ryan Pohlner
- pagerctl bindings: WiFMan payload
- Platform: Hak5 WiFi Pineapple Pager / OpenWrt

Fork maintained by wickednull. Version 1.3.0.
