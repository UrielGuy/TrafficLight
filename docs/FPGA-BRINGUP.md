# FPGA bring-up: custom gateware on the Colorlight 5A-75B V8.2

This guide prepares the hardware side of replacing the card's stock firmware
with our own LiteX gateware (LiteEth UDP, LiteDRAM SDRAM, a HUB75 streamer
for 8 connectors, no CPU). It covers toolchain install, the V8.2 pinout and
how sure we are of it, the JTAG pads and a Raspberry Pi Pico as the cable,
the `openFPGALoader` commands, and a pin-discovery procedure that never
writes the flash.

Nothing here has been run against the real card yet. Every statement carries
a confidence tag:

* **[src]**: stated by at least one source linked in the text.
* **[multi]**: stated by two or more independent sources.
* **[confirm]**: must be checked on the real card (section 6 says how).

The stock firmware is backed up at `data/card-backup-2026-11-06/flash-4mb.bin`
(path only; this guide never reads `data/`). Keep that file safe before any
flash write.

## 1. What the card is (V8.2)

| Item | Part | Notes |
| --- | --- | --- |
| FPGA | Lattice `LFE5U-25F-7BG256I` (U33) | 25k LUT, CABGA256, speed grade 7, industrial. V8.0 is `-6BG256C`. [src: chubby75 V8.2 page] |
| Config flash | Winbond `25Q32JVSIQ` (U31) | 32 Mbit = 4 MiB, matches our dump size. WP# and HOLD# tied to 3.3 V, so no hardware write protect. [src] |
| SDRAM | ESMT `M12L64322A` (U29) | 2M x 32 bit = 64 Mbit = 8 MiB, 4 banks x 512k x 32, 200 MHz. One chip, 32-bit bus. [multi: chubby75 V8.0 and V8.2, litex-boards] |
| Ethernet PHYs | 2 x Realtek `RTL8211F`-family (U11, U13), RGMII | V8.0 page says `RTL8211FD`, V8.2 page says `RTL8211FP` with the suffix unconfirmed. Both are RTL8211F class. V6.1 and V7.0 had Broadcom B50612D, so do not use older pin tables. [src] |
| HUB75 level shifters | 12 x 74HC245-class octal buffers, powered from 5 V | Output only (see 3). [src] |
| Core / IO supply | 1.1 V and 3.3 V buck converters from the 5 V input | Input must stay below 5.5 V. [src] |

Clock: 25 MHz comes from PHY U13 and feeds PHY U11 and FPGA pin P6. If the
PHYs are held in reset (R6 low), the FPGA loses its clock. Gateware must
drive R6 high (or leave the reset unused) at all times. [src: chubby75 V8.0
and V8.2 pages; the reason litex-boards leaves `rst_n` commented out]

## 2. Toolchain install (for you to run)

Nothing below has been run by the agent. Linux x86-64 (WSL or a PC), not the
Pi: building gateware on a Pi 5 is slow.

### 2.1 oss-cad-suite (Yosys, nextpnr-ecp5, Project Trellis, openFPGALoader)

**Use the 2026-10-01 release** (installed here as `~/tools/oss-cad-suite-20261001`: Yosys 0.69+173,
nextpnr-0.11.1-40). The 2026-11-06 nightly (Yosys 0.70+344) breaks on LiteX designs: nextpnr crashes in
"Packing constants" on the netlist that Yosys writes (a plain Verilog blinky still works). With 2026-10-01
the stock litex-boards 5A-75B V8.2 target with Ethernet builds and meets timing (Ethernet RX 145 MHz
against 125, system clock 89 MHz against 60). Try a newer release only after it builds that target.

```
mkdir -p ~/tools && cd ~/tools
curl -sL https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-10-01/oss-cad-suite-linux-x64-20261001.tgz \
  | tar xz --transform 's,^oss-cad-suite,oss-cad-suite-20261001,'
export PATH=~/tools/oss-cad-suite-20261001/bin:$PATH
```

The general form, for another release:

