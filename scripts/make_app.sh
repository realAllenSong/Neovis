#!/bin/bash
# Build Neovis.app — a tiny double-click launcher for the desktop GUI.
#
# It does NOT freeze Python; it boots the GUI from the project's own venv
# (falling back to `uv run`). That sidesteps the fragile parts of freezing
# (sherpa-onnx native libs, the Node-based Claude CLI) while still giving a
# no-terminal, double-click launch.
#
#   ./scripts/make_app.sh            # builds ./Neovis.app
#   open ./Neovis.app               # launches the GUI
#
# Move the .app anywhere (Dock, /Applications) — it remembers the repo path,
# and if you move the whole repo it falls back to its own location.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/Neovis.app"
MACOS="$APP/Contents/MacOS"
RES="$APP/Contents/Resources"

rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Neovis</string>
  <key>CFBundleDisplayName</key><string>Neovis</string>
  <key>CFBundleIdentifier</key><string>com.neovis.app</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleExecutable</key><string>Neovis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Neovis listens on your machine when you hold the push-to-talk key.</string>
</dict>
</plist>
PLIST

cat > "$MACOS/Neovis" <<LAUNCH
#!/bin/bash
# Launch the Neovis GUI from the project venv. Baked repo path first, with a
# relative fallback if the whole repo (and this .app inside it) was moved.
ROOT="$ROOT"
[ -d "\$ROOT/neovis" ] || ROOT="\$(cd "\$(dirname "\$0")/../../.." && pwd)"
mkdir -p "\$HOME/.neovis"
LOG="\$HOME/.neovis/app.log"
cd "\$ROOT" || exit 1
if [ -x "\$ROOT/.venv/bin/python" ]; then
  exec "\$ROOT/.venv/bin/python" -m neovis.ui.app >>"\$LOG" 2>&1
elif command -v uv >/dev/null 2>&1; then
  exec uv run --project "\$ROOT" neovis-app >>"\$LOG" 2>&1
else
  osascript -e 'display alert "Neovis" message "Python environment not found. In the project folder run: uv sync --extra app --extra slack --extra voice --extra desktop"'
  exit 1
fi
LAUNCH

chmod +x "$MACOS/Neovis"
echo "Built $APP"
