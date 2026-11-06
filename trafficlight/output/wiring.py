"""How the panels are wired, from what the "wiring" test picture shows on them.

The wiring picture is drawn straight into the card's image, without the
mapping: band b (connector J(b+1)) shows the number b+1 and an arrow to the
top of the band. A panel mounted upright shows its band's arrow pointing up;
a panel mounted rotated 180 degrees shows it pointing down (and its number
upside down). So, for the panel at position i of the sign (0 = top), seeing
connector n with the arrow up means ``panel_order[i] = n - 1``,
``panel_flip[i] = False``; with the arrow down, ``panel_flip[i] = True``.
The result is meant for ``rotate180 = False``: the observations already say
which panels are upside down.

The web UI applies the same rule (web/src/pages/wiring.ts); keep them in step.
"""
from __future__ import annotations

from typing import Optional, Sequence

from .. import config

# What one panel shows under the wiring picture: (connector 1..PORTS, arrow points down), or None for nothing.
Seen = Optional[tuple[int, bool]]


def order_problem(order: Sequence[int]) -> str | None:
    """Why `order` cannot be a panel_order, or None if it can."""
    if len(order) != config.PANELS:
        return f"needs {config.PANELS} entries (one per panel, top to bottom), got {len(order)}"
    for i, band in enumerate(order):
        if not 0 <= band < config.PORTS:
            return (f"panel {i + 1} is on band {band}, but the card has bands 0..{config.PORTS - 1} "
                    f"(connectors J1..J{config.PORTS})")
    for band in sorted(set(order)):
        on = [i + 1 for i, b in enumerate(order) if b == band]
        if len(on) > 1:
            return f"panels {_names(on)} are on the same connector J{band + 1}: each connector feeds one panel"
    return None


def flip_problem(flip: Sequence[bool]) -> str | None:
    if len(flip) != config.PANELS:
        return f"needs {config.PANELS} entries (one per panel, top to bottom), got {len(flip)}"
    return None


def _names(panels: list[int]) -> str:
    return ", ".join(map(str, panels[:-1])) + " and " + str(panels[-1])


def from_seen(seen: Sequence[Seen]) -> tuple[list[int], list[bool]]:
    """panel_order and panel_flip (for rotate180 off) from what each panel shows, top to bottom.

    A panel that shows nothing gets the lowest connector nobody else uses, unflipped, so the
    others can be saved; it shows whatever that band carries once it works. ValueError for a
    wrong count, a connector out of range or one connector named twice.
    """
    if len(seen) != config.PANELS:
        raise ValueError(f"need what {config.PANELS} panels show, got {len(seen)}")
    for i, s in enumerate(seen):
        if s is not None and not 1 <= s[0] <= config.PORTS:
            raise ValueError(f"panel {i + 1}: connector {s[0]} does not exist (J1..J{config.PORTS})")
    named = [s[0] - 1 for s in seen if s is not None]
    for band in sorted(set(named)):
        on = [i + 1 for i, s in enumerate(seen) if s is not None and s[0] - 1 == band]
        if len(on) > 1:
            raise ValueError(f"panels {_names(on)} show the same number {band + 1}: each connector feeds one panel")
    free = iter(b for b in range(config.PORTS) if b not in named)
    order = [s[0] - 1 if s is not None else next(free) for s in seen]
    flip = [bool(s[1]) if s is not None else False for s in seen]
    return order, flip


def to_seen(order: Sequence[int], flip: Sequence[bool], rotate180: bool = False) -> list[tuple[int, bool]]:
    """What each panel shows under the wiring picture if these settings match the sign (the inverse of
    from_seen). rotate180 is folded in: a sign turned over is the reversed order with every panel flipped."""
    if rotate180:
        order, flip = list(order)[::-1], [not f for f in list(flip)[::-1]]
    return [(band + 1, bool(f)) for band, f in zip(order, flip)]
