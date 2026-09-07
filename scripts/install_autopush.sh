#!/bin/bash
# Instala el agente que revisa cada 2 minutos si hay cambios de Claude para subir a GitHub.
PLIST="$HOME/Library/LaunchAgents/com.claude.mlsystem.autopush.plist"
chmod +x "$HOME/Desktop/claude/ml_system/scripts/claude_autopush.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claude.mlsystem.autopush</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$HOME/Desktop/claude/ml_system/scripts/claude_autopush.sh</string>
  </array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" && echo "Listo: auto-push instalado (revisa cada 2 minutos)."
