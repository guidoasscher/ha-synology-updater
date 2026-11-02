# Home Assistant Synology NAS Updater (DSM 7.1)

🇬🇧 **English (Default)** | 🇳🇱 **[Nederlands hieronder](#nederlands-handleiding)**

A bilingual (**English** / **Dutch**) desktop application (**CustomTkinter GUI**) and unattended **Windows Task Scheduler CLI (`--auto`)** designed to keep `homeassistant/home-assistant:latest` up to date on a **Synology NAS running DSM 7.1 (Docker 20.10)** — where direct Docker Hub pulls or Zstandard (`zstd`) image layers often fail on the NAS itself.

---

## ✨ Key Features

1. **Bilingual Interface (`🇬🇧 EN` / `🇳🇱 NL`)**:
   - Defaults to **English** and includes a compact flag switcher in the top-right corner of the GUI to switch live between **English** and **Dutch** (persisted in `.env` via `APP_LANGUAGE`).
2. **Smart Digest & Image ID Verification**:
   - Compares the remote Docker Hub Manifest Digest and `linux/amd64` Image Config ID (`sha256:...`) against the image currently loaded on your Synology NAS.
   - Detects whether your **running Home Assistant container** is actually running the newest loaded NAS image.
3. **Automatic Zstandard (`zstd`) → Synology DSM 7.1 Tar Conversion**:
   - Modern Docker images use Zstandard-compressed layers that Synology DSM 7.1's older Docker 20.10 daemon cannot load (`archive/tar: invalid tar header`).
   - This tool pulls the image locally on Windows, automatically decompresses any `zstd` layers into a clean Docker v1.2 legacy `.tar` archive, and writes it atomically to your SMB share.
4. **SSH `docker load` & Automatic Container Recreation (with Rollback)**:
   - Connects to the NAS over SSH (`paramiko`), loads the `.tar` archive, removes the temporary file, and recreates the Home Assistant container while preserving all mounts/volumes (`/config`), `--net=host`, USB devices (Zigbee/Z-Wave/P1 sticks), capabilities, and environment variables.
   - Keeps a temporary backup container (`<name>_backup_old`) and automatically rolls back if the new container fails to start.
5. **Security Hardened**:
   - Strict shell argument escaping (`shlex.quote`) on all remote SSH commands.
   - Zip-Slip / Tar-Slip path traversal validation when inspecting and repacking image layers.
   - Optional strict SSH host key verification (`SSH_STRICT_HOST_KEY=true`) via `~/.ssh/known_hosts`.
   - Newline-sanitized `.env` writer with restricted file permissions (`0600`) and rotating logs (`5 MB × 3`).

---

## 🚀 Quick Start

1. **Requirements on Windows**:
   - Windows 10/11 with **Docker Desktop** running.
   - **Python 3.10+** (automatically detected or installed by `run_update.bat`).
   - Network access to your Synology NAS via **SMB** (e.g. `\\192.168.1.100\docker`) and **SSH**.
2. **Launch the GUI**:
   - Double-click [`run_update.bat`](run_update.bat).
   - On first run, it automatically creates `.venv`, installs dependencies from [`requirements.txt`](requirements.txt), copies [`.env.example`](.env.example) to `.env`, and opens the GUI.
3. **Configure `.env` & Synology Permissions**:
   - Click **⚙ Settings (.env)** in the GUI to enter your Synology IP, SSH port, username, SSH key path, and SMB path.
   - Click **🔐 Synology Permissions** for step-by-step instructions or use the **one-click auto-provisioning** button to install your public SSH key and configure passwordless Docker socket access on DSM 7.1.
4. **Automate with Windows Task Scheduler**:
   - **Program/script**: `C:\path\to\HAUpdate\run_update.bat`
   - **Add arguments**: `--auto`
   - **Start in**: `C:\path\to\HAUpdate`

### CLI Options
```powershell
python ha_updater.py           # Launch GUI (default)
python ha_updater.py --check   # Check Docker Hub vs. NAS status in console
python ha_updater.py --auto    # Unattended update (for Task Scheduler)
python ha_updater.py --auto --force  # Force pull & reload even if up to date
python ha_updater.py --lang nl # Override language ('en' or 'nl')
```

---
---

<a id="nederlands-handleiding"></a>

# 🇳🇱 Nederlands — Handleiding

Een tweetalige (**Engels standaard** / **Nederlands**) desktop-applicatie (**CustomTkinter GUI**) én **Windows Taakplanner CLI (`--auto`)** om je Synology NAS (DSM 7.1 / Docker 20.10) automatisch te voorzien van de nieuwste `homeassistant/home-assistant:latest` image en de draaiende container veilig bij te werken.

## Wat deze applicatie doet

1. **Tweetalige Interface (`🇬🇧 EN` / `🇳🇱 NL`)**:
   - Rechtsboven in het hoofdvenster kies je met één klik op het vlaggetje tussen **Engels (`EN`)** en **Nederlands (`NL`)**. Je keuze wordt direct opgeslagen in `.env` (`APP_LANGUAGE`).
2. **Digest & Image ID controle**:
   - Vergelijkt de actuele digest én Image Config ID (`sha256:...`) op Docker Hub met de geladen image op je Synology NAS en controleert of de draaiende container al op die nieuwste image draait.
3. **Lokale Docker Pull & Zstandard (`zstd`) conversie voor DSM 7.1**:
   - Pullt de image op Windows en pakt moderne `zstd`-gecomprimeerde lagen automatisch uit naar een Synology DSM 7.1-compatibel `.tar`-archief op je SMB-share.
4. **SSH `docker load` & Container Updaten (met automatische rollback)**:
   - Laadt de image in via SSH, ruimt het `.tar`-bestand op en zet de draaiende Home Assistant container over naar de nieuwe image met behoud van al je volumes, netwerkinstellingen, USB-sticks en omgevingsvariabelen.

## Starten & Taakplanner

- **Grafische Interface openen**: Dubbelklik op [`run_update.bat`](run_update.bat).
- **Automatisch uitvoeren via Windows Taakplanner**:
  - **Programma/script**: `C:\pad\naar\HAUpdate\run_update.bat`
  - **Parameters toevoegen**: `--auto`
  - **Beginnen in**: `C:\pad\naar\HAUpdate`
