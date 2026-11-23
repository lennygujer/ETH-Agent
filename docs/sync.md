# OneDrive → Server

Der Agent liest das Material aus `~/eth-agent/material/` auf dem Server. Dorthin wird der ganze
OneDrive-Ordner `ETH/` gespiegelt, mit der Struktur `<Semester>/<Fach>/…` (z. B. `HS26/Physik 2/Exams/…`).
Die Fächer erkennt der Agent daraus automatisch.

Nicht kopiert werden: versteckte Ordner, `.venv`, `node_modules`, `.git`, `Code-Demo-*`, Videos, Archive
(`deploy/rclone-excludes.txt`) und Dateien über 200 MB. Was auf dem Server gelöscht oder überschrieben
wird, landet 30 Tage in `~/eth-agent/material-trash/`.

> **Achtung:** Die Spiegelung macht `material/` zu einer genauen Kopie von `ETH/`. Der bisher von Hand
> hochgeladene Ordner `material/Physik 2` und `Physik 2.zip` werden dabei in den Papierkorb verschoben
> und durch `material/HS26/Physik 2` ersetzt. Der Index bleibt gültig (gleiche Dateien, gleiche Kurs-ID).

## Variante A – Server holt direkt aus OneDrive (von der ETH blockiert)

Vorteil: läuft alle 3 Stunden automatisch, egal ob Mac oder PC an sind.

1. rclone auf dem Server installieren:
   ```bash
   sudo -v && curl https://rclone.org/install.sh | sudo bash
   ```
2. Auf dem **Windows-PC** rclone herunterladen (https://rclone.org/downloads/, ZIP entpacken nach `C:\Tools\rclone\`)
   und in PowerShell die Anmeldung testen:
   ```powershell
   C:\Tools\rclone\rclone.exe authorize "onedrive"
   ```
   Der Browser öffnet sich; mit dem ETH-Konto anmelden.
   - Erscheint **„Administratorgenehmigung erforderlich“** (Need admin approval), erlaubt die ETH das nicht → Variante B.
   - Sonst gibt PowerShell einen Token-Block `{"access_token": … }` aus. Den kompletten Block kopieren.
3. Auf dem Server das Remote anlegen:
   ```bash
   rclone config
   ```
   `n` (neues Remote) → Name `eth` → Typ `onedrive` → client_id/secret leer lassen → Region `global` →
   „Edit advanced config“ `n` → „Use web browser“ **`n`** → den Token-Block einfügen →
   Typ **`onedrive`** (OneDrive Personal or Business) → das angezeigte Laufwerk wählen → bestätigen.
4. Testen:
   ```bash
   rclone lsd eth:ETH          # sollte FS26, HS25, HS26, … zeigen
   ```
5. Automatisch synchronisieren (alle 3 h, danach Indexierung):
   ```bash
   cd ~/eth-agent/app
   cp deploy/eth-agent-sync.service deploy/eth-agent-sync.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now eth-agent-sync.timer
   systemctl --user start eth-agent-sync        # erster Lauf sofort
   journalctl --user -u eth-agent-sync -f       # Fortschritt
   ```

## Variante B – Windows-PC schiebt auf den Server (aktiv)

Die ETH erlaubt rclone keinen direkten OneDrive-Zugriff (Admin-Freigabe nötig, getestet 2026-11-23).
Darum spiegelt der Windows-PC seinen lokalen OneDrive-Ordner per SFTP. Läuft nur, wenn der PC an ist.

1. rclone nach `C:\Tools\rclone\rclone.exe` legen.
2. SSH-Schlüssel ohne Passwort erzeugen (bei der Passwortfrage zweimal Enter) und auf den Server kopieren:
   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\eth_agent
   type $env:USERPROFILE\.ssh\eth_agent.pub | ssh gujerl@100.83.134.66 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ssh -i $env:USERPROFILE\.ssh\eth_agent gujerl@100.83.134.66 "echo ok"
   ```
3. rclone-Ziel anlegen und testen:
   ```powershell
   C:\Tools\rclone\rclone.exe config create server sftp host 100.83.134.66 user gujerl key_file "$env:USERPROFILE\.ssh\eth_agent"
   C:\Tools\rclone\rclone.exe lsd server:eth-agent
   ```
4. `scripts/sync_windows.ps1` als `C:\Tools\eth-agent\sync_windows.ps1` speichern, zuerst als Trockenlauf
   (`-DryRun`), dann echt ausführen.
5. Aufgabenplanung: Trigger „Bei Anmeldung“ + „Täglich, alle 3 Stunden wiederholen“, Aktion `powershell.exe`,
   Argumente `-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Tools\eth-agent\sync_windows.ps1`.

Dateien, die in OneDrive nur online liegen, lädt Windows beim Kopieren herunter. Am einfachsten den Ordner
`ETH` auf „Immer auf diesem Gerät behalten“ stellen.

## Variante C – Mac

Wie B, aber mit `rsync` und launchd (folgt bei Bedarf).

## Nach dem ersten Sync

```bash
cd ~/eth-agent/app && source .venv/bin/activate
eth-agent index          # alle Fächer; beim ersten Mal mit Embeddings je nach Menge 1–3 Stunden
eth-agent files | head
```

Lange erste Indexierung besser im Hintergrund: `systemctl --user start eth-agent-index` und mit
`journalctl --user -u eth-agent-index -f` zuschauen. Danach läuft sie jede Nacht um 03:30 nur für neue Dateien.
