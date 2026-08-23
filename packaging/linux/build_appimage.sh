#!/usr/bin/env bash
# Builds PRISM-x86_64.AppImage from the PyInstaller onedir output at dist/PRISM.
# Run after: pyinstaller prism.spec --clean --noconfirm
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/PRISM"
APPDIR="$ROOT/dist/AppDir"

if [ ! -d "$DIST" ]; then
  echo "dist/PRISM not found - run 'pyinstaller prism.spec --clean --noconfirm' first." >&2
  exit 1
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r "$DIST/." "$APPDIR/usr/bin/"

cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/usr/bin/PRISM" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cp "$ROOT/packaging/linux/prism.desktop" "$APPDIR/prism.desktop"
if [ -f "$ROOT/app/resources/prism_logo.png" ]; then
  cp "$ROOT/app/resources/prism_logo.png" "$APPDIR/prism.png"
fi

if [ ! -f "$ROOT/appimagetool" ]; then
  curl -L -o "$ROOT/appimagetool" \
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$ROOT/appimagetool"
fi

cd "$ROOT"
ARCH=x86_64 ./appimagetool "$APPDIR" "dist/PRISM-x86_64.AppImage"
echo "Built dist/PRISM-x86_64.AppImage"