"""Setup checks: what the sign still needs before it works, for the UI (State.checks, docs/ARCHITECTURE.md).

Built from the output and audio statuses the engine already has. The failures keep the texts of the code
that detects them (output/base.problem and OutputError in the output drivers, audio/base.device_problem and
backend_problem); this module puts them in a fixed order, says what is fine, leaves out what cannot be
known yet (no answer from the card: nothing to say about its firmware), and adds the checks no driver knows about
(the panel wiring, the web UI build). Cheap: no I/O beyond what the caller passes in.

A check: {"id", "level", "title", "detail", "fix"}. Levels: "error" (the sign or the sound cannot work),
"warning" (works, but something is off), "info" (a neutral note or a hint), "ok".
"""
from __future__ import annotations

from typing import Any, Optional

from . import config
from .model import OutputSettings, Settings
from .output import network
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
                      "To drive the sign, choose Custom firmware in Settings → LED output → Driver.")]
    found: dict[str, dict[str, Any]] = {}
    for p in st.get("problems") or []:
        found.setdefault(p["id"], p)
    out: list[dict[str, Any]] = []
    opened = bool(st.get("open"))
    if "output.driver" in found:
        out.append(found["output.driver"])
    elif opened:
        fps = st.get("sent_fps")
        if st.get("holding"):
            detail = "Showing a still picture: the card keeps it, nothing is sent until it changes."
        elif fps:
            detail = f"Sending {fps:.0f} frames per second."
        else:
            detail = "Open, waiting for frames."
        out.append(check("output.driver", "ok", "LED output: custom firmware", detail))
    elif not found:
        error = st.get("error")
        if error and error != "not started":
            out.append(check("output.driver", "error", "The LED output is not working", error))
        else:
            out.append(check("output.driver", "info", "Starting the LED output", "The driver is being opened."))
    if st.get("network") is not None:
        level, title, detail, fix = network.problem(st["network"], st.get("need_mbps"), st.get("frame_rate") or 0.0)
        out.append(check("output.network", level, title, detail, fix))
    # A send failure usually means the port has no address in the card's network: then no card answers
    # either, so it comes before the card.
    for cid in ("output.settings", "output.access", "output.send"):
        if cid in found:
            out.append(found[cid])
    if not opened:
        return out
    card, heard = st.get("card"), st.get("found")
    if "output.receiver" in found:
        out.append(found["output.receiver"])
        return out
    if card is None:
        if heard:
            out.append(check("output.receiver", "info", f"Found card {heard['ip']}",
                             f"MAC {heard['mac']}, firmware {heard['fw']}; asking it for its status."))
        elif o.card_address:
            out.append(check("output.receiver", "info", "Looking for the LED card",
                             f"Asking {st.get('address')}; a card with the custom firmware answers within a second."))
        else:
            out.append(check("output.receiver", "info", "Looking for the LED card",
                             "Listening for the cards' announcements and asking who is there; a card with the "
                             "custom firmware announces itself every second."))
        return out
    where = (f"Found card {heard['ip']} (MAC {heard['mac']}, firmware {card['firmware']})" if heard
             else f"Custom firmware {card['firmware']} at {st.get('address')}")
    pinned = " Its address is set in Settings → LED output." if o.card_address else ""
    out.append(check("output.receiver", "ok", "LED card found", f"{where}, up for {card['uptime_s']:.0f} s.{pinned}"))
    if "output.cards" in found:
        out.append(found["output.cards"])
    if "output.firmware" in found:
        out.append(found["output.firmware"])
        return out
    out.append(check("output.firmware", "ok", f"Firmware {card['firmware']} fits this sign",
                     f"{config.PORTS} connectors of {config.PANEL_W}x{config.PANEL_H} panels."))
    if "output.schedule" in found:
        out.append(found["output.schedule"])
    elif st.get("schedule"):
        sc = st["schedule"]
        out.append(check("output.schedule", "ok", f"Refresh schedule: {sc['planes']} bits at {sc['frame_hz']:g} Hz",
                         f"Bright pixels are refreshed {sc['subframe_hz']} times a second; the shortest light pulse is "
                         f"{sc['lsb_ns']} ns, the panels are lit {round(100 * sc['duty'])} % of the time at full white."))
    else:
        out.append(check("output.schedule", "info", "Waiting for the refresh schedule",
                         "It is built from what the card reports and sent to it."))
    for cid, ok_title, ok_detail in (("output.packets", "No packets lost", "Every frame has reached the card whole."),
                                     ("output.timing", "The card keeps its refresh", "No late fetches reported.")):
        out.append(found.get(cid) or check(cid, "ok", ok_title, ok_detail))
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
