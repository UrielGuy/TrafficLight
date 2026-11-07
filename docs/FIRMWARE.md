# Custom firmware for the Colorlight 5A-75B: the contract

This file specifies the open-source gateware for the receiver card and the
host side that feeds it. It is the contract between three pieces of work
done separately: the gateware (LiteX, no CPU), the host output driver
(`trafficlight/output/`), and the reference implementation in
`trafficlight/output/hub75/` (schedule builder, packer, packet formats and a
behavioural model of the card, with tests). If code and this file disagree,
fix one of them in the same change.

The stock firmware and its raw-Ethernet protocol are described in
`docs/HARDWARE.md` sections 8–9. What was wrong with it: the card swaps its
picture buffers on every display packet, in the middle of its own LED
refresh, so uneven host timing shows as flicker; it gives 6–11 bits of
colour under its own undocumented gamma; and it needs raw sockets (Npcap on
Windows, `CAP_NET_RAW` on the Pi).

Statements marked **[confirm]** are assumptions to check on the hardware;
the design tolerates them being wrong where it says so.

## 1. What the card does

The card is a **sequential streamer** and nothing else:

* It keeps three frame buffers in SDRAM: the one being **shown**, one
  **pending** (complete, waiting to be shown) and one being **received**.
* It runs a **visit schedule** from a small table in block RAM, over and
  over: for each entry, for each row address, read one record (one bit
  plane of one row address, all eight connectors) from the shown buffer,
  shift it into the panels, latch, and light the row for the entry's OE
  time. One pass of the table is one **refresh frame**, about 1/60 s.
* At the start of a pass (the **boundary**) it switches to the pending
  frame if there is one, and to a pending schedule table if there is one.
  Never in the middle of a pass. If nothing new arrived, it shows the same
  frame again, for ever.
* It receives UDP packets: frame chunks, a commit per frame, schedule
  tables, status requests; it answers status requests and schedule uploads
  with a status reply.

Everything else is on the host: gamma, brightness, temporal dithering,
splitting the picture into bit planes, laying the bytes out in the exact
order the streamer reads them, computing the schedule (which planes, how
long, in what order) from the clock, the depth, the refresh target and the
frame rate. Depth, refresh and shift clock are host settings, not gateware
builds. The gateware never interprets pixels.

### Block diagram

```
 host ──UDP/IP, gigabit──▶ RGMII PHY ─▶ LiteEth MAC + ARP/IP/UDP (hardware)
                                             │ datagrams to port 5775
                                             ▼
                                      packet parser ──▶ status reply builder ──▶ MAC TX
                                       │          │
                       frame chunks    │          │ schedule tables
                                       ▼          ▼
                              rx FIFO ─▶ SDRAM writer     table RAM A / B (BRAM)
                                            │                   │
                                     LiteDRAM (one 32-bit SDR SDRAM, 8 MiB)
                                            │ 3 frame slots
                                            ▼
                                 record fetcher ─▶ line buffer A / B (BRAM, cols x 6 bytes)
                                            │
                                      sequencer: entry / row counters, visit timer,
                                      pass boundary (frame swap, table switch)
                                            │
                                 shifter: CLK, 48 colour lines, LAT, OE, A..E
                                            │ 74HC245 buffers, 5 V
                                            ▼
                                   8 x HUB75 (J1..J8), 128x64, 1/32 scan
```

## 2. The hardware (from the board research; `docs/FPGA-BRINGUP.md`)

* Lattice ECP5 **LFE5U-25F-7BG256I**, 25 MHz clock from the PHY. About
  126 KB of block RAM (56 x 18 kbit).
* SDRAM: one ESMT **M12L64322A**, 2M x 32 bit = **8 MiB**, 4 banks, 256
  words (1 KiB) per page; CS low, CKE high, **DQM tied low: no byte masks,
  every write is a whole 32-bit word.** litex-boards has this module for
  revision 8.2. [confirm: the part and the clock it runs at under LiteDRAM]
* Ethernet: two Realtek RTL8211F PHYs (RGMII); either port is used
  (`--eth-phy=0|1`). Flash: Winbond 25Q32JV, 4 MiB.
* HUB75: eight connectors J1..J8. **CLK, LAT, OE and the row address A..E
  are common to all eight**; only R1 G1 B1 R2 G2 B2 are per connector, so
  one shift clock carries 48 colour bits and every visit lights the same row
  of all panels for the same time. Outputs go through 74HC245 buffers.
* Panels: 128x64, 1/32 scan (A..E), FM6124 constant-current drivers: a
  plain shift register plus latch, no PWM memory, no init sequence. Row
  address *a* drives panel row *a* on R1 G1 B1 and row *a* + 32 on R2 G2 B2
  [confirm]. The ESP32 predecessor clocked them at 20–23 MHz; the stock
  firmware at 25 MHz. The stock firmware's defaults: blanking 5 x 100 ns,
  minimum OE 99.8 ns.

