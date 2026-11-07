#!/usr/bin/env bash
# Fill the pinned commit into the catalog entry: catalog/pin.sh <40-char sha>
# Run after pushing the commit to github.com/justin06lee/hyperactive, then re-run the validator.
set -euo pipefail
cd "$(dirname "$0")"
sha="${1:-}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: catalog/pin.sh <40-char commit sha>"; exit 1; }
sed -i "s/PINNED_SHA/$sha/g" hyperactive.yaml
grep -c "$sha" hyperactive.yaml | xargs echo "pinned in places:"
