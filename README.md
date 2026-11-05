# TrafficLight

Show controller for a traffic-light art piece: three lights made of six 128x64 HUB75 panels
(128x384, 24-bit colour) on a Colorlight 5A-75B receiver card, driven by raw Ethernet frames.
A Python server (FastAPI) renders the shows and plays their sound; a web UI (React) edits sets on a
timeline and controls the sign. It runs on a Windows PC for development and on a Raspberry Pi 4/5
inside the sign.

* `docs/HARDWARE.md`: wiring, LEDVision setup, bring-up with the test-pattern tool, protocol notes.
* `docs/ARCHITECTURE.md`: data model, modules, HTTP/WebSocket API.

## Windows

Needs Python 3.11+ (with the `py` launcher) and, to build the web UI, Node.js.

```
scripts\setup.bat      REM .venv, Python packages, web UI build; re-run any time
scripts\run.bat        REM start the server; arguments are passed on
```

Then open <http://localhost:8080>. `run.bat --port 8090 --data D:\tl-data` changes port and data
directory. To drive the sign you also need Npcap installed with "Restrict Npcap driver's access to
Administrators only" unticked (`docs/HARDWARE.md`, section 5). Without it the server runs and the UI
reports the output as unavailable.

## Raspberry Pi

Raspberry Pi OS **Lite**, 64-bit (Bookworm or later), Pi 4 or 5, with ssh enabled. Use Lite: the
service plays sound straight through ALSA, and on the desktop image PipeWire can hold the sound
card. The Pi has no Node, so the web UI is built on the PC. The first install needs internet on the
Pi (apt and pip); do it at home, not on the playa.

From the PC (WSL or Git Bash, in the repo):

```
scripts/update_pi.sh pi@raspberrypi.local
```

This is both the first install and every later update. It builds `web/dist`, copies the code and
`web/dist` to `~/trafficlight` on the Pi, then runs `scripts/install_pi.sh` there, which installs the
apt packages (`python3-venv`, `libportaudio2`), the venv and Python packages, and the `trafficlight`
systemd service, and restarts it. Both scripts are safe to repeat. `--dry-run` lists what would be
sent, `-d DIR` picks another directory, `--no-build` skips the web build. Set up ssh keys
(`ssh-copy-id`) to avoid password prompts.

If you copy the repo by hand instead, build first (`cd web && npm run build`), copy the whole
folder including `web/dist`, and run `bash scripts/install_pi.sh` on the Pi as your normal user.

The service starts at boot, restarts on failure, runs as that user with only `CAP_NET_RAW` (and
`CAP_NET_ADMIN` for a bigger socket buffer) for the raw socket, and has the `audio` group.
Logs are kept in RAM only (journald `Storage=volatile`) to spare the SD card:

```
systemctl status trafficlight
journalctl -u trafficlight -f
sudo systemctl restart trafficlight
```

The Pi's `eth0` is for the receiver card (gigabit, direct cable). The phone or laptop reaches the UI
over Wi-Fi: join the Pi's network, or make the Pi the access point
(`sudo nmcli device wifi hotspot ssid TrafficLight password <8+ characters>`, then
`nmcli connection modify Hotspot connection.autoconnect yes`), and open `http://<pi-address>:8080`
(`http://raspberrypi.local:8080` where mDNS works). There is no login: anyone on that network can
control the sign. The Pi has no battery-backed clock, so the clock clip shows the wrong time until the
time is set; set the time zone with `sudo timedatectl set-timezone Zone/City`.

Power can drop at any time. The app writes its files atomically, the root filesystem is ext4 (checked
at boot), and the journal stays off the card. For more protection, enable the read-only overlay
filesystem in `raspi-config` (Performance Options) after everything works, with the data directory on
a separate writable partition; that is not set up here.

## The UI

Open `http://localhost:8080` (Windows) or `http://<pi-address>:8080`. From any other machine the
server must be reachable on port 8080 (the default `--host 0.0.0.0` listens on all interfaces; allow
it in the Windows firewall prompt if you want to use a phone against the PC).

## Data

Sets, settings and media live in a data directory:

| Where | Directory |
| --- | --- |
| Windows, default | `data\` in the repo |
| Raspberry Pi service | `/var/lib/trafficlight` (outside the code, so updates leave it alone) |
| Anywhere | `TRAFFICLIGHT_DATA=<dir>` or `--data <dir>` |

To back up the Pi: `ssh pi@host "sudo tar -C /var/lib -cz trafficlight" > trafficlight-data.tgz`.
Do not restore over a running service: `sudo systemctl stop trafficlight` first.

## Tests

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt     (Windows, once)
.venv\Scripts\python.exe -m pytest -q

.venv/bin/python -m pip install -r requirements-dev.txt             (Linux/Pi, once)
.venv/bin/python -m pytest -q
```

They need no hardware, sound card or network, and use a temporary data directory. Web UI:
`cd web && npm run dev` (proxies to a server on port 8080); see `web/README.md`.
