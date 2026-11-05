#!/bin/bash
# usage: shoot.sh route width height name   (mock on port 8091; screenshots land in web/mock/shots)
EDGE="/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
OUT=$(wslpath -w "$(cd "$(dirname "$0")" && pwd)/shots/$4.png")
"$EDGE" --headless=new --disable-gpu --hide-scrollbars --screenshot="$OUT" --window-size=$2,$3 "http://localhost:8091$1" >/dev/null 2>&1
