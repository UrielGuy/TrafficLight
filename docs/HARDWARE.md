# Hardware: the Colorlight 5A-75B and six 128×64 panels

This guide is for someone who has the card and the panels and wants the sign
lit: what to buy, power, wiring, the computer's network port, and bring-up
with the test-pattern tool and the web UI's wiring wizard.

The card runs **our own firmware** (`docs/FIRMWARE.md` is its contract;
`docs/FPGA-BRINGUP.md` covers building and flashing it). The card's stock
Colorlight firmware is no longer supported by this software: the server no
longer has a driver for it, and LEDVision plays no part. The flash backups
and how to put the stock firmware back are in `docs/FPGA-BRINGUP.md`.

**[confirm]** marks what still has to be checked on the real sign.

## 1. What you need

* A Colorlight **5A-75B** card (V8.2), flashed with our firmware, and its
  power lead.
* Six **128×64 HUB75E panels**, 1/32 scan (address lines A..E, E on pin 8).
  The sign's panels are AGS P2.5 (label `AGSP2.5-260706-5000
  IC:FM6124HJ+TC7262G`): FM6124 constant-current column drivers (plain
  shift register and latch, no PWM memory, no init sequence) and TC7262G
  row drivers. The ESP32 predecessor (`../TehilaStoplight`) drove the same
  panels with the standard ESP32-HUB75-MatrixPanel-DMA driver.
* Six 16-pin HUB75 ribbon cables, one per panel, long enough to reach from
  the card to each panel's **input** connector. Bad ribbon cables are a
  common cause of trouble.
* A **5 V** power supply (sizing below) and wiring for it.
* A **gigabit** Ethernet port for the card alone: a PC port, a USB 3
  gigabit adapter, or the Raspberry Pi's built-in `eth0`. Plus a Cat5e or
  Cat6 cable.

## 2. Power

* **Panels:** budget about **4 A per panel at full white**, measured on the
  predecessor sign. Six panels come to about 24 A. The card's own rating is
  0.6 A (3 W) at 3.3–5.5 V (5A-75B spec V8.0). Use a 5 V supply of at least
  **30 A (150 W)**. 40 A gives headroom. Real shows draw much less: the
  default brightness is 0.5, and the lights are mostly discs. [confirm:
  measure the current on a full-white test at your brightness]
* Give each panel its own pair of 5 V / GND leads from the supply (about
  1 mm² / 18 AWG each). Do not chain panel power through one panel's thin
  leads. Use heavier wire from the supply to the distribution point.
* The card is powered from 5 V too, through its VH3.96 4-pin or 2-pin screw
  terminal (use one, not both). Keep its input below 5.5 V. Connect all
  grounds together.
* Power the panels and the card from the same supply, or at least share the
  ground.

## 3. Data wiring

* Plug each panel into whichever card port **J1..J8** is convenient, one
  panel per port, and mount it whichever way up is convenient. Each cable
  goes into the panel's **input** connector (the arrows on the panel PCB
  point away from it). A 128×64 1/32-scan panel uses one port. Two ports
  stay free; the firmware drives all eight, the free ones black.
* Which panel is on which port, and which panels hang upside down, is
  worked out afterwards from what the panels show (section 5, step 3).
* CLK, LAT, OE and the row address are shared by all eight ports on this
  card; only the colour lines are per port (`docs/FIRMWARE.md`, section 2).
  So every panel is driven with the same timing.
* **Ethernet:** connect the card's port A or B to the computer's gigabit
  port with a **direct cable**. The firmware listens on one of them (a
  build option, `docs/FIRMWARE.md` section 2).
* Keep the port for the sign: the card's network is 10.75.0.0/16 and
  nothing else should live on it. Use Wi-Fi (or a second adapter) for the
  internet and the web UI.

## 4. The computer's network port

The card speaks plain UDP: no special drivers, no administrator rights, no
capabilities. The computer's port for the card needs a fixed address:
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
* In **Settings → LED output**, **Driver** is **Custom firmware** (the
  default; **None** sends nothing, for a computer without the sign) and
  **Card address** stays **Automatic**: the server takes the first card
  that announces itself, shows its address and MAC under the field and in
  the setup list ("Found card 10.75.c.d"), follows it if its address
  changes, and warns about any other card it hears. Once the card is known,
  `ping 10.75.c.d` answers. **Fixed address** pins one card (a build with
  `--ip`, or one card among several).
* One server per computer: a second one cannot listen on UDP 5776, and two
  senders would fight over the card. The setup list says so.

**Settings → Setup** in the web UI checks the rest: whether the socket
opens, whether sending works, whether a card answers, its firmware version,
the refresh schedule it runs, whether it loses packets, and the panel
wiring, each with what to do about it. The Dashboard shows the ones that
need attention.

## 5. Bring-up

The web UI does all of this (**Settings → LED output → Test pattern** and
**Settings → Panel wiring**). The test-pattern tool does the same without
the server; stop the server first, since two programs sending to one card
fight over it. Run it from the repo:

```
python -m trafficlight.tools.testpattern --detect
python -m trafficlight.tools.testpattern --pattern wiring --brightness 0.2
python -m trafficlight.tools.testpattern --pattern panels --brightness 0.2 --seen 3,8d,2,1d,5,4d
```

(On Windows: `.venv\Scripts\python.exe -m trafficlight.tools.testpattern …`.
On the Pi: `~/.venvs/trafficlight/bin/python -m …`.) The tool finds the
card as the server does; `--card 10.75.c.d` pins an address.

1. **Power and link.** Power the card and the panels; the card's port
   shows a link to the computer.
2. **`--detect`** waits for the card and prints its address, MAC, flash id,
   firmware version and uptime, and anything wrong (the same texts as the
   setup list). *No card answered*: check the port's address (section 4),
   the cable and the card's power, and that the card runs our firmware.
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
   settings. Stop with Ctrl+C: the sign is left dark on exit (the card
   would otherwise keep the last picture). Read the sign from the front:

| What you see | Setting | Fix |
| --- | --- | --- |
| Numbers out of order, or a panel upside down (triangle bottom-right) | `panel_order`, `panel_flip` | Redo step 3; an entry was wrong. |
| The whole sign upside down | `rotate180` | Turns the whole picture over; the wiring wizard switches it off, so after step 3 it should stay off. |
| Swatch R looks blue, G green, B red | `color_order` | Write down what R, G and B look like, in that order: `bgr`. Try it with `--color-order bgr`, then set it in Settings → LED output. |
| One panel dark, flickering, or with garbage | — | Its ribbon cable or its power. Swap cables between panels to tell which. |
| A number reads **mirrored** (not rotated), stripes, repeated or shifted rows, part of the frame edge missing, half a panel dark on every panel | — | How the firmware drives the panels (scan order, clock, blanking): `docs/FIRMWARE.md` and its open questions. Not fixable in the settings. |
| Dark steps look too bright or too dark | `gamma` | 2.2 is right for these panels; `--gamma` tries others. |

   Work in that order: wiring first, then colours. Once the sign reads
   1..6 from the top, upright, with correct swatches, enter the colour
   order in the web UI's output settings, or run the tool without `--raw`
   to check what is saved.
5. **`--pattern gradient`** shows red, green, blue and white ramps: they
   should rise smoothly, without steps or crushed darks.
6. **`--pattern white --brightness 1`** for a few seconds is the
   power-supply test. Measure the 5 V at the farthest panel.

Other flags: `--seconds N`, `--fps N` (frames per second, which is also
the card's refresh pass rate, as `Settings.fps` is for the server), `--port N`, and `--options key=value,…` (the
driver's knobs, as in `TRAFFICLIGHT_FPGA`, `docs/FIRMWARE.md` section 9).

## 6. How the software maps the picture

`trafficlight/output/mapping.py` turns the logical frame (128×384, row 0 =
top of the sign as the audience sees it) into the card's 128×512 image
(band *b* = rows 64·*b* .. 64·*b*+63 = port J*b+1*):

1. `rotate180` turns the whole frame.
2. Canvas panel *i* (64-row band *i* of the result, 0 = top) goes to band
   `panel_order[i]` of the card's image, i.e. port J`panel_order[i]+1`
   (six different values in 0..7). If `panel_flip[i]` is set, that panel is
   rotated 180°. The two bands without a panel are sent black.
3. `color_order` says which colour goes to the panels' R, G, B inputs:
   `bgr` puts blue on R, green on G and red on B.

The driver's packer (`trafficlight/output/hub75/pack.py`) then applies
`gamma` and brightness in 12.4 fixed point and splits the image into bit planes
(`docs/FIRMWARE.md`, section 9).

The wiring test picture is drawn in the card's image and skips steps 1 and
2 (colour order, gamma and brightness still apply). `output/wiring.py`
holds the rule from what the panels show to `panel_order` / `panel_flip`;
the web UI has the same rule in `web/src/pages/wiring.ts`.

## 7. Not verified yet (do on the hardware)

* Power draw at the chosen brightness.
* The colour order the panels need (`color_order`).
