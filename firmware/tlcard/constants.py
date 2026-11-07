"""The contract's numbers (docs/FIRMWARE.md sections 3, 5, 6 and 8), kept here so the gateware
builds without the host package; firmware/tests check them against trafficlight.output.hub75."""

FW_VERSION = (0, 1)
PROTO_VERSION = 1

# Section 3: reported in the status reply. tick_hz is the system clock and a build parameter.
DEFAULT_TICK_HZ = 50_000_000
MIN_VISIT_TICKS = 256
MAX_ENTRIES = 176
MAX_ROWS = 32
MAX_COLS = 256
MAX_PLANES = 16
PORTS = 8
BUFFERS = 3
LANES = PORTS * 6                # colour lines per shift clock
BYTES_PER_COLUMN = 6

# Section 5: SDRAM layout.
SDRAM_BYTES = 8 << 20
BUFFER_BYTES = MAX_PLANES * MAX_ROWS * MAX_COLS * BYTES_PER_COLUMN // 2    # 393,216
BUFFER_WORDS = BUFFER_BYTES // 4
MAX_CHUNKS = 512
CHUNK_ALIGN = 32
MAX_CHUNK_DATA = 1440

# Section 6: addressing and packets.
CARD_IP = "10.75.0.2"
CARD_MAC = 0x025A75000002
UDP_PORT = 5775
MAGIC_WORD = int.from_bytes(b"5A75", "little")
COMMON_BYTES = 16
STATUS_BYTES = 100
SCHEDULE_HEADER_BYTES = 32
ENTRY_BYTES = 8

TYPE_STATUS_REQ, TYPE_FRAME_DATA, TYPE_FRAME_COMMIT, TYPE_SCHEDULE, TYPE_STATUS = 0x01, 0x02, 0x03, 0x04, 0x81

# Section 8.
ERR_NONE, ERR_BAD_PACKET, ERR_SCHED_TICK, ERR_SCHED_LIMITS, ERR_SCHED_ENTRY = 0, 1, 2, 3, 4
ERR_CHUNK_RANGE, ERR_FRAME_INCOMPLETE, ERR_FRAME_SCHEDULE, ERR_STALE = 5, 6, 7, 8

# Status flags (section 6.6).
FLAG_HAVE_FRAME, FLAG_SCHED_ACTIVE, FLAG_SCHED_PENDING, FLAG_FRAME_PENDING, FLAG_BLANK = 1, 2, 4, 8, 16

# Section 5.2: the receive FIFO holds 11 full packets (the host sends bursts of 8).
RX_FIFO_WORDS = 4096
MSG_FIFO_DEPTH = 16

# The built-in self-test schedule of the pattern build: one plane, every row lit 40 us per pass.
PATTERN_TABLE = dict(rows=MAX_ROWS, planes=1, cols=128, clk_div=2, blank_before=10, lat_ticks=4,
                     blank_after=16, oe_ticks=2000, visit_ticks=2300)
