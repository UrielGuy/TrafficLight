# Hardware bring-up: Colorlight 5A-75B and six 128×64 panels

This guide is for someone who has the receiver card and the panels but has
not wired or configured anything yet. It ends with a protocol reference for
whoever works on `trafficlight/output/`.

The guide marks how sure each statement is:

* **[src]**: taken from the sources listed under "Protocol reference" and
  checked against at least one other source where possible.
* **[tested]**: checked here without the hardware (unit tests, a fake card
  over a virtual Ethernet link, packet captures read back with Wireshark).
* **[confirm]**: must be confirmed on the real card and panels. Nothing in
  this project has been run against a real 5A-75B yet.

## 1. What you need

* Colorlight **5A-75B** receiver card (stock firmware) and its power lead.
* Six **128×64 HUB75E panels**. The predecessor sign's panels are P2,
  1/32 scan, with the E address line on pin 8, and need no FM6126A init
  sequence (`../TehilaStoplight/docs/HARDWARE.md`).
* Six 16-pin HUB75 ribbon cables, one per panel, long enough to reach from
  the card to each panel's **input** connector. Bad ribbon cables are a
  common cause of trouble (forum guide).
* A **5 V** power supply (sizing below) and wiring for it.
* A **gigabit** Ethernet port that does nothing else: a PC port, a USB 3
  gigabit adapter, or the Raspberry Pi's built-in `eth0`. Plus a Cat5e or
  Cat6 cable.
* A Windows PC for the one-time setup in Colorlight's **LEDVision**
  software. After that, LEDVision is no longer needed.

## 2. Power

* **Panels:** budget about **4 A per panel at full white**, measured on the
  predecessor sign. Six panels come to about 24 A. The card's own rating is
  0.6 A (3 W) at 3.3–5.5 V [src: 5A-75B spec V8.0]. Use a 5 V supply of at
  least **30 A (150 W)**. 40 A gives headroom. Real shows draw much less:
  the default brightness is 0.5, and the lights are mostly discs.
  [confirm: measure the current on a full-white test at your brightness]
* Give each panel its own pair of 5 V / GND leads from the supply (about
  1 mm² / 18 AWG each). Do not chain panel power through one panel's thin
  leads. Use heavier wire from the supply to the distribution point.
* The card is powered from 5 V too, through its VH3.96 4-pin or 2-pin screw
  terminal (use one, not both) [src: spec]. Connect all grounds together.
* Power the panels and the card from the same supply, or at least share the
  ground.

## 3. Data wiring

* Plug each panel into whichever card port **J1..J8** is convenient, one
  panel per port, and mount it whichever way up is convenient. Each cable
  goes into the panel's **input** connector (the arrows on the panel PCB
  point away from it). The card has 8 HUB75 ports carrying 16 RGB data
  groups. A 128×64 1/32-scan panel uses one port (two groups) [src: spec;
  one panel per port: confirm]. Two ports stay free.
* Which panel is on which port, and which panels hang upside down, is
  worked out afterwards from what the panels show (section 6, step 3).
  Only the LEDVision setup (section 4) has to be right for each panel to
  show a clean 128×64 picture.
* **Ethernet:** connect the card's port A or B to the sender's gigabit port
  with a **direct cable**. Either port works, and the other is for chaining
  more cards [src: spec]. The link must run at **1000 Mb/s**. The card does
  not work at 100 Mb/s: FPP refuses non-gigabit links, and users report that
  the card is not found otherwise [src]. The tools show the link speed.
* **A switch** works if it is an unmanaged gigabit switch, but avoid one.
  Every card uses the fixed MAC address 11:22:33:44:55:66, so two cards
  cannot share a switch [src: Kubota]. The output is about 80 Mb/s of
  frames, which an unlearned switch floods to every port. Use a direct
  cable on a dedicated port.
* The sign's traffic never needs IP: the port can keep a self-assigned
  address. Do not use it for internet or the web UI. On the Pi, use Wi-Fi
  (or a second adapter) for those.

