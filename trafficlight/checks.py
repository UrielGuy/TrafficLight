"""Setup checks: what the sign still needs before it works, for the UI (State.checks, docs/ARCHITECTURE.md).

Built from the output and audio statuses the engine already has. The failures keep the texts of the code
that detects them (output/base.problem and OutputError in the output drivers, audio/base.device_problem and
backend_problem); this module puts them in a fixed order, says what is fine, leaves out what cannot be
known yet (no link: nothing to say about the receiver card), and adds the checks no driver knows about
(the panel wiring, the web UI build). Cheap: no I/O beyond what the caller passes in.

A check: {"id", "level", "title", "detail", "fix"}. Levels: "error" (the sign or the sound cannot work),
"warning" (works, but something is off), "info" (a neutral note or a hint), "ok".
"""
from __future__ import annotations

from typing import Any, Optional

from . import config
from .model import OutputSettings, Settings
from .output.wiring import to_seen

LEVELS = ("error", "warning", "info", "ok")
DEFAULT_WIRING = OutputSettings()


def check(id: str, level: str, title: str, detail: str, fix: Optional[str] = None) -> dict[str, Any]:
    return {"id": id, "level": level, "title": title, "detail": detail, "fix": fix}


def setup_checks(settings: Settings, output: dict[str, Any], audio: dict[str, Any], web_ui: bool = True) -> list[dict[str, Any]]:
    """output, audio: the engine's status dicts (State.output / State.audio, with their "problems")."""
    out = _output_checks(settings.output, output) + _audio_checks(audio)
    if not web_ui:
        out.append(check("server.web_ui", "warning", "The web UI is not built",
                         f"{config.WEB_DIST} is missing, so the server shows a placeholder page at /.",
                         "Run `npm install` and `npm run build` in web/ (needs Node.js), then reload the page."))
    return out


def _output_checks(o: OutputSettings, st: dict[str, Any]) -> list[dict[str, Any]]:
    if o.driver == "none":
        return [check("output.driver", "info", "LED output is off",
                      "The output driver is set to None: nothing is sent to the sign. The preview still works.",
                      "To drive the sign, choose Colorlight in Settings → LED output → Driver.")]
    found: dict[str, dict[str, Any]] = {}
    for p in st.get("problems") or []:
        found.setdefault(p["id"], p)
    out: list[dict[str, Any]] = []
    opened = bool(st.get("open"))

    if "output.driver" in found:
        out.append(found["output.driver"])
    elif opened:
        fps = st.get("sent_fps")
        out.append(check("output.driver", "ok", "LED output: Colorlight",
                         f"Sending {fps:.0f} frames per second." if fps else "Open, waiting for frames."))
    elif not found:
        error = st.get("error")
        if error and error != "not started":
            out.append(check("output.driver", "error", "The LED output is not working", error))
        else:
            out.append(check("output.driver", "info", "Starting the LED output", "The driver is being opened."))

    name = st.get("interface_name") or o.interface
    if "output.interface" in found:
        out.append(found["output.interface"])
    elif opened:
        out.append(check("output.interface", "ok", f"Network interface: {name}", "The port the card is plugged into."))
    if "output.access" in found:
        out.append(found["output.access"])
    elif opened:
        out.append(check("output.access", "ok", "Raw Ethernet access",
                         "The server may send raw Ethernet frames on this interface."))
    if "output.settings" in found:
        out.append(found["output.settings"])
    if not opened:
        return out

    up, mbps = st.get("link"), st.get("link_mbps")
    if "output.link" in found:
        out.append(found["output.link"])
    elif up:
        out.append(check("output.link", "ok", f"Link up{f', {mbps} Mb/s' if mbps else ''}", "The cable to the card is in."))
    else:
        out.append(check("output.link", "info", "Link state unknown", "The operating system does not report it."))
    if "output.send" in found:
        out.append(found["output.send"])
    if up is False or not st.get("detecting", True):
        return out

    rec = st.get("receiver")
    if "output.receiver" in found:
        out.append(found["output.receiver"])
        return out
    if rec is None:
        out.append(check("output.receiver", "info", "Looking for the receiver card",
                         "Detection packets are going out; a card on this port answers within a second or two."))
        return out
    out.append(check("output.receiver", "ok", "Receiver card found",
                     f"Colorlight card, firmware {rec.get('firmware')}."))
    if "output.card_size" in found:
        out.append(found["output.card_size"])
        return out
    out.append(check("output.card_size", "ok", f"Receiver card set up for {config.W}x{config.CARD_H}",
                     f"{config.PORTS} modules of {config.PANEL_W}x{config.PANEL_H}, J1..J{config.PORTS} from the top."))
    out.append(_wiring_check(o))
    return out


def _wiring_check(o: OutputSettings) -> dict[str, Any]:
    if (o.panel_order, o.panel_flip, o.rotate180) == (DEFAULT_WIRING.panel_order, DEFAULT_WIRING.panel_flip,
                                                      DEFAULT_WIRING.rotate180):
        return check("output.wiring", "info", "Panel wiring not set",
                     f"The settings still assume the panels are on J1..J{config.PANELS} from the top, all upright.",
                     "Run Settings → Panel wiring: show the connector numbers and enter what each panel shows. "
                     "If the panels really are wired that way, there is nothing to do.")
    seen = ", ".join(f"J{n}{' upside down' if down else ''}" for n, down in to_seen(o.panel_order, o.panel_flip, o.rotate180))
    return check("output.wiring", "ok", "Panel wiring set", f"From the top: {seen}.")


def _audio_checks(st: dict[str, Any]) -> list[dict[str, Any]]:
    problems = st.get("problems") or []
    if problems:
        return [problems[0]]
    if st.get("error"):
        return [check("audio.device", "error", "The sound output is not working", st["error"])]
    if st.get("device_name"):
        return [check("audio.device", "ok", f"Sound: {st['device_name']}",
                      f"Volume {round(100 * float(st.get('volume') or 0))} %.")]
    return [check("audio.device", "info", "Opening the sound device", "The sound output is starting.")]
