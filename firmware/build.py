#!/usr/bin/env python3
"""Build the 5A-75B V8.2 bitstreams (firmware/README.md).

  build.py main      the card gateware (docs/FIRMWARE.md); --pattern lights the built-in test
                     picture until the host uploads a schedule
  build.py blink     LED at 1 Hz from the 25 MHz clock
  build.py mdio      PHY id scan, result on the LED
  build.py memtest   SDRAM test, result on the LED

Run with the LiteX venv; the oss-cad-suite in --toolchain goes first on PATH. Outputs land in
<out>/<name>/gateware/<name>.bit and .svf (SRAM load: openFPGALoader -c dirtyJtag -m x.bit).
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tlcard import constants as C                      # noqa: E402
from tlcard.identity import ip_int, mac_for_ip         # noqa: E402
from tlcard.platform import Platform                   # noqa: E402

# The 2026-11-06 nightly crashes nextpnr on LiteX designs.
TOOLCHAIN = Path.home() / "tools" / "oss-cad-suite-20261001"


def build_id() -> int:
    """fw_build: today's date as 0xYYYYMMDD, informational."""
    return int(datetime.date.today().strftime("%Y%m%d"), 16)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=["main", "blink", "mdio", "memtest"])
    ap.add_argument("--out", default=str(Path.home() / "tools" / "tlcard-build"), help="build directory (outside the repo)")
    ap.add_argument("--name", default=None, help="build name (default: the target, plus -pattern)")
    ap.add_argument("--eth-phy", type=int, default=1, choices=[0, 1],
                    help="which RJ45 (PHY) carries the host; 1 on the sign's card (the gateware listens on one only)")
    ap.add_argument("--ip", default="auto",
                    help="'auto': 10.75.c.d from the flash's unique id at start; or a fixed IPv4 address")
    ap.add_argument("--mac", default=None,
                    help="with a fixed --ip: the MAC, 12 hex digits (default 02:5a:75:00:c:d from the IP's last two bytes)")
    ap.add_argument("--sys-clk-freq", type=float, default=C.DEFAULT_TICK_HZ, help="tick_hz: the system clock (also LiteDRAM's)")
    ap.add_argument("--sdram-cl", type=int, default=2, help="SDRAM CAS latency")
    ap.add_argument("--tx-delay", type=float, default=0.0,
                    help="RGMII TX clock delay in the FPGA (s); 0 on V8.2: the PHY adds its own (2 ns here: no frames)")
    ap.add_argument("--rx-delay", type=float, default=2e-9, help="RGMII RX clock delay in the FPGA (s)")
    ap.add_argument("--pattern", action="store_true", help="main: show the built-in test picture until a schedule arrives")
    ap.add_argument("--seed", type=int, default=1, help="nextpnr seed")
    ap.add_argument("--timingstrict", action="store_true", help="fail the build on a timing violation")
    ap.add_argument("--no-run", action="store_true", help="only write the Verilog and scripts")
    ap.add_argument("--toolchain", default=str(TOOLCHAIN), help="oss-cad-suite directory put first on PATH")
    args = ap.parse_args()

    if args.ip == "auto":
        if args.mac is not None:
            ap.error("--mac needs a fixed --ip")
        address = None
    else:
        ip = ip_int(args.ip)
        address = (ip, mac_for_ip(ip) if args.mac is None else int(args.mac.replace(":", ""), 16))
    toolchain_bin = Path(args.toolchain) / "bin"
    if toolchain_bin.is_dir():
        os.environ["PATH"] = str(toolchain_bin) + os.pathsep + os.environ.get("PATH", "")

    platform = Platform()
    name = args.name or (args.target + ("_pattern" if args.pattern and args.target == "main" else ""))    # a Verilog module name
    if args.target == "main":
        from tlcard.top import TLCard
        top = TLCard(platform, sys_clk_freq=args.sys_clk_freq, eth_phy=args.eth_phy, address=address,
                     pattern=args.pattern, fw_build=build_id(),
                     tx_delay=args.tx_delay, rx_delay=args.rx_delay, sdram_cl=args.sdram_cl)
    elif args.target == "blink":
        from tlcard.bringup import Blink
        top = Blink(platform)
    elif args.target == "mdio":
        from tlcard.bringup import MdioScan
        top = MdioScan(platform)
    else:
        from tlcard.bringup import MemTest
        top = MemTest(platform, sys_clk_freq=args.sys_clk_freq, cl=args.sdram_cl)

    build_dir = Path(args.out) / name / "gateware"
    platform.build(top, build_dir=str(build_dir), build_name=name, run=not args.no_run,
                   seed=args.seed, timingstrict=args.timingstrict)
    print(f"bitstream: {build_dir / (name + '.bit')}")


if __name__ == "__main__":
    main()
