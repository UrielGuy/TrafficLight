"""Drive the sign without the server: the bring-up tool.

    python -m trafficlight.tools.testpattern --list
    python -m trafficlight.tools.testpattern --iface ID --detect
    python -m trafficlight.tools.testpattern --iface ID --pattern panels [--brightness 0.3] [--seconds N]

Mapping settings come from data/settings.json when it exists (what the
server uses); --raw ignores them, and --order / --flip / --rotate180 /
--color-order / --gamma override them, so the right values can be found by
trying them here and then entered in the UI. See docs/HARDWARE.md.
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from .. import config
from ..model import OutputSettings, Settings
from ..output import patterns
from ..output.base import OutputError
from ..output.colorlight import ColorlightDriver, ColorlightOptions, detect, list_interfaces
from ..output.rawsock_base import open_raw


def _saved_output() -> OutputSettings | None:
    p = config.data_dir() / "settings.json"
    if not p.exists():
        return None
    try:
        return Settings.model_validate_json(p.read_text(encoding="utf-8")).output
    except ValueError as e:
        print(f"(ignoring {p}: {e})", file=sys.stderr)
        return None


def _ints(text: str) -> list[int]:
    return [int(x) for x in text.replace(" ", "").split(",") if x != ""]


def build_settings(a: argparse.Namespace) -> OutputSettings:
    base = None if a.raw else _saved_output()
    s = (base or OutputSettings()).model_copy(deep=True)
    s.driver = "colorlight"
    s.interface = a.iface
    if a.order:
        s.panel_order = _ints(a.order)
    if a.flip:
        s.panel_flip = [bool(v) for v in _ints(a.flip)]
    if a.rotate180:
        s.rotate180 = True
    if a.color_order:
        s.color_order = a.color_order
    if a.gamma is not None:
        s.gamma = a.gamma
    if a.brightness is not None:
        s.brightness = a.brightness
    return s


def cmd_list() -> int:
    ifs = list_interfaces()
    if not ifs:
        print("No Ethernet interfaces found.")
        return 1
    for i in ifs:
        print(f"{'up  ' if i.up else 'down'}  {i.name:<20} {i.mac:<17}  {i.description}")
        print(f"      --iface \"{i.id}\"")
    return 0


def cmd_detect(iface: str) -> int:
    sock = open_raw(iface)
    try:
        up, mbps = sock.link()
        print(f"link: {'up' if up else 'down' if up is False else 'unknown'}" + (f", {mbps} Mb/s" if mbps else ""))
        found = detect(sock)
    finally:
        sock.close()
    if not found:
        print("No receiver card answered.")
        return 1
    for r in found:
        print(f"receiver {r.index}: firmware {r.firmware}, configured {r.width}x{r.height}, "
              f"up {r.uptime_s:.0f} s, card byte 0x{r.card:02x}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    if a.pattern == "black":
        frame = np.zeros((config.H, config.W, 3), np.uint8)
    else:
        frame = patterns.make(a.pattern)
        if frame is None:
            print(f"Unknown pattern {a.pattern!r}; one of: black, {', '.join(patterns.NAMES)}", file=sys.stderr)
            return 2
    s = build_settings(a)
    drv = ColorlightDriver(s, options=ColorlightOptions.parse(a.options))
    drv.open()
    print(f"order {s.panel_order} flip {[int(f) for f in s.panel_flip]} rotate180 {s.rotate180} "
          f"color_order {s.color_order} gamma {s.gamma} brightness {s.brightness}")
    period = 1.0 / a.fps if a.fps > 0 else 0.0
    start = time.perf_counter()
    next_frame = start
    next_print = start + 1.0
    render_ms = 0.0
    sent = 0
    try:
        while a.seconds <= 0 or time.perf_counter() - start < a.seconds:
            t = time.perf_counter()
            drv.send(frame)
            render_ms = max(render_ms, (time.perf_counter() - t) * 1000)
            sent += 1
            if t >= next_print:
                next_print += 1.0
                _print_status(drv, render_ms)
                render_ms = 0.0
            if period:
                next_frame += period
                d = next_frame - time.perf_counter()
                if d > 0:
                    time.sleep(d)
                else:
                    next_frame = time.perf_counter()
            else:
                time.sleep(0)
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.perf_counter() - start
        st = drv.status()
        if not a.keep:
            drv.send(np.zeros((config.H, config.W, 3), np.uint8))
            time.sleep(0.1)
        drv.close()
    frames = st["frames"]
    per_frame = st["packets_per_frame"]
    print(f"done: {frames} frames in {elapsed:.2f} s = {frames / elapsed:.1f} fps, "
          f"{frames * per_frame / elapsed:.0f} packets/s, {st['dropped']} of {sent} offered frames dropped, "
          f"sender {st['send_ms']:.2f} ms/frame")
    return 0 if st["ok"] else 1


def _print_status(drv: ColorlightDriver, render_ms: float) -> None:
    s = drv.status()
    rec = s["receiver"]
    rtxt = f"card fw {rec['firmware']} {rec['width']}x{rec['height']}" if rec else "no card answer yet"
    link = "up" if s["link"] else "DOWN" if s["link"] is False else "?"
    print(f"{s['sent_fps']:5.1f} fps  sender {s['send_ms']:5.2f} ms  send() max {render_ms:.2f} ms  "
          f"dropped {s['dropped']}  link {link}{' ' + str(s['link_mbps']) + ' Mb/s' if s['link_mbps'] else ''}  "
          f"{rtxt}" + (f"  ERROR: {s['error']}" if s["error"] else "") +
          "".join(f"  WARNING: {w}" for w in s["warnings"]))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m trafficlight.tools.testpattern", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list network interfaces and their ids")
    ap.add_argument("--iface", help="interface id (from --list); on Windows the adapter name works too")
    ap.add_argument("--detect", action="store_true", help="ask the receiver card for its firmware and size")
    ap.add_argument("--pattern", default="panels", help=f"black, {', '.join(patterns.NAMES)}")
    ap.add_argument("--brightness", type=float, help="0..1 (default: saved setting, else 0.5)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (default: until Ctrl+C)")
    ap.add_argument("--fps", type=float, default=60, help="frames per second offered; 0 = as fast as possible")
    ap.add_argument("--keep", action="store_true", help="leave the pattern lit on exit (default: blank)")
    ap.add_argument("--raw", action="store_true", help="ignore data/settings.json")
    ap.add_argument("--order", help="panel_order, e.g. 0,1,2,3,4,5")
    ap.add_argument("--flip", help="panel_flip as 0/1, e.g. 0,1,0,1,0,1")
    ap.add_argument("--rotate180", action="store_true")
    ap.add_argument("--color-order", choices=["rgb", "rbg", "grb", "gbr", "brg", "bgr"])
    ap.add_argument("--gamma", type=float)
    ap.add_argument("--options", help="protocol options, e.g. sync_gap_ms=5,sync_repeat=2 (see HARDWARE.md)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    a = ap.parse_args(argv)
    try:
        if a.list:
            return cmd_list()
        if not a.iface:
            ap.error("--iface is required (see --list)")
        if a.detect:
            return cmd_detect(a.iface)
        return cmd_show(a)
    except OutputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
