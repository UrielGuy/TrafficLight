"""Drive the sign without the server: the bring-up tool.

    python -m trafficlight.tools.testpattern --detect
    python -m trafficlight.tools.testpattern --pattern wiring
    python -m trafficlight.tools.testpattern --pattern panels --seen 3,8d,2,1d,5,4

The card is found as the server finds it (by its announcements, or at the
address in the settings); --card pins an address. Mapping settings come from
data/settings.json when it exists (what the server uses); --raw ignores
them, and --order / --flip / --rotate180 / --color-order / --gamma override
them, so the right values can be found by trying them here and then entered
in the UI. "wiring" shows each connector's number and an arrow, past the
mapping; --seen takes what the panels showed (top to bottom: the number, "d"
if the arrow pointed down, "-" for a dark panel) and works out panel_order
and panel_flip, the rule of the web UI's wiring wizard. Stop the server
first: two programs sending to one card fight over it. See docs/HARDWARE.md.
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
from ..output.fpga import FpgaDriver, FpgaOptions
from ..output.wiring import Seen, from_seen

DETECT_SECONDS = 5.0


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


def parse_seen(text: str) -> list[Seen]:
    """ "3,8d,2,1d,5,-" -> [(3, False), (8, True), ..., None]."""
    out: list[Seen] = []
    for part in text.replace(" ", "").lower().split(","):
        if part in ("-", "0", ""):
            out.append(None)
            continue
        down = part.endswith("d")
        num = part[:-1] if down or part.endswith("u") else part
        if not num.isdigit():
            raise ValueError(f"--seen: {part!r} is not a connector number, optionally followed by u or d, or -")
        out.append((int(num), down))
    return out


def build_settings(a: argparse.Namespace) -> OutputSettings:
    base = None if a.raw else _saved_output()
    s = (base or OutputSettings()).model_dump()
    s["driver"] = "fpga"
    if a.card is not None:
        s["card_address"] = a.card
    if a.port is not None:
        s["card_port"] = a.port
    if a.seen:
        if a.order or a.flip or a.rotate180:
            raise ValueError("--seen sets the order, flips and rotation itself: leave out --order, --flip, --rotate180")
        s["panel_order"], s["panel_flip"] = from_seen(parse_seen(a.seen))
        s["rotate180"] = False
    if a.order:
        s["panel_order"] = _ints(a.order)
    if a.flip:
        s["panel_flip"] = [bool(v) for v in _ints(a.flip)]
    if a.rotate180:
        s["rotate180"] = True
    if a.color_order:
        s["color_order"] = a.color_order
    if a.gamma is not None:
        s["gamma"] = a.gamma
    if a.brightness is not None:
        s["brightness"] = a.brightness
    return OutputSettings.model_validate(s)


def _driver(a: argparse.Namespace, s: OutputSettings) -> FpgaDriver:
    options = FpgaOptions.parse(a.options) if a.options else FpgaOptions.from_env()
    drv = FpgaDriver(s, options)
    drv.set_frame_rate(a.fps if a.fps > 0 else 60)
    drv.open()
    return drv


def _problems(st: dict) -> str:
    return "".join(f"\n  {p['level'].upper()}: {p['title']}. {p['detail']}" + (f" {p['fix']}" if p["fix"] else "")
                   for p in st["problems"])


def cmd_detect(a: argparse.Namespace) -> int:
    drv = _driver(a, build_settings(a))
    try:
        end = time.monotonic() + (a.seconds if a.seconds > 0 else DETECT_SECONDS)
        while drv.status()["card"] is None and time.monotonic() < end:
            time.sleep(0.05)
        st = drv.status()
    finally:
        drv.close()
    card, found = st["card"], st["found"]
    if card is None:
        print("No card answered." + _problems(st))
        return 1
    if found:
        print(f"card {found['ip']}:{found['port']}  MAC {found['mac']}  flash id {found['uid']}")
    else:
        print(f"card {st['address']}")
    print(f"firmware {card['firmware']} (build {card['build']}), up {card['uptime_s']:.0f} s, "
          f"tick {card['tick_hz'] / 1e6:g} MHz" + _problems(st))
    for o in st["others"]:
        print(f"also heard: {o['ip']}  MAC {o['mac']}  firmware {o['fw']}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    card = patterns.make_card(a.pattern)
    if a.pattern == "black":
        frame = np.zeros((config.H, config.W, 3), np.uint8)
    elif card is None:
        frame = patterns.make(a.pattern)
        if frame is None:
            names = ", ".join(("black",) + patterns.NAMES + patterns.CARD_NAMES)
            print(f"Unknown pattern {a.pattern!r}; one of: {names}", file=sys.stderr)
            return 2
    s = build_settings(a)
    drv = _driver(a, s)
    print(f"order {s.panel_order} flip {[int(f) for f in s.panel_flip]} rotate180 {s.rotate180} "
          f"color_order {s.color_order} gamma {s.gamma} brightness {s.brightness}" +
          ("  (the wiring picture ignores order, flip and rotate180)" if card is not None else ""))
    period = 1.0 / a.fps if a.fps > 0 else 0.0
    start = time.perf_counter()
    next_frame = start
    next_print = start + 1.0
    render_ms = 0.0
    sent = 0
    try:
        while a.seconds <= 0 or time.perf_counter() - start < a.seconds:
            t = time.perf_counter()
            if card is not None:
                drv.send_card(card)
            else:
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
        drv.close()      # leaves the sign dark: the card would keep the last picture for ever
    print(f"done: {st['frames']} frames sent in {elapsed:.2f} s, {st['held']} unchanged ones skipped, "
          f"{st['dropped']} of {sent} offered dropped; packing {st['pack_ms']:.2f} ms, "
          f"sending {st['send_ms']:.2f} ms per frame" + _problems(st))
    return 0 if st["ok"] else 1


def _print_status(drv: FpgaDriver, render_ms: float) -> None:
    s = drv.status()
    card, sched = s["card"], s["schedule"]
    ctxt = f"card {s['address']} fw {card['firmware']}" if card else "no card answer yet"
    if sched:
        ctxt += f", {sched['planes']} bits at {sched['frame_hz']:g} Hz"
    print(f"{s['sent_fps']:5.1f} fps  pack {s['pack_ms']:5.2f} ms  send {s['send_ms']:5.2f} ms  "
          f"send() max {render_ms:.2f} ms  dropped {s['dropped']}  {ctxt}" +
          (f"  ERROR: {s['error']}" if s["error"] else "") + "".join(f"  WARNING: {w}" for w in s["warnings"]))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m trafficlight.tools.testpattern", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", help="the card's address (default: saved setting, else found automatically; "
                                   "\"\" = automatic)")
    ap.add_argument("--port", type=int, help=f"the card's UDP port (default: saved setting, else {OutputSettings().card_port})")
    ap.add_argument("--detect", action="store_true", help="find the card and print what it says about itself")
    ap.add_argument("--pattern", default="panels", help=f"black, {', '.join(patterns.NAMES + patterns.CARD_NAMES)}")
    ap.add_argument("--brightness", type=float, help="0..1 (default: saved setting, else 0.5)")
    ap.add_argument("--seconds", type=float, default=0,
                    help=f"stop after this long (default: until Ctrl+C; --detect: {DETECT_SECONDS:g})")
    ap.add_argument("--fps", type=float, default=60, help="frames per second offered; 0 = as fast as possible")
    ap.add_argument("--raw", action="store_true", help="ignore data/settings.json")
    ap.add_argument("--order", help="panel_order, e.g. 0,1,2,3,4,5")
    ap.add_argument("--flip", help="panel_flip as 0/1, e.g. 0,1,0,1,0,1")
    ap.add_argument("--rotate180", action="store_true")
    ap.add_argument("--seen", help="what --pattern wiring showed, top to bottom, e.g. 3,8d,2,1d,5,- "
                                   "(d = arrow down, - = dark): sets order and flips from it")
    ap.add_argument("--color-order", choices=["rgb", "rbg", "grb", "gbr", "brg", "bgr"])
    ap.add_argument("--gamma", type=float)
    ap.add_argument("--options", help="driver options as in TRAFFICLIGHT_FPGA, e.g. planes=11,burst=4 "
                                      "(default: that variable)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return cmd_detect(a) if a.detect else cmd_show(a)
    except OutputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
