# Variante B: Windows-PC schiebt den lokalen OneDrive-Ordner per SFTP auf den Server.
# Voraussetzungen (einmalig, siehe docs/sync.md):
#   - rclone.exe liegt in C:\Tools\rclone\ (oder im PATH)
#   - rclone-Remote "server" (Typ sftp) mit SSH-Schlüssel ist eingerichtet
# Aufruf:  powershell -ExecutionPolicy Bypass -File sync_windows.ps1
$ErrorActionPreference = "Stop"

$Source  = "$env:USERPROFILE\OneDrive - ETH Zurich\ETH"
$Dest    = "server:eth-agent/material"
$Trash   = "server:eth-agent/material-trash/" + (Get-Date -Format "yyyy-MM-dd")
$Rclone  = if (Get-Command rclone -ErrorAction SilentlyContinue) { "rclone" } else { "C:\Tools\rclone\rclone.exe" }
$Exclude = Join-Path $PSScriptRoot "..\deploy\rclone-excludes.txt"
$Log     = "$env:LOCALAPPDATA\eth-agent-sync.log"

& $Rclone sync $Source $Dest `
  --exclude-from $Exclude `
  --max-size 200M `
  --backup-dir $Trash `
  --transfers 4 `
  --log-file $Log --log-level NOTICE

# Index auf dem Server aktualisieren (nur neue/geänderte Dateien)
ssh gujerl@100.83.134.66 "systemctl --user start eth-agent-index"
