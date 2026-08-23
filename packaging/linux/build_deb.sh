#!/usr/bin/env bash
# Builds prism_<version>_amd64.deb from the PyInstaller onedir output.
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/PRISM"
PKG="$ROOT/dist/deb/prism_${VERSION}_amd64"

if [ ! -d "$DIST" ]; then
  echo "dist/PRISM not found - run 'pyinstaller prism.spec --clean --noconfirm' first." >&2
  exit 1
fi

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" \
         "$PKG/opt/prism" \
         "$PKG/usr/bin" \
         "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps"

cp -r "$DIST/." "$PKG/opt/prism/"
ln -sf /opt/prism/PRISM "$PKG/usr/bin/prism"
cp "$ROOT/packaging/linux/prism.desktop" "$PKG/usr/share/applications/prism.desktop"
if [ -f "$ROOT/app/resources/prism_logo.png" ]; then
  cp "$ROOT/app/resources/prism_logo.png" "$PKG/usr/share/icons/hicolor/256x256/apps/prism.png"
fi

cat > "$PKG/DEBIAN/control" <<EOF
Package: prism
Version: ${VERSION}
Section: science
Priority: optional
Architecture: amd64
Maintainer: PRISM <noreply@example.com>
Description: PRISM - behavioral consistency benchmark for local LLMs
 Benchmarks how a local model's output changes across semantically
 equivalent prompt formulations. Runs entirely on-device via Ollama.
EOF

dpkg-deb --build --root-owner-group "$PKG" "$ROOT/dist/prism_${VERSION}_amd64.deb"
echo "Built dist/prism_${VERSION}_amd64.deb"