#!/usr/bin/env bash
# Builds PRISM-<version>.dmg from dist/PRISM.app (produced by the BUNDLE()
# step in prism.spec).
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/dist/PRISM.app"
STAGE="$ROOT/dist/dmg_stage"
OUT="$ROOT/dist/PRISM-${VERSION}.dmg"

if [ ! -d "$APP" ]; then
  echo "dist/PRISM.app not found - run 'pyinstaller prism.spec --clean --noconfirm' first." >&2
  exit 1
fi

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "PRISM" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
echo "Built $OUT"

# Optional: codesign + notarize if credentials are available in CI.
# codesign --deep --force --options runtime --sign "Developer ID Application: <you>" "$APP"
# xcrun notarytool submit "$OUT" --keychain-profile "prism-notary" --wait
# xcrun stapler staple "$OUT"