## 4. One-time receiver setup in LEDVision (stock firmware)

The card stores its panel configuration. Our software only sends pixels: it
cannot set the scan rate, driver chip or port layout. Those come from
LEDVision, once.

Menu names below are from Colorlight's S2 manual (LEDVISION section) and
from a forum guide for 5A-75B cards with FPP. They differ between LEDVision
versions. The forum guide recommends **LEDVision 5.x** for the 5A cards,
but another user had no luck with 5 or 6.9 [src: forum]. [confirm: which
version works with this card]

1. Install LEDVision on Windows and connect the PC's gigabit port directly to
   the card. Power the card and the panels.
   * LEDVision needs a pcap driver to send raw Ethernet. If Npcap is already
     installed for this project (section 5), install it with "WinPcap
     API-compatible mode", or let LEDVision install its own driver.
     [confirm]
2. Open **Setting → LED Screen Settings** (in some versions **LED Screen
   Setting**) and enter the password **168** [src: S2 manual, forum].
3. **Sending device:** choose the PC's network card and detect receivers.
   One receiver card should be listed. If none appears, check the link is
   gigabit and the card is powered.
4. **Screen parameters / Receiver card (module setup):**
   * If the panel vendor supplies a receiver parameter file (`.rcvbp`), use
     **Load** to load it. This is the most reliable route: the forum guide
     reports that the automatic setup only works with some panel types.
   * Otherwise run **Smart Settings** (the intelligent setup wizard).
     Module: **128 × 64 pixels**, **1/32 scan**, conventional (general)
     driver chip. Then answer the wizard's questions by looking at the panel
     (which colour shows, which row or point lights up). [confirm: the exact
     wizard steps for these panels]
   * Cabinet / box size: **128 wide × 512 high**. That is one module across
     and **eight** down, with ports J1..J8 feeding modules 1..8 from the
     top, whether or not a panel hangs on the port. The software sends all
     eight bands and leaves the two without a panel black.
     [confirm: that LEDVision accepts a 128×512 cabinet with one 128×64
     module per port for the 5A-75B; the card's height limit is not
     verified. LEDVision's per-port (data group) layout for this cabinet:
     confirm too]
   * If the card cannot take 512 rows, configure **128 × 384** (J1..J6
     feeding modules 1..6) and plug the panels into J1..J6 only. That needs
     a code change: `PORTS = 6` in `trafficlight/config.py` and in
     `web/src/pages/wiring.ts` (and the few tests that use J7 or J8).
   * Click **Send**. Each panel should now show a clean picture. Then click
     **Save To Receiver**.
5. **Connection parameters / Receiver mapping (look from front):** one card,
   row 1 column 1, width 128, height 512. Click **Send**, then **Save to
   Receiver**. The forum guide stresses that **Save to Receiver** must be
   clicked on *both* the Screen Parameters and the mapping tab, because one
   does not save the other.
6. In the same module settings you can choose the refresh rate and grey
   levels the card drives the panels with. A higher refresh rate looks
   better on camera. The card also applies its own gamma, so keep the
   software gamma (`Settings.output.gamma`) at 1.0 unless pictures look
   washed out.
7. Close LEDVision and check from our tool (section 6, step 2) that the card
   reports **configured 128x512**.

What matters for this project: every panel shows an undistorted 128×64
picture, and the card's image is 128×512 with each port on its own 64-row
band, J1 at the top. Which port each panel is on and each panel's rotation
are fixed in software (section 6, step 3; section 7). A **mirrored** picture, stripes, repeated or missing rows, or
half a panel dark cannot be fixed in software: go back to step 4.

## 5. Sender requirements

The web UI checks all of this for you: **Settings → Setup** lists every
check (Npcap, permissions, interface, link speed, the receiver card and its
configured size, the panel wiring, the sound output) with what to do about
each one that fails, and the Dashboard shows the ones that need attention.

