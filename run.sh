#!/usr/bin/env bash
# TableSense launcher (macOS/Linux).  PORT=8077 ./run.sh | ./run.sh test | ./run.sh demo
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
  .venv/bin/pip install -r requirements.txt
fi
case "${1:-}" in
  test) exec .venv/bin/python -m pytest -q ;;
  demo) exec .venv/bin/python tools/build_static.py ;;
esac
exec .venv/bin/python -m backend
