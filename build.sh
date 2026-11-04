#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/core"
EXT=so; [ "$(uname)" = "Darwin" ] && EXT=dylib
g++ -O3 -std=c++17 -shared -fPIC -pthread -Wall core.cpp -o libshortcore.$EXT
echo "built core/libshortcore.$EXT"
