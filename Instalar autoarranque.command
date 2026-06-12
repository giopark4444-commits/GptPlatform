#!/bin/bash
# Instala el Studio como servicio de macOS: arranca solo al iniciar sesión
# y se reinicia si se cae. Doble clic y listo.
PLIST="$HOME/Library/LaunchAgents/com.gio.studio.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.gio.studio</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$HOME/GptPlatform/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/studio.log</string>
  <key>StandardErrorPath</key><string>/tmp/studio.log</string>
</dict></plist>
XML
launchctl bootout "gui/$(id -u)/com.gio.studio" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
sleep 2
if curl -s -o /dev/null --max-time 3 http://localhost:7860/keystatus; then
  echo "✓ Studio instalado como servicio: arranca solo al iniciar sesión y se reinicia si se cae."
  echo "  Para desinstalar: launchctl bootout gui/\$(id -u)/com.gio.studio && rm $PLIST"
else
  echo "✗ Algo falló; revisa /tmp/studio.log"
fi