### Windows

* Install **Npcap** from <https://npcap.com>. In the installer:
  * **Untick "Restrict Npcap driver's access to Administrators only".** With
    it ticked, the server must run as administrator. The driver then says
    *"Npcap is installed in 'Administrators only' mode, and the server is
    not running as administrator."* and does not open, rather than popping
    a UAC prompt. The development PC is in this
    state today (`AdminOnly = 1`).
  * "WinPcap API-compatible mode" is optional for this project: the driver
    loads `System32\Npcap\wpcap.dll` itself. LEDVision may need it.
  * Loopback and raw 802.11 support are not needed.
* Without Npcap the UI shows *"Npcap is not installed (wpcap.dll not
  found)…"*. The interface list still works: it comes from Windows' IP
  Helper API, not from Npcap.
* The interface id stored in the settings is the Npcap device name,
  `\Device\NPF_{GUID}`. It survives restarts and renames. The UI shows the
  adapter's name ("Ethernet 2") and description. The tool also accepts the
  name: `--iface "Ethernet 2"`.
* The adapter needs no IP configuration, and the firewall plays no part
  (the frames are layer 2).

### Raspberry Pi (Pi 4 / 5, 64-bit OS)

* Use the built-in `eth0` (gigabit) for the card. Check
  `cat /sys/class/net/eth0/speed` shows `1000` once the card is connected
  and powered.
* Raw sockets need **CAP_NET_RAW**. Without it the UI shows *"No permission
  to send raw Ethernet frames…"*. Either:
  * run the server as a systemd service with
    `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN` (preferred), or
  * `sudo setcap cap_net_raw,cap_net_admin+eip "$(readlink -f ~/.venvs/trafficlight/bin/python)"`.
    A venv's `python` is a symlink to the system interpreter, so this gives
    the capability to every script run with that interpreter. To avoid
    that, create the venv with `python3 -m venv --copies` and run setcap on
    the copy. `cap_net_admin` is optional: it only lets the driver enlarge
    the socket send buffer.
* NetworkManager will try DHCP on `eth0` and fall back to a link-local
  address. That is harmless (stock firmware; the custom firmware needs the
  static address below). [confirm: no link flapping on the Pi]

### Custom firmware: the computer's network port

With the custom firmware (`docs/FIRMWARE.md`, driver **Custom firmware**) the
card speaks plain UDP: no Npcap, no raw sockets, no `CAP_NET_RAW`. The
computer's port for the card needs a fixed address:
**10.75.0.1, netmask 255.255.0.0** (a /16), no gateway, no DNS. Each card
gives itself an address 10.75.c.d (c, d from 1 to 254) from its flash's
unique id and announces itself every second to 10.75.255.255. With the
netmask 255.255.255.0 both the announcements and the card's address are
outside the port's network and the card is not found.

* **Windows**, in a command prompt run as administrator (the adapter's name
  is in `netsh interface show interface`, e.g. "Ethernet 2"):

  ```
  netsh interface ip set address "Ethernet 2" static 10.75.0.1 255.255.0.0
  ```

  No firewall rule is needed: besides listening for the announcements, the
  server asks who is there from the port it listens on (UDP 5776), and
  Windows lets the answers to its own request in even where it drops
  unsolicited broadcasts.
* **Raspberry Pi OS** (Bookworm and later use NetworkManager): a profile for
  `eth0` that outranks the automatic "Wired connection 1":

  ```
  sudo nmcli connection add type ethernet ifname eth0 con-name sign \
      ipv4.method manual ipv4.addresses 10.75.0.1/16 ipv6.method disabled \
      connection.autoconnect yes connection.autoconnect-priority 10
  sudo nmcli connection up sign
  ```

  `ip -4 addr show eth0` then shows `inet 10.75.0.1/16`. Wi-Fi keeps the
  default route.