```
mkdir -p ~/tools && cd ~/tools
# replace YYYYMMDD with the date in the file name you picked
wget https://github.com/YosysHQ/oss-cad-suite-build/releases/download/YYYY-MM-DD/oss-cad-suite-linux-x64-YYYYMMDD.tgz
tar xzf oss-cad-suite-linux-x64-YYYYMMDD.tgz        # creates ~/tools/oss-cad-suite
source ~/tools/oss-cad-suite/environment             # per shell; do not put it in .bashrc globally
yosys -V && nextpnr-ecp5 --version && openFPGALoader --Version
```

The `environment` script puts the suite first on `PATH`, so it shadows any
distro `openFPGALoader`. The suite's bundled openFPGALoader is newer than the
Debian bookworm package (0.10.0).

### 2.2 LiteX in its own venv

```
python3 -m venv ~/.venvs/litex
source ~/.venvs/litex/bin/activate
pip install --upgrade pip wheel setuptools meson ninja
mkdir -p ~/src/litex && cd ~/src/litex
wget https://raw.githubusercontent.com/enjoy-digital/litex/master/litex_setup.py
python litex_setup.py --init --install --config=standard
```

`--init` clones migen, litex, litex-boards, liteeth, litedram, litescope and
the rest into the current directory; `--install` pip-installs them in
development mode into the active venv. A RISC-V GCC is only needed for CPU
designs; we have none, so skip `--gcc=riscv`. To update later:
`python litex_setup.py --update` then `--install` again.

### 2.3 Smoke test: build the stock litex-boards target

```
source ~/tools/oss-cad-suite/environment
source ~/.venvs/litex/bin/activate
python -m litex_boards.targets.colorlight_5a_75x --board=5a-75b --revision=8.2 \
    --cpu-type=None --with-ethernet --eth-phy=0 --sys-clk-freq=60e6 --build
ls build/colorlight_5a_75b/gateware/    # colorlight_5a_75b.bit, .svf, .config
```

Do not add `--load`: that path calls OpenOCD, not openFPGALoader. Load the
`.bit` with the commands in section 5. A working smoke test proves the
toolchain only; the design does not do anything useful and the `--with-ethernet`
variant needs a PHY on the wire to be tested.

### 2.4 The litex-boards target, as of today

