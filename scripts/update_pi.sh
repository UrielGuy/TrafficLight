#!/usr/bin/env bash
# Deploy this checkout to the Raspberry Pi over ssh: builds the web UI here (the Pi has no
# Node), copies code + web/dist, then runs scripts/install_pi.sh on the Pi, which installs
# any new Python packages and restarts the service. Also does the first install.
# Run it from WSL or Git Bash on the PC.
#
#   scripts/update_pi.sh [-d REMOTE_DIR] [--no-build] [--dry-run] [user@]host [Pi setup options]
#
# REMOTE_DIR is relative to the Pi user's home (default: trafficlight). Data lives in
# /var/lib/trafficlight on the Pi and is never touched. Needs ssh and tar on both sides.
# Pi setup options, passed to install_pi.sh (see there): --hostname NAME, --timezone ZONE,
# --hotspot SSID [--hotspot-password PW], --sign-port IFACE, --no-sign-port. Give them on the
# first install; later updates keep what they set.
# Example: scripts/update_pi.sh pi@raspberrypi.local --hostname trafficlight --timezone America/Los_Angeles
set -euo pipefail

remote_dir=trafficlight build=1 dry=0 host=
setup=()       # for install_pi.sh
while (( $# )); do
    case "$1" in
        -d) remote_dir="${2:?-d needs a directory}"; shift 2 ;;
        --no-build) build=0; shift ;;
        --dry-run) dry=1; shift ;;
        --hostname|--timezone|--hotspot|--hotspot-password|--sign-port)
            setup+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --no-sign-port) setup+=("$1"); shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) host="$1"; shift ;;
    esac
done
[[ -n $host || $dry == 1 ]] || { echo "usage: $0 [-d REMOTE_DIR] [--no-build] [--dry-run] [user@]host [Pi setup options]" >&2; exit 2; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# --- the web UI: rebuilt every time, so a stale web/dist cannot be shipped ---------------
if (( build && !dry )); then
    echo "Building the web UI"
    if command -v npm >/dev/null 2>&1; then
        (cd web && { [[ -d node_modules ]] || npm ci --no-audit --no-fund; } && npm run build)
    elif command -v cmd.exe >/dev/null 2>&1; then      # WSL: Node is installed on Windows
        cmd.exe /c "cd web && npm run build"
    else
        echo "No npm found. Install Node.js, or build web/dist elsewhere and use --no-build." >&2
        exit 1
    fi
fi
if [[ ! -s web/dist/index.html ]]; then
    echo "web/dist/index.html is missing: the UI is not built." >&2
    (( dry )) || exit 1
elif (( ! build && ! dry )); then
    newest="$(find web/src web/index.html web/package.json web/vite.config.ts -type f -newer web/dist/index.html -print -quit 2>/dev/null || true)"
    [[ -z $newest ]] || echo "WARNING: $newest is newer than web/dist: the UI may be stale." >&2
fi

# Not shipped: .venv, data, tests, web sources and node_modules, and libraries built here (install_pi.sh
# builds the Pi's own).
items=(trafficlight assets scripts web/dist docs requirements.txt pyproject.toml README.md)
pack() { tar --exclude=__pycache__ --exclude='*.pyc' --exclude='*.so' --owner=0 --group=0 -czf - "${items[@]}"; }
if (( dry )); then
    echo "Would send (top two path levels):"
    pack | tar -tz | awk -F/ '{print $1 ($2 != "" ? "/" $2 : "")}' | sort -u
    exit 0
fi

# --- copy, then swap each item in place; .venv is not in the archive -------------------
# Extract to a staging directory first so an interrupted copy cannot leave a half-new tree.
q="$(printf '%q' "$remote_dir")"
echo "Copying to $host:~/$remote_dir"
pack | ssh "$host" "set -e
mkdir -p $q; cd $q
rm -rf .incoming .old; mkdir .incoming .old
tar -xzf - --no-same-owner -C .incoming
test -s .incoming/web/dist/index.html
for it in trafficlight assets scripts web docs requirements.txt pyproject.toml README.md; do
    test -e \".incoming/\$it\"
done
for it in trafficlight assets scripts web docs requirements.txt pyproject.toml README.md; do
    if [ -e \"\$it\" ]; then mv \"\$it\" .old/; fi
    mv \".incoming/\$it\" .
done
rm -rf .incoming .old"

echo "Installing and restarting on the Pi (may ask for the sudo password)"
ssh -t "$host" "cd $q && bash scripts/install_pi.sh${setup[*]:+ $(printf '%q ' "${setup[@]}")}"
