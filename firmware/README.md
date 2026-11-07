# Custom gateware for the Colorlight 5A-75B V8.2

The card side of `docs/FIRMWARE.md`: a LiteX/Migen design without a CPU that receives frame
chunks, commits and schedule tables over UDP, keeps three frame slots in the SDRAM and streams
bit planes to the eight HUB75 connectors on a host-built visit table. Behaviour is defined by
the spec and the host-side reference model (`trafficlight/output/hub75/model.py`); the
simulation tests here compare the gateware with that model, datagram for datagram and pulse
for pulse.

## Layout

| File | Spec | What |
| --- | --- | --- |
| `tlcard/constants.py` | 3, 5, 6, 8 | The contract's numbers (checked against the host package by the tests). |
| `tlcard/platform.py` | FPGA-BRINGUP 3.1 | V8.2 platform (litex-boards' V8.0 tables) plus the HUB75 lines as one resource in lane order 6c + s. |
| `tlcard/packet.py` | 6.2–6.5 | Datagram parser on the 32-bit UDP user stream; 16 KiB receive FIFO with rollback (bad or oversize packets leave nothing); message FIFO in arrival order. |
| `tlcard/receiver.py` | 6.5, 6.6, 7, 8 | Frame slots and roles, chunk bitmap/count/bytes, commits, schedule validation and table RAM (3 tables: active, pending, upload), the pass boundary, counters, `last_error`, status reply builder. |
| `tlcard/sdram.py` | 4.4, 5.1 | SDR init sequencer (no CPU, so no software init), LiteDRAM controller + crossbar, one native port with the record fetcher ahead of the chunk writer. |
| `tlcard/streamer.py` | 1, 4.1, 5.1 | Record fetcher (SDRAM -> line buffer, 48-bit columns from 32-bit words, two visits ahead), visit sequencer (OE off / LAT / shift / OE on, underruns, `frame_ticks`), shifter (CLK, 48 data lines), built-in test pattern. |
| `tlcard/core.py` | — | Parser + receiver + streamer behind one SDRAM access interface, with the HUB75 outputs registered once. |
| `tlcard/top.py` | 2 | The FPGA top: PLL, RGMII PHY + LiteEth UDP/IP (ARP, ICMP, broadcast), GENSDRPHY, the core, the LED. |
| `tlcard/bringup.py` | FPGA-BRINGUP 6 | The LED-reporting bring-up designs: blink, MDIO PHY-id scan, SDRAM memtest. |
| `build.py` | — | Builds any of them; options for PHY, IP/MAC, clock, RGMII delays, seed. |
| `tests/` | — | Migen simulations (below). |

## Building

```
export PATH=~/tools/oss-cad-suite-20261001/bin:$PATH        # Yosys 0.69 / nextpnr-ecp5 0.11.1; the 2026-11-06 nightly crashes
P=~/.venvs/litex/bin/python
$P firmware/build.py main                 # the card gateware   -> ~/tools/tlcard-build/main/gateware/main.bit, .svf
$P firmware/build.py main --pattern       # same, lighting the built-in picture until a schedule arrives -> main_pattern/ (HUB75 pin test)
$P firmware/build.py blink                # Test B: LED 1 Hz from the 25 MHz clock
$P firmware/build.py mdio                 # Test D: PHY id scan on the LED
$P firmware/build.py memtest              # Test E: 8 MiB SDRAM test on the LED
```

Options: `--eth-phy 0|1` (which RJ45; the tested card uses 1), `--ip`, `--mac`, `--sys-clk-freq` (= `tick_hz`, default 50e6),
`--sdram-cl`, `--tx-delay`/`--rx-delay` (RGMII delays in the FPGA; 0 and 2 ns by default, the values that work on V8.2),
`--seed`, `--timingstrict`, `--out` (default `~/tools/tlcard-build`, keep bitstreams out of git),
`--no-run` (write Verilog and scripts only). Load with SRAM only until it works:
`openFPGALoader -c dirtyJtag -m <file>.bit` (docs/FPGA-BRINGUP.md section 5).

LED codes: **main** off until the SDRAM init is through, 1 Hz blink until a frame is shown,
then steady. **mdio**: per PHY address with a Realtek id (0x001C in register 2; the V8.2 card's read 0x001CC858), (address + 1) blinks at 2 Hz,
1.5 s pause; 3 s between rounds; a 5 Hz blink means no PHY answered. **memtest**: steady while
running, 0.5 Hz = pass, 5 Hz = fail.

### The test pattern (`--pattern`)

The pattern build runs a built-in one-plane table (32 rows, 128 columns, 25 MHz shift clock,
40 µs OE per row, ~1.4 ms per pass) over a generated picture, with the Ethernet and SDRAM
still present; the first accepted schedule switches it to normal operation. On every
connector:

* top half (R1 G1 B1): rows 0–7 red, rows 24–31 blue; rows 8–23 white blocks of 16 columns,
  as many blocks as the connector number (J1 one block … J8 eight); column 0 green on all rows;
* bottom half (R2 G2 B2): bars of 32 columns red, green, blue, white, with one black pixel per
  row at column = row + 48 (a diagonal from top-left to bottom-right when column 0 is on the left).

What it proves: the connector-to-pin map (block count), colour pins (bar colours), the row
address lines and their order (red/blue stripes, diagonal), the half mapping (R1 vs R2 rows),
the column direction (green column, diagonal), the clock edge and the latch/OE polarity.

## Simulation tests

```
PYTHONPATH=~/.venvs/trafficlight/lib/python3.12/site-packages ~/.venvs/litex/bin/python -m pytest firmware/tests -q
```

The LiteX venv has neither `pytest` nor `numpy` (both are needed; the reference model uses
numpy); the `PYTHONPATH` borrows them from the trafficlight venv (both are CPython 3.12). With
`pip install pytest numpy` in `~/.venvs/litex` the plain
`~/.venvs/litex/bin/python -m pytest firmware/tests -q` works. The repo's main suite only
collects `tests/`. The tests use Migen's simulator; `TLCARD_VCD=x.vcd` writes a trace.

| Test | What it checks |
| --- | --- |
| `test_packet.py` | Constants equal the host package's. The parser on `protocol.py` datagrams: status/commit/chunk/schedule messages with their fields and payload words in order; short, bad-magic, bad-version, unknown-type, length-mismatch, malformed-body packets are counted and leave nothing in the FIFO; a STATUS echo is ignored; a MAC error drops the packet; a full FIFO drops whole packets and later ones get through. |
| `test_receiver.py` | The card against `model.Card`: identical datagrams, every status field the model defines compared after each request. In-order frames, out-of-order and duplicated chunks, commit before chunks, a lost chunk abandoned by a newer frame, stale packets, frame id wrap at 2^32, wrong commit totals, out-of-range chunk, frame for another schedule, schedule switch dropping the pending frame and taking effect at the boundary, re-uploading the running table, rejected tables (tick rate, limits, bad entry, lat 0), bad packets. |
| `test_streamer.py` | The light: a small table (4 entries, 2 rows, 4 columns, 2 planes) and random frames go in as packets; the HUB75 pins are traced and every OE pulse of a pass (start tick, length, row address, the 48 x cols latched bits) must equal `model.Streamer`'s; a second frame appears only at a pass boundary and whole; status flags/ids/`frame_ticks`. The pattern build's picture. A slow SDRAM counts underruns and never changes a pulse length. |
| `test_sdram.py` | On LiteDRAM's SDRAM model: init, write/read-back of records across page boundaries, and a 192-word fetch completing within `min_visit_ticks` while the writer streams 360-word chunks. |

Not simulated: LiteEth itself (the MAC/ARP/IP/UDP stack is used as shipped and tested by its
authors) and the RGMII timing; see the bring-up notes.

## Build results (2026-11-06, Yosys 0.69 / nextpnr-ecp5 0.11.1, LFE5U-25F-7, seed 1)

| Bitstream | LUT4 | FF | DP16KD | MULT18 | sys (50 MHz) | eth_rx/tx (125 MHz) |
| --- | --- | --- | --- | --- | --- | --- |
| main | 13305 / 24288 (54 %) | 6040 (24 %) | 12 / 56 | 5 / 28 | 70.5 MHz | 159 MHz |
| main_pattern | 13134 (54 %) | 6013 (24 %) | 12 | 5 | 69.4 MHz | 143 MHz |
| memtest | 1382 (5 %) | | | | 109 MHz | |
| mdio | 466 (1 %) | | | | 156 MHz (clk25) | |
| blink | ~50 | | | | 237 MHz (clk25) | |

`tick_hz` stays at the reference 50 MHz: the sys clock closes at 70 MHz, so 100 MHz would
need pipelining of the receiver's checks and the fetcher's address arithmetic (and the SDRAM
at CL 3); not attempted. The main design first failed 125 MHz on the PHY clock (107 MHz, a
chain through LiteEth's 8-bit TX converter and the CDC FIFO); moving the MAC datapath into
`sys` (`with_sys_datapath`) fixed it with no change elsewhere.

## Design notes

* **One clock.** `sys` = `tick_hz` = the LiteDRAM clock (50 MHz; `--sys-clk-freq` changes all
  three and the status reports it). LiteEth's PHY clocks are 125 MHz; the MAC datapath runs in
  `sys` (`with_sys_datapath`) so only the CDC FIFOs are on the PHY clocks.
* **Order.** Datagrams become messages in arrival order; a commit is processed only after its
  chunks were handed to the controller, and the single native port keeps reads after writes
  (the crossbar locks the master to a bank until its queue drains).
* **Priority.** The fetcher owns the port whenever it has reads outstanding; the writer uses
  the gaps (~35 % of the cycles under the default schedule, 72 MB/s against 18 needed).
* **Boundary.** The fetcher requests it before fetching (entry 0, row 0); the receiver serves
  it in any cycle where no message is changing the same state. With no table active the
  fetcher requests it continuously, so an accepted table takes effect at once.
* **Tables.** Three table RAMs (36-bit entries: plane, OE, visit) rotate between active,
  pending and upload, so a rejected upload never damages the pending table. Header fields
  are registers.
* **Startup.** The sequencer starts when two records are in the line buffers (record k+1 is
  always fetched before visit k), shifts record 0 with OE off, then visit 0 latches it.
* **Odd column counts** are supported (records then start mid-word; the fetcher skips a half
  word), though the host only uses 128.
* **DQM tied low**: byte enables are not used; chunks are whole 32-byte words as the spec requires.

## Known gaps and bring-up risks

* Confirmed on the card with hand-written SRAM-loaded tests (docs/FIRMWARE.md section 11,
  note 15): JTAG, clk25/LED, the J1 colour pins, the shared A..E/CLK/LAT/OE pins and their
  polarities, A = LSB, the clock edge, the half mapping, the per-row sequence and the column
  direction (column 0 is shifted first and lands on the left), and the colour pins of
  J1..J6 (J7/J8 have no panel); these are the design's defaults. Not yet: the RGMII delays,
  the SDRAM timing, and this gateware itself on the card.
* The SDR init sequencer is new code (LiteDRAM has no CPU-less init); `memtest` is the test.
* The UDP user stream's byte order is assumed little-endian per 32-bit word (LiteEth's 8->32
  stride converter puts the first byte in bits 7:0). If the first `ping` works but every
  packet is a bad packet, that is the first thing to check (`bad_packets` in the status).
* No ARP timeout handling beyond LiteEth's: a reply to a host whose MAC is not cached waits
  for the ARP exchange (LiteEth does it in hardware).
