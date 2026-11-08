#!/usr/bin/env bash
# Install or update TrafficLight on a Raspberry Pi (64-bit Raspberry Pi OS), in place:
# the checkout this script lives in becomes the installation, and the Pi is set up for the
# sign. Run it as the normal user that should own the service (not with sudo; it calls sudo
# where needed). Safe to re-run: it only changes what is not set up yet, and it works
# offline when nothing is missing. Usually run by scripts/update_pi.sh from the PC.
#
#   scripts/install_pi.sh [--sign-port IFACE | --no-sign-port] [--hostname NAME]
#                         [--timezone Zone/City] [--hotspot SSID [--hotspot-password PW]]
#
#   --sign-port IFACE    the port the LED card is plugged into (default eth0): gets 10.75.0.1/16
#   --no-sign-port       leave the network ports alone
#   --hostname NAME      rename the Pi; the UI is then at http://NAME.local:8080
#   --timezone ZONE      e.g. America/Los_Angeles (the clock clip shows local time)
#   --hotspot SSID       a Wi-Fi network of the Pi's own, used when no known Wi-Fi is in range;
#                        the password is asked for unless given (8 to 63 characters)
set -euo pipefail

usage() { sed -n '8,16p' "$0" | sed 's/^# \{0,1\}//'; }

sign_port=eth0 new_host="" tz="" hotspot="" hotspot_pw=""
while (( $# )); do
    case "$1" in
        --sign-port) sign_port="${2:?--sign-port needs an interface}"; shift 2 ;;
        --no-sign-port) sign_port=""; shift ;;
        --hostname) new_host="${2:?--hostname needs a name}"; shift 2 ;;
        --timezone) tz="${2:?--timezone needs a zone}"; shift 2 ;;
        --hotspot) hotspot="${2:?--hotspot needs a network name}"; shift 2 ;;
        --hotspot-password) hotspot_pw="${2:?--hotspot-password needs a password}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [[ $EUID -eq 0 ]]; then
    echo "Run this as the normal user, not root: it uses sudo itself." >&2
    exit 1
fi
if [[ $REPO == *[[:space:]]* ]]; then
    echo "The install path must not contain spaces: $REPO" >&2
    exit 1
fi
case "$(uname -m)" in
    aarch64|x86_64) ;;
    *) echo "Need a 64-bit OS (aarch64); this is $(uname -m). The wheels for numpy/av are 64-bit only." >&2; exit 1 ;;
