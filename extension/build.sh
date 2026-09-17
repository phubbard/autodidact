#!/usr/bin/env bash
# Builds dist/chromium and dist/firefox from src/. The only difference is the
# manifest: Firefox MV3 wants background.scripts instead of a service worker,
# and needs a gecko id to install a signed .xpi.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf dist
mkdir -p dist/chromium dist/firefox
cp -r src/. dist/chromium/
cp -r src/. dist/firefox/

jq '
  .background = { "scripts": ["background.js"] }
  | .browser_specific_settings = { "gecko": { "id": "autodidact@phfactor.net", "strict_min_version": "128.0" } }
' src/manifest.json > dist/firefox/manifest.json

(cd dist/chromium && zip -qr ../autodidact-chromium.zip .)
(cd dist/firefox && zip -qr ../autodidact-firefox.zip .)

echo "built:"
ls -1 dist/*.zip
