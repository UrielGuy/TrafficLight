# TrafficLight

Show controller for a traffic-light art piece: three lights made of six 128x64 HUB75 panels
(128x384, 24-bit colour) on a Colorlight 5A-75B card running our own firmware, driven over UDP.
A Python server (FastAPI) renders the shows and plays their sound; a web UI (React) edits sets on a
timeline and controls the sign. It runs on a Windows PC for development and on a Raspberry Pi 4/5
inside the sign.

* `docs/HARDWARE.md`: power, wiring, the network port, bring-up with the test-pattern tool.
* `docs/ARCHITECTURE.md`: data model, modules, HTTP/WebSocket API.
* `docs/FIRMWARE.md`: the card's firmware and its protocol; `docs/FPGA-BRINGUP.md`: building and flashing it.

## Windows

Needs Python 3.11+ (with the `py` launcher) and, to build the web UI, Node.js.

```
scripts\setup.bat      REM .venv, Python packages, web UI build; re-run any time
scripts\run.bat        REM start the server; arguments are passed on
```

Then open <http://localhost:8080>. `run.bat --port 8090 --data D:\tl-data` changes port and data
directory. To drive the sign, the computer's network port for the card needs the address 10.75.0.1
with netmask 255.255.0.0 (`docs/HARDWARE.md`, section 4). Without the card the server runs and the UI
says what is missing.

## Raspberry Pi

Raspberry Pi OS **Lite**, 64-bit (Bookworm or later), Pi 4 or 5. Use Lite: the service plays sound
straight through ALSA, and on the desktop image PipeWire can hold the sound card. The Pi has no
Node, so the web UI is built on the PC. The first install needs internet on the Pi (apt and pip);
do it at home, not on the playa.

### From a blank SD card

1. **Raspberry Pi Imager**: Raspberry Pi OS Lite (64-bit). In its OS customisation set a user name
   and password, your **home Wi-Fi**, and under Services **enable SSH** (paste your public key,
   `~/.ssh/id_ed25519.pub` in WSL, or allow the password).
2. Boot the Pi and wait a minute. It joins the Wi-Fi; `ssh you@raspberrypi.local` should work
   (or use the address your router shows). Connect over Wi-Fi rather than a cable: the Pi's
   network socket becomes the LED card's.
3. From the PC (WSL or Git Bash, in the repo), with the names you like:

   ```
   scripts/update_pi.sh you@raspberrypi.local --hostname trafficlight \
       --timezone America/Los_Angeles --hotspot TrafficLight
   ```

   It asks for the hotspot's password at the start, then builds the UI, copies everything,
   installs, and prints where the UI is: `http://trafficlight.local:8080`.
4. Plug the LED card into the Pi's network socket. **Settings → Setup** in the UI should show the
   network port, then "Found card 10.75.c.d".
5. Move the show: on the PC's UI, Dashboard → **Download everything**; on the Pi's UI, **Upload…**
   that zip (sets, media and show settings).

What `scripts/install_pi.sh` sets up on the Pi (all safe to repeat; updates keep it):

* apt packages (`python3-venv`, `libportaudio2`), the venv and Python packages, the
  `trafficlight` systemd service (starts at boot, restarts on failure, runs as your user without
  any capabilities, with the `audio` group), logs in RAM only (journald `Storage=volatile`, to
  spare the SD card), UDP socket buffers big enough for the LED driver.
* **The LED card's port**: `eth0` gets 10.75.0.1/16 (NetworkManager profile `trafficlight-sign`,
  no gateway, so the default route stays on Wi-Fi; `docs/HARDWARE.md` section 4). If your ssh
  session runs over `eth0`, the profile takes over at the next reboot instead of cutting you off,
  and if the Pi has no other connection it is not set up at all: connect Wi-Fi first.
  `--sign-port IFACE` picks another port, `--no-sign-port` leaves the network alone.
* `--hostname NAME`: the Pi is then `NAME.local`. Later updates use the new name.
* `--timezone Zone/City`: the clock clip shows local time. The Pi has no battery-backed clock
  (except a Pi 5 with its battery) and no internet on the playa: it keeps the time from its last
  connection only while powered.
* `--hotspot SSID` (`--hotspot-password PW`, or asked for): a Wi-Fi network of the Pi's own
  (profile `trafficlight-hotspot`, WPA2), used only when no known Wi-Fi is in range. At home the
  Pi stays on the home network; on the playa phones join the hotspot and open
  `http://10.42.0.1:8080`. To switch by hand: `sudo nmcli connection up trafficlight-hotspot`
  (and `down` to go back). There is no login: anyone on that network can control the sign.

Every later update is the same command without the options: `scripts/update_pi.sh
you@trafficlight.local`. `--dry-run` lists what would be sent, `-d DIR` picks another directory
than `~/trafficlight`, `--no-build` skips the web build. If you copy the repo by hand instead, build
first (`cd web && npm run build`), copy the whole folder including `web/dist`, and run
`bash scripts/install_pi.sh [options]` on the Pi as your normal user.

```
systemctl status trafficlight
journalctl -u trafficlight -f
sudo systemctl restart trafficlight
```

Power can drop at any time. The app writes its files atomically, the root filesystem is ext4 (checked
at boot), and the journal stays off the card. For more protection, enable the read-only overlay
filesystem in `raspi-config` (Performance Options) after everything works, with the data directory on
a separate writable partition; that is not set up here.

## The UI

Open `http://localhost:8080` (Windows) or `http://<pi-address>:8080`. From any other machine the
server must be reachable on port 8080 (the default `--host 0.0.0.0` listens on all interfaces; allow
it in the Windows firewall prompt if you want to use a phone against the PC).

**Settings → Setup** lists everything the sign still needs (the network port, the LED card and its
firmware, the panel wiring, the sound output), each with what to do; the Dashboard shows the ones
that need attention. **Settings → Panel wiring** works out which
card port each panel is on and which panels hang upside down: plug them in anywhere, press "Show
connector numbers", enter what each panel shows (`docs/HARDWARE.md`, section 5).

## Data

Sets, settings and media live in a data directory:

| Where | Directory |
| --- | --- |
| Windows, default | `data\` in the repo |
| Raspberry Pi service | `/var/lib/trafficlight` (outside the code, so updates leave it alone) |
| Anywhere | `TRAFFICLIGHT_DATA=<dir>` or `--data <dir>` |

To back up the Pi: `ssh pi@host "sudo tar -C /var/lib -cz trafficlight" > trafficlight-data.tgz`.
Do not restore over a running service: `sudo systemctl stop trafficlight` first.

Media keeps every upload or download as it came (`media/<id>/original.*`); crops and trims made in
the crop tool only change what the sign plays, never the original. Importing from a link (Media
page) needs internet and uses yt-dlp; when a site changes and imports start failing, update it
(`pip install -U yt-dlp` in the venv, or run the setup / update script again). Better import the
videos before the trip: the sign plays from its library offline.

## Tests

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt     (Windows, once)
.venv\Scripts\python.exe -m pytest -q

.venv/bin/python -m pip install -r requirements-dev.txt             (Linux/Pi, once)
.venv/bin/python -m pytest -q
```

They need no hardware, sound card or network, and use a temporary data directory. Web UI:
`cd web && npm run dev` (proxies to a server on port 8080); see `web/README.md`.