File `litex_boards/targets/colorlight_5a_75x.py`
([source](https://github.com/litex-hub/litex-boards/blob/master/litex_boards/targets/colorlight_5a_75x.py)),
platform `litex_boards/platforms/colorlight_5a_75b.py`
([source](https://github.com/litex-hub/litex-boards/blob/master/litex_boards/platforms/colorlight_5a_75b.py)).
[src]

* `--board` : `5a-75b` (default), `5a-75e`, `i5a-907`.
* `--revision` : `6.0, 6.1, 7.0, 8.0, 8.2`; default `7.0`. For the 5A-75B the
  platform accepts only `6.1, 7.0, 8.0, 8.2`; 6.0 exists for the 5A-75E only.
* `--eth-phy` : 0 or 1, selects which PHY is wired to the SoC (default 0).
  `--with-ethernet` (UDP/IP, needs `--eth-ip`, default 192.168.1.50, and
  `--remote-ip`, default 192.168.1.100) and `--with-etherbone` are mutually
  exclusive. `--eth-dynamic-ip` enables ARP/DHCP-less operation off.
* `--sys-clk-freq` (default 60e6), `--sdram-rate 1:1|1:2`, `--with-spi-flash`,
  `--use-internal-osc`.
* SDRAM module used for 8.0 and 8.2 : `M12L64322A`.
* The platform's 8.2 branch reuses the 8.0 IO and connector tables and only
  changes the device string to `LFE5U-25F-7BG256I`.

## 3. Pin tables

HUB75 connector pin order in litex-boards is the 16 physical pins in order,
so list index `i` is connector pin `i+1`:

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| HUB75 | R1 | G1 | B1 | GND | R2 | G2 | B2 | E | A | B | C | D | CLK | LAT | OE | GND |

(chubby75 calls the upper-half colours R0/G0/B0 and the lower half R1/G1/B1.
Pin 4 and pin 16 are GND. E is on pin 8, matching the predecessor's panels.)

Pins 7..14 (E A B C D CLK LAT OE) are **shared**: one FPGA pin per signal
drives all eight connectors, through several parallel buffers with 33 R (E:
extra 22 R) series resistors. Only the six colour lines are per connector.
Consequence for the streamer: all eight outputs run on one clock, latch,
blank and row address, so all attached panels must use the same scan layout
(ours do: 128x64, 1/32 scan). [multi: chubby75 V6.1/V8.0/V8.2 pages and the
litex-boards connector lists]

### 3.1 V8.0 and V8.2 (the table we will use)

Source: litex-boards `_io_v8_0` / `_connectors_v8_0` (used for 8.2 too) and
the chubby75 V8.0 and V8.2 pages, which agree with each other on every pin
below. [multi]

| Function | FPGA pins |
| --- | --- |
| clk25 | P6 |
| user LED (`DATA_LED-`, active low, open drain; shared with serial tx in litex) | T6 |
| user button (`KEY+`; shared with serial rx in litex) | R7 |
| Pad `N16` (unpopulated R26) | N16 |

HUB75 connectors, colour pins (R1 G1 B1 R2 G2 B2):

| Conn | R1 | G1 | B1 | R2 | G2 | B2 |
| --- | --- | --- | --- | --- | --- | --- |
| J1 | C4 | D4 | E4 | D3 | F5 | E3 |
| J2 | F1 | F2 | G2 | G1 | H2 | H3 |
| J3 | B1 | C2 | C1 | D1 | E2 | E1 |
| J4 | P5 | R3 | P2 | R2 | T2 | N6 |
| J5 | T13 | R12 | R13 | R14 | T14 | P12 |
| J6 | R15 | T15 | P13 | P14 | N14 | H15 |
| J7 | G16 | H14 | G15 | F15 | F16 | E16 |
| J8 | D16 | E15 | C16 | B16 | C15 | B15 |

Shared on all connectors: E = N4, A = N5, B = N3, C = P3, D = P4, CLK = M3,
LAT (STB) = N1, OE = M4.

Note: J1 B2 (E3) sits on a Bank 3 pin that is also `LRC_GPLL0C_IN`
(chubby75 V8.0 and V8.2 pages); harmless unless we use that PLL input.

SDRAM (M12L64322A), 32-bit, no CS (tied low), no CKE control (tied high),
DQM0..3 tied low (no byte masks, every write writes all 32 bits):

| Signal | Pins |
| --- | --- |
| CLK | C8 |
| A0..A10 | A9 B9 B10 C10 D9 C9 E9 D8 E8 C7 B8 (A10 / auto-precharge is B8) |
| BA0, BA1 | B7, A8 |
| WE#, CAS#, RAS# | B5, A6, B6 |
| DQ0..7 | B2 A2 C3 A3 B3 A4 B4 A5 |
| DQ8..15 | E7 C6 D7 D6 E6 D5 C5 E5 |
| DQ16..23 | A11 B11 B12 A13 B13 A14 B14 D14 |
| DQ24..31 | D13 E11 C13 D11 C12 E10 C11 D10 |
| not connected | A7 |

Ethernet. Reset, MDC and MDIO are shared by both PHYs; the RGMII pins of
PHY0 (U11) and PHY1 (U13) are separate. litex-boards leaves `rst_n`
commented out because of the clock dependency above.

| Signal | PHY0 (U11, `--eth-phy=0`) | PHY1 (U13, `--eth-phy=1`) |
| --- | --- | --- |
| PHYRstB (shared) | R6 | R6 |
| MDC (shared) | R5 | R5 |
| MDIO (shared) | T4 | T4 |
| TX clock | L1 | J16 |
| TX ctl | L2 | K14 |
| TXD0..3 | M2 M1 P1 R1 | K16 J15 J14 K15 |
| RX clock | J1 | M16 |
| RX ctl | J2 | P16 |
| RXD0..3 | K2 J3 K1 K3 | M15 R16 L15 L16 |

The RX pins double as PHY strap inputs sampled at reset (RXD3 PhyAddr0,
RXCTL PhyAddr1, RXC PhyAddr2, RXD2 PllOff, RXD1 TXDly, RXD0 RXDly), so the
PHY MDIO address is not known from the pages: scan 0..31. INTB is not
connected.
Which RGMII delays the PHYs add is therefore set by the board straps and not
known; LiteEth's `LiteEthPHYRGMII` has its own TX/RX delay options, so check
with a capture [confirm].

SPI flash (U31): CS# N8, SO (MISO) T7, SI (MOSI) T8. SCK is not a user
pin; it is reached through the `USRMCLK` primitive (V8.0 page: pad N9, with
`USRMCLKTS = 0`; the V8.2 page marks SCK "FIXME" but the circuit is the same
family). litex-boards uses `USRMCLK` for all revisions. [multi]

Configuration pads (V8.0 page, each pulled up to 3.3 V): PROGRAMN R9
(R106), CFG_1 P10 (R83), INITN T9 (R105), DONE P9 (R102). [src; confirm on
V8.2]

### 3.2 Older revisions (for reference; do not use for V8.2)

| | 6.1 | 7.0 |
| --- | --- | --- |
| FPGA | LFE5U-25F-6BG381C | LFE5U-25F-6BG256C |
| clk25 | P3 | P6 |
| LED / button | U16 / R16 | P11 / M13 |
| SDRAM chip | EM636165 (2 x 16-bit, 1 bank bit) | M12L16161A (2 x 16-bit, 1 bank bit) |
| SDRAM clock | B9 | C6 |
| Flash | GD25Q16 (2 MiB) CS R2 SO V2 SI W2 | W25Q32JV CS N8 SO T7 SI T8 |
| PHY | Broadcom B50612D | Broadcom B50612D |
| eth shared | rst P4, mdio P5, mdc N5 | mdio T2, mdc P3, rst unassigned |
| Colour pins J1 (R1 G1 B1 R2 G2 B2) | B3 A2 B2 B1 C2 C1 | F3 F1 G3 G2 H3 H5 |
| Shared E A B C D CLK LAT OE | J17 F1 E2 E1 F2 C18 J18 H16 | F15 L2 K1 J5 K2 B16 J14 F12 |

The full 6.1 and 7.0 connector lists are in the platform file linked above
(J2..J8 colour pins differ per connector). litex-boards does not support a
5A-75B 6.0; the 5A-75E 6.0 is a different board (platform
`colorlight_5a_75e.py`).

## 4. How sure are we that V8.2 uses the V8.0 pinout

**Confidence: medium-high that every pin in 3.1 is correct for V8.2.
Nothing is verified on our card; section 6 verifies it.**

For the V8.0 table:

* The chubby75 V8.2 page
  (<https://github.com/q3k/chubby75/blob/master/5a-75b/hardware_V8.2.md>)
  gives the same table for HUB75, SDRAM, PHY and flash as the V8.0 page
  (<https://github.com/q3k/chubby75/blob/master/5a-75b/hardware_V8.0.md>).
  It lists the same board parts, with the FPGA speed grade and the PHY suffix
  being the only differences.
* litex-boards gives revision 8.2 the 8.0 IO and connector tables.
* A LinuxCNC forum user wrote that "the v6 mapping seems to work with V8
  board, only validated the PHY and JTAG" (thread "LitexCNC: firmware and
  driver for 5A-75B and 5A-75E", <https://forum.linuxcnc.org/27-driver-boards/50383-litexcnc-firmware-and-driver-for-5a-75b-and-5a-75e?start=80>).
  This is about PHY and JTAG only, not the HUB75 pins, and the "V6" there
  most likely refers to the 5A-75E 6.0 tables, which differ from the 5A-75B's.
* The same thread (posts by uba100 and hmnijp, V8.2 boards) reports that a
  claim in the chubby75 GitHub issues says "pinout 8.2 corresponds to
  version 6.0", which hmnijp doubted after comparing photos with V8.0. We found
  no such statement in the chubby75 repository files; treat it as hearsay.
  Our recommendation: ignore the V6 claim for the 5A-75B.
* That thread also found that J3 pin 5 and 6 (encoder inputs) are driven
  through buffers (U24, and U18 which also serves "the distributed pins"), so
  they cannot be used as inputs without bypassing the buffer. That is expected
  from output-only buffers and does not contradict the table.
* litex-boards issue #362 (5A-75E, not 75B) reports a V8 board whose pin scan
  matched the V6.0 tables except for one typo. It is the likely origin of
  "V8 matches V6", for the 5A-75E.

Unknown for V8.2: the exact RTL8211F suffix, flash SCK pad (use USRMCLK), PHY
MDIO addresses, the RGMII delay straps, and whether any HUB75 pin differs from
V8.0 on a board revision after the chubby75 author's.

### 4.1 Are all HUB75 pins output only?

Yes. All eight connectors go through 74HC245-class buffers (12 chips) with
fixed direction FPGA to panel, powered from 5 V, with 33 R series resistors.
chubby75: "The level shifters are wired output only"; changing that means
replacing the buffers with SN74CBT3245A bus switches
(<https://zeromips.org/posts/2022-05-29-5a-75b/>). The FPGA cannot read the
connector pins back, so the pin-discovery procedure must be read with a meter,
LED or a panel, not by loopback. The HUB75 signal levels are 5 V (buffer
supply), the FPGA side is 3.3 V. [multi]

## 5. JTAG with a Raspberry Pi Pico

### 5.1 JTAG pads on the card

V8.0 and V8.2 (and V6.1 and V7.0) share this pad naming: [multi]

| Pad | Signal |
| --- | --- |
| J27 | TCK |
| J31 | TMS |
| J32 | TDI |
| J30 | TDO |
| J33 | 3.3 V (2-pin pad pair) |
| J34 | GND (2-pin pad pair) |

The four JTAG pads are an unpopulated 4-pin footprint near the FPGA (U33),
the 3.3 V and GND pads an unpopulated 2-pin footprint nearby. The chubby75
README ties its photo `jtag.jpg`
(<https://github.com/q3k/chubby75/blob/master/5a-75b/jtag.jpg>) to V6.1 and
V7.0 and says V8.0 has a different pad position; the V8.0 and V8.2 pages give
the same J-labels. Silkscreen labels `J27`, `J30`, `J31`, `J32`, `J33`, `J34`
should be printed on the board: find them by label, not by the photo. The
physical order of the pads along the footprint is not documented anywhere
we found [confirm].

Check before connecting: with the board unpowered, the multimeter continuity
test must beep between J34 and the GND of the 5 V input; with the board
powered from 5 V, J33 must read 3.3 V relative to J34.

Solder pin headers (or thin wire) to J27, J30, J31, J32 and J34. Use J33 only
as a voltage reference check; do not feed the board from it.

### 5.2 Pico firmware (pico-dirtyJtag)

The project <https://github.com/phdussud/pico-dirtyJtag> has release tags
(latest at time of writing: `V1.07`, which fixes `set-tdi` always driving
low; earlier: V1.06 TDO sampling timing, V1.03 OpenOCD support). Release
pages are <https://github.com/phdussud/pico-dirtyJtag/releases>; each carries
`.uf2` assets (3 to 4 of them, file names not visible to the researcher:
pick the one for a plain `Pico`/`rpi-pico`, usually `dirtyJtag.uf2`). If a
release has no suitable UF2, build it with the Pico SDK: `mkdir build && cd
build && cmake .. && make`, giving `dirtyJtag.uf2`. [src]

Flash: hold BOOTSEL while plugging the Pico into USB, copy the UF2 onto the
`RPI-RP2` drive; it reboots as USB `1209:c0ca` (dirtyJTAG).

### 5.3 Wiring (default Pico configuration)

| Signal | Pico GPIO | Pico pin | Card pad |
| --- | --- | --- | --- |
| TDI | GPIO16 | 21 | J32 |
| TDO | GPIO17 | 22 | J30 |
| GND | GND | 23 | J34 |
| TCK | GPIO18 | 24 | J27 |
| TMS | GPIO19 | 25 | J31 |
| RST (optional) | GPIO20 | 26 | not connected (ECP5 has no usable reset; the card's PROGRAMN pad R106 is not needed) |
| TRST (unused) | GPIO21 | 27 | not connected |

Pico TDI goes to the card's TDI (a target input), Pico TDO to the card's TDO.
[src: dirtyJtagConfig.h defaults]

Voltage: Pico GPIO is 3.3 V, the ECP5 JTAG pins are in a 3.3 V bank (the
J33 pad carries 3.3 V). Connect directly; no level shifter. Do not connect
the Pico's VBUS/3V3 pins to the card. Power the card from its own 5 V
supply and the Pico over USB; they only share GND. Keep leads short (under
15 cm); default TCK is 6 MHz and can be lowered with `--freq 1m` if
detection is flaky. [src for the 3.3 V pad; the compatibility is our
conclusion, confirm J33 reads 3.3 V]

### 5.4 openFPGALoader commands

`openFPGALoader` option names are from its source (`src/main.cpp`,
<https://github.com/trabucayre/openFPGALoader>) and the docs
(<https://trabucayre.github.io/openFPGALoader/>). The board name
`-b colorlight` is a 5A-75B (version 7) alias that sets the FPGA and flash
type; with the dirtyJTAG cable pass `-c dirtyJtag` as well. ECP5 flash
access goes through a temporary JTAG-to-SPI bridge loaded into SRAM, so the
running design is replaced while flashing.

```
source ~/tools/oss-cad-suite/environment    # or apt openfpgaloader, 5.5

# 1. Detect (expect the ECP5 LFE5U-25 IDCODE 0x41111043)
openFPGALoader -c dirtyJtag --detect

# 2. Load a bitstream into SRAM only (default mode; lost at power cycle)
openFPGALoader -c dirtyJtag -m build/colorlight_5a_75b/gateware/colorlight_5a_75b.bit

# 3. Back up the flash (4 MiB). Do this twice and compare:
openFPGALoader -c dirtyJtag -b colorlight --dump-flash --file-size 4194304 dump-a.bin
openFPGALoader -c dirtyJtag -b colorlight --dump-flash --file-size 4194304 dump-b.bin
cmp dump-a.bin dump-b.bin && cmp dump-a.bin /mnt/c/repos/TrafficLight/data/card-backup-2026-11-06/flash-4mb.bin

# 4. Write our gateware to flash (power-cycle the card afterwards)
openFPGALoader -c dirtyJtag -b colorlight -f --verify path/to/our.bit

# 5. Restore the stock firmware (raw 4 MiB image, offset 0)
openFPGALoader -c dirtyJtag -b colorlight -f --verify --file-type raw -o 0 \
    /mnt/c/repos/TrafficLight/data/card-backup-2026-11-06/flash-4mb.bin
```

Notes:

* `--dump-flash` requires `--file-size`; the output file is the positional
  argument. If your version writes elsewhere, check `openFPGALoader --help`
  [confirm; not run].
* `-f` erases only the sectors it writes. A raw `.bin` of 4 MiB from offset 0
  rewrites the whole stock image. After the write, power-cycle the card.
* If a write fails with a protection error, try `--unprotect-flash` once.
  WP# is tied high on V8.x and we found no report of a protected flash on
  these cards; chubby75 issue #129 shows someone dumping a V8.2 flash with
  openFPGALoader without any workaround.
* Writing the flash is not needed for development: use step 2 until the
  gateware works, so a bad bitstream is gone after a power cycle.

### 5.5 Raspberry Pi 5 (Raspberry Pi OS bookworm)

Option A, the distro package:

```
sudo apt update
sudo apt install openfpgaloader
openFPGALoader --Version
```

The bookworm package is `openfpgaloader` **0.10.0+git20230202-edea24f-1**
(Debian package search; trixie has 0.13.1-1). It supports the dirtyJtag
cable and ECP5. If it misbehaves, use B.

Option B, build from source (version 0.13 or newer):

```
sudo apt install git gzip libftdi1-2 libftdi1-dev libhidapi-hidraw0 libhidapi-dev \
    libudev-dev zlib1g-dev cmake pkg-config make g++
git clone https://github.com/trabucayre/openFPGALoader
cd openFPGALoader && mkdir build && cd build
cmake .. && cmake --build . -j4
sudo make install            # installs to /usr/local
sudo cp ../99-openfpgaloader.rules /etc/udev/rules.d/
```

Option C: if the oss-cad-suite release list has a `linux-arm64` tarball, use it
(same steps as 2.1).

udev rule for non-root USB access (the apt package installs one; for source
builds add it by hand). The upstream rule for dirtyJTAG is:

```
ATTRS{idVendor}=="1209", ATTRS{idProduct}=="c0ca", MODE="664", GROUP="plugdev", TAG+="uaccess"
```

```
echo 'ATTRS{idVendor}=="1209", ATTRS{idProduct}=="c0ca", MODE="664", GROUP="plugdev", TAG+="uaccess"' \
    | sudo tee /etc/udev/rules.d/99-dirtyjtag.rules
grep -w plugdev /etc/group || sudo groupadd --system plugdev
sudo usermod -a -G plugdev $USER
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Log out and in again (or reboot), unplug and replug the Pico, then
`openFPGALoader -c dirtyJtag --detect` should work without sudo. `lsusb`
should show `1209:c0ca`.

### 5.6 Windows (documented only, not tested here)

* Install the oss-cad-suite Windows build (`oss-cad-suite-windows-x64-*.exe`
  self-extractor on the YosysHQ releases page); it contains `openFPGALoader`.
  Run commands from its `start.bat` shell.
* dirtyJTAG uses libusb. If `openFPGALoader -c dirtyJtag --detect` finds no
  device, install the WinUSB driver for the dirtyJTAG interface with Zadig
  (<https://zadig.akeo.ie/>): select the `dirtyJTAG` device (`1209:C0CA`) and
  install WinUSB. Do not replace the driver of the CDC serial interface.
* Command syntax is as in 5.4.

## 6. Pin-discovery procedure (SRAM only)

Goal: confirm the V8.2 pin table with small bitstreams that never touch the
flash. Every test is loaded with `openFPGALoader -c dirtyJtag -m test.bit`
and disappears on power-cycle. If the card then boots the stock firmware
from flash again, nothing is lost. Build the test designs as LiteX/Migen
platforms using the V8.0 pin table as the starting point (a minimal
platform with `clk25`, the LED and plain `IO` resources; no SoC).

Test A, JTAG and the board identity. Detect (5.4 step 1). Expect the ECP5
IDCODE for LFE5U-25 (`0x41111043`) and nothing else in the chain. If the
IDCODE is different, stop: it is not the card we think it is.

Test B, LED and clock. 25 MHz on P6 divided down so the LED on T6 (active
low, open drain) blinks at 1 Hz. Proves the clock pin, LED pin and the
toolchain. Keep PHYRstB (R6) driven high from here on.

Test C, HUB75 connector pins. Walking single output: take the list of the
48 colour pins (8 connectors x 6) and the 8 shared pins; drive pin `n` high
for 2 s while all others are low, then pin `n+1`, and so on, with the LED on
T6 lit for the first step of every cycle (and a 1 s pause after the last), so
you can tell where the cycle starts. Every output is buffered, so the pin
shows 5 V at the connector.

Read it with a multimeter, black lead on card GND, red lead on one connector
pin. Walk the red lead over the 16 pins of J1 while the cycle runs and note
at which step number each pin goes high; compare with 3.1. One run per
connector for the colour pins; the 8 shared pins are verified once. Without
a panel connected this is safe. A faster alternative is a 1 k resistor plus
LED to GND on the connector pin with a second variant of the design that toggles
pin `k` at (k+1) * 0.5 Hz, so the blink rate names the pin; this reads
several pins per run. A panel on J1 also works for the shared lines (a CLK
burst or OE change is visible) but is the less precise check.
Mark each pin confirmed, swapped or dead.

Test D, Ethernet PHY over MDIO. Drive R6 high, toggle MDC (R5) at under
2.5 MHz and bit-bang MDIO (T4): for each PHY address 0..31 read register 2
and 3 (PHY identifier). Expected for a Realtek RTL8211F: OUI part
`0x001C` in register 2 and `0xC916` in register 3 (identifier 0x001CC916).
Both PHYs share the bus, so you will see two addresses answering (strapped
by the RX pins as listed in 3.1). Record both addresses, the link status
bit (register 1 bit 2) with a cable plugged into each RJ45, and which
RJ45 belongs to which address. LiteEth has an MDIO core (`LiteEthPHYMDIO`)
that can be reused in the test design with a small state machine to print
the result on the LED (blink code) or as a 1 s-per-bit pattern; a UART is
not available (the serial pins are the LED and button).

Test E, SDRAM. Build a design with LiteDRAM `M12L64322A` at 60 MHz,
`--sdram-rate 1:1`, plus a memtest core (`LiteDRAMBISTGenerator` and
`LiteDRAMBISTChecker`, or the LiteX BIOS-less `LiteDRAMNativePort` writer
and a checker). Result on the LED: slow blink = pass, fast blink = fail;
LED off or steady = hung. Test 8 MiB (full) with a counter pattern and an
address-xor pattern. If the init fails at 60 MHz, try 50 MHz and swap
`--sdram-rate`.

Test F, flash access through USRMCLK (optional, read only). Instantiate
the `USRMCLK` primitive with SPI pins N8/T8/T7, send the `9F` JEDEC id
command and show the manufacturer byte on the LED. Expect `EF 40 16`
(Winbond W25Q32). Optional; openFPGALoader's own `-f --detect` reports the
flash id as well:

```
openFPGALoader -c dirtyJtag -b colorlight -f --detect
```

## 7. Risks

* **Flash bricking.** A bad flash write leaves the card running only until
  the next power cycle; JTAG still works (the ECP5 JTAG port cannot be
  disabled by the flash unless the bitstream sets the JTAG port to SPI),
  so recovery is possible. Do not flash a bitstream with the `JTAG_PORT`
  option set to SPI, and do not drive the sysCONFIG pads (3.1) from user
  logic. Keep the backup (`data/card-backup-2026-11-06/flash-4mb.bin`),
  verify it against two fresh dumps first (5.4 step 3).
* **Wrong revision table.** Driving a wrong pin as an output can fight the
  PHY or SDRAM. Section 6 tests on an empty board first, and test A (IDCODE)
  and B (clock) prove the basics before any other pin is driven.
* **PHY reset kills the clock.** Never hold R6 low. [src]
* **5 V on the connectors.** The HUB75 buffers output 5 V; the panels also
  back-power. Do not probe with a 3.3 V instrument input, do not connect a
  panel while testing pins (except test C, option 2), and never feed 5 V into
  an FPGA-side pin. ECP5 I/O is not 5 V tolerant. [src]
* **Powering.** Power from the 5 V input only (below 5.5 V); never from the
  JTAG 3.3 V pad or the Pico 3V3. The Pico's USB ground and the card's
  ground are tied by the GND wire; use USB from one computer only.
* **Pad polarity.** The physical order of the J27/J30/J31/J32 pads is not
  documented; a mistake there can short 3.3 V to a JTAG pin. Check by
  label and with a meter before powering. [confirm]
* **Same pin for LED, serial and button.** In litex-boards the LED T6 and
  button R7 are also the serial TX/RX; they are the only user I/O, so test
  results must be shown with the LED.
* **No byte masks and no CS on the SDRAM.** DQM and CS# are tied; the
  LiteDRAM module needs no `dm`/`cs_n` pins, but writes are always 32 bits.
* **Shared HUB75 control.** All eight connectors have one common clock,
  latch, blank and row address. That limits ports to the same scan
  geometry and the same bit-plane timing; it is a physical fact of the card.
* **Speed grade.** V8.2 uses `-7`; the 8.0 build target (`-6BG256C`) is
  merely slower. Build for `LFE5U-25F-7BG256I` (the 8.2 revision selects
  it) so nextpnr checks timing at the real grade.
* **Ethernet delays.** RGMII delay strap (TXDly / RXDly) values are not
  documented for V8.2; the first `--with-ethernet` bitstream may need the
  TX or RX clock phase flipped in LiteEth (`tx_delay`, `rx_delay` options)
  before packets pass. Test with a back-to-back cable and a capture.
* **Debian 0.10.0 openFPGALoader** is old. If detection or flash access
  fails, use 0.13 (source or oss-cad-suite).
* **Hearsay in sources.** Everything above under "src" is documentation by
  third parties; nothing is verified on our card yet, and several details
  (PHY suffix, SCK pad, pad order) are open.
