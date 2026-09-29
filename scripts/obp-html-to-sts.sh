#!/usr/bin/env bash
# Convert an OBP preview page you saved from your own browser into NISO STS
# XML, then index it.
#
# The ISO OBP is behind bot protection and its Terms of Use govern reuse of
# the content, so there is no automated fetching here by design. Open the
# standard's OBP page yourself, save the "Terms and definitions" preview as
# HTML, and pass that file to this script.
#
# obp-access is a Ruby gem requiring Ruby >= 3.1, so it runs in a container
# rather than against the system Ruby.
#
# Usage: scripts/obp-html-to-sts.sh <saved.html> <urn> <caption> [title]
#   e.g. scripts/obp-html-to-sts.sh iso8373.html \
#          iso:std:iso:8373:ed-3:v1:en "ISO 8373:2021" "Robotics - Vocabulary"
set -euo pipefail

if [ "$#" -lt 3 ]; then
  sed -n '2,16p' "$0"
  exit 64
fi

HTML="$(realpath "$1")"
URN="$2"
CAPTION="$3"
TITLE="${4:-$CAPTION}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${ISO_OBP_CACHE:-$HOME/.cache/iso-obp-mcp}"
GEM="$CACHE/obp-access"
BUNDLE="$CACHE/bundle"
OUT="$CACHE/sts"
mkdir -p "$BUNDLE" "$OUT"

if [ ! -d "$GEM/.git" ]; then
  echo "==> Cloning obp-access"
  git clone --depth 1 --quiet https://github.com/metanorma/obp-access.git "$GEM"
fi

SAFE="$(printf '%s' "$URN" | tr ':' '-')"
echo "==> Converting $(basename "$HTML") to STS XML"
docker run --rm \
  -v "$GEM":/gem -v "$BUNDLE":/bundle -v "$(dirname "$HTML")":/in:ro -v "$OUT":/out \
  -e BUNDLE_PATH=/bundle \
  -e URN="$URN" -e CAPTION="$CAPTION" -e TITLE="$TITLE" \
  -e SRC="$(basename "$HTML")" -e DEST="$SAFE.xml" \
  -w /gem ruby:3.3 bash -c '
    git config --global --add safe.directory /gem
    bundle install --quiet
    bundle exec ruby -e "
      require %q(obp-access)
      obp = Obp::Access.from_html(
        urn:     ENV[%q(URN)],
        html:    File.read(File.join(%q(/in), ENV[%q(SRC)])),
        caption: ENV[%q(CAPTION)],
        titles:  { %q(en) => ENV[%q(TITLE)] },
      )
      File.write(File.join(%q(/out), ENV[%q(DEST)]), obp.to_xml(pretty: true))
    "' 2>&1 | tail -5

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
echo "==> Indexing $CAPTION"
PYTHONPATH="$ROOT/src" "$PY" -m iso_obp_mcp.ingest sts "$OUT/$SAFE.xml" --slug "sts:$SAFE"
