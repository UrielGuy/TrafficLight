"""The gateware tests import the host package (trafficlight.output.hub75) as the reference."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "firmware"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def pytest_configure(config):
    # migen's simulator inverts Python bools for 1-bit `~`; the result is what the gateware means.
    config.addinivalue_line("filterwarnings", "ignore:Bitwise inversion:DeprecationWarning")
