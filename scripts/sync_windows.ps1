# ETH-Agent: lokalen OneDrive-Ordner "ETH" per SFTP auf den Server spiegeln (Variante B, docs/sync.md).
# Ablage: C:\Tools\eth-agent\sync_windows.ps1
# Erster Test ohne Änderungen:  powershell -ExecutionPolicy Bypass -File C:\Tools\eth-agent\sync_windows.ps1 -DryRun
# Echter Lauf:                  powershell -ExecutionPolicy Bypass -File C:\Tools\eth-agent\sync_windows.ps1
param([switch]$DryRun)
$ErrorActionPreference = "Stop"

$Server  = "gujerl@100.83.134.66"
$Key     = "$env:USERPROFILE\.ssh\eth_agent"
$Rclone  = "C:\Tools\rclone\rclone.exe"
$Source  = "$env:USERPROFILE\OneDrive - ETH Zurich\ETH"
$Dest    = "server:eth-agent/material"
$Trash   = "server:eth-agent/material-trash/" + (Get-Date -Format "yyyy-MM-dd")
$Log     = "$env:LOCALAPPDATA\eth-agent-sync.log"

$Excludes = @(
  ".*/**", "**/.venv/**", "**/venv/**", "**/node_modules/**", "**/__pycache__/**", "**/.git/**",
  "Code-Demo-*/**", "*.mp4", "*.mov", "*.mkv", "*.avi", "*.zip", "*.7z", "*.rar", "*.exe", "*.msi",
  "*.iso", "*.dmg", "~$*", "desktop.ini", "Thumbs.db"
)
$ExcludeArgs = $Excludes | ForEach-Object { "--exclude", $_ }

$RcloneArgs = @("sync", $Source, $Dest) + $ExcludeArgs + @(
  "--max-size", "200M", "--backup-dir", $Trash, "--transfers", "4", "--stats-one-line", "--stats", "30s"
)
if ($DryRun) {
  & $Rclone @Args --dry-run --log-level NOTICE
  Write-Host "`nTrockenlauf fertig – nichts wurde geändert."
  exit 0
}
& $Rclone @Args --log-file $Log --log-level NOTICE
if ($LASTEXITCODE -ne 0) { Write-Host "rclone-Fehler, siehe $Log"; exit $LASTEXITCODE }

# Index auf dem Server aktualisieren (nur neue/geänderte Dateien, läuft im Hintergrund)
ssh -i $Key -o BatchMode=yes $Server "systemctl --user start --no-block eth-agent-index"
Write-Host "Sync fertig, Indexierung auf dem Server gestartet."
