#!/usr/bin/env bash
# Build the ISO term index from openly published Glossarist registers.
#
# These registers are the authoritative terminology databases maintained by
# their ISO committees and published openly on GitHub, so this script needs
# no credentials and does not touch the ISO OBP website.
#
# Usage: scripts/build-index.sh [cache-dir]
set -euo pipefail

CACHE="${1:-${ISO_OBP_CACHE:-$HOME/.cache/iso-obp-mcp}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Languages to index. The IEV carries 16, which multiplies both the ingest
# time and the index size; English only by default.
LANGUAGES="${ISO_OBP_LANGUAGES:-eng}"

# Registers to index: repository|slug.
#
# The IEV is ~144 MB to clone and several minutes to ingest, so set
# ISO_OBP_SKIP_IEV=1 to leave it out. It is included by default because it is
# by far the largest openly published standards vocabulary.
REGISTERS=(
  "https://github.com/geolexica/isotc211-glossary.git|isotc211"
  "https://github.com/geolexica/isotc204-glossary.git|isotc204"
)
if [ -z "${ISO_OBP_SKIP_IEV:-}" ]; then
  REGISTERS+=("https://github.com/glossarist/glossarist-data-iev.git|iev")
fi

mkdir -p "$CACHE"

for entry in "${REGISTERS[@]}"; do
  url="${entry%%|*}"
  slug="${entry##*|}"
  dir="$CACHE/$slug"

  if [ -d "$dir/.git" ]; then
    echo "==> Updating $slug"
    git -C "$dir" fetch --depth 1 origin HEAD --quiet
    git -C "$dir" reset --hard FETCH_HEAD --quiet
  else
    echo "==> Cloning $slug"
    git clone --depth 1 --quiet "$url" "$dir"
  fi

  echo "==> Indexing $slug"
  PYTHONPATH="$ROOT/src" "$PY" -m iso_obp_mcp.ingest glossarist "$dir" \
    --slug "$slug" --languages "$LANGUAGES"
done

echo
PYTHONPATH="$ROOT/src" "$PY" -m iso_obp_mcp.ingest info