* In **Settings → LED output**, **Card address** stays **Automatic**: the
  server takes the first card that announces itself, shows its address and
  MAC under the field and in the setup list ("Found card 10.75.c.d"), follows
  it if its address changes, and warns about any other card it hears. Once
  the card is known, `ping 10.75.c.d` answers. **Fixed address** pins one
  card (a build with `--ip`, or one card among several).
* One server per computer: a second one cannot listen on UDP 5776, and two
  senders would fight over the card. The setup list says so.

## 6. Bring-up with the test pattern tool

The tool drives the sign without the server. Run it from the repo:

```
python -m trafficlight.tools.testpattern --list
python -m trafficlight.tools.testpattern --iface ID --detect
python -m trafficlight.tools.testpattern --iface ID --pattern wiring --brightness 0.2
python -m trafficlight.tools.testpattern --iface ID --pattern panels --brightness 0.2 --seen 3,8d,2,1d,5,4d
```

(On Windows: `.venv\Scripts\python.exe -m trafficlight.tools.testpattern …`.
On the Pi: `~/.venvs/trafficlight/bin/python -m …`.)

1. **`--list`** shows the Ethernet interfaces, their link state and speed,
   and the `--iface` value to use for each.
2. **`--detect`** asks the card for its firmware and configured size, for
   example `receiver 0: firmware 10.16, configured 128x512`.
   * *No receiver card answered*: check the cable, the card's power (red
     LED) and that the link is gigabit. Card LED D1 flashing once a second
     means it sees a sender [src: spec].
   * *Configured WxH* not 128x512: redo section 4. The server's output
     status shows the same as a warning.
   * Note the firmware version. The driver sends the display packet twice
     on firmware 13 and later, as FPP does.
3. **Wiring.** In the web UI, open **Settings → Panel wiring** (a phone
   works: stand in front of the sign) and press **Show connector numbers**.
   Every panel then shows, white on black, the number of the card port it
   is plugged into (1–8 for J1–J8, with a bar under the number) and an
   arrow. This picture goes to the card as it is, past the panel order and
   flips, so it shows the real wiring whatever the settings say. For each
   panel, top to bottom, enter the number and which way the arrow points:
   * arrow **up**: the panel hangs upright; arrow **down**: it hangs upside
     down, and so does its number (the bar is then above it: a "9" with the
     bar on top is a 6);
   * a panel that stays dark: choose **Nothing (dark)**. It gets a free
     port for now so the others can be saved; check its cable and power
     and do this step again.

   **Apply** works out `panel_order` (the port each panel is on, minus one)
   and `panel_flip` (arrow down) and switches `rotate180` off. The page
   refuses two panels with the same number. Example: from the top, 3 up,
   8 down, 2 up, 1 down, 5 up, 4 down gives `panel_order [2,7,1,0,4,3]`,
   `panel_flip [F,T,F,T,F,T]`.

   Without the server, the tool does the same: `--pattern wiring` shows the
   numbers, and `--seen 3,8d,2,1d,5,4d` (top to bottom; `d` = arrow down,
   `-` = dark) applies the rule to whatever pattern is shown and prints the
   resulting order and flips. Enter those in the web UI's wizard, or keep
   passing `--seen`.

   While the wiring picture is up, the web UI's preview shows each panel
   with the number and arrow it *should* show under the current settings,
   so after Apply the preview and the sign must agree.
4. **Check: `--pattern panels`** (or **Show panels 1–6** after Apply) shows
   each panel in its own colour with its number 1–6, which must now read
   1 to 6 from the top, all upright. Each panel also has a white triangle
   in its top-left corner, a one-pixel white frame round its edge, and red,
   green and blue swatches labelled R G B. `--raw` ignores any saved
   settings. Stop with Ctrl+C: the sign is blanked on exit (`--keep`
   leaves the pattern lit). Read the sign from the front:

