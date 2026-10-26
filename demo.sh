#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec backend/.venv/bin/python backend/scripts/recording_demo.py "${1:-reset}"