## 3. Gateware constants (reported in the status reply)

| Name | Reference value | Meaning |
| --- | --- | --- |
| `tick_hz` | 50,000,000 | The sequencer's clock: every time in the schedule is in ticks of it. The reference build runs the sequencer, the SDRAM and the packet path on this one clock; 100 MHz is the alternative if LiteDRAM runs there (see 4.4). The host reads it from the status and builds the schedule for it. |
| `min_visit_ticks` | 256 | The card fetches one record from SDRAM within this many ticks under worst-case concurrent writes and refresh. The host never schedules a shorter visit. |
| `max_entries` | 176 | Schedule table entries (one UDP packet). |
| `max_rows` | 32 | Row addresses (A..E). |
| `max_cols` | 256 | Columns per record (line buffer size). |
| `max_planes` | 16 | Bit planes per frame. |
| `ports` | 8 | Connectors: 48 colour lines per column. |
| `buffers` | 3 | Frame slots in SDRAM. |

The Python constants are in `trafficlight/output/hub75/schedule.py`
(`CardLimits`) and `protocol.py`.

## 4. Timing

### 4.1 One visit

A *visit* is one (entry, row address): shift one record, latch, light. The
shift of the next record overlaps the light of this one (the FM6124 has a
separate latch register). The sequencer's visit, in ticks from the moment
OE goes off (`B1`, `L`, `B2` are the table's `blank_before`, `lat_ticks`,
`blank_after`; `oe` and `visit` are the entry's):

```
t = 0                 OE off (end of the previous visit's light)
t = B1                LAT high; A..E := this visit's row address
t = B1 + L            LAT low. The shifter starts clocking the NEXT visit's record
                      out of its line buffer: cols clocks of clk_div ticks each.
t = B1 + L + B2       OE on
t = B1 + L + B2 + oe  OE off. Idle (dark) until t = visit.
t = visit             next visit's t = 0
```

OE is active low on the connector; "on" means the pin is low. CLK idles
low; data is set up on the falling edge and the panel samples on the
rising edge (`clkphase = false` in the ESP32 driver worked for these
panels [confirm]). LAT and A..E are sampled by the panel around the LAT
pulse, so A..E change while OE is off; both blanking times are there
against ghosting and are tuned on the hardware (defaults 10 / 4 / 16 ticks
= 200 / 80 / 320 ns, total 600 ns, close to the stock 500 ns).

The record for visit *k*+1 is shifted during visit *k* (from its LAT to the
next LAT), and the record for visit *k*+2 is fetched from SDRAM during
visit *k* into the other line buffer. Hence the three lower bounds on a
visit, which the host's validator enforces and the card relies on:

```
visit >= B1 + L + B2 + oe                   the light fits
visit >= cols * clk_div + L + shift_margin  the shift fits between two LATs
visit >= min_visit_ticks                    the fetch fits
```

If a fetch is nevertheless late (it must not be), the sequencer waits for
it with OE off and counts an **underrun** in the status: timing stretches,
intensity does not change, because light is only ever the OE time.

### 4.2 Binary-coded modulation with bit splitting

The frame buffer holds `planes` bit planes (12 by default). Plane *p* must
be lit for 2^*p* x LSB per frame for the output to be linear. A plane lit
once per frame flickers at the frame rate (60 Hz), which cameras and eyes
see for the bright planes. So planes from `split` (6) up are shown as
2^(*p* − `split`) **segments**, each of 2^`split` LSBs = the same length as
the plane `split` itself, spread evenly over the frame; planes below
`split` are shown once. The frame is divided into 2^(`planes` − 1 −
`split`) = 32 **sub-frames**; every sub-frame carries one MSB segment, every
second one a segment of the next plane, and so on (a ruler pattern: plane
*p* with *n* segments sits in sub-frames `step/2 + i*step`, `step = 32/n`).
The six short planes are added one by one to the sub-frame with the least
time so far, so the sub-frames come out nearly equal. Within a sub-frame
the entries run brightest first, and each entry visits all 32 rows in
order before the next entry starts.

For the defaults this gives 69 entries per pass (69 visits per row per
frame): 32 + 16 + 8 + 4 + 2 + 1 segments of planes 11..6, plus planes 5..0.
The sub-frames are `[11,5,4] [11,10,3] [11,9,2] [11,10,1] [11,8,0] [11,10]
[11,9] [11,10] [11,7] [11,10] [11,9] [11,10] [11,8] [11,10] [11,9] [11,10]
[11,6] ...` (then the same without the short planes).

A pixel at value 2048 (MSB only) is lit 32 times per frame at ~1920 Hz;
a full-white pixel 69 times. The light of a dim pixel (planes 0..5 only)
comes once per frame; that is inherent in BCM and dim.

### 4.3 The budget

Per row address, a frame gives 1/60 s / 32 = 520.8 µs for all of the row's
visits. With a 50 MHz tick (20 ns), a 25 MHz shift clock (`clk_div` 2,
shift 256 ticks = 5.12 µs) and 600 ns of blanking:

| Visit | OE | Length | Why |
| --- | --- | --- | --- |
| short plane (0..5) | 5..160 ticks | 264 ticks = 5.28 µs | shift-bound: 256 + L 4 + margin 4 |
| segment (planes 6..11) | 320 ticks = 6.4 µs | 350 ticks = 7.0 µs | light-bound: 30 + 320 |

6 x 264 + 63 x 350 = 23,634 ticks per row, x 32 rows = 756,288 ticks =
15.13 ms. That fits 16.67 ms, and the spare 1.54 ms is spread as equal
dark padding over the 69 entries (35 ticks each) so a pass is exactly
1/60 s (within 32 ticks); the padding changes no intensity. An LSB of 6
ticks would need 17.7 ms: at a 50 MHz tick the LSB lands on 100 ns, the
stock firmware's minimum OE.

`Schedule.budget()` prints these numbers for any parameters. The
trade-offs (the first row is the default):

| Tick / shift clock, fps | planes | visits per row | sub-frames | refresh Hz mean (min–max) | LSB ns | segment µs | duty | SDRAM read MB/s peak / mean |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 50 MHz / 25 MHz, 60 | 12 | 69 | 32 | 1920 (1462–2035) | 100 | 6.40 | 0.79 | 128 / 102 |
| 50 MHz / 25 MHz, 60 | 11 | 68 | 32 | 1920 (1477–1998) | 220 | 7.04 | 0.86 | 140 / 100 |
| 50 MHz / 25 MHz, 30 | 12 | 69 | 32 | 960 (838–978) | 240 | 15.36 | 0.94 | 145 / 51 |
| 50 MHz / 25 MHz, 30 | 13 | 70 | 32 | 960 (780–998) | 100 | 12.80 | 0.79 | 102 / 52 |
| 50 MHz / 12.5 MHz, 60 | 12 | 38 | 16 | 960 (786–1097) | 100 | 12.80 | 0.79 | 68 / 56 |
| 50 MHz / 25 MHz, 60, no split | 8 | 8 | 1 | 60 | 2000 | 512 | 0.98 | 137 / 12 |
| 100 MHz / 25 MHz, 60 | 12 | 69 | 32 | 1920 (1482–2021) | 110 | 7.04 | 0.86 | 137 / 102 |
| 100 MHz / 20 MHz, 60 | 12 | 69 | 32 | 1920 (1419–2051) | 110 | 7.04 | 0.86 | 113 / 102 |

"Refresh" is the sub-frame rate: how often a bright pixel is relit. "Duty"
is the fraction of the frame a full-white pixel is lit: the brightness
cost of the blanking and padding. 12 planes with 32 sub-frames at 60 fps
needs the 25 MHz (or 20 MHz) shift clock; 13 planes need 30 fps. The
builder refuses what does not fit and says what to change.

### 4.4 SDRAM bandwidth

A record is `cols` x 6 = 768 bytes, read in one burst of 192 words; a
visit needs one record. Peak demand is during the short planes: 768 B per
264 ticks = 128 MB/s at 50 MHz; the mean over a frame is 102 MB/s (69 x
32 x 768 B per 1/60 s). Writes: one frame of 294,912 B per 1/60 s = 17.7
MB/s on average, but a packet is 1440 B in 12 µs at wire speed.

One 32-bit SDR SDRAM at 50 MHz gives 200 MB/s peak; sequential 768-byte
bursts cost an activate and a precharge per page (two records in four
cross a 1 KiB page boundary), about 5 % overhead, and refresh under 1 %.
So about 185 MB/s is usable, against 128 + writes. **The record fetcher has
priority on the SDRAM; the packet writer takes what is left** (a share of
at least 25 % is enough: 50 MB/s against 17.7 MB/s needed). The host must
therefore **pace its packets** (section 9): a whole frame at wire speed is
118 MB/s of writes for 2.4 ms, more than the card can absorb, and would
overflow the receive FIFO. The FIFO is at least 16 KiB (11 packets); a
packet that does not fit is dropped whole and counted in `rx_dropped`, and
its frame is then incomplete and never shown.

If LiteDRAM runs at 100 MHz the margin doubles and `tick_hz` is 100 MHz;
nothing on the host changes but the numbers it computes. If the SDRAM
turns out slower than planned, the card reports a larger
`min_visit_ticks` and the host builds a schedule with fewer planes or a
lower refresh; the gateware is not rebuilt. [confirm: the sustained
sequential read rate of LiteDRAM on this board]

### 4.5 Why the swap is at the pass boundary

A frame's planes are shown over the whole pass. Swapping buffers in the
middle of a pass would show the MSB of one frame with the LSBs of the
next for one frame: a wrong intensity, visible as a flash on moving
pictures. Swapping at a sub-frame boundary has the same problem (a
sub-frame carries only some planes). So the swap is where a pass starts,
and a frame is shown whole. Latency from commit to first light is up to
one pass (16.7 ms) plus the pass itself; host frames arriving faster than
passes replace each other in the pending slot (the newest wins), slower
ones are repeated. The host's frame timing has no effect on the panels'
timing at all.

The precise boundary: the moment the fetcher starts fetching the first
record of a pass (entry 0, row 0), which is two visits before the last
visit of the previous pass ends. A frame committed in those last two
visits waits one more pass. `frame_ticks` in the status is the measured
length of the last pass, LAT of (0, 0) to LAT of (0, 0).

## 5. Memory layout

### 5.1 Frame buffer (SDRAM), byte-exact

A frame is `planes` x `rows` x `cols` x 6 bytes (294,912 for 12 x 32 x
128). Offsets within a frame:

```
record(p, a)      at (p * rows + a) * cols * 6        plane p (0 = LSB), row address a
column x of it    at + x * 6                          a 48-bit little-endian word
bit 6*c + s       connector J(c+1), colour line s: 0 R1, 1 G1, 2 B1, 3 R2, 4 G2, 5 B2
```

Bit (6*c* + *s*) of column *x* of record (*p*, *a*) is bit *p* of the PWM
value of the LED at panel column *x*, panel row *a* (lines 0..2) or *a* +
32 (lines 3..5), on connector J(*c*+1). In the card image
(`config.CARD_H` x `W` x 3, band *b* = connector J(*b*+1)) that LED is
pixel (64*c* + 32*g* + *a*, *x*), channel *s* mod 3, with *g* = *s* div 3.

The shifter reads a record as 192 little-endian 32-bit words and shifts
**column 0 first**, bit 6*c* + *s* on connector J(*c*+1)'s line *s*. If a
panel turns out to want the last column first, or a different row-address
order, or its halves the other way round, the host packs differently: the
gateware is fixed. [confirm: column direction and half order on these
panels]

The SDRAM holds three frame slots of `BUFFER_BYTES` = 393,216 bytes (the
largest frame the limits allow: 16 x 32 x 256 x 6 / 2; a 256-column
frame can have at most 8 planes, a 128-column one 16), at offsets 0,
393,216 and 786,432: 1.125 MiB of the 8 MiB. Chunk writes land at `slot +
offset`; the card only checks `offset + length <= BUFFER_BYTES`. Offsets and
lengths are multiples of 32 bytes, so every write is whole words (DQM is
tied low) and whole bursts.

Records of one entry (its 32 row addresses) are contiguous: 24 KiB of
sequential reads per entry.

### 5.2 Block RAM

| What | Size | Why |
| --- | --- | --- |
| Schedule tables A and B | 2 x 176 x 8 B = 2.75 KiB | The table in use and the one being uploaded; they swap at a boundary. |
| Line buffers A and B | 2 x 1536 B | The record being shifted and the one being fetched. |
| Receive FIFO | >= 16 KiB | Absorbs packet bursts while the fetcher has the SDRAM (section 4.4). |
| Chunk bitmap | 512 bits | Which chunks of the frame being received have arrived. |
| LiteEth buffers | a few KiB | RX/TX slots of the MAC. |

## 6. UDP protocol

### 6.1 Addressing

* The card has a fixed IPv4 address and MAC built into the gateware:
  **10.75.0.2**, netmask 255.255.255.0, MAC `02:5a:75:00:00:02` (locally
  administered). The host uses **10.75.0.1/24** on the dedicated port
  (static, configurable in the host settings; the card's address is a
  host setting too, default 10.75.0.2). To change the card's address:
  rebuild with other constants. A later gateware could read it from the
  flash; the protocol does not care.
* The card answers ARP for its address and ICMP echo (LiteEth does both),
  so `ping 10.75.0.2` is the first test.
* UDP port **5775** on the card. Every reply goes to the source IP and port
  of the request. The card also answers requests sent to the broadcast
  address (10.75.0.255 or 255.255.255.255), which is how a host finds the
  card without knowing anything: broadcast a status request, read the
  reply's source.
* MTU 1500: UDP payload at most 1472 bytes. No jumbo frames (the PC uses a
  USB gigabit adapter). The host uses ordinary UDP sockets: no
  administrator rights, no Npcap, no `CAP_NET_RAW`.

### 6.2 Common header (16 bytes, all integers little-endian)

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 4 | magic | ASCII `5A75` |
| 4 | 1 | version | 1 |
| 5 | 1 | type | below |
| 6 | 2 | body length | bytes after this header; must match the datagram |
| 8 | 4 | seq | chosen by the host; echoed in the reply it causes |
| 12 | 4 | reserved | 0 |

| Type | Name | Direction | Reply |
| --- | --- | --- | --- |
| 0x01 | STATUS_REQ | host -> card | STATUS |
| 0x02 | FRAME_DATA | host -> card | none |
| 0x03 | FRAME_COMMIT | host -> card | none |
| 0x04 | SCHEDULE | host -> card | STATUS (carries the result) |
| 0x81 | STATUS | card -> host | — |

A datagram with a wrong magic, version, type, or a body length that does
not match is dropped and counted in `bad_packets`; `last_error` = 1.

### 6.3 FRAME_DATA (type 0x02): one chunk of a frame

Body: 16-byte chunk header, then the data.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | frame_id |
| 4 | 2 | chunk_index: 0 .. chunk_count − 1, below 512 |
| 6 | 2 | data length: 32 .. 1440, a multiple of 32 |
| 8 | 4 | offset into the frame: a multiple of 32 |
| 12 | 4 | reserved (0) |
| 16 | n | data |

The reference chunking (`protocol.chunk_frame`) uses 1440-byte chunks:
a 294,912-byte frame is 205 chunks (the last of 1152 bytes), 206
datagrams with the commit, 1496 bytes on the wire each: about 314 KB per
frame, 18.9 MB/s = 151 Mb/s at 60 fps, 12,360 packets/s. Chunks may be any
size and order; the card writes each at its offset.

### 6.4 FRAME_COMMIT (type 0x03): the frame is complete

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | frame_id |
| 4 | 4 | schedule_id the frame was packed for |
| 8 | 4 | frame_bytes: the sum of the chunk lengths |
| 12 | 2 | chunk_count |
| 14 | 2 | reserved (0) |

The commit may arrive before some (or all) of the chunks: the frame is
complete when the commit has been seen **and** `chunk_count` distinct
chunk indices have arrived (section 7.1).

### 6.5 SCHEDULE (type 0x04): a whole visit table

Body: the table, 32-byte header plus 8 bytes per entry, in one datagram
(at most 176 entries = 1440 bytes).

Table header:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | schedule_id (host-chosen; the reference uses a CRC-32 of the rest, so equal tables have equal ids) |
| 4 | 4 | tick_hz the table was built for: must equal the card's |
| 8 | 2 | entries: 1 .. 176 |
| 10 | 1 | rows: 1 .. 32 row addresses per entry |
| 11 | 1 | planes: 1 .. 16 bit planes in the frame |
| 12 | 2 | cols: 1 .. 256 columns per record |
| 14 | 1 | clk_div: >= 2, shift clock period in ticks (CLK high for clk_div/2, rounded down) |
| 15 | 1 | flags (0) |
| 16 | 2 | blank_before: OE off -> LAT rise, ticks |
| 18 | 2 | lat_ticks: LAT pulse, >= 1 |
| 20 | 2 | blank_after: LAT fall -> OE on, ticks |
| 22 | 2 | reserved (0) |
| 24 | 4 | frame_bytes = planes x rows x cols x 6 |
| 28 | 4 | reserved (0) |

Entry *k* at 32 + 8*k*:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | plane: 0 .. planes − 1 |
| 1 | 1 | reserved (0) |
| 2 | 2 | oe_ticks: OE on time (0 allowed: a dark visit) |
| 4 | 2 | visit_ticks: length of the visit, OE-off to OE-off (section 4.1) |
| 6 | 2 | reserved (0) |

The card checks: body length = 32 + 8 x entries; entries, rows, planes,
cols, clk_div within the limits; tick_hz equal to its own; frame_bytes =
planes x rows x cols x 6 and <= BUFFER_BYTES; every plane < planes. It
does **not** check the timing rules of 4.1 (the host's validator does;
a bad table makes a mess but nothing worse). A rejected table changes
nothing; `last_error` says why (2 tick rate, 3 limits, 4 entry) and
`sched_rejected` counts it. An accepted table becomes **pending** and takes
over at the next boundary (section 7.3); the reply shows it in
`sched_pending_id`. Uploading the table the card already runs (same id) is
harmless: it is accepted and switched to, with the same content.

### 6.6 STATUS_REQ (type 0x01) and STATUS (type 0x81)

STATUS_REQ has no body. STATUS's body is 100 bytes:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | proto_version | 1 |
| 1 | 1 | fw_major | gateware version |
| 2 | 1 | fw_minor | |
| 3 | 1 | flags | bit 0 have_frame (a frame has been shown since power-up), 1 sched_active, 2 sched_pending, 3 frame_pending, 4 blank (OE held off: no schedule or no frame yet) |
| 4 | 4 | fw_build | build identifier (git hash or date), informational |
| 8 | 4 | tick_hz | section 3 |
| 12 | 4 | uptime_ms | since power-up, wraps after 49 days |
| 16 | 2 | min_visit_ticks | section 3 |
| 18 | 2 | max_entries | |
| 20 | 2 | max_cols | |
| 22 | 1 | max_rows | |
| 23 | 1 | max_planes | |
| 24 | 1 | ports | 8 |
| 25 | 1 | buffers | 3 |
| 26 | 1 | last_error | the most recent error code (section 8); 0 = none since power-up |
| 27 | 1 | reserved | |
| 28 | 4 | sdram_bytes | 8,388,608 |
| 32 | 4 | sched_active_id | 0 = none |
| 36 | 4 | sched_pending_id | 0 = none |
| 40 | 4 | frame_id_shown | of the frame on the panels |
| 44 | 4 | frame_id_rx | of the frame being (or last) received |
| 48 | 4 | frames_shown | swaps at a boundary |
| 52 | 4 | frames_committed | frames that became pending |
| 56 | 4 | frames_incomplete | frames abandoned (a newer one started) or whose commit totals were wrong |
| 60 | 4 | frames_rejected | complete frames for a schedule that is not the active or pending one |
| 64 | 4 | frames_replaced | pending frames overwritten by a newer one before being shown (host faster than the card: expected), or dropped by a schedule change |
| 68 | 4 | chunks_received | |
| 72 | 4 | chunks_dropped | stale frame id, offset out of range |
| 76 | 4 | rx_dropped | packets lost in the card: receive FIFO full, MAC errors |
| 80 | 4 | underruns | visits delayed by a late fetch (must stay 0) |
| 84 | 4 | frame_ticks | length of the last pass in ticks (the real frame period) |
| 88 | 4 | bad_packets | |
| 92 | 4 | sched_accepted | |
| 96 | 4 | sched_rejected | |

All counters are 32-bit, wrap, and reset only at power-up. The host polls
the status about twice a second (like the current driver's detection): a
reply with a smaller `uptime_ms`, or without `sched_active`, means the card
restarted and needs its schedule again; `frames_incomplete`,
`rx_dropped` or `underruns` growing means packets are being lost or the
schedule is too tight.

## 7. State machines

### 7.1 Receiving a frame

State: `rx_frame_id`, `rx_active`, a 512-bit chunk bitmap with a count of
set bits, `rx_bytes` (sum of distinct chunks' lengths), `commit_seen` with
the commit's totals; `rx_seen` (any frame since power-up). Frame ids are
compared with 32-bit wrap: *a* is newer than *b* when 0 < (*a* − *b*) mod
2^32 < 2^31.

For a chunk or a commit with frame id *F*:

1. `rx_active` and *F* = `rx_frame_id`: it belongs to the current frame.
2. Else if `rx_seen` and *F* is not newer than `rx_frame_id`: **stale**,
   dropped, `chunks_dropped`++, `last_error` 8. (A late packet of a frame
   already committed or abandoned.)
3. Else *F* starts a new frame: if `rx_active`, the current one is
   abandoned (`frames_incomplete`++): a lost chunk is only known to be
   lost when something newer arrives. Bitmap, count, bytes and commit are
   cleared; `rx_frame_id` = *F*, `rx_active` = 1, `rx_seen` = 1.

Then a **chunk** with `offset + length > BUFFER_BYTES` is dropped
(`chunks_dropped`++, error 5); otherwise its data is written to the
receiving slot at `offset`; if its index bit was clear, the bit is set, the
count and `rx_bytes` grow. Duplicates are harmless. A **commit** stores its
totals and sets `commit_seen`.

After either, if `commit_seen` and count >= `chunk_count`: the frame is
finished (`rx_active` = 0). It is **complete** if count = `chunk_count`,
`rx_bytes` = the commit's `frame_bytes`, and no index >= `chunk_count` was
seen; otherwise `frames_incomplete`++, error 6, and the slot is simply
reused. A complete frame is **accepted** if its `schedule_id` and
`frame_bytes` match the pending schedule, or the active one when none is
pending; otherwise `frames_rejected`++, error 7. An accepted frame becomes
the **pending frame**: the receiving slot and the spare slot swap roles
(if a frame was pending, it is lost: `frames_replaced`++),
`frames_committed`++.

Reception never writes the shown slot or the pending slot: with three
slots the host can stream continuously at any rate and the panels always
show a whole frame.

### 7.2 The pass boundary

When the fetcher starts a pass:

1. If a schedule is pending: it becomes active (tables swap; the new
   `rows`, `planes`, `cols`, `clk_div` and blanking apply from entry 0).
2. If a frame is pending (and a schedule is active): the shown slot and the
   spare slot swap roles, `frame_id_shown` = its id, `frames_shown`++,
   `have_frame` = 1.
3. Entry 0, row 0.

With no active schedule the card is idle with OE off and the "boundary"
is immediate: an accepted schedule takes effect at once and the first
pass starts. Until the first frame has been shown, OE stays off (the
slots hold garbage after power-up): flag `blank`.

### 7.3 Schedule activation

An accepted table waits in the other table RAM until a boundary (7.2). A
pending table replaces an earlier pending one. Because a frame is packed
for a particular table (planes, rows, cols), a frame and the table it
needs switch together: when a table with a different id from the active
one is accepted, a pending frame is dropped (`frames_replaced`++), and
from then on commits must name the pending table's id (7.1). The host's
order of operations for a change of depth or refresh: upload the table,
then send frames packed for it; the first such frame and the table appear
together at the next boundary, and the panels never show a frame under
the wrong table. Changing only timing (blanking, LSB length, padding) with
the same planes/rows/cols still gets a new id, so the same order applies;
one frame may be replaced during the switch, nothing is shown wrongly.

### 7.4 Power-up and loss of the host

Power-up: no schedule, no frame, OE off, all counters 0. The host notices
(status without `sched_active`, or `uptime_ms` going back) and uploads its
schedule and the current frame. When the host stops sending, the card
keeps showing the last frame indefinitely. There is no timeout and no
watchdog on the host: a sign that keeps its picture is better than a dark
one.

## 8. Error codes (`last_error`)

| Code | Name | When |
| --- | --- | --- |
| 0 | ok | nothing since power-up |
| 1 | bad packet | header, length or type wrong |
| 2 | schedule: wrong tick rate | `tick_hz` is not the card's |
| 3 | schedule: over the limits | entries, rows, planes, cols, clk_div, frame_bytes |
| 4 | schedule: bad entry | a plane out of range |
| 5 | chunk out of range | `offset + length > BUFFER_BYTES` |
| 6 | frame incomplete at commit | wrong totals, or an index beyond `chunk_count` |
| 7 | frame for another schedule | `schedule_id` / `frame_bytes` do not match |
| 8 | stale frame id | a packet of an older frame |

The reference parser (`protocol.parse`) and card model (`model.Card`)
implement exactly these; gateware tests can replay the same datagrams and
compare the status fields.

## 9. The host's duties

* **Build the schedule** from the card's status (`tick_hz`,
  `min_visit_ticks`, limits) and the settings (planes, split, clk_div,
  fps, blanking, min OE): `schedule.build`. Validate it; upload it at
  open, after a card restart, and whenever the settings change.
* **Pack every frame** for the active (or pending) schedule:
  `pack.Packer` takes the mapper's card image (`Mapper.apply`, which
  already handles panel order, flips, rotation and the colour order) and
  gives the frame bytes: 8-bit -> gamma LUT (12.4 fixed point, brightness
  folded in) -> optional temporal dithering (the fraction left over is
  carried to the next frame; a still picture averages to the 16-bit value
  over 16 frames) -> bit planes -> records. About 1.5 ms on a PC, a few ms
  on a Pi 5, whole-array numpy. The stock-firmware setting `gamma = 1.0`
  "because the card applies its own" no longer applies: this card is
  linear, the host's gamma (2.2) is the gamma.
* **Send** the chunks and the commit (`protocol.chunk_frame`) with a
  monotonic `frame_id`, **paced**: at most 8 packets back to back, then
  at least 250 µs before the next group (a frame then takes 6.5 ms to
  send, less than a 60 fps period). Equivalent spacing (a packet every
  30 µs) is fine too. The counters `rx_dropped` and `frames_incomplete`
  show when pacing is insufficient.
* **Poll** the status about twice a second, with `seq` to match replies;
  report firmware, uptime, counters and `last_error` in the driver status;
  re-upload the schedule when the card restarted or runs another id.
* **At close**, send a black frame (the card keeps it).

The host never needs to know the card's timing except through the status
fields. A frame sent for the wrong schedule is rejected, never shown.

### Using the custom firmware

The driver is `trafficlight/output/fpga.py` (`FpgaDriver`). Plug the PC's
network port into the card, give that port 10.75.0.1/24, check
`ping 10.75.0.2`, then choose **Custom firmware** in Settings → LED output
(the card address field defaults to the firmware's 10.75.0.2; the UI sets
gamma to 2.2 when switching, as this card is linear). The setup checks list
whether the card answers, its firmware version, the schedule it runs and
whether it loses packets. Notes on what the driver does beyond the list above:

* Frame ids start after the card's `frame_id_rx` from the first status, so a
  restarted program is never taken for a stale sender.
* The frame rate the engine sets is the card's pass rate: a change builds
  and uploads a new schedule (new id). If the configured depth does not fit
  that rate, it falls back to fewer planes (down to 8) and warns.
* Brightness and gamma change only the packer's LUT; the picture is packed
  again and sent, the schedule stays.
* A frame equal (in packed bytes) to the last one sent is not sent; with
  dithering on, a still picture keeps changing and keeps flowing.
* A frame the card does not show 0.3 s after sending (lost chunk or commit,
  or a card restart) is sent again, at most once a second.
* Knobs, in the environment variable `TRAFFICLIGHT_FPGA`, e.g.
  `planes=11,dither=off,burst=4,gap_us=300`: `planes`, `split`, `clk_div`,
  `blank_before`, `lat_ticks`, `blank_after` (ticks), `min_oe_ns`, `dither`,
  `pace`, `burst`, `gap_us`, `chunk_bytes`, `broadcast` (addresses asked,
  separated by `/`, when the card does not answer at its own address).

## 10. Limits

| | |
| --- | --- |
| Frame slots | 3 of 393,216 bytes; a frame <= 393,216 bytes |
| Chunks per frame | 512; chunk data 32..1440 bytes, 32-byte aligned offsets |
| Schedule | 176 entries, rows <= 32, cols <= 256, planes <= 16, clk_div 2..255, 16-bit OE and visit times (655 µs at 50 MHz) |
| Depth | 12 planes at 60 fps with a 25 MHz shift clock; 13 at 30 fps |
| Frame ids, schedule ids | 32-bit; frame ids compared with wrap, 0 is a valid id; schedule id 0 means "none" in the status, so the host never uses 0 |
| Packet rate | about 12,400 packets/s at 60 fps; 151 Mb/s |

## 11. Open questions: to confirm on the hardware or with the user

Hardware:

1. SDRAM: that the M12L64322A runs at 50 MHz under LiteDRAM with the
   expected sequential read rate (>= 150 MB/s usable), or at 100 MHz. The
   reference tick is 50 MHz; the host adapts to whatever the status says.
2. Which pins carry the common CLK/LAT/OE/A..E and the 48 colour lines on
   V8.2 (pin map in progress), and whether the 74HC245 buffers pass 25 MHz
   cleanly over the ribbon cables: if not, `clk_div` 3 (16.7 MHz: 12
   planes do not fit 60 fps, see 4.3; 11 planes or 30 fps do) or 4.
3. Panel conventions: column 0 first or last; row address *a* = panel row
   *a* for R1 G1 B1 and *a* + 32 for R2 G2 B2; A..E straight binary; OE
   active low; LAT active high; clock edge. All fixable on the host if
   wrong, except the polarities (one-line gateware constants).
4. The minimum OE pulse the FM6124 renders linearly (the stock 99.8 ns
   suggests 100 ns; a 50 MHz tick gives exactly that for the LSB). Measure a
   grey ramp. If the LSB must be longer: 11 planes (220 ns LSB) or a
   100 MHz tick (110 ns).
5. Blanking values against ghosting (defaults 200 / 80 / 320 ns), tuned by
   eye; they are schedule fields, not gateware constants.
6. The receive FIFO size that BRAM allows after LiteEth and LiteDRAM, and
   the real packet rate the Windows USB adapter and the Pi deliver with
   plain UDP sockets (12,400 packets/s needed at 60 fps).
7. RGMII timing of the RTL8211F on this board at gigabit (litex-boards has
   a working target for the 5A-75B; the 25 MHz clock comes from the PHY).

User decisions:

8. The card's address 10.75.0.2/24 and port 5775 (built in).
9. Whether a dark card until the host connects is acceptable (section
   7.4): a built-in default schedule would only light garbage anyway, since
   no frame exists at power-up.
10. Whether 12 planes at 1920 Hz / 60 fps is the right default, or 11
    planes with a longer LSB and more duty (table in 4.3).

Gateware implementation notes (firmware/, 2026-11-06; see firmware/README.md):

11. Values the gateware fixed: `tick_hz` 50,000,000 (the one system clock, also
    LiteDRAM's); receive FIFO 16 KiB (4096 words, 11 full packets) plus a 16-entry
    message FIFO; `min_visit_ticks` 256 as in section 3 (a 192-word fetch takes about
    220 cycles with the writer streaming, in simulation on LiteDRAM's model).
12. The gateware checks exactly what 6.5 lists and nothing about timing. The reference
    model (`model.Card`) goes further: `Schedule.from_bytes` runs the host validator, so
    it also rejects tables whose visits are shorter than the shift, light or fetch need,
    or whose shortest OE is under `min_oe_ns` (100 ns, i.e. 5 ticks). Tests that compare
    the two therefore use tables that pass both; the card accepts any pass length.
13. The gateware accepts odd `cols` (records then start on a half word; the fetcher skips
    it), and `visit_ticks` 0 is treated as 1 tick rather than rejected.
14. Status `frame_ticks` is measured LAT(0,0) to LAT(0,0) in ticks, exactly rows x sum of
    visits when no underrun stretched the pass.
15. Confirmed on the V8.2 card (2026-11-06, SRAM loads over a Pico dirtyJTAG): the JTAG
    IDCODE, clk25 = P6, LED = T6, the J1 colour pins and the shared A..E / CLK / LAT / OE
    pins of section FPGA-BRINGUP 3.1, A = LSB of a binary row address, data set while CLK is
    low and sampled on its rising edge, LAT active high, OE active low, R1 G1 B1 = panel
    rows 0..31 and R2 G2 B2 = rows 32..63, colours uncrossed; the per-row sequence of 4.1
    (shift, OE off, address, LAT pulse, OE on, next shift overlapping the light) lit the
    expected picture, and the first shifted column (record column 0, 5.1) is the LEFT
    column of the panel seen from the front. J1..J6's colour pins all drive their panel
    as the table says (lane order 6c + s); J7 and J8 use the same table and have no panel
    to check them with. These are the gateware's defaults.
