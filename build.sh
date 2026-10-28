#!/bin/zsh
# Construit « Atelier NX.app » dans dist/ ; ./build.sh install la copie aussi dans /Applications.
set -e
cd "$(dirname "$0")"
APP="dist/Atelier NX.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -o "$APP/Contents/MacOS/AtelierNX" App/main.swift -framework Cocoa -framework WebKit
cp server.py index.html "$APP/Contents/Resources/"
[[ -f App/AppIcon.icns ]] && cp App/AppIcon.icns "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Atelier NX</string>
  <key>CFBundleDisplayName</key><string>Atelier NX</string>
  <key>CFBundleIdentifier</key><string>me.vgiom.ateliernx</string>
  <key>CFBundleExecutable</key><string>AtelierNX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.1</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
EOF
codesign --force --sign - "$APP"
echo "OK : $APP"
if [[ "$1" == "install" ]]; then
  rm -rf "/Applications/Atelier NX.app"
  cp -R "$APP" /Applications/
  echo "Installé dans /Applications"
fi
