#!/usr/bin/env bash
# Install or update TrafficLight on a Raspberry Pi (64-bit Raspberry Pi OS), in place:
# the checkout this script lives in becomes the installation. Run it as the normal user
# that should own the service (not with sudo; it calls sudo where needed). Safe to re-run:
# it only installs what is missing, and it works offline when nothing is missing.
set -euo pipefail

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

# --- apt packages --------------------------------------------------------------------
# python3-venv: venv + ensurepip (Debian splits it out of python3).
# libportaudio2: sounddevice's wheel is pure Python and loads the system PortAudio
#   (checked: sounddevice-0.5.6 has no bundled library on Linux).
# Nothing else: the aarch64 wheels of numpy, pillow, av (FFmpeg, ALSA included), pydantic,
# uvloop... are manylinux and bundle their libraries (checked in av.libs / pillow.libs).
missing=()
for pkg in python3-venv libportaudio2; do
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

if [[ ! -f web/dist/index.html ]]; then
    echo "WARNING: web/dist is missing: the UI is not built. The Pi has no Node; build it on the PC" >&2
    echo "         and deploy with scripts/update_pi.sh (see README)." >&2
fi

# --- systemd unit --------------------------------------------------------------------
tmp="$(mktemp)"
trap 'rm -f "$tmp" "${jtmp:-}"' EXIT
sed -e "s|@USER@|$(id -un)|g" -e "s|@REPO@|$REPO|g" scripts/trafficlight.service > "$tmp"
unit=/etc/systemd/system/trafficlight.service
if ! sudo cmp -s "$tmp" "$unit"; then
    sudo install -m 644 "$tmp" "$unit"
    sudo systemctl daemon-reload
fi

# --- journald: RAM only --------------------------------------------------------------
# Constant log writes to an SD card are wear and one more thing a power cut can interrupt.
# Logs then live in RAM (capped) and vanish at reboot: this sign is not debugged after the fact.
jtmp="$(mktemp)"
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=64M\n' > "$jtmp"
jconf=/etc/systemd/journald.conf.d/trafficlight.conf
if ! sudo cmp -s "$jtmp" "$jconf"; then
    sudo install -D -m 644 "$jtmp" "$jconf"
    sudo systemctl restart systemd-journald
fi

sudo systemctl enable trafficlight.service
sudo systemctl restart trafficlight.service   # always: an update means new code
sleep 2
systemctl is-active --quiet trafficlight.service ||
    { echo "The service did not start. See: journalctl -u trafficlight -e" >&2; exit 1; }

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "TrafficLight is running and will start at boot."
echo "Open http://${ip:-<pi-address>}:8080  (or http://$(hostname).local:8080)"
echo "Data: /var/lib/trafficlight    Logs: journalctl -u trafficlight -f"
