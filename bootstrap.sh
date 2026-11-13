#!/bin/sh
# Run once on the pager to fetch pagerctl bindings from the wifman payload
# and ensure runtime dependencies are installed.
#
# If this script fails with "Illegal option -" or ": not foundh", the
# file has CRLF line endings (usual cause: cloned/unpacked on Windows).
# Fix with:  sed -i 's/\r$//' bootstrap.sh payload.sh launch_*.sh
# The repo's .gitattributes forces LF on text files so a fresh `git clone`
# should be immune.

PAYLOAD_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$PAYLOAD_DIR/lib"
WIFMAN_DIR="/root/payloads/user/reconnaissance/wifman"
WIFMAN_LIB="$WIFMAN_DIR/lib"

mkdir -p "$LIB"

echo "[bootstrap] payload tree"
_tmp_missing=0
for f in wdgwars.py handoff.py config.json \
         scanners/gps.py scanners/wifi.py ui/menu.py ui/theme.py \
         storage/session.py uploader/wdgwars.py; do
    if [ ! -f "$PAYLOAD_DIR/$f" ]; then
        echo "  warn: missing $f"
        _tmp_missing=1
    fi
done
if [ "$_tmp_missing" = 1 ]; then
    echo "  This folder is an incomplete copy."
    echo "  Copy the whole payload folder, not just the scripts."
fi

echo "[bootstrap] pagerctl bindings"
# Look for an existing pagerctl pair anywhere a payload install keeps it.
# pagerctl.py loads libpagerctl.so from its own directory, so we need a dir
# that holds both files.
SRC_LIB=""
for d in "$LIB" \
         "$PAYLOAD_DIR" \
         "$WIFMAN_LIB" \
         "$WIFMAN_DIR" \
         /mmc/usr/lib \
         /mmc/root/payloads/user/reconnaissance/*/lib \
         /root/payloads/user/reconnaissance/*/lib \
         /root/payloads/user/*/*/lib \
         /usr/local/lib /usr/lib; do
    if [ -f "$d/pagerctl.py" ] && [ -f "$d/libpagerctl.so" ]; then
        SRC_LIB="$d"
        break
    fi
done

if [ -n "$SRC_LIB" ]; then
    cp -f "$SRC_LIB/pagerctl.py"    "$LIB/"
    cp -f "$SRC_LIB/libpagerctl.so" "$LIB/"
    echo "  copied from $SRC_LIB"
else
    WIFMAN_BASE="https://raw.githubusercontent.com/LOCOSP/pineapple_pager_wifman/main/wifman/lib"
    for f in pagerctl.py libpagerctl.so; do
        if [ ! -f "$LIB/$f" ]; then
            wget -q -O "$LIB/$f.tmp" "$WIFMAN_BASE/$f" && mv "$LIB/$f.tmp" "$LIB/$f" \
                && echo "  downloaded $f" \
                || echo "  warn: download of $f failed (pager offline?)"
        fi
    done
fi

echo "[bootstrap] opkg runtime packages (best-effort, needs internet)"
opkg update >/dev/null 2>&1 || true
for pkg in iw bluez-utils kmod-usb-acm gpsd gpsd-clients tcpdump-mini; do
    opkg list-installed 2>/dev/null | grep -q "^$pkg " \
        || opkg install "$pkg" >/dev/null 2>&1 \
        || echo "  warn: could not install $pkg"
done

mkdir -p /mmc/root/loot/wdgwars/sessions

chmod +x "$PAYLOAD_DIR/payload.sh" 2>/dev/null
chmod +x "$PAYLOAD_DIR"/launch_*.sh 2>/dev/null
# The outgoing launcher is advertised in the app menu too, so it needs its bit.
chmod +x "$PAYLOAD_DIR"/launchers/*.sh 2>/dev/null

# Push the reverse launcher (launch_wdgwars.sh) into every installed peer
# payload so they can JUMP TO WDGoWars. Skips peers that aren't installed.
echo "[bootstrap] installing reverse JUMP TO launcher in peers"
RL="$PAYLOAD_DIR/launchers/launch_wdgwars.sh"
if [ -f "$RL" ]; then
    PEERS="/root/payloads/user/reconnaissance"
    for peer in loki pagergotchi wifman pager_bjorn; do
        if [ -d "$PEERS/$peer" ]; then
            cp "$RL" "$PEERS/$peer/launch_wdgwars.sh" \
                && chmod +x "$PEERS/$peer/launch_wdgwars.sh" \
                && echo "  -> $peer"
        fi
    done
else
    echo "  warn: $RL missing, skipping (outgoing JUMP TO still works)"
fi

echo "[bootstrap] done"
echo "[bootstrap] GPS: plug in a USB dongle, or use the Mobile2GPS"
echo "            payload for phone GPS (github.com/ryanpohlner/"
echo "            wifipineapplepager-payloads, general/mobile2gps)."
