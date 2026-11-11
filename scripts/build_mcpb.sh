#!/usr/bin/env bash
# Сборка Claude Desktop .mcpb из исходников проекта.
# Использование: scripts/build_mcpb.sh emias-mcp | smartmed-mcp
set -euo pipefail

PROJECT="${1:?usage: build_mcpb.sh <emias-mcp|smartmed-mcp>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/$PROJECT"
EXTENSION="$DIR/bank_extension"

[ -f "$DIR/pyproject.toml" ] || { echo "Нет проекта: $DIR" >&2; exit 1; }
[ -f "$EXTENSION/manifest.json" ] || { echo "Нет манифеста: $EXTENSION" >&2; exit 1; }

NAME="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$EXTENSION/manifest.json")"
VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$EXTENSION/manifest.json")"
OUTPUT="${2:-$DIR/dist/$NAME-$VERSION.mcpb}"

mkdir -p "$EXTENSION/src" "$(dirname "$OUTPUT")"

# Копируем только рабочие пакеты (каталоги с __init__.py), без тестов и сборок.
for pkg in "$DIR"/*/; do
    [ -f "$pkg/__init__.py" ] || continue
    name="$(basename "$pkg")"
    rm -rf "$EXTENSION/src/$name"
    mkdir -p "$EXTENSION/src/$name"
    rsync -a --exclude '__pycache__' --exclude '*.pyc' "$pkg/" "$EXTENSION/src/$name/"
done
cp "$DIR/run_server.py" "$EXTENSION/src/run_server.py"

npx --yes @anthropic-ai/mcpb validate "$EXTENSION/manifest.json"
npx --yes @anthropic-ai/mcpb pack "$EXTENSION" "$OUTPUT"

echo "$OUTPUT"