esac
# Check the options before changing anything, so a typo costs nothing.
if [[ -n $new_host && ! $new_host =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    echo "--hostname: use lower-case letters, digits and '-' (not at either end): $new_host" >&2
    exit 2
fi
if [[ -n $tz && ! -f /usr/share/zoneinfo/$tz ]]; then
    echo "--timezone: unknown zone $tz (see: timedatectl list-timezones)" >&2
    exit 2
fi
if [[ -n $hotspot_pw && ( ${#hotspot_pw} -lt 8 || ${#hotspot_pw} -gt 63 ) ]]; then
    echo "--hotspot-password must have 8 to 63 characters." >&2
    exit 2
fi
if [[ -n $hotspot_pw && -z $hotspot ]]; then
    echo "--hotspot-password needs --hotspot SSID." >&2
    exit 2
fi

SIGN_CON=trafficlight-sign HOTSPOT_CON=trafficlight-hotspot
nm_running() { command -v nmcli >/dev/null && [[ "$(nmcli -t -f RUNNING general 2>/dev/null)" == running ]]; }
has_con() { nmcli -t -f NAME connection show | grep -qxF "$1"; }
# Ask for a new hotspot's password now, not after minutes of installing; an existing hotspot
# keeps its password unless one is given.
if [[ -n $hotspot && -z $hotspot_pw ]] && nm_running && ! has_con "$HOTSPOT_CON"; then
    [[ -t 0 ]] || { echo "--hotspot needs --hotspot-password when not run in a terminal." >&2; exit 2; }
    while :; do
        read -rsp "Password for the Wi-Fi network $hotspot (8-63 characters): " hotspot_pw; echo
        (( ${#hotspot_pw} >= 8 && ${#hotspot_pw} <= 63 )) || { echo "8 to 63 characters."; continue; }
        read -rsp "Again: " again; echo
        [[ $again == "$hotspot_pw" ]] && break
        echo "They differ."
    done
fi

# --- apt packages --------------------------------------------------------------------
# python3-venv: venv + ensurepip (Debian splits it out of python3).
# libportaudio2: sounddevice's wheel is pure Python and loads the system PortAudio
#   (checked: sounddevice-0.5.6 has no bundled library on Linux).
# gcc, libc6-dev: build the C transmit loop below (Raspberry Pi OS has them; Lite images may not).
# Nothing else: the aarch64 wheels of numpy, pillow, av (FFmpeg, ALSA included), pydantic,
# uvloop... are manylinux and bundle their libraries (checked in av.libs / pillow.libs).
missing=()
for pkg in python3-venv libportaudio2 gcc libc6-dev; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
    echo "Installing apt packages: ${missing[*]}"
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends "${missing[@]}"
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
    { echo "Python 3.11 or newer is required (Raspberry Pi OS Bookworm or later)." >&2; exit 1; }

# --- venv and packages ---------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
    echo "Creating .venv"
    python3 -m venv .venv
fi
want="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [[ "$(cat .venv/.requirements.sha256 2>/dev/null || true)" != "$want" ]]; then
    echo "Installing Python packages (needs internet)"
    .venv/bin/python -m pip install --disable-pip-version-check --no-input -r requirements.txt
    echo "$want" > .venv/.requirements.sha256
fi

# The LED output's transmit loop in C (trafficlight/output/hub75/native.c): without it the Python loop
# runs, which holds a Pi 4 near 28 frames a second.
native=trafficlight/output/hub75
if cc -O2 -Wall -shared -fPIC -o "$native/_native-$(uname -m).so" "$native/native.c"; then
    echo "Built the C transmit loop"
else
    echo "WARNING: the C transmit loop did not build; the slower Python loop will be used." >&2
fi

if [[ ! -f web/dist/index.html ]]; then
    echo "WARNING: web/dist is missing: the UI is not built. The Pi has no Node; build it on the PC" >&2
    echo "         and deploy with scripts/update_pi.sh (see README)." >&2
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
# install_file TEXT DEST: write TEXT and a final newline to DEST (root-owned) only when that differs;
# true if it changed.
install_file() {
    printf '%s\n' "$1" > "$tmp"
    sudo cmp -s "$tmp" "$2" && return 1
    sudo install -D -m 644 "$tmp" "$2"
}

# --- systemd unit --------------------------------------------------------------------
if install_file "$(sed -e "s|@USER@|$(id -un)|g" -e "s|@REPO@|$REPO|g" scripts/trafficlight.service)" \
        /etc/systemd/system/trafficlight.service; then
    sudo systemctl daemon-reload
fi

# --- journald: RAM only --------------------------------------------------------------
# Constant log writes to an SD card are wear and one more thing a power cut can interrupt.
# Logs then live in RAM (capped) and vanish at reboot: this sign is not debugged after the fact.
if install_file $'[Journal]\nStorage=volatile\nRuntimeMaxUse=64M' /etc/systemd/journald.conf.d/trafficlight.conf; then
    sudo systemctl restart systemd-journald
fi

# --- UDP buffers ---------------------------------------------------------------------
# The LED driver asks for a 1 MiB send and 256 KiB receive buffer (trafficlight/output/fpga.py);
# Linux silently caps both at 208 KiB unless these maximums allow more.
if install_file $'# TrafficLight: let the LED driver have the socket buffers it asks for.\nnet.core.wmem_max = 1048576\nnet.core.rmem_max = 262144' \
        /etc/sysctl.d/90-trafficlight.conf; then
    sudo sysctl -q -p /etc/sysctl.d/90-trafficlight.conf
fi

# --- time zone and name --------------------------------------------------------------
if [[ -n $tz && "$(timedatectl show -p Timezone --value 2>/dev/null)" != "$tz" ]]; then
    echo "Time zone: $tz"
    sudo timedatectl set-timezone "$tz"
fi
if [[ -n $new_host && "$(hostname)" != "$new_host" ]]; then
    echo "Renaming the Pi: $(hostname) -> $new_host"
    # /etc/hosts first: sudo looks the host name up and complains while it does not resolve.
    if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
        sudo sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$new_host/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$new_host" | sudo tee -a /etc/hosts >/dev/null
    fi
    sudo hostnamectl set-hostname "$new_host"
    sudo systemctl try-restart avahi-daemon.service     # announce NAME.local now, not after a reboot
fi

# --- network: the sign's port and the hotspot (docs/HARDWARE.md section 4) ---------------
# add_or_modify NAME TYPE PROPERTY VALUE...: the profile ends up with these properties either way.
add_or_modify() {
    local name="$1" type="$2"; shift 2
    if has_con "$name"; then sudo nmcli connection modify "$name" "$@"
    else sudo nmcli connection add type "$type" con-name "$name" "$@" >/dev/null; fi
}
# The interface this ssh session came in on; empty on the console.
session_dev() {
    local client="${SSH_CONNECTION%% *}"
    [[ -n $client ]] || return 0
    ip -o route get "$client" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p'
}
sign_note=""
if [[ -n $sign_port || -n $hotspot ]] && ! nm_running; then
    echo "WARNING: NetworkManager is not running, so the network is not set up here." >&2
    echo "         Give $sign_port the address 10.75.0.1/16 by hand (docs/HARDWARE.md section 4)." >&2
    sign_note="not set up (no NetworkManager)"
    sign_port="" hotspot=""
fi
came_in="$(session_dev)"

# Wi-Fi power saving makes the Pi's Wi-Fi slow and laggy (seen: 13 Mbit/s, a show upload at 3 Mbit/s);
# the sign is on mains power. The setting applies when a Wi-Fi connection starts; iw switches it off now
# without dropping the link this session may run over.
if nm_running && install_file $'# TrafficLight: no Wi-Fi power saving (throughput and lag).\n[connection]\nwifi.powersave = 2' \
        /etc/NetworkManager/conf.d/90-trafficlight-wifi.conf; then
    [[ ! -e /sys/class/net/wlan0 ]] || sudo /usr/sbin/iw dev wlan0 set power_save off 2>/dev/null || true
fi

if [[ -n $sign_port ]]; then
    if [[ ! -e /sys/class/net/$sign_port ]]; then
        echo "WARNING: there is no network port $sign_port; the sign's port is not set up (--sign-port)." >&2
        sign_note="not set up: no port $sign_port"
    elif [[ "$(nmcli -g GENERAL.CONNECTION device show "$sign_port" 2>/dev/null)" == "$SIGN_CON" ]]; then
        sign_note="$sign_port, 10.75.0.1/16"
    elif [[ $came_in == "$sign_port" ]] &&
         ! nmcli -t -f DEVICE,STATE device | grep -v "^$sign_port:" | grep -q ':connected$'; then
        # Taking this port would cut the session, and after a reboot nothing would reach the Pi.
        echo "WARNING: this ssh session runs over $sign_port, the port for the LED card, and the Pi" >&2
        echo "         has no other connection. Connect it to Wi-Fi first, then run this again:" >&2
        echo "             sudo nmcli device wifi connect \"<network>\" password \"<password>\"" >&2
        sign_note="NOT set up: connect Wi-Fi first (see the warning above)"
    else
        # Priority 10 outranks the automatic DHCP profile ("Wired connection 1"); no gateway, so
        # the default route stays on Wi-Fi.
        add_or_modify "$SIGN_CON" ethernet connection.interface-name "$sign_port" \
            ipv4.method manual ipv4.addresses 10.75.0.1/16 ipv4.never-default yes ipv6.method disabled \
            connection.autoconnect yes connection.autoconnect-priority 10
        if [[ $came_in == "$sign_port" ]]; then
            sign_note="$sign_port, 10.75.0.1/16 from the next reboot (this session uses $sign_port now)"
        elif sudo nmcli --wait 10 connection up "$SIGN_CON" >/dev/null 2>&1; then
            sign_note="$sign_port, 10.75.0.1/16"
        else
            sign_note="$sign_port, 10.75.0.1/16 once the card's cable is plugged in and the card has power"
        fi
    fi
fi

hotspot_note=""
if [[ -n $hotspot ]]; then
    if [[ ! -e /sys/class/net/wlan0 ]]; then
        echo "WARNING: this Pi has no Wi-Fi (wlan0); no hotspot." >&2
    else
        # WPA2 with CCMP only: phones refuse or warn about TKIP. "shared": the Pi hands out
        # addresses (10.42.0.x) and is 10.42.0.1. Priority -10: a known Wi-Fi in range wins, so
        # at home the Pi stays on the home network and the hotspot opens where there is none.
        props=(connection.interface-name wlan0 802-11-wireless.ssid "$hotspot" 802-11-wireless.mode ap
               802-11-wireless.band bg ipv4.method shared ipv6.method disabled
               wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp
               connection.autoconnect yes connection.autoconnect-priority -10)
        [[ -z $hotspot_pw ]] || props+=(wifi-sec.psk "$hotspot_pw")
        add_or_modify "$HOTSPOT_CON" wifi "${props[@]}"
        hotspot_note="\"$hotspot\" when no known Wi-Fi is in range; then open http://10.42.0.1:8080"
        if [[ $came_in == wlan0 ]]; then
            hotspot_note+=$'\n           (to switch now, which ends a session over Wi-Fi: sudo nmcli connection up '"$HOTSPOT_CON)"
        fi
    fi
fi

# --- the service ---------------------------------------------------------------------
sudo systemctl enable --quiet trafficlight.service
sudo systemctl restart trafficlight.service   # always: an update means new code
sleep 2
systemctl is-active --quiet trafficlight.service ||
    { echo "The service did not start. See: journalctl -u trafficlight -e" >&2; exit 1; }

# The sign's own address is no use to a phone or laptop: leave it out.
ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | grep -v '^10\.75\.' || true)"
echo
echo "TrafficLight is running and will start at boot."
echo "Open http://$(hostname).local:8080$(for ip in $ips; do printf '  or  http://%s:8080' "$ip"; done)"
[[ -z $sign_note ]] || echo "LED card port: $sign_note"
[[ -z $hotspot_note ]] || echo "Hotspot: $hotspot_note"
echo "Time zone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
echo "Data: /var/lib/trafficlight    Logs: journalctl -u trafficlight -f"
echo "Settings → Setup in the UI checks the rest (the card, the panels, the sound)."
