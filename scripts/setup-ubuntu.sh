#!/usr/bin/env bash
# Set up mutator with Lua 5.4 on Ubuntu (including Ubuntu under WSL2).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ../crapper ]; then
  echo "mutator imports ../crapper; clone it next to this repo first." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv build-essential \
  lua5.4 liblua5.4-dev luarocks

sudo luarocks --lua-version=5.4 install busted
sudo luarocks --lua-version=5.4 install luacov
sudo luarocks --lua-version=5.4 install cluacov 1.0.0-1
sudo luarocks --lua-version=5.4 install luacov-reporter-lcov

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

echo "Done. Run: .venv/bin/pytest"
