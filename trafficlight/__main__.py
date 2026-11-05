"""python -m trafficlight [--host 0.0.0.0] [--port 8080] [--data DIR]"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m trafficlight", description="TrafficLight show controller")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", type=Path, help="data directory (default: TRAFFICLIGHT_DATA or ./data)")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = ap.parse_args()
    if args.data is not None:
        # Set before anything reads config.data_dir().
        os.environ["TRAFFICLIGHT_DATA"] = str(args.data.resolve())
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level,
                ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