| What you see | Setting | Fix |
| --- | --- | --- |
| Numbers out of order, or a panel upside down (triangle bottom-right) | `panel_order`, `panel_flip` | Redo step 3; an entry was wrong. |
| The whole sign upside down | `rotate180` | Turns the whole picture over; the wiring wizard switches it off, so after step 3 it should stay off. |
| Swatch R looks blue, G green, B red | `color_order` | Write down what R, G and B look like, in that order: `bgr`. Try it with `--color-order bgr`. Sources say the card usually wants `bgr`. |
| A number reads **mirrored** (not rotated), stripes, repeated or shifted rows, part of the frame edge missing, half a panel dark | — | LEDVision module or cabinet setup (section 4). Not fixable here. |
| The bottom row of the last band flickers or lights part-way | protocol option `sync_gap_ms` | Try `--options sync_gap_ms=3`, then 5 (section 8). |
| Movement stutters at 60 fps | `Settings.fps` / options | Try `--fps 50`. One source saw stutter above about 50 Hz. Also try `--options sync_repeat=2` (FPP's choice on firmware ≥ 13). |
| `--brightness` changes nothing | protocol option `brightness_mode` | `--options brightness_mode=software`. |
| Still pictures are steady, moving ones shimmer slightly | card refresh (LEDVision) | Each new frame swaps the card's picture buffers and disturbs its refresh (section 9, "Buffer swap"). Raise the refresh rate in LEDVision. |

   Work in that order: wiring first, then colours. Once the sign reads
   1..6 from the top, upright, with correct swatches, enter the colour
   order in the web UI's output settings, or run the tool without `--raw`
   to check what is saved.
5. **`--pattern gradient`** shows red, green, blue and white ramps. Uneven
   steps or crushed darks are a matter for LEDVision's grey and gamma
   settings, or the software `gamma`.
6. **`--pattern white --brightness 1`** for a few seconds is the
   power-supply test. Measure the 5 V at the farthest panel.

Other flags: `--seconds N`, `--fps N` (0 = as fast as possible; prints
packets/s), and `--options key=value,…` (section 8).

## 7. How the software maps the picture

`trafficlight/output/mapping.py` turns the logical frame (128×384, row 0 =
top of the sign as the audience sees it) into the card's 128×512 image
(band *b* = rows 64·*b* .. 64·*b*+63 = port J*b+1*):

1. `rotate180` turns the whole frame.
2. Canvas panel *i* (64-row band *i* of the result, 0 = top) goes to band
   `panel_order[i]` of the card's image, i.e. port J`panel_order[i]+1`
   (six different values in 0..7). If `panel_flip[i]` is set, that panel is
   rotated 180°. The two bands without a panel are sent black.
3. `color_order` is the order of a pixel's bytes on the wire: `bgr` sends
   blue, green, red, the same convention as FPP's panel colour order.
4. `gamma` (if not 1.0) is a software lookup table.

The wiring test picture is drawn in the card's image and skips steps 1 and
2 (colour order, gamma and brightness still apply). `output/wiring.py`
holds the rule from what the panels show to `panel_order` / `panel_flip`;
the web UI has the same rule in `web/src/pages/wiring.ts`.

## 8. Protocol options

The sources disagree on a few points (section 9), so these are switchable.
They are not part of `Settings`. Set them in the environment of the server:

```
TRAFFICLIGHT_COLORLIGHT="sync_gap_ms=3,sync_repeat=2"
```

or with the tool's `--options`.

| Option | Default | Meaning |
| --- | --- | --- |
| `sync_gap_ms` | 1.0 | Pause between the last row leaving the port and the display packet. FPP uses 0.5 ms, Kubota 1 ms, PanelPlayer at least 5 ms. |
| `sync_repeat` | 1 | Display packets per frame. FPP sends 2 on firmware ≥ 13, but each one swaps the card's picture buffers: with 2 the card sometimes showed the previous frame (13.39, section 9). |
| `brightness_packet` | `linear` | The 0x0A packet: `linear` (FPP: same value as the display packet), `ledvision` (LEDVision's non-linear levels, after Kubota), `off` (PanelPlayer: not needed). |
| `brightness_mode` | `auto` | `card`: brightness is sent to the card. `software`: pixels are scaled and the card is told 100 %. `auto` = card, or software when the card reports firmware 2.x / 3.x (FPP: brightness "doesn't seem to work" there). |
| `white_balance` | `1/1/1` | R/G/B multipliers in the display packet (colour temperature): e.g. `1/0.86/0.56` ≈ 4500 K (Kubota's table). |
| `detect` | `1` | Send detection packets until the card answers, then every 2 s (a card that restarted gets the picture again). |
| `pace` | `1` | Display packets on an even grid of the frame period (section 9, "Timing"). `0` sends each as soon as its rows have left. |
| `pace_margin_ms` | 1.5 | Slack kept between a frame being ready to show and its slot on the grid. |
| `skip_unchanged` | `1` | Do not re-send a picture the card already shows (section 9, "Buffer swap"). `0` sends every frame. |
| `assumed_mbps` | 1000 | Link speed for wire-time estimates when the OS does not report it. |

## 9. Protocol reference

The protocol is undocumented. Everything below comes from other people's
reverse engineering:

* **FPP**: Falcon Player's ColorLight output,
  [`src/channeloutput/ColorLight-5a-75.cpp`](https://github.com/FalconChristmas/fpp/blob/master/src/channeloutput/ColorLight-5a-75.cpp)
  and `.h` (master, read 2026-11-04). Many users drive 5A-75 cards with it.
  It credits the original reverse engineering by mplayer-colorlight
  (mylifesucks.de) and Kubota.
* **Kubota**: Harald Kubota,
  ["Colorlight 5A-75B protocol"](https://hkubota.wordpress.com/2022/01/31/winter-project-colorlight-5a-75b-protocol/)
  (captures from LEDVision, card 5A firmware 10.16), and his senders in
  [haraldkubota/colorlight](https://github.com/haraldkubota/colorlight)
  (`deno/lib/colorlight.ts`, `py/detect.py`).
* **PanelPlayer**:
  [ZoidTechnology/PanelPlayer](https://github.com/ZoidTechnology/PanelPlayer):
  `protocol/readme.md`, `source/colorlight.c`, `source/main.c`.
* **Rust crate** [`colorlight` 0.1.0](https://crates.io/crates/colorlight),
  which follows Kubota.
* Colorlight's **5A-75B Specification V8.0** (2020-07-17), the **S2 user
  manual** (LEDVISION setup), and the forum guide
  [Multiple Colorlight Cards with FPP](https://auschristmaslighting.com/threads/multiple-colorlight-cards-with-fpp-for-p5-panels-guide.11762/).

### Framing [src: all four agree]

* Destination MAC `11:22:33:44:55:66`, source MAC `22:22:33:44:55:66`.
  Kubota says to take both literally. FPP's comment says the source "doesn't
  seem necessary".
* There is no real EtherType. Byte 12 is the packet type, and byte 13 is
  already payload. Kubota writes the pair as an EtherType (`0x0107`,
  `0x55RR`, …). Offsets below are from the start of the Ethernet frame.
* The display packet's "EtherType" 0x0107 is below 0x0600, so it reads as an
  802.3 length field. PanelPlayer warns that some NICs may drop such
  frames, and Wireshark shows it as "Length 263" [tested]. [confirm: the
  sender's NIC passes it]

### Packets

| Type | Bytes | Layout | Sources |
| --- | --- | --- | --- |
| `0x55` row data | 21 + 3·n | [13..14] row (big-endian; rows ≥ 256 have 0x01 in byte 13), [15..16] first pixel, [17..18] pixel count n, [19] `0x08`, [20] `0x88`, [21..] pixels, 3 bytes each | FPP, Kubota, PanelPlayer, Rust. Kubota's text says byte 20 is 0x80 "(I got 0x88)"; all code sends 0x88. |
| `0x0A` brightness | 77 | [13..15] R, G, B level, [16] `0xFF`, rest 0 | FPP (all three = brightness), Kubota (`XX XX ff`), PanelPlayer (R, G, B) |
| `0x01` display / latch | 112 | [13] `0x07` (from a PC; `0x00` from an S2 sender), [35] brightness, [36] `0x05`, [38..40] R, G, B levels, rest 0 | FPP, Kubota (verbatim capture), PanelPlayer, Rust. Kubota's offsets were corrected in 2022-09 to these. |
| `0x07` detect | 284 | [16] receiver index (0 = first card), rest 0 | FPP, Kubota (270 data bytes + 14), Rust |
| `0x08 0x05` reply | 1070 | sent from `11:22:33:44:55:66` to broadcast. [14] `0x04` on 5A cards, [15].[16] firmware major.minor, [34..35] width, [36..37] height (the card's configured size), [51..54] a packet counter, [59..62] uptime in ms, [98] receiver index | FPP, Kubota (capture). FPP calls the size fields "cabinet width/height?" and not entirely right with several cards. Kubota lists the index as "Data[62]", which matches only as the hex offset 0x62 = 98. |

* **Pixels per packet:** at most **497** (21 + 1491 = 1512 bytes ≤ 1514)
  [src: FPP, PanelPlayer]. Our 128-pixel rows are one 405-byte packet each.
* **Pixel byte order:** this is where the sources disagree. PanelPlayer and
  the Rust crate send **BGR**. Kubota got BGR "for my panel" and calls RGB
  standard. FPP sends in its configurable panel colour order (default RGB),
  and FPP users with these cards set it to BGR (forum guide). The driver
  sends `color_order` literally (default `rgb`), so expect to set `bgr`
  (section 6). [confirm]
* **Order per frame:** brightness packet (FPP sends it every frame), the
  rows, then the display packet, which shows what was received
  [src: FPP, Kubota, PanelPlayer].
* **Gap before the display packet:** the sources disagree.
  * FPP waits until the socket queue is empty (the rows are on the wire),
    then `ColorlightSyncGapUS` = 500 µs by default (0–20 ms). Its comment
    explains that a display packet following the last row too closely makes
    that row "light only part way".
  * Kubota waits 1 ms: "without the following delay the end of the bottom
    row module flickers in the last line".
  * PanelPlayer says "at least 5 ms" and uses 10 ms in its code.

  The driver waits until the rows are out (Linux: the socket queue is empty;
  Windows: the estimated wire time), then `sync_gap_ms` = 1 ms.
  [tested: measured 1.05–1.11 ms at the receiving end of a veth link]
  [confirm on the card]
* **Duplicates:** for firmware ≥ 13, LEDVision sends the display and
  brightness packets (and rows) twice. FPP repeats the display packet and
  the brightness packet, but not the rows ("seems unnecessary"). The driver
  sends the display packet once (`sync_repeat` = 1): on 13.39 each one swaps
  the picture buffers, and two per frame made moving pictures now and then
  jump back to the previous frame. [tested on the card, 2026-11-06]
* **Buffer swap:** the card is double-buffered. Rows go into a hidden
  buffer; each display packet swaps it with the shown one (display packets
  alone flip between the last two pictures). On firmware 13.39 / 5A-75B the
  swap disturbs the LED refresh: a solid colour sent at 60 fps visibly
  flickers (less at 10 fps); rows without a display packet, detection
  packets, or no traffic at all do not. So the driver sends a picture only
  when it changed (`skip_unchanged`); the card keeps the last one. Moving
  pictures still swap every frame. [tested on the card, 2026-11-06]
* **Timing:** with "Multiple: Refresh x N" the card does N refreshes per
  frame, locked to the display packets, so these must come at even
  intervals. Tested at 30 fps with a moving picture: display packets
  spaced by sleep() (±0.5 ms, extremes ±5 ms on Windows) flicker; spaced
  evenly by spinning (±0.08 ms) flicker much less; deliberately ±3 ms
  uneven flicker most. The driver sends them on an even grid of the frame
  period (`pace`, `Pacer`): rows as soon as a frame is ready, the display
  packet at its slot, the last 2 ms of the wait spun, with a high thread
  priority (Windows) and a short GIL switch interval. Keep the frame rate a
  whole fraction of LEDVision's Refresh Rate / Multiple (1920 / 32 = 60:
  60, 30, 20 or 15 fps). [tested on the card, 2026-11-06]
* **Brightness:** byte 35 of the display packet is linear: 0x80 = 50 %,
  0xbf = 75 % (Kubota's table; FPP sends 2.55 × percent). Bytes 38..40 are
  linear R, G, B levels for colour temperature. 6500 K = all equal to the
  brightness. At 100 %, 2000 K = ff 76 06, 4500 K = ff dc 8f, 8000 K =
  ce d8 ff (Kubota). For the 0x0A packet, LEDVision uses non-linear levels
  (1 % = 0x28, 50 % = 0xc1; Kubota), FPP sends the linear value, and
  PanelPlayer finds the packet unnecessary. FPP notes that brightness may
  not work on firmware 2.x / 3.x.
* **Frame rate:** LEDVision sends 20 Hz by default. Kubota reached about
  60 Hz, with "skipped frames" above. PanelPlayer saw stutter above about
  50 Hz. The engine's default is 60 fps. [confirm]

### Throughput

A frame is 514 packets (515 with a repeated display packet): one row
packet for each of the card's 512 rows, the 128 rows of the two unused
ports included (the card expects its whole configured image), plus the
brightness and display packets. That is 208 KB, about 220 KB on the wire.
At 60 fps that is about 30,800 packets/s and 106 Mb/s: well within
gigabit, more than 100 Mb/s Ethernet can carry.

`send()` only copies the frame (about 0.1 ms) into a slot and returns. A
sender thread maps the newest frame into a prebuilt packet buffer and sends
the whole frame in one system call. Frames it could not get to are dropped
and counted.

* **Linux:** `sendmmsg` through ctypes on an `AF_PACKET` socket.
* **Windows:** a single `pcap_sendqueue_transmit`. The queue buffer *is*
  the packet buffer, with Npcap's 16-byte record headers in place. It is
  byte-identical to what `pcap_sendqueue_queue` builds in Npcap 1.88
  [tested].

Measured without hardware (development PC), with the earlier 384-row card
image; the 512-row image is a third more packets:

| | Linux (WSL 2) | Windows 11, Python 3.13 |
| --- | --- | --- |
| `send()` on the render thread | median 0.14 ms, max 0.36 ms | median 0.07 ms, max 0.37 ms |
| map + fill packets (order, flips, bgr, gamma) | 0.16 ms | 0.45 ms |
| 60 fps over a veth link to a fake card | 60.0 fps, 23,159 packets/s, 0 dropped, every frame complete | not measured: transmit needs Npcap without admin-only mode |
| flat out over veth | 748 fps, 289,000 packets/s | — |

[confirm: packets/s on the Pi and through Npcap on a real gigabit port]

## 10. Not verified yet (do on the hardware)

* The LEDVision setup for these panels: version, wizard answers, port
  layout, and that one port drives one 128×64 panel.
* That the card latches cleanly with a 1 ms gap. Watch the bottom row of
  each band.
* Pixel byte order (`bgr` expected) and that brightness and colour
  temperature act as described.
* That LEDVision configures the 5A-75B for a 128×512 cabinet (eight
  128×64 modules, one per port), and that the detection reply then
  carries firmware and a 128×512 size.
* That the Windows NIC sends the 0x0107 display frame unchanged. Capture on
  a second machine, or check that the sign updates at all.
* Npcap transmit rate on Windows, and sendmmsg rate on the Pi.
* Power draw at the chosen brightness.
