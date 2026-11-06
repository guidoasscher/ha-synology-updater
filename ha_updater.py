#!/usr/bin/env python3
r"""
Home Assistant Synology NAS (DSM 7.1) Image & Container Updater
---------------------------------------------------------------
Bilingual (English default / Dutch selectable) desktop application (CustomTkinter GUI)
and unattended CLI automation tool (--auto) for Windows Task Scheduler.

Features:
1. Compares the remote digest / Image Config ID of 'homeassistant/home-assistant:latest'
   on Docker Hub against the locally loaded image on a Synology NAS (DSM 7.1 / Docker 20.10).
2. Pulls the image locally on Windows via the Docker SDK with live progress reporting.
3. Automatically converts Zstandard (zstd) OCI layers to uncompressed tar layers so
   Synology DSM 7.1 (Docker 20.10) can load modern Docker images without errors.
4. Exports the image to an SMB share and runs 'docker load' + cleanup on the NAS over SSH.
5. Automatically recreates the running Home Assistant container on the NAS while preserving
   all volumes, network mode, USB/serial devices, capabilities, and environment variables,
   with automatic rollback on failure.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable, Optional

try:
    from compression import zstd as _stdlib_zstd  # Python 3.14+ (PEP 784)
except ImportError:
    _stdlib_zstd = None  # type: ignore[assignment]

try:
    import zstandard as _ext_zstd  # Python 3.10-3.13 fallback
except ImportError:
    _ext_zstd = None  # type: ignore[assignment]

import docker
from dotenv import load_dotenv
import paramiko
import requests

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger("ha_updater")

# Internal base-image environment prefixes that should not be copied from an older
# container into a newer image during recreation.
IMAGE_INTERNAL_ENV_PREFIXES = (
    "PATH=",
    "LANG=",
    "GPG_KEYS=",
    "PYTHON_",
    "S6_",
    "UV_",
    "WHEELS_LINKS=",
)

# Strict validation pattern for POSIX usernames (prevents shell/sudoers injection)
SAFE_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$")


# ==============================================================================
# Bilingual Translations (i18n: English default, Dutch selectable)
# ==============================================================================
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # Window & Header
        "window_title": "Home Assistant Synology NAS Updater (DSM 7.1)",
        "header_title": "🏠 Home Assistant Synology NAS Updater",
        "subtitle_fmt": "NAS: {user}@{ip}:{port}  •  SMB: {smb}  •  Container: {container}",
        "badge_checking": "⏳ CHECKING...",
        "badge_container_update": "⚠ CONTAINER UPDATE NEEDED",
        "badge_up_to_date": "✔ FULLY UP TO DATE",
        "badge_update_avail": "⬆ NEW IMAGE AVAILABLE",
        "badge_check_req": "✖ CHECK REQUIRED",
        "btn_help": "🔐 Synology Permissions",
        "btn_settings": "⚙ Settings (.env)",
        # Cards
        "card_conn_title": "🔌 Connections",
        "card_remote_title": "☁ Docker Hub (Remote)",
        "card_nas_title": "💾 Synology NAS Image",
        "card_container_title": "📦 HA Container (NAS)",
        "card_checking": "Checking...",
        "conn_docker_ok": "✔ Active",
        "conn_docker_err": "✖ Inactive",
        "conn_smb_ok": "✔ Reachable",
        "conn_smb_err": "✖ Unreachable",
        "conn_ssh_ok": "✔ Connected",
        "conn_ssh_err": "✖ Error",
        "conn_sudo_on": "On",
        "conn_sudo_off": "Off",
        "conn_body_fmt": (
            "Windows Docker: {docker}\n"
            "SMB Share: {smb}\n"
            "Synology SSH: {ssh}\n"
            "Sudo mode: {sudo}"
        ),
        "remote_body_fmt": (
            "Image: {image}\n"
            "Platform: {platform}\n"
            "Digest: {digest}...\n"
            "Image ID: {image_id}..."
        ),
        "remote_err": "Could not fetch remote digest from Docker Hub.",
        "nas_digest_none": "None (loaded via tar)",
        "nas_body_fmt": (
            "Status: Present on NAS\n"
            "Image ID: {image_id}...\n"
            "RepoDigest: {digest}\n"
            "Built: {created}"
        ),
        "nas_not_loaded": "No image loaded on the Synology NAS yet.",
        "nas_unavail_fmt": "Unavailable ({err})",
        "nas_ssh_err": "SSH error",
        "cont_sync_ok": "✔ Running latest NAS image",
        "cont_sync_old": "⚠ Still running OLD image!",
        "cont_body_fmt": (
            "Name: {name}\n"
            "Status: {status}\n"
            "Active ID: {image_id}\n"
            "{sync}"
        ),
        "cont_not_found_fmt": "Container '{name}' not found on the NAS.",
        "cont_no_conn": "No connection to NAS.",
        # Action Buttons & Progress
        "btn_check": "🔍 Refresh Status",
        "btn_update": "🚀 Update (If New)",
        "btn_force": "⚡ Force Image Update",
        "btn_recreate": "🔄 Update Container",
        "btn_restart": "⏯ Restart HA",
        "btn_prune": "🧹 Clean Old Images",
        "prog_ready": "Ready.",
        "log_header_fmt": "📜 Live Progress & Log ({name})",
        "btn_clear_log": "Clear log",
        # Dialogs & Confirmations
        "recreate_confirm_title": "Update & Restart Container",
        "recreate_confirm_msg": (
            "Do you want to recreate the running container '{cname}' on the Synology NAS "
            "using the latest loaded '{image}' image?\n\n"
            "All existing settings (volumes, network, USB devices, and environment variables) "
            "will be preserved, and a backup container is kept for automatic rollback."
        ),
        "prune_confirm_title": "Clean Up Old Images",
        "prune_confirm_msg": (
            "Do you want to remove unused/old (<none>) Docker images on the Synology NAS "
            "(`docker image prune -f`) to free up disk space?"
        ),
        # Settings Window
        "settings_title": "⚙ Settings (.env Configuration)",
        "settings_frame": "Configuration Variables (.env)",
        "btn_browse": "Browse...",
        "btn_choose_path": "Choose path...",
        "lbl_nas_ip": "NAS IP Address (NAS_IP):",
        "lbl_ssh_port": "SSH Port (SSH_PORT):",
        "lbl_ssh_user": "SSH Username (SSH_USER):",
        "lbl_ssh_key": "SSH Private Key (SSH_KEY_PATH):",
        "lbl_ssh_pw": "SSH / Sudo Password (optional):",
        "lbl_smb_path": "Windows SMB Path (SMB_PATH):",
        "lbl_nas_tar": "NAS Linux Tar Path (NAS_TAR_PATH):",
        "lbl_nas_docker": "NAS Docker Binary (NAS_DOCKER_BIN):",
        "lbl_image_name": "Docker Image (IMAGE_NAME):",
        "lbl_image_plat": "Target Platform (IMAGE_PLATFORM):",
        "lbl_cont_name": "HA Container Name (HA_CONTAINER_NAME):",
        "lbl_recreate_mode": "Container Update Mode (inspect / compose):",
        "lbl_compose_path": "NAS Compose Path (if compose):",
        "sw_use_sudo": "Use 'sudo' for Docker commands on the NAS (USE_SUDO)",
        "sw_auto_recreate": "Automatically update & restart running HA container after 'docker load' (AUTO_RECREATE_CONTAINER)",
        "sw_strict_host": "Strict SSH host key verification via ~/.ssh/known_hosts (SSH_STRICT_HOST_KEY)",
        "btn_cancel": "Cancel",
        "btn_save": "💾 Save & Refresh Status",
        "dlg_select_key": "Select SSH Private Key",
        "dlg_select_tar": "Select location for ha_latest.tar on SMB share",
        "filetype_tar": "Tar Archive",
        "filetype_all": "All files",
        "err_invalid_port_title": "Invalid Port",
        "err_invalid_port_msg": "SSH_PORT must be an integer between 1 and 65535.",
        # Permissions Help Window
        "perm_win_title": "🔐 Synology DSM 7.1 Permissions & SSH Setup",
        "perm_header": "Synology DSM 7.1 Preparation (SSH, Docker Socket & SMB)",
        "perm_pw_label": "One-time Synology password:",
        "perm_pw_placeholder": "Enter your Synology SSH/admin password...",
        "perm_auto_btn": "⚡ Auto-Configure SSH Key & Docker Permissions on NAS",
        "perm_auto_busy": "⏳ Configuring NAS...",
        "perm_close_btn": "Close",
        "perm_pw_req_title": "Password Required",
        "perm_pw_req_msg": "Please enter the password for your Synology SSH user first.",
        "perm_done_title": "Synology Setup Completed",
        "perm_fail_title": "Setup Failed",
        # Validation & Status Messages
        "val_no_ip": "NAS_IP is missing in .env",
        "val_no_user": "SSH_USER is missing in .env",
        "val_bad_user": "SSH_USER contains invalid characters",
        "val_no_auth": "Neither SSH_KEY_PATH nor SSH_PASSWORD is set in .env",
        "val_no_keyfile": "SSH_KEY_PATH file not found: {path}",
        "val_no_smb": "SMB_PATH is missing in .env",
        "val_no_tar": "NAS_TAR_PATH is missing in .env",
        "err_docker_perm": (
            "Permission denied accessing Docker on the NAS ({err}). "
            "Open '🔐 Synology Permissions' or set 'USE_SUDO=true' in Settings."
        ),
        "err_inspect_nas": "Failed to inspect NAS image: {err}",
        "reason_not_on_nas": "Image is not yet present on the Synology NAS",
        "reason_digest_match": "RepoDigest matches ({digest}...)",
        "reason_id_match": "Image ID matches ({image_id}...)",
        "reason_digest_diff": "Remote digest ({rem}...) differs from NAS ({nas}...)",
        "reason_id_diff": "Remote Image ID ({rem}...) differs from NAS ({nas}...)",
        "reason_cont_old_suffix": "{reason} — Note: running container is still using the old image!",
        "err_smb_dir": "Directory not reachable: {path}",
        "err_fetch_remote": "Failed to fetch remote digest: {err}",
        "err_ssh_nas": "SSH/NAS error: {err}",
        # Workflow & Logs
        "log_cfg_reloaded": "Configuration (.env) reloaded.",
        "prog_checking_all": "Fetching status from Docker Hub, SMB, and Synology NAS...",
        "log_status_check": "Status check: {reason}",
        "prog_last_check": "Last checked: {ts}",
        "log_pull_start": "Starting pull of '{image}' ({platform}) via local Docker SDK...",
        "prog_pulling": "Pulling {image}...",
        "err_docker_pull": "Docker pull error: {err}",
        "prog_pull_mb": "Docker Pull ({status}): {curr:.1f} / {tot:.1f} MB",
        "prog_pull_status": "Docker Pull: {status} {layer}",
        "log_pull_done": "Image pulled locally on Windows (ID: {image_id}...)",
        "prog_pull_done": "Docker Pull completed",
        "prog_layer_conv": "Unpacking layer {idx}/{tot} (zstd → Synology tar) & writing to SMB...",
        "log_zstd_unpacked": "Automatically unpacked {count} Zstandard (zstd) image layers for Synology DSM 7.1 (Docker 20.10) compatibility.",
        "err_smb_missing": "SMB target folder does not exist or is unreachable: {path}",
        "log_export_start": "Exporting image and preparing for Synology DSM 7.1 to SMB path: {path}",
        "prog_export_raw": "Exporting local Docker image: {mb:.0f} MB...",
        "log_export_done": "Synology-compatible tar archive saved ({mb:.1f} MB) to {path}",
        "prog_export_done": "Tar archive saved to SMB",
        "log_ssh_conn": "Connecting to Synology NAS ({user}@{ip}:{port}) via SSH...",
        "prog_ssh_conn": "Connecting to Synology NAS via SSH...",
        "log_nas_exec_load": "Running on NAS: '{cmd}' (this may take several minutes)...",
        "prog_nas_load": "NAS: executing {cmd}...",
        "err_nas_load": "Error running '{cmd}' (exit code {code}): {err}",
        "log_nas_load_ok": "Docker load completed on NAS: {out}",
        "prog_nas_rm": "NAS: Removing temporary tar archive...",
        "log_nas_exec_rm": "Running on NAS: '{cmd}'...",
        "err_nas_rm": "Failed to remove tar archive '{cmd}' (exit code {code}): {err}",
        "log_nas_rm_ok": "Temporary archive '{path}' removed from the NAS.",
        "log_no_cont_cfg": "No HA_CONTAINER_NAME configured in .env; skipping container recreation.",
        "log_compose_update": "Updating container via docker-compose ({path})...",
        "err_compose": "docker-compose up -d failed (exit {code}): {err}",
        "log_compose_ok": "Home Assistant container updated via docker-compose: {out}",
        "log_inspect_cont": "Reading current configuration of container '{name}' on the NAS...",
        "err_cont_not_found": "Could not find container '{name}' on the NAS: {err}",
        "err_cont_empty": "Empty inspect output for container '{name}'.",
        "log_stop_cont": "Stopping container '{name}' on the NAS...",
        "prog_stop_cont": "Stopping container '{name}'...",
        "err_stop_cont": "Failed to stop container '{name}': {err}",
        "log_rename_backup": "Temporarily renaming existing container to '{backup}' (safe backup)...",
        "err_rename_backup": "Failed to rename container to backup: {err}",
        "log_start_new": "Creating and starting new container '{name}' with latest image...",
        "prog_start_new": "Starting new container '{name}'...",
        "log_rollback_start": "Failed to create new container ({err}). Rolling back to previous container...",
        "log_rollback_done": "Rollback completed: original container '{name}' is running again.",
        "log_recreate_ok": "Container '{name}' is now running on the latest Home Assistant image! (Container ID: {cid})",
        "prog_recreate_ok": "Container '{name}' updated and started!",
        "err_recreate_exc": "Error updating/restarting container '{name}': {err}",
        "err_no_cont_env": "No HA_CONTAINER_NAME set in .env.",
        "log_restart_start": "Restarting container '{name}' on Synology NAS...",
        "prog_restart": "Restarting container '{name}'...",
        "err_restart_code": "Restart failed (exit {code}): {err}",
        "log_restart_ok": "Container '{name}' restarted on the NAS.",
        "prog_restart_ok": "Container '{name}' restart completed",
        "err_restart_exc": "Error restarting container: {err}",
        "log_prune_start": "Cleaning up unused (dangling/<none>) Docker images on the Synology NAS...",
        "prog_prune": "Cleaning up old images on NAS...",
        "err_prune_code": "Image cleanup failed (exit {code}): {err}",
        "prune_none_found": "No unused images found.",
        "log_prune_ok": "Cleanup completed on NAS: {summary}",
        "prog_prune_ok": "Cleanup completed: {summary}",
        "err_prune_exc": "Error cleaning up old images: {err}",
        "err_pub_not_found": (
            "Public key not found at: {path}\n"
            "Please verify SSH_KEY_PATH in Settings."
        ),
        "err_pub_empty": "Public key file {path} is empty.",
        "log_prov_start": "Starting one-time setup on Synology NAS ({user}@{ip}:{port})...",
        "err_prov_auth_keys": "Error configuring ~/.ssh/authorized_keys: {err}",
        "log_prov_key_ok": "SSH public key added to ~/.ssh/authorized_keys (with chmod 755/700/600).",
        "log_prov_sudo_warn": "Note while configuring sudo/docker group: {err}",
        "msg_prov_partial": (
            "SSH key was installed on the Synology NAS!\n"
            "However, configuring Docker permissions returned a notice: {err}"
        ),
        "log_prov_all_ok": "Docker socket permissions (/var/run/docker.sock) and passwordless access configured on the NAS!",
        "msg_prov_all_ok": (
            "Everything has been automatically configured on your Synology NAS:\n"
            "✔ Public SSH key added to ~/.ssh/authorized_keys\n"
            "✔ Directory permissions (chmod 755 ~, 700 ~/.ssh, 600 authorized_keys) set\n"
            "✔ Docker socket (/var/run/docker.sock) and sudoers NOPASSWD configured!"
        ),
        "err_prov_exc": "Error during automatic NAS setup: {err}",
        "msg_prov_exc": "Connection or setup failed: {err}",
        "prog_wf_check": "Checking for updates...",
        "log_wf_start": "Started check for new Home Assistant image...",
        "log_wf_no_ssh": "Cannot establish SSH connection to Synology NAS: {err}",
        "prog_wf_no_ssh": "Error: no SSH connection",
        "log_wf_no_rem": "Cannot fetch remote digest: {reason}",
        "prog_wf_no_rem": "Error: remote digest unknown",
        "log_wf_uptodate": "Image on Synology NAS is already up to date ({reason}).",
        "log_wf_cont_catchup": "Latest image was already on the NAS, but the container was still running the old image. Updating container now...",
        "prog_wf_uptodate": "System is fully up to date",
        "log_wf_force": "Force mode active: pulling and loading image despite matching digest.",
        "log_wf_new_ver": "New version detected! {reason}",
        "log_wf_no_docker": "Local Docker Engine on Windows is not running or unreachable: {err}",
        "prog_wf_no_docker": "Error: Windows Docker not running",
        "log_wf_no_smb": "SMB path is unreachable: {err}",
        "prog_wf_no_smb": "Error: SMB path unreachable",
        "log_wf_auto_recreate": "AUTO_RECREATE_CONTAINER is enabled: updating container '{name}' to the new image...",
        "log_wf_failed": "Update failed: {exc}",
        "prog_wf_failed": "Update aborted due to an error",
        "log_wf_done": "Full update process completed in {elapsed:.1f} seconds!",
        "prog_wf_done": "Update completed!",
        "guide_text": r"""1. ENABLE SSH & CONFIGURE SSH KEY ON SYNOLOGY (DSM 7.1)
---------------------------------------------------------------------------
• In DSM, open: Control Panel -> Terminal & SNMP -> check 'Enable SSH service'.
• Make sure User Home service is enabled:
  Control Panel -> User & Group -> Advanced -> check 'Enable user home service'.
• Generate an Ed25519 SSH key on Windows (in PowerShell) if you do not have one yet:
    ssh-keygen -t ed25519
• Copy your public key to the Synology and set the exact directory permissions
  (Synology DSM rejects SSH keys if your home folder has group write permissions!):
    chmod 755 ~
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys


2. RUN DOCKER WITHOUT SUDO / PASSWORD ON DSM 7.1 (RECOMMENDED!)
---------------------------------------------------------------------------
On Synology DSM 7.1, '/var/run/docker.sock' is owned by 'root' by default.
You can solve this in two ways so this tool never needs to ask for a sudo password:

METHOD A (Recommended & Safest): 'docker' group with DSM Boot-up Task
1. Log in once via SSH to your NAS and create the 'docker' group with your user:
     sudo synogroup --add docker YOUR_SSH_USER
2. Grant permissions on the current Docker socket immediately:
     sudo chown root:docker /var/run/docker.sock
     sudo chmod 660 /var/run/docker.sock
3. To keep these permissions across Synology reboots:
   - In DSM, go to: Control Panel -> Task Scheduler
   - Click: Create -> Triggered Task -> User-defined script
   - Task name: Docker Socket Permissions
   - User: root
   - Event: Boot-up
   - Task Settings tab -> Run command:
       chown root:docker /var/run/docker.sock && chmod 660 /var/run/docker.sock
   - Then set 'USE_SUDO' to OFF (false) in this application.

METHOD B (Alternative via sudoers NOPASSWD):
Prefer using 'USE_SUDO=true' without a password? Create a restricted sudoers file via SSH:
     sudo sh -c 'echo "YOUR_SSH_USER ALL=(ALL) NOPASSWD: /usr/local/bin/docker, /bin/rm" > /etc/sudoers.d/ha_updater'
     sudo chmod 440 /etc/sudoers.d/ha_updater


3. SMB SHARED FOLDER PERMISSIONS FOR '\\nas\docker'
---------------------------------------------------------------------------
• In DSM, go to: Control Panel -> Shared Folder -> select 'docker' -> Edit -> Permissions.
• Grant your Windows/NAS user 'Read/Write' permissions on the 'docker' shared folder.
• Verify in Windows Explorer that you can open '\\YOUR_NAS_IP\docker'.
""",
    },
    "nl": {
        # Window & Header
        "window_title": "Home Assistant Synology NAS Updater (DSM 7.1)",
        "header_title": "🏠 Home Assistant Synology NAS Updater",
        "subtitle_fmt": "NAS: {user}@{ip}:{port}  •  SMB: {smb}  •  Container: {container}",
        "badge_checking": "⏳ CONTROLEREN...",
        "badge_container_update": "⚠ CONTAINER UPDATE VEREIST",
        "badge_up_to_date": "✔ VOLLEDIG UP-TO-DATE",
        "badge_update_avail": "⬆ NIEUWE IMAGE BESCHIKBAAR",
        "badge_check_req": "✖ CONTROLE VEREIST",
        "btn_help": "🔐 Synology Rechten Hulp",
        "btn_settings": "⚙ Instellingen (.env)",
        # Cards
        "card_conn_title": "🔌 Verbindingen",
        "card_remote_title": "☁ Docker Hub (Remote)",
        "card_nas_title": "💾 Synology NAS Image",
        "card_container_title": "📦 HA Container (NAS)",
        "card_checking": "Controleren...",
        "conn_docker_ok": "✔ Actief",
        "conn_docker_err": "✖ Niet actief",
        "conn_smb_ok": "✔ Bereikbaar",
        "conn_smb_err": "✖ Onbereikbaar",
        "conn_ssh_ok": "✔ Verbonden",
        "conn_ssh_err": "✖ Fout",
        "conn_sudo_on": "Aan",
        "conn_sudo_off": "Uit",
        "conn_body_fmt": (
            "Windows Docker: {docker}\n"
            "SMB Share: {smb}\n"
            "Synology SSH: {ssh}\n"
            "Sudo modus: {sudo}"
        ),
        "remote_body_fmt": (
            "Image: {image}\n"
            "Platform: {platform}\n"
            "Digest: {digest}...\n"
            "Image ID: {image_id}..."
        ),
        "remote_err": "Kon remote digest niet ophalen van Docker Hub.",
        "nas_digest_none": "Geen (geladen via tar)",
        "nas_body_fmt": (
            "Status: Aanwezig op NAS\n"
            "Image ID: {image_id}...\n"
            "RepoDigest: {digest}\n"
            "Gebouwd: {created}"
        ),
        "nas_not_loaded": "Nog geen image geladen op de Synology NAS.",
        "nas_unavail_fmt": "Niet beschikbaar ({err})",
        "nas_ssh_err": "SSH fout",
        "cont_sync_ok": "✔ Draait op nieuwste NAS image",
        "cont_sync_old": "⚠ Draait nog op OUDE image!",
        "cont_body_fmt": (
            "Naam: {name}\n"
            "Status: {status}\n"
            "Actieve ID: {image_id}\n"
            "{sync}"
        ),
        "cont_not_found_fmt": "Container '{name}' niet gevonden op de NAS.",
        "cont_no_conn": "Geen verbinding met NAS.",
        # Action Buttons & Progress
        "btn_check": "🔍 Status Vernieuwen",
        "btn_update": "🚀 Update (Indien Nieuw)",
        "btn_force": "⚡ Forceer Image Update",
        "btn_recreate": "🔄 Container Updaten",
        "btn_restart": "⏯ Herstart HA",
        "btn_prune": "🧹 Oude Images Opruimen",
        "prog_ready": "Gereed.",
        "log_header_fmt": "📜 Live Voortgang & Logboek ({name})",
        "btn_clear_log": "Log wissen",
        # Dialogs & Confirmations
        "recreate_confirm_title": "Container Updaten & Herstarten",
        "recreate_confirm_msg": (
            "Wil je de draaiende container '{cname}' op de Synology NAS nu "
            "omzetten naar de nieuwst geladen '{image}' image?\n\n"
            "Alle bestaande instellingen (volumes, netwerk, USB-sticks en omgevingsvariabelen) "
            "blijven behouden en er wordt automatisch een backup aangehouden tijdens het wisselen."
        ),
        "prune_confirm_title": "Oude Images Opruimen",
        "prune_confirm_msg": (
            "Wil je ongebruikte/oude (<none>) Docker images op de Synology NAS opruimen "
            "(`docker image prune -f`) om schijfruimte vrij te maken?"
        ),
        # Settings Window
        "settings_title": "⚙ Instellingen (.env Configuratie)",
        "settings_frame": "Configuratie Variabelen (.env)",
        "btn_browse": "Bladeren...",
        "btn_choose_path": "Pad kiezen...",
        "lbl_nas_ip": "NAS IP-adres (NAS_IP):",
        "lbl_ssh_port": "SSH Poort (SSH_PORT):",
        "lbl_ssh_user": "SSH Gebruiker (SSH_USER):",
        "lbl_ssh_key": "SSH Private Key (SSH_KEY_PATH):",
        "lbl_ssh_pw": "SSH / Sudo Wachtwoord (optioneel):",
        "lbl_smb_path": "Windows SMB-pad (SMB_PATH):",
        "lbl_nas_tar": "NAS Linux Tar-pad (NAS_TAR_PATH):",
        "lbl_nas_docker": "NAS Docker Binary (NAS_DOCKER_BIN):",
        "lbl_image_name": "Docker Image (IMAGE_NAME):",
        "lbl_image_plat": "Doelplatform (IMAGE_PLATFORM):",
        "lbl_cont_name": "HA Container Naam (HA_CONTAINER_NAME):",
        "lbl_recreate_mode": "Container Update Modus (inspect / compose):",
        "lbl_compose_path": "NAS Compose Pad (indien compose):",
        "sw_use_sudo": "Gebruik 'sudo' voor Docker op de NAS (USE_SUDO)",
        "sw_auto_recreate": "Draaiende HA container automatisch updaten & herstarten na 'docker load' (AUTO_RECREATE_CONTAINER)",
        "sw_strict_host": "Strikte SSH host-key verificatie via ~/.ssh/known_hosts (SSH_STRICT_HOST_KEY)",
        "btn_cancel": "Annuleren",
        "btn_save": "💾 Opslaan & Status Vernieuwen",
        "dlg_select_key": "Selecteer SSH Private Key",
        "dlg_select_tar": "Selecteer locatie voor ha_latest.tar op SMB-share",
        "filetype_tar": "Tar Archief",
        "filetype_all": "Alle bestanden",
        "err_invalid_port_title": "Ongeldige poort",
        "err_invalid_port_msg": "SSH_PORT moet een getal zijn tussen 1 en 65535.",
        # Permissions Help Window
        "perm_win_title": "🔐 Synology DSM 7.1 Rechten & SSH Inregelen",
        "perm_header": "Synology DSM 7.1 Voorbereiding (SSH, Docker Socket & SMB)",
        "perm_pw_label": "Eenmalig Synology wachtwoord:",
        "perm_pw_placeholder": "Typ je Synology SSH/admin wachtwoord...",
        "perm_auto_btn": "⚡ Automatisch SSH-Key & Docker Rechten op NAS zetten",
        "perm_auto_busy": "⏳ Bezig met inrichten...",
        "perm_close_btn": "Sluiten",
        "perm_pw_req_title": "Wachtwoord vereist",
        "perm_pw_req_msg": "Vul eerst eenmalig het wachtwoord van je Synology SSH-gebruiker in.",
        "perm_done_title": "Synology Inrichting Voltooid",
        "perm_fail_title": "Inrichting Mislukt",
        # Validation & Status Messages
        "val_no_ip": "NAS_IP ontbreekt in .env",
        "val_no_user": "SSH_USER ontbreekt in .env",
        "val_bad_user": "SSH_USER bevat ongeldige tekens",
        "val_no_auth": "Geen SSH_KEY_PATH of SSH_PASSWORD opgegeven in .env",
        "val_no_keyfile": "SSH_KEY_PATH bestand niet gevonden: {path}",
        "val_no_smb": "SMB_PATH ontbreekt in .env",
        "val_no_tar": "NAS_TAR_PATH ontbreekt in .env",
        "err_docker_perm": (
            "Geen toegang tot Docker op de NAS ({err}). "
            "Open '🔐 Synology Rechten Hulp' of zet 'USE_SUDO=true' in de instellingen."
        ),
        "err_inspect_nas": "Kon NAS image niet inspecteren: {err}",
        "reason_not_on_nas": "Image is nog niet aanwezig op de Synology NAS",
        "reason_digest_match": "RepoDigest komt overeen ({digest}...)",
        "reason_id_match": "Image ID komt overeen ({image_id}...)",
        "reason_digest_diff": "Remote digest ({rem}...) wijkt af van NAS ({nas}...)",
        "reason_id_diff": "Remote Image ID ({rem}...) wijkt af van NAS ({nas}...)",
        "reason_cont_old_suffix": "{reason} — Let op: draaiende container gebruikt nog de oude image!",
        "err_smb_dir": "Map niet bereikbaar: {path}",
        "err_fetch_remote": "Fout bij ophalen remote digest: {err}",
        "err_ssh_nas": "SSH/NAS fout: {err}",
        # Workflow & Logs
        "log_cfg_reloaded": "Configuratie (.env) herladen.",
        "prog_checking_all": "Status van Docker Hub, SMB en Synology NAS ophalen...",
        "log_status_check": "Statuscheck: {reason}",
        "prog_last_check": "Laatste controle: {ts}",
        "log_pull_start": "Start pull van '{image}' ({platform}) via lokale Docker SDK...",
        "prog_pulling": "Pulling {image}...",
        "err_docker_pull": "Docker pull fout: {err}",
        "prog_pull_mb": "Docker Pull ({status}): {curr:.1f} / {tot:.1f} MB",
        "prog_pull_status": "Docker Pull: {status} {layer}",
        "log_pull_done": "Image lokaal gepulled op Windows (ID: {image_id}...)",
        "prog_pull_done": "Docker Pull voltooid",
        "prog_layer_conv": "Layer {idx}/{tot} uitpakken (zstd → Synology tar) & naar SMB schrijven...",
        "log_zstd_unpacked": "{count} Zstandard (zstd) image-layers automatisch uitgepakt voor Synology DSM 7.1 (Docker 20.10) compatibiliteit.",
        "err_smb_missing": "SMB doelmap bestaat niet of is niet bereikbaar: {path}",
        "log_export_start": "Image exporteren en klaarmaken voor Synology DSM 7.1 naar SMB-pad: {path}",
        "prog_export_raw": "Lokale Docker image exporteren: {mb:.0f} MB...",
        "log_export_done": "Synology-compatibel tar-archief succesvol opgeslagen ({mb:.1f} MB) op {path}",
        "prog_export_done": "Tar-archief opgeslagen op SMB",
        "log_ssh_conn": "Verbinden met Synology NAS ({user}@{ip}:{port}) via SSH...",
        "prog_ssh_conn": "Verbinden met Synology NAS via SSH...",
        "log_nas_exec_load": "Uitvoeren op NAS: '{cmd}' (dit kan enkele minuten duren)...",
        "prog_nas_load": "NAS: {cmd} uitvoeren...",
        "err_nas_load": "Fout bij uitvoeren van '{cmd}' (exit code {code}): {err}",
        "log_nas_load_ok": "Docker load voltooid op NAS: {out}",
        "prog_nas_rm": "NAS: Tijdelijk tar-archief verwijderen...",
        "log_nas_exec_rm": "Uitvoeren op NAS: '{cmd}'...",
        "err_nas_rm": "Fout bij verwijderen tar-archief '{cmd}' (exit code {code}): {err}",
        "log_nas_rm_ok": "Tijdelijk archief '{path}' succesvol verwijderd van de NAS.",
        "log_no_cont_cfg": "Geen HA_CONTAINER_NAME geconfigureerd in .env; container recreatie overgeslagen.",
        "log_compose_update": "Container bijwerken via docker-compose ({path})...",
        "err_compose": "docker-compose up -d mislukt (exit {code}): {err}",
        "log_compose_ok": "Home Assistant container bijgewerkt via docker-compose: {out}",
        "log_inspect_cont": "Huidige configuratie van container '{name}' uitlezen op de NAS...",
        "err_cont_not_found": "Kon container '{name}' niet vinden op de NAS: {err}",
        "err_cont_empty": "Lege inspect-output voor container '{name}'.",
        "log_stop_cont": "Container '{name}' stoppen op de NAS...",
        "prog_stop_cont": "Container '{name}' stoppen...",
        "err_stop_cont": "Kon container '{name}' niet stoppen: {err}",
        "log_rename_backup": "Bestaande container tijdelijk hernoemen naar '{backup}' (veilige backup)...",
        "err_rename_backup": "Kon container niet hernoemen naar backup: {err}",
        "log_start_new": "Nieuwe container '{name}' aanmaken en starten met nieuwste image...",
        "prog_start_new": "Nieuwe container '{name}' starten...",
        "log_rollback_start": "Aanmaken nieuwe container mislukt ({err}). Automatische rollback naar vorige container...",
        "log_rollback_done": "Rollback voltooid: originele container '{name}' draait weer.",
        "log_recreate_ok": "Container '{name}' draait nu succesvol op de nieuwste Home Assistant image! (Container ID: {cid})",
        "prog_recreate_ok": "Container '{name}' succesvol bijgewerkt en gestart!",
        "err_recreate_exc": "Fout bij updaten/herstarten van container '{name}': {err}",
        "err_no_cont_env": "Geen HA_CONTAINER_NAME ingesteld in .env.",
        "log_restart_start": "Container '{name}' herstarten op Synology NAS...",
        "prog_restart": "Container '{name}' herstarten...",
        "err_restart_code": "Herstarten mislukt (exit {code}): {err}",
        "log_restart_ok": "Container '{name}' is succesvol herstart op de NAS.",
        "prog_restart_ok": "Container '{name}' herstart voltooid",
        "err_restart_exc": "Fout bij herstarten container: {err}",
        "log_prune_start": "Ongebruikte (dangling/<none>) Docker images opruimen op de Synology NAS...",
        "prog_prune": "Oude images opruimen op NAS...",
        "err_prune_code": "Opruimen van oude images mislukt (exit {code}): {err}",
        "prune_none_found": "Geen ongebruikte images gevonden.",
        "log_prune_ok": "Opruimen voltooid op NAS: {summary}",
        "prog_prune_ok": "Opruimen voltooid: {summary}",
        "err_prune_exc": "Fout bij opruimen oude images: {err}",
        "err_pub_not_found": (
            "Publieke sleutel niet gevonden op: {path}\n"
            "Controleer of SSH_KEY_PATH in Instellingen goed staat."
        ),
        "err_pub_empty": "Publieke sleutelbestand {path} is leeg.",
        "log_prov_start": "Start eenmalige inrichting op Synology NAS ({user}@{ip}:{port})...",
        "err_prov_auth_keys": "Fout bij instellen van ~/.ssh/authorized_keys: {err}",
        "log_prov_key_ok": "SSH publieke sleutel succesvol toegevoegd aan ~/.ssh/authorized_keys (met chmod 755/700/600).",
        "log_prov_sudo_warn": "Let op bij instellen sudo/docker-groep: {err}",
        "msg_prov_partial": (
            "SSH-key is succesvol geïnstalleerd op de Synology!\n"
            "Alleen het automatisch aanpassen van de Docker-rechten gaf een melding: {err}"
        ),
        "log_prov_all_ok": "Docker socket rechten (/var/run/docker.sock) en wachtwoordloze toegang succesvol ingesteld op de NAS!",
        "msg_prov_all_ok": (
            "Alles is automatisch ingericht op je Synology NAS:\n"
            "✔ Publieke SSH-sleutel toegevoegd aan ~/.ssh/authorized_keys\n"
            "✔ Maprechten (chmod 755 ~, 700 ~/.ssh, 600 authorized_keys) ingesteld\n"
            "✔ Docker socket (/var/run/docker.sock) en sudoers NOPASSWD geconfigureerd!"
        ),
        "err_prov_exc": "Fout bij automatisch inrichten NAS: {err}",
        "msg_prov_exc": "Verbinding of inrichting mislukt: {err}",
        "prog_wf_check": "Controleren op updates...",
        "log_wf_start": "Controle op nieuwe Home Assistant image gestart...",
        "log_wf_no_ssh": "Kan geen SSH-verbinding maken met de Synology NAS: {err}",
        "prog_wf_no_ssh": "Fout: geen SSH-verbinding",
        "log_wf_no_rem": "Kan remote digest niet ophalen: {reason}",
        "prog_wf_no_rem": "Fout: remote digest onbekend",
        "log_wf_uptodate": "Image op Synology NAS is al up-to-date ({reason}).",
        "log_wf_cont_catchup": "De nieuwste image stond al op de NAS, maar de container draaide nog op de oude image. Container wordt nu bijgewerkt...",
        "prog_wf_uptodate": "Systeem is volledig up-to-date",
        "log_wf_force": "Forceer-modus actief: image wordt opnieuw gepulled en ingeladen ondanks gelijke digest.",
        "log_wf_new_ver": "Nieuwe versie gedetecteerd! {reason}",
        "log_wf_no_docker": "Lokale Docker Engine op Windows draait niet of is niet bereikbaar: {err}",
        "prog_wf_no_docker": "Fout: Windows Docker draait niet",
        "log_wf_no_smb": "SMB-pad is niet bereikbaar: {err}",
        "prog_wf_no_smb": "Fout: SMB-pad niet bereikbaar",
        "log_wf_auto_recreate": "AUTO_RECREATE_CONTAINER staat aan: container '{name}' wordt nu bijgewerkt naar de nieuwe image...",
        "log_wf_failed": "Update mislukt: {exc}",
        "prog_wf_failed": "Update afgebroken vanwege een fout",
        "log_wf_done": "Volledig updateproces succesvol afgerond in {elapsed:.1f} seconden!",
        "prog_wf_done": "Update succesvol voltooid!",
        "guide_text": r"""1. SSH INSCHAKELEN & SSH-KEY INSTELLEN OP SYNOLOGY (DSM 7.1)
---------------------------------------------------------------------------
• Ga in DSM naar: Configuratiescherm -> Terminal & SNMP -> vink 'SSH-service inschakelen' aan.
• Zorg dat Gebruikers-home-service aan staat:
  Configuratiescherm -> Gebruiker en groep -> Geavanceerd -> vink 'Gebruikers-home-service inschakelen' aan.
• Genereer op Windows (in PowerShell) een SSH-sleutel als je die nog niet hebt:
    ssh-keygen -t ed25519
• Kopieer je publieke sleutel naar de Synology en stel de maprechten exact goed in
  (Synology DSM weigert SSH-keys als de home-map groepsschrijfrechten heeft!):
    chmod 755 ~
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys


2. DOCKER UITVOEREN ZONDER SUDO / WACHTWOORD OP DSM 7.1 (AANBEVOLEN!)
---------------------------------------------------------------------------
Op Synology DSM 7.1 is '/var/run/docker.sock' standaard alleen toegankelijk voor 'root'.
Je kunt dit op 2 manieren netjes oplossen zodat deze tool nooit om een sudo-wachtwoord hoeft te vragen:

METHODE A (Aanbevolen & Veiligst): 'docker' groep met DSM Opstarttaak
1. Log eenmalig in via SSH op je NAS en maak de 'docker' groep aan met jouw gebruiker erin:
     sudo synogroup --add docker JOUW_SSH_USER
2. Geef direct rechten op de huidige Docker socket:
     sudo chown root:docker /var/run/docker.sock
     sudo chmod 660 /var/run/docker.sock
3. Om te zorgen dat dit na een herstart van de Synology behouden blijft:
   - Ga in DSM naar: Configuratiescherm -> Taakplanner
   - Klik op: Maken -> Geactiveerde taak -> Door gebruiker gedefinieerd script
   - Taaknaam: Docker Socket Permissions
   - Gebruiker: root
   - Gebeurtenis: Opstarten (Boot-up)
   - Tabblad Taakinstellingen -> Opdracht uitvoeren:
       chown root:docker /var/run/docker.sock && chmod 660 /var/run/docker.sock
   - Zet in deze app vervolgens 'USE_SUDO' op UIT (false).

METHODE B (Alternatief via sudoers NOPASSWD):
Wil je liever 'USE_SUDO=true' gebruiken zonder wachtwoord? Maak dan via SSH op de NAS een sudoers-bestand aan:
     sudo sh -c 'echo "JOUW_SSH_USER ALL=(ALL) NOPASSWD: /usr/local/bin/docker, /bin/rm" > /etc/sudoers.d/ha_updater'
     sudo chmod 440 /etc/sudoers.d/ha_updater


3. SMB GEDEELDE MAP RECHTEN VOOR '\\nas\docker'
---------------------------------------------------------------------------
• Ga in DSM naar: Configuratiescherm -> Gedeelde map -> selecteer 'docker' -> Bewerken -> Machtigingen.
• Geef je Windows/NAS-gebruiker 'Lezen/Schrijven' rechten op de gedeelde map 'docker'.
• Controleer in Windows Verkenner of je '\\JOUW_NAS_IP\docker' kunt openen.
""",
    },
}


def tr(lang: str, key: str, **kwargs: Any) -> str:
    """Translate a key for the given language ('en' or 'nl') with fallback to English."""
    lang_code = lang if lang in TRANSLATIONS else "en"
    template = TRANSLATIONS[lang_code].get(key) or TRANSLATIONS["en"].get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template


def _sanitize_env_value(value: str) -> str:
    """Strip CR/LF characters to prevent .env newline injection."""
    return str(value).replace("\r", "").replace("\n", "")


def _validate_tar_member_name(name: str) -> str:
    """
    Validate a relative archive entry name inside a Docker image tar.
    Prevents Zip-Slip / Tar-Slip path traversal attacks.
    """
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [p for p in normalized.split("/") if p]
    if not parts or ".." in parts or any(":" in p for p in parts):
        raise ValueError(f"Unsafe tar archive member path rejected: {name!r}")
    return "/".join(parts)


def _open_zstd_reader(raw_stream: Any) -> Any:
    """
    Open a Zstandard decompression stream using either Python 3.14+ stdlib
    `compression.zstd` or the `zstandard` package on Python 3.10-3.13.
    """
    if _stdlib_zstd is not None:
        return _stdlib_zstd.open(raw_stream, "rb")
    if _ext_zstd is not None:
        dctx = _ext_zstd.ZstdDecompressor()
        return dctx.stream_reader(raw_stream)
    raise RuntimeError(
        "Zstandard decompression requires Python 3.14+ ('compression.zstd') "
        "or the 'zstandard' package ('pip install zstandard')."
    )


def _restrict_file_permissions(path: Path) -> None:
    """Restrict file permissions (owner read/write only where supported)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


# ==============================================================================
# Configuration & Data Structures
# ==============================================================================
@dataclass
class AppConfig:
    language: str = "en"
    nas_ip: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_path: str = ""
    ssh_password: str = ""
    use_sudo: bool = False
    ssh_strict_host_key: bool = False
    smb_path: str = r"\\192.168.1.100\docker\ha_latest.tar"
    nas_tar_path: str = "/volume1/docker/ha_latest.tar"
    nas_docker_bin: str = "/usr/local/bin/docker"
    image_name: str = "homeassistant/home-assistant:latest"
    image_platform: str = "linux/amd64"
    ha_container_name: str = "home-assistant"
    auto_recreate_container: bool = True
    recreate_mode: str = "inspect"
    nas_compose_path: str = "/volume1/docker/homeassistant/docker-compose.yml"
    log_file: str = "ha_update.log"
    env_file_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / ".env"
    )

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "AppConfig":
        target_env = env_path or (Path(__file__).resolve().parent / ".env")
        if target_env.exists():
            load_dotenv(dotenv_path=target_env, override=True)

        raw_port = os.getenv("SSH_PORT", "22").strip()
        try:
            port = int(raw_port)
            if not (1 <= port <= 65535):
                port = 22
        except ValueError:
            port = 22

        raw_lang = os.getenv("APP_LANGUAGE", "en").strip().lower()
        if raw_lang not in ("en", "nl"):
            raw_lang = "en"

        recreate_mode = os.getenv("RECREATE_MODE", "inspect").strip().lower()
        if recreate_mode not in ("inspect", "compose"):
            recreate_mode = "inspect"

        return cls(
            language=raw_lang,
            nas_ip=os.getenv("NAS_IP", "").strip(),
            ssh_port=port,
            ssh_user=os.getenv("SSH_USER", "").strip(),
            ssh_key_path=os.path.expandvars(
                os.path.expanduser(os.getenv("SSH_KEY_PATH", "").strip())
            ),
            ssh_password=os.getenv("SSH_PASSWORD", "").strip(),
            use_sudo=os.getenv("USE_SUDO", "false").strip().lower()
            in ("1", "true", "yes", "on"),
            ssh_strict_host_key=os.getenv("SSH_STRICT_HOST_KEY", "false").strip().lower()
            in ("1", "true", "yes", "on"),
            smb_path=os.getenv("SMB_PATH", r"\\192.168.1.100\docker\ha_latest.tar").strip(),
            nas_tar_path=os.getenv("NAS_TAR_PATH", "/volume1/docker/ha_latest.tar").strip(),
            nas_docker_bin=os.getenv("NAS_DOCKER_BIN", "/usr/local/bin/docker").strip(),
            image_name=os.getenv(
                "IMAGE_NAME", "homeassistant/home-assistant:latest"
            ).strip(),
            image_platform=os.getenv("IMAGE_PLATFORM", "linux/amd64").strip(),
            ha_container_name=os.getenv("HA_CONTAINER_NAME", "home-assistant").strip(),
            auto_recreate_container=os.getenv("AUTO_RECREATE_CONTAINER", "true")
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
            recreate_mode=recreate_mode,
            nas_compose_path=os.getenv(
                "NAS_COMPOSE_PATH", "/volume1/docker/homeassistant/docker-compose.yml"
            ).strip(),
            log_file=os.getenv("LOG_FILE", "ha_update.log").strip(),
            env_file_path=target_env,
        )

    def save_to_env(self) -> None:
        """Persist configuration safely to .env with newline sanitization and restricted permissions."""
        lang = self.language if self.language in ("en", "nl") else "en"
        lines = [
            "# ==============================================================================",
            "# Application Language (en = English [default], nl = Nederlands)",
            "# ==============================================================================",
            f"APP_LANGUAGE={_sanitize_env_value(lang)}",
            "",
            "# ==============================================================================",
            "# Synology NAS & SSH Configuration (DSM 7.1)",
            "# ==============================================================================",
            f"NAS_IP={_sanitize_env_value(self.nas_ip)}",
            f"SSH_PORT={int(self.ssh_port)}",
            f"SSH_USER={_sanitize_env_value(self.ssh_user)}",
            f"SSH_KEY_PATH={_sanitize_env_value(self.ssh_key_path)}",
            f"SSH_PASSWORD={_sanitize_env_value(self.ssh_password)}",
            f"USE_SUDO={'true' if self.use_sudo else 'false'}",
            f"SSH_STRICT_HOST_KEY={'true' if self.ssh_strict_host_key else 'false'}",
            "",
            "# ==============================================================================",
            "# Paths & Docker Configuration",
            "# ==============================================================================",
            f"SMB_PATH={_sanitize_env_value(self.smb_path)}",
            f"NAS_TAR_PATH={_sanitize_env_value(self.nas_tar_path)}",
            f"NAS_DOCKER_BIN={_sanitize_env_value(self.nas_docker_bin)}",
            "",
            "# ==============================================================================",
            "# Home Assistant Image & Container Settings",
            "# ==============================================================================",
            f"IMAGE_NAME={_sanitize_env_value(self.image_name)}",
            f"IMAGE_PLATFORM={_sanitize_env_value(self.image_platform)}",
            f"HA_CONTAINER_NAME={_sanitize_env_value(self.ha_container_name)}",
            f"AUTO_RECREATE_CONTAINER={'true' if self.auto_recreate_container else 'false'}",
            f"RECREATE_MODE={_sanitize_env_value(self.recreate_mode)}",
            f"NAS_COMPOSE_PATH={_sanitize_env_value(self.nas_compose_path)}",
            "",
            "# ==============================================================================",
            "# Logging",
            "# ==============================================================================",
            f"LOG_FILE={_sanitize_env_value(self.log_file)}",
            "",
        ]
        self.env_file_path.write_text("\n".join(lines), encoding="utf-8")
        _restrict_file_permissions(self.env_file_path)

    def save_language_preference(self, lang: str) -> None:
        """Update APP_LANGUAGE in .env in-place without losing other settings."""
        self.language = lang if lang in ("en", "nl") else "en"
        os.environ["APP_LANGUAGE"] = self.language
        if not self.env_file_path.exists():
            self.save_to_env()
            return
        try:
            content = self.env_file_path.read_text(encoding="utf-8")
            if re.search(r"(?m)^APP_LANGUAGE=", content):
                updated = re.sub(
                    r"(?m)^APP_LANGUAGE=.*$",
                    f"APP_LANGUAGE={self.language}",
                    content,
                )
            else:
                updated = f"APP_LANGUAGE={self.language}\n" + content
            self.env_file_path.write_text(updated, encoding="utf-8")
            _restrict_file_permissions(self.env_file_path)
        except Exception:
            self.save_to_env()

    def validate(self) -> list[str]:
        L = self.language
        errors: list[str] = []
        if not self.nas_ip:
            errors.append(tr(L, "val_no_ip"))
        if not self.ssh_user:
            errors.append(tr(L, "val_no_user"))
        elif not SAFE_USERNAME_RE.match(self.ssh_user):
            errors.append(tr(L, "val_bad_user"))
        if not self.ssh_key_path and not self.ssh_password:
            errors.append(tr(L, "val_no_auth"))
        if self.ssh_key_path and not Path(self.ssh_key_path).exists():
            errors.append(tr(L, "val_no_keyfile", path=self.ssh_key_path))
        if not self.smb_path:
            errors.append(tr(L, "val_no_smb"))
        if not self.nas_tar_path:
            errors.append(tr(L, "val_no_tar"))
        return errors


@dataclass
class UpdateStatus:
    checked_at: str = ""
    local_docker_ok: bool = False
    local_docker_error: str = ""
    smb_dir_ok: bool = False
    smb_dir_error: str = ""
    ssh_ok: bool = False
    ssh_error: str = ""
    remote_digest: str = ""
    remote_image_id: str = ""
    nas_image_exists: bool = False
    nas_repo_digests: list[str] = field(default_factory=list)
    nas_image_id: str = ""
    nas_created: str = ""
    nas_container_exists: bool = False
    nas_container_running: bool = False
    nas_container_status: str = ""
    nas_container_image_id: str = ""
    container_needs_recreate: bool = False
    needs_update: bool = False
    reason: str = ""


# ==============================================================================
# Core Logic: Docker Hub, Local Docker, SMB & Synology SSH
# ==============================================================================
class HAUpdater:
    def __init__(
        self,
        config: AppConfig,
        log_callback: Optional[Callable[[str, str], None]] = None,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        self.config = config
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self._cancel_requested = False

    def _tr(self, key: str, **kwargs: Any) -> str:
        return tr(self.config.language, key, **kwargs)

    def _log(self, level: str, message: str) -> None:
        log_fn = getattr(logger, level.lower(), logger.info)
        log_fn(message)
        if self.log_callback:
            self.log_callback(level.upper(), message)

    def _progress(self, fraction: float, status_text: str) -> None:
        if self.progress_callback:
            self.progress_callback(max(0.0, min(1.0, fraction)), status_text)

    @staticmethod
    def _parse_image_ref(image_ref: str) -> tuple[str, str]:
        if ":" in image_ref and "/" in image_ref.split(":")[-1]:
            return image_ref, "latest"
        if ":" in image_ref:
            repo, tag = image_ref.rsplit(":", 1)
            return repo, tag
        return image_ref, "latest"

    def fetch_remote_image_info(self) -> tuple[str, str]:
        """
        Fetch both the remote Manifest Digest and the architecture-specific
        Image Config ID (sha256:...) for linux/amd64 from Docker Hub using
        a single pooled HTTPS session.
        """
        repo, tag = self._parse_image_ref(self.config.image_name)
        if "/" not in repo:
            repo = f"library/{repo}"

        target_os, target_arch = "linux", "amd64"
        if "/" in self.config.image_platform:
            parts = self.config.image_platform.split("/", 1)
            target_os, target_arch = parts[0], parts[1]

        with requests.Session() as session:
            token_url = (
                f"https://auth.docker.io/token?service=registry.docker.io"
                f"&scope=repository:{repo}:pull"
            )
            resp = session.get(token_url, timeout=15)
            resp.raise_for_status()
            token = resp.json()["token"]

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.oci.image.manifest.v1+json, "
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            }
            manifest_url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
            m_resp = session.get(manifest_url, headers=headers, timeout=15)
            m_resp.raise_for_status()

            top_digest = m_resp.headers.get("Docker-Content-Digest", "").strip()
            data = m_resp.json()

            if "manifests" in data:
                arch_digest = ""
                for m in data.get("manifests", []):
                    plat = m.get("platform", {})
                    if (
                        plat.get("os") == target_os
                        and plat.get("architecture") == target_arch
                    ):
                        arch_digest = m.get("digest", "")
                        break

                if arch_digest:
                    sub_url = (
                        f"https://registry-1.docker.io/v2/{repo}/manifests/{arch_digest}"
                    )
                    sub_resp = session.get(sub_url, headers=headers, timeout=15)
                    sub_resp.raise_for_status()
                    sub_data = sub_resp.json()
                    config_digest = sub_data.get("config", {}).get("digest", "")
                    return top_digest or arch_digest, config_digest

            config_digest = data.get("config", {}).get("digest", "")
            return top_digest, config_digest

    def _create_ssh_client(self) -> paramiko.SSHClient:
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        if self.config.ssh_strict_host_key:
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": self.config.nas_ip,
            "port": self.config.ssh_port,
            "username": self.config.ssh_user,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
        }
        if self.config.ssh_key_path and Path(self.config.ssh_key_path).exists():
            connect_kwargs["key_filename"] = self.config.ssh_key_path
            if self.config.ssh_password:
                connect_kwargs["passphrase"] = self.config.ssh_password
        elif self.config.ssh_password:
            connect_kwargs["password"] = self.config.ssh_password

        ssh.connect(**connect_kwargs)
        return ssh

    def _run_ssh_command(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        use_sudo: Optional[bool] = None,
        timeout: int = 600,
    ) -> tuple[int, str, str]:
        sudo_active = self.config.use_sudo if use_sudo is None else use_sudo
        if sudo_active:
            full_cmd = (
                f"sudo -S -p '' {command}"
                if self.config.ssh_password
                else f"sudo {command}"
            )
        else:
            full_cmd = command

        stdin, stdout, stderr = ssh.exec_command(full_cmd, timeout=timeout)
        if sudo_active and self.config.ssh_password:
            try:
                stdin.write(self.config.ssh_password + "\n")
                stdin.flush()
            except Exception:
                pass
        try:
            stdin.close()
        except Exception:
            pass

        out_str = stdout.read().decode("utf-8", errors="replace").strip()
        err_str = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out_str, err_str

    def get_nas_image_info(self, ssh: paramiko.SSHClient) -> dict[str, Any]:
        """Inspect the image on the Synology NAS using safely quoted arguments."""
        docker_bin = shlex.quote(self.config.nas_docker_bin)
        image_name = shlex.quote(self.config.image_name)
        cmd = f"{docker_bin} image inspect {image_name}"
        code, out, err = self._run_ssh_command(ssh, cmd, timeout=30)
        if code != 0:
            err_lower = err.lower()
            if "no such image" in err_lower or "no such object" in err_lower:
                return {"exists": False}
            if "permission denied" in err_lower:
                raise RuntimeError(self._tr("err_docker_perm", err=err))
            raise RuntimeError(self._tr("err_inspect_nas", err=err or out))

        inspect_list = json.loads(out)
        if not inspect_list:
            return {"exists": False}

        img = inspect_list[0]
        return {
            "exists": True,
            "id": img.get("Id", ""),
            "repo_digests": img.get("RepoDigests") or [],
            "created": img.get("Created", ""),
        }

    def get_nas_container_info(self, ssh: paramiko.SSHClient) -> dict[str, Any]:
        """Inspect the running Home Assistant container on the Synology NAS."""
        cname = self.config.ha_container_name.strip()
        if not cname:
            return {"exists": False}

        docker_bin = shlex.quote(self.config.nas_docker_bin)
        cmd = f"{docker_bin} inspect {shlex.quote(cname)}"
        code, out, err = self._run_ssh_command(ssh, cmd, timeout=30)
        if code != 0:
            err_lower = err.lower()
            if (
                "no such object" in err_lower
                or "no such container" in err_lower
                or "error: no such" in err_lower
            ):
                return {"exists": False}
            return {"exists": False, "error": err or out}

        try:
            data = json.loads(out)
            if not data:
                return {"exists": False}
            c = data[0]
            state = c.get("State", {})
            return {
                "exists": True,
                "running": bool(state.get("Running", False)),
                "status": state.get("Status", "unknown"),
                "image_id": c.get("Image", ""),
                "name": c.get("Name", "").lstrip("/"),
                "inspect": c,
            }
        except Exception as exc:
            return {"exists": False, "error": str(exc)}

    @staticmethod
    def _evaluate_update_needed(
        remote_digest: str,
        remote_image_id: str,
        nas_info: dict[str, Any],
        lang: str = "en",
    ) -> tuple[bool, str]:
        if not nas_info.get("exists"):
            return True, tr(lang, "reason_not_on_nas")

        nas_digests: list[str] = nas_info.get("repo_digests") or []
        nas_image_id: str = nas_info.get("id") or ""

        if remote_digest:
            for rd in nas_digests:
                if rd.endswith(remote_digest) or remote_digest in rd:
                    return False, tr(lang, "reason_digest_match", digest=remote_digest[:19])

        if remote_image_id and nas_image_id:
            if remote_image_id == nas_image_id:
                return False, tr(lang, "reason_id_match", image_id=nas_image_id[:19])

        if remote_digest and nas_digests:
            nas_d_short = nas_digests[0].split("@")[-1][:19]
            return True, tr(
                lang,
                "reason_digest_diff",
                rem=remote_digest[:19],
                nas=nas_d_short,
            )

        if remote_image_id and nas_image_id:
            return True, tr(
                lang,
                "reason_id_diff",
                rem=remote_image_id[:19],
                nas=nas_image_id[:19],
            )

        return True, tr(lang, "reason_not_on_nas")

    def check_status(self) -> UpdateStatus:
        status = UpdateStatus(checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        L = self.config.language

        # 1. Local Docker Engine on Windows
        client: Optional[docker.DockerClient] = None
        try:
            client = docker.from_env()
            client.ping()
            status.local_docker_ok = True
        except Exception as exc:
            status.local_docker_ok = False
            status.local_docker_error = str(exc)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        # 2. SMB share folder check
        try:
            smb_parent = Path(self.config.smb_path).parent
            if smb_parent.exists():
                status.smb_dir_ok = True
            else:
                status.smb_dir_ok = False
                status.smb_dir_error = self._tr("err_smb_dir", path=smb_parent)
        except Exception as exc:
            status.smb_dir_ok = False
            status.smb_dir_error = str(exc)

        # 3. Remote Docker Hub digest & image ID
        try:
            rem_digest, rem_img_id = self.fetch_remote_image_info()
            status.remote_digest = rem_digest
            status.remote_image_id = rem_img_id
        except Exception as exc:
            try:
                fallback_client = docker.from_env()
                try:
                    reg_data = fallback_client.images.get_registry_data(
                        self.config.image_name
                    )
                    status.remote_digest = reg_data.id
                finally:
                    fallback_client.close()
            except Exception:
                status.reason = self._tr("err_fetch_remote", err=exc)

        # 4. Synology NAS SSH + image & container inspection
        try:
            ssh = self._create_ssh_client()
            try:
                status.ssh_ok = True
                nas_info = self.get_nas_image_info(ssh)
                status.nas_image_exists = bool(nas_info.get("exists"))
                status.nas_image_id = nas_info.get("id", "")
                status.nas_repo_digests = nas_info.get("repo_digests", [])
                status.nas_created = nas_info.get("created", "")

                c_info = self.get_nas_container_info(ssh)
                status.nas_container_exists = bool(c_info.get("exists"))
                status.nas_container_running = bool(c_info.get("running"))
                status.nas_container_status = c_info.get("status", "")
                status.nas_container_image_id = c_info.get("image_id", "")

                if (
                    status.nas_image_exists
                    and status.nas_container_exists
                    and status.nas_image_id
                    and status.nas_container_image_id
                ):
                    status.container_needs_recreate = (
                        status.nas_container_image_id != status.nas_image_id
                    )

                needs_upd, reason = self._evaluate_update_needed(
                    status.remote_digest, status.remote_image_id, nas_info, lang=L
                )
                status.needs_update = needs_upd
                if not needs_upd and status.container_needs_recreate:
                    status.reason = self._tr("reason_cont_old_suffix", reason=reason)
                else:
                    status.reason = reason
            finally:
                ssh.close()
        except Exception as exc:
            status.ssh_ok = False
            status.ssh_error = str(exc)
            if not status.reason:
                status.reason = self._tr("err_ssh_nas", err=exc)

        return status

    def pull_image_locally(self) -> docker.models.images.Image:
        """Pull the image locally on Windows with live progress reporting."""
        repo, tag = self._parse_image_ref(self.config.image_name)
        self._log(
            "INFO",
            self._tr(
                "log_pull_start",
                image=self.config.image_name,
                platform=self.config.image_platform,
            ),
        )
        self._progress(0.10, self._tr("prog_pulling", image=self.config.image_name))

        client = docker.from_env()
        try:
            layer_progress: dict[str, tuple[int, int]] = {}
            pull_stream = client.api.pull(
                repo,
                tag=tag,
                platform=self.config.image_platform,
                stream=True,
                decode=True,
            )
            for event in pull_stream:
                if "error" in event:
                    raise RuntimeError(
                        self._tr("err_docker_pull", err=event["error"])
                    )
                status_txt = event.get("status", "")
                layer_id = event.get("id", "")
                detail = event.get("progressDetail") or {}
                current = detail.get("current")
                total = detail.get("total")
                if layer_id and isinstance(current, int) and isinstance(total, int) and total > 0:
                    layer_progress[layer_id] = (current, total)
                    sum_cur = sum(c for c, _ in layer_progress.values())
                    sum_tot = sum(t for _, t in layer_progress.values())
                    frac = 0.10 + 0.40 * (sum_cur / max(sum_tot, 1))
                    mb_cur = sum_cur / (1024 * 1024)
                    mb_tot = sum_tot / (1024 * 1024)
                    self._progress(
                        frac,
                        self._tr(
                            "prog_pull_mb",
                            status=status_txt,
                            curr=mb_cur,
                            tot=mb_tot,
                        ),
                    )
                elif status_txt:
                    self._progress(
                        0.25,
                        self._tr(
                            "prog_pull_status", status=status_txt, layer=layer_id
                        ).strip(),
                    )

            image = client.images.get(self.config.image_name)
            self._log(
                "INFO", self._tr("log_pull_done", image_id=image.id[:19])
            )
            self._progress(0.50, self._tr("prog_pull_done"))
            return image
        finally:
            try:
                client.close()
            except Exception:
                pass

    def convert_tar_for_synology(self, raw_tar_path: Path, final_tar_path: Path) -> None:
        """
        Convert a modern Docker Engine (v25+/v29+) image tar archive into a clean
        Docker v1.2 legacy archive compatible with Synology DSM 7.1 (Docker 20.10):
        - Validates all archive member paths against path traversal (Tar-Slip).
        - Decompresses Zstandard (zstd) compressed layers into plain .tar layers.
        - Writes only the files referenced in manifest.json + repositories.
        """
        with tarfile.open(raw_tar_path, "r") as src_tar:
            manifest_member = src_tar.getmember("manifest.json")
            f_man = src_tar.extractfile(manifest_member)
            if f_man is None:
                raise RuntimeError("manifest.json missing in exported Docker archive")
            manifest_data = json.loads(f_man.read().decode("utf-8"))

            config_rel = _validate_tar_member_name(manifest_data[0]["Config"])
            orig_layers = [
                _validate_tar_member_name(l) for l in manifest_data[0]["Layers"]
            ]

            new_layers: list[str] = []
            zstd_converted_count = 0

            with tarfile.open(
                final_tar_path, "w", format=tarfile.GNU_FORMAT
            ) as dst_tar:
                # 1. Copy the image Config JSON
                cfg_member = src_tar.getmember(config_rel)
                cfg_stream = src_tar.extractfile(cfg_member)
                if cfg_stream is None:
                    raise RuntimeError(f"Config {config_rel} unreadable in tar")
                cfg_bytes = cfg_stream.read()

                cfg_basename = Path(config_rel).name
                cfg_info = tarfile.TarInfo(name=cfg_basename)
                cfg_info.size = len(cfg_bytes)
                cfg_info.mtime = int(time.time())
                cfg_info.mode = 0o644
                dst_tar.addfile(cfg_info, io.BytesIO(cfg_bytes))

                # 2. Process all image layers
                total_layers = len(orig_layers)
                for idx, layer_path in enumerate(orig_layers, start=1):
                    frac = 0.62 + 0.16 * (idx / max(total_layers, 1))
                    self._progress(
                        frac,
                        self._tr("prog_layer_conv", idx=idx, tot=total_layers),
                    )

                    member = src_tar.getmember(layer_path)
                    if member.issym() or member.islnk():
                        target_rel = _validate_tar_member_name(
                            str(Path(layer_path).parent / member.linkname)
                        )
                        member = src_tar.getmember(target_rel)

                    f_in = src_tar.extractfile(member)
                    if f_in is None:
                        raise RuntimeError(f"Cannot read layer {layer_path} from tar")

                    magic = f_in.read(4)
                    f_in.seek(0)

                    layer_hash = Path(layer_path).name.split(".")[0]
                    new_layer_name = f"{layer_hash}/layer.tar"
                    new_layers.append(new_layer_name)

                    if magic == b"\x28\xb5\x2f\xfd":
                        # Zstandard compressed layer -> decompress for Docker 20.10
                        zstd_converted_count += 1
                        with tempfile.TemporaryFile() as tmp_unpacked:
                            with _open_zstd_reader(f_in) as z_reader:
                                shutil.copyfileobj(
                                    z_reader, tmp_unpacked, length=4 * 1024 * 1024
                                )
                            unpacked_size = tmp_unpacked.tell()
                            tmp_unpacked.seek(0)

                            t_info = tarfile.TarInfo(name=new_layer_name)
                            t_info.size = unpacked_size
                            t_info.mtime = int(time.time())
                            t_info.mode = 0o644
                            dst_tar.addfile(t_info, tmp_unpacked)
                    else:
                        t_info = tarfile.TarInfo(name=new_layer_name)
                        t_info.size = member.size
                        t_info.mtime = int(time.time())
                        t_info.mode = 0o644
                        dst_tar.addfile(t_info, f_in)

                # 3. Write clean manifest.json
                repo_Tags = manifest_data[0].get("RepoTags") or [self.config.image_name]
                clean_manifest = [
                    {
                        "Config": cfg_basename,
                        "RepoTags": repo_Tags,
                        "Layers": new_layers,
                    }
                ]
                man_bytes = json.dumps(clean_manifest).encode("utf-8")
                man_info = tarfile.TarInfo(name="manifest.json")
                man_info.size = len(man_bytes)
                man_info.mtime = int(time.time())
                man_info.mode = 0o644
                dst_tar.addfile(man_info, io.BytesIO(man_bytes))

                # 4. Write repositories metadata file for Docker 20.10
                repo_name, repo_tag = self._parse_image_ref(self.config.image_name)
                top_layer_id = new_layers[-1].split("/")[0] if new_layers else ""
                repos_bytes = json.dumps(
                    {repo_name: {repo_tag: top_layer_id}}
                ).encode("utf-8")
                repos_info = tarfile.TarInfo(name="repositories")
                repos_info.size = len(repos_bytes)
                repos_info.mtime = int(time.time())
                repos_info.mode = 0o644
                dst_tar.addfile(repos_info, io.BytesIO(repos_bytes))

            if zstd_converted_count > 0:
                self._log(
                    "INFO",
                    self._tr("log_zstd_unpacked", count=zstd_converted_count),
                )

    def export_image_to_smb(self, image: docker.models.images.Image) -> None:
        """
        Export the Docker image locally, convert zstd layers to Synology DSM 7.1
        format, and write atomically to the SMB destination path.
        """
        smb_target = Path(self.config.smb_path)
        if not smb_target.parent.exists():
            raise FileNotFoundError(
                self._tr("err_smb_missing", path=smb_target.parent)
            )

        self._log("INFO", self._tr("log_export_start", path=smb_target))
        self._progress(0.52, self._tr("prog_export_raw", mb=0))

        raw_fd, raw_temp_str = tempfile.mkstemp(
            prefix="ha_raw_", suffix=".tar"
        )
        os.close(raw_fd)
        raw_temp_path = Path(raw_temp_str)
        smb_temp_path = smb_target.with_suffix(".tar.part")

        bytes_written = 0
        try:
            with open(raw_temp_path, "wb") as f_raw:
                for chunk in image.save(named=True):
                    if chunk:
                        f_raw.write(chunk)
                        bytes_written += len(chunk)
                        mb_written = bytes_written / (1024 * 1024)
                        frac = min(
                            0.62, 0.52 + 0.10 * (mb_written / 1800.0)
                        )
                        self._progress(
                            frac, self._tr("prog_export_raw", mb=mb_written)
                        )

            # Write to .tar.part on SMB first, then rename atomically
            self.convert_tar_for_synology(raw_temp_path, smb_temp_path)
            smb_temp_path.replace(smb_target)

            final_mb = smb_target.stat().st_size / (1024 * 1024)
            self._log(
                "INFO",
                self._tr("log_export_done", mb=final_mb, path=smb_target),
            )
            self._progress(0.78, self._tr("prog_export_done"))
        except Exception:
            if smb_temp_path.exists():
                try:
                    smb_temp_path.unlink()
                except Exception:
                    pass
            raise
        finally:
            if raw_temp_path.exists():
                try:
                    raw_temp_path.unlink()
                except Exception:
                    pass

    def load_and_cleanup_on_nas(self) -> None:
        """Connect over SSH to the NAS, run `docker load -i` and remove the temporary tar file."""
        self._log(
            "INFO",
            self._tr(
                "log_ssh_conn",
                user=self.config.ssh_user,
                ip=self.config.nas_ip,
                port=self.config.ssh_port,
            ),
        )
        self._progress(0.80, self._tr("prog_ssh_conn"))

        docker_bin_q = shlex.quote(self.config.nas_docker_bin)
        tar_path_q = shlex.quote(self.config.nas_tar_path)

        ssh = self._create_ssh_client()
        try:
            load_cmd = f"{docker_bin_q} load -i {tar_path_q}"
            self._log("INFO", self._tr("log_nas_exec_load", cmd=load_cmd))
            self._progress(0.85, self._tr("prog_nas_load", cmd=load_cmd))

            code, out, err = self._run_ssh_command(ssh, load_cmd, timeout=900)
            if code != 0:
                raise RuntimeError(
                    self._tr(
                        "err_nas_load", cmd=load_cmd, code=code, err=err or out
                    )
                )
            self._log("INFO", self._tr("log_nas_load_ok", out=out or "OK"))

            rm_cmd = f"rm -f {tar_path_q}"
            self._progress(0.95, self._tr("prog_nas_rm"))
            self._log("INFO", self._tr("log_nas_exec_rm", cmd=rm_cmd))
            rm_code, rm_out, rm_err = self._run_ssh_command(
                ssh, rm_cmd, timeout=60
            )
            if rm_code != 0:
                raise RuntimeError(
                    self._tr(
                        "err_nas_rm",
                        cmd=rm_cmd,
                        code=rm_code,
                        err=rm_err or rm_out,
                    )
                )
            self._log(
                "INFO", self._tr("log_nas_rm_ok", path=self.config.nas_tar_path)
            )
        finally:
            ssh.close()

    @staticmethod
    def _build_docker_run_from_inspect(
        inspect_data: dict[str, Any],
        docker_bin: str,
        container_name: str,
        image_name: str,
    ) -> str:
        """
        Reconstruct a complete, shell-escaped `docker run -d` command from `docker inspect`
        output so all mounts, host networking, USB devices, capabilities, and user env vars
        are preserved on Synology DSM 7.1.
        """
        cfg = inspect_data.get("Config") or {}
        host_cfg = inspect_data.get("HostConfig") or {}

        args: list[str] = [
            shlex.quote(docker_bin),
            "run",
            "-d",
            "--name",
            shlex.quote(container_name),
        ]

        # Restart policy
        rp = host_cfg.get("RestartPolicy") or {}
        rp_name = rp.get("Name") or ""
        if rp_name and rp_name != "no":
            max_retry = rp.get("MaximumRetryCount") or 0
            if rp_name == "on-failure" and max_retry > 0:
                args.extend(["--restart", shlex.quote(f"on-failure:{max_retry}")])
            else:
                args.extend(["--restart", shlex.quote(rp_name)])
        else:
            args.extend(["--restart", "unless-stopped"])

        # Network mode
        net_mode = host_cfg.get("NetworkMode") or ""
        if net_mode and net_mode != "default":
            args.extend(["--net", shlex.quote(net_mode)])

        # Hostname (when not using host network mode)
        hostname = cfg.get("Hostname") or ""
        if hostname and net_mode != "host":
            args.extend(["--hostname", shlex.quote(hostname)])

        # Privileged mode
        if host_cfg.get("Privileged"):
            args.append("--privileged")

        # Linux capabilities
        for cap in host_cfg.get("CapAdd") or []:
            args.extend(["--cap-add", shlex.quote(cap)])
        for cap in host_cfg.get("CapDrop") or []:
            args.extend(["--cap-drop", shlex.quote(cap)])

        # Devices (e.g. Zigbee / Z-Wave / P1 smart meter USB sticks)
        for dev in host_cfg.get("Devices") or []:
            p_host = dev.get("PathOnHost")
            p_cont = dev.get("PathInContainer") or p_host
            perms = dev.get("CgroupPermissions") or "rwm"
            if p_host:
                args.extend(
                    ["--device", shlex.quote(f"{p_host}:{p_cont}:{perms}")]
                )

        # Bind mounts & volumes
        binds = host_cfg.get("Binds") or []
        if binds:
            for b in binds:
                args.extend(["-v", shlex.quote(b)])
        else:
            for m in inspect_data.get("Mounts") or []:
                src = m.get("Source")
                dst = m.get("Destination")
                rw = m.get("RW", True)
                if src and dst:
                    mode_str = "rw" if rw else "ro"
                    args.extend(["-v", shlex.quote(f"{src}:{dst}:{mode_str}")])

        # Port mappings (when not using host networking)
        if net_mode != "host":
            port_bindings = host_cfg.get("PortBindings") or {}
            for c_port, bindings in port_bindings.items():
                for b in bindings or []:
                    h_ip = b.get("HostIp") or ""
                    h_port = b.get("HostPort") or ""
                    if h_port:
                        mapping = (
                            f"{h_ip}:{h_port}:{c_port}"
                            if h_ip
                            else f"{h_port}:{c_port}"
                        )
                        args.extend(["-p", shlex.quote(mapping)])

        # User-defined environment variables (filtering internal base image vars)
        for env_var in cfg.get("Env") or []:
            if not any(
                env_var.startswith(prefix)
                for prefix in IMAGE_INTERNAL_ENV_PREFIXES
            ):
                args.extend(["-e", shlex.quote(env_var)])

        # Extra hosts
        for eh in host_cfg.get("ExtraHosts") or []:
            args.extend(["--add-host", shlex.quote(eh)])

        args.append(shlex.quote(image_name))
        return " ".join(args)

    def recreate_container_on_nas(
        self, ssh: Optional[paramiko.SSHClient] = None
    ) -> bool:
        """
        Recreate and restart the running Home Assistant container on the Synology NAS
        using the newly loaded image, with automatic backup & rollback on failure.
        """
        cname = self.config.ha_container_name.strip()
        if not cname:
            self._log("WARNING", self._tr("log_no_cont_cfg"))
            return False

        own_ssh = False
        if ssh is None:
            ssh = self._create_ssh_client()
            own_ssh = True

        docker_bin_q = shlex.quote(self.config.nas_docker_bin)
        cname_q = shlex.quote(cname)

        try:
            mode = self.config.recreate_mode.strip().lower()
            if mode == "compose":
                compose_file = self.config.nas_compose_path.strip()
                compose_dir = str(Path(compose_file).parent).replace("\\", "/")
                self._log(
                    "INFO", self._tr("log_compose_update", path=compose_file)
                )
                compose_dir_q = shlex.quote(compose_dir)
                compose_file_q = shlex.quote(compose_file)
                cmd = (
                    f"sh -c 'cd {compose_dir_q} && "
                    f"(/usr/local/bin/docker-compose -f {compose_file_q} up -d || "
                    f"{docker_bin_q} compose -f {compose_file_q} up -d)'"
                )
                code, out, err = self._run_ssh_command(ssh, cmd, timeout=300)
                if code != 0:
                    raise RuntimeError(
                        self._tr("err_compose", code=code, err=err or out)
                    )
                self._log("INFO", self._tr("log_compose_ok", out=out or err))
                return True

            # Default mode: inspect -> stop -> rename backup -> run -> remove backup
            self._log("INFO", self._tr("log_inspect_cont", name=cname))
            inspect_cmd = f"{docker_bin_q} inspect {cname_q}"
            code, out, err = self._run_ssh_command(ssh, inspect_cmd, timeout=30)
            if code != 0:
                raise RuntimeError(
                    self._tr("err_cont_not_found", name=cname, err=err or out)
                )

            inspect_list = json.loads(out)
            if not inspect_list:
                raise RuntimeError(self._tr("err_cont_empty", name=cname))
            inspect_data = inspect_list[0]

            run_cmd = self._build_docker_run_from_inspect(
                inspect_data=inspect_data,
                docker_bin=self.config.nas_docker_bin,
                container_name=cname,
                image_name=self.config.image_name,
            )

            backup_name = f"{cname}_backup_old"
            backup_name_q = shlex.quote(backup_name)
            self._run_ssh_command(
                ssh, f"{docker_bin_q} rm -f {backup_name_q}", timeout=30
            )

            self._log("INFO", self._tr("log_stop_cont", name=cname))
            self._progress(0.96, self._tr("prog_stop_cont", name=cname))
            stop_code, stop_out, stop_err = self._run_ssh_command(
                ssh, f"{docker_bin_q} stop -t 30 {cname_q}", timeout=60
            )
            if stop_code != 0:
                raise RuntimeError(
                    self._tr(
                        "err_stop_cont",
                        name=cname,
                        err=stop_err or stop_out,
                    )
                )

            self._log("INFO", self._tr("log_rename_backup", backup=backup_name))
            ren_code, ren_out, ren_err = self._run_ssh_command(
                ssh,
                f"{docker_bin_q} rename {cname_q} {backup_name_q}",
                timeout=30,
            )
            if ren_code != 0:
                self._run_ssh_command(
                    ssh, f"{docker_bin_q} start {cname_q}", timeout=30
                )
                raise RuntimeError(
                    self._tr("err_rename_backup", err=ren_err or ren_out)
                )

            self._log("INFO", self._tr("log_start_new", name=cname))
            self._progress(0.98, self._tr("prog_start_new", name=cname))
            try:
                run_code, run_out, run_err = self._run_ssh_command(
                    ssh, run_cmd, timeout=180
                )
            except Exception as cmd_exc:
                # Fallback verification: if SSH timed out, check if container actually started
                time.sleep(5)
                try:
                    chk_code, chk_out, _ = self._run_ssh_command(
                        ssh,
                        f"{docker_bin_q} inspect {cname_q} --format '{{{{.State.Running}}}}'",
                        timeout=20,
                    )
                    if chk_code == 0 and "true" in chk_out.lower():
                        run_code = 0
                        run_out = "started"
                        run_err = ""
                    else:
                        raise cmd_exc
                except Exception:
                    raise cmd_exc

            if run_code != 0:
                self._log(
                    "ERROR",
                    self._tr("log_rollback_start", err=run_err or run_out),
                )
                self._run_ssh_command(
                    ssh, f"{docker_bin_q} rm -f {cname_q}", timeout=30
                )
                self._run_ssh_command(
                    ssh,
                    f"{docker_bin_q} rename {backup_name_q} {cname_q}",
                    timeout=30,
                )
                self._run_ssh_command(
                    ssh, f"{docker_bin_q} start {cname_q}", timeout=60
                )
                self._log("INFO", self._tr("log_rollback_done", name=cname))
                raise RuntimeError(run_err or run_out)

            self._run_ssh_command(
                ssh, f"{docker_bin_q} rm -f {backup_name_q}", timeout=30
            )
            self._log(
                "INFO",
                self._tr("log_recreate_ok", name=cname, cid=run_out[:12]),
            )
            self._progress(1.0, self._tr("prog_recreate_ok", name=cname))
            return True
        except Exception as exc:
            err_msg = str(exc).strip()
            if not err_msg:
                err_msg = f"{type(exc).__name__} (time-out)"
            self._log(
                "ERROR", self._tr("err_recreate_exc", name=cname, err=err_msg)
            )
            return False
        finally:
            if own_ssh and ssh is not None:
                ssh.close()

    def restart_container_on_nas(self) -> bool:
        """Restart the existing Home Assistant container on the NAS."""
        cname = self.config.ha_container_name.strip()
        if not cname:
            self._log("ERROR", self._tr("err_no_cont_env"))
            return False

        docker_bin_q = shlex.quote(self.config.nas_docker_bin)
        cname_q = shlex.quote(cname)

        try:
            self._log("INFO", self._tr("log_restart_start", name=cname))
            self._progress(0.5, self._tr("prog_restart", name=cname))
            ssh = self._create_ssh_client()
            try:
                cmd = f"{docker_bin_q} restart {cname_q}"
                code, out, err = self._run_ssh_command(ssh, cmd, timeout=90)
                if code != 0:
                    raise RuntimeError(
                        self._tr("err_restart_code", code=code, err=err or out)
                    )
                self._log("INFO", self._tr("log_restart_ok", name=cname))
                self._progress(1.0, self._tr("prog_restart_ok", name=cname))
                return True
            finally:
                ssh.close()
        except Exception as exc:
            self._log("ERROR", self._tr("err_restart_exc", err=exc))
            return False

    def prune_old_images_on_nas(self) -> tuple[bool, str]:
        """Clean up dangling/unused Docker images on the Synology NAS (`docker image prune -f`)."""
        docker_bin_q = shlex.quote(self.config.nas_docker_bin)
        try:
            self._log("INFO", self._tr("log_prune_start"))
            self._progress(0.4, self._tr("prog_prune"))
            ssh = self._create_ssh_client()
            try:
                cmd = f"{docker_bin_q} image prune -f"
                code, out, err = self._run_ssh_command(ssh, cmd, timeout=120)
                if code != 0:
                    raise RuntimeError(
                        self._tr("err_prune_code", code=code, err=err or out)
                    )
                summary = (
                    out.splitlines()[-1]
                    if out
                    else self._tr("prune_none_found")
                )
                self._log("INFO", self._tr("log_prune_ok", summary=summary))
                self._progress(
                    1.0, self._tr("prog_prune_ok", summary=summary)
                )
                return True, out
            finally:
                ssh.close()
        except Exception as exc:
            self._log("ERROR", self._tr("err_prune_exc", err=exc))
            return False, str(exc)

    def provision_synology_nas(self, password: str) -> tuple[bool, str]:
        """
        One-time Synology DSM 7.1 provisioning helper using a temporary password:
        1. Installs the user's public SSH key (.pub) into ~/.ssh/authorized_keys
           and sets strict permissions (chmod 755 ~, 700 ~/.ssh, 600 authorized_keys).
        2. Configures the 'docker' group + /var/run/docker.sock permissions (660)
           and a restricted /etc/sudoers.d/ha_updater rule for passwordless updates.
        """
        if not SAFE_USERNAME_RE.match(self.config.ssh_user):
            return False, self._tr("val_bad_user")

        pub_key_path = Path(self.config.ssh_key_path + ".pub")
        if not pub_key_path.exists():
            return False, self._tr("err_pub_not_found", path=pub_key_path)

        pub_key = pub_key_path.read_text(encoding="utf-8").strip()
        if not pub_key or "\n" in pub_key or "\r" in pub_key:
            return False, self._tr("err_pub_empty", path=pub_key_path)

        self._log(
            "INFO",
            self._tr(
                "log_prov_start",
                user=self.config.ssh_user,
                ip=self.config.nas_ip,
                port=self.config.ssh_port,
            ),
        )
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        if self.config.ssh_strict_host_key:
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(
                hostname=self.config.nas_ip,
                port=self.config.ssh_port,
                username=self.config.ssh_user,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=15,
            )

            # 1. Configure SSH authorized_keys and directory permissions safely
            pub_key_q = shlex.quote(pub_key)
            setup_ssh_cmd = (
                "chmod 755 ~ && "
                "mkdir -p ~/.ssh && "
                "chmod 700 ~/.ssh && "
                "touch ~/.ssh/authorized_keys && "
                f"grep -qxF {pub_key_q} ~/.ssh/authorized_keys || "
                f"printf '%s\\n' {pub_key_q} >> ~/.ssh/authorized_keys && "
                "chmod 600 ~/.ssh/authorized_keys"
            )
            stdin, stdout, stderr = ssh.exec_command(setup_ssh_cmd, timeout=30)
            stdin.close()
            code = stdout.channel.recv_exit_status()
            if code != 0:
                err = stderr.read().decode("utf-8", errors="replace").strip()
                return False, self._tr("err_prov_auth_keys", err=err)

            self._log("INFO", self._tr("log_prov_key_ok"))

            # 2. Configure docker socket group and restricted sudoers rule
            user = self.config.ssh_user
            sudoers_line_q = shlex.quote(
                f"{user} ALL=(ALL) NOPASSWD: /usr/local/bin/docker, /bin/rm"
            )
            root_Inner = (
                f"synogroup --add docker {user} >/dev/null 2>&1 || true; "
                f"synogroup --member docker {user} >/dev/null 2>&1 || true; "
                "chown root:docker /var/run/docker.sock && "
                "chmod 660 /var/run/docker.sock && "
                f"printf '%s\\n' {sudoers_line_q} > /etc/sudoers.d/ha_updater && "
                "chmod 440 /etc/sudoers.d/ha_updater"
            )
            sudo_script = f"sudo -S -p '' sh -c {shlex.quote(root_Inner)}"
            stdin, stdout, stderr = ssh.exec_command(sudo_script, timeout=30)
            stdin.write(password + "\n")
            stdin.flush()
            stdin.close()
            s_code = stdout.channel.recv_exit_status()
            s_err = stderr.read().decode("utf-8", errors="replace").strip()

            if s_code != 0:
                self._log("WARNING", self._tr("log_prov_sudo_warn", err=s_err))
                return True, self._tr("msg_prov_partial", err=s_err)

            self._log("INFO", self._tr("log_prov_all_ok"))
            return True, self._tr("msg_prov_all_ok")
        except Exception as exc:
            self._log("ERROR", self._tr("err_prov_exc", err=exc))
            return False, self._tr("msg_prov_exc", err=exc)
        finally:
            ssh.close()

    def run_update_workflow(self, force: bool = False) -> bool:
        """
        Execute the complete update workflow:
        1. Check remote vs. NAS image digest/ID
        2. Pull image locally on Windows (if needed or forced)
        3. Convert zstd layers & export tar to SMB
        4. Run `docker load` and remove tar on NAS via SSH
        5. Optionally recreate the running Home Assistant container on the NAS
        """
        start_ts = time.time()
        self._progress(0.02, self._tr("prog_wf_check"))
        self._log("INFO", "=" * 68)
        self._log("INFO", self._tr("log_wf_start"))

        val_errors = self.config.validate()
        if val_errors:
            for err in val_errors:
                self._log("ERROR", err)
            self._progress(0.0, val_errors[0])
            return False

        status = self.check_status()
        if not status.ssh_ok:
            self._log(
                "ERROR", self._tr("log_wf_no_ssh", err=status.ssh_error)
            )
            self._progress(0.0, self._tr("prog_wf_no_ssh"))
            return False

        if not status.remote_digest and not force:
            self._log("ERROR", self._tr("log_wf_no_rem", reason=status.reason))
            self._progress(0.0, self._tr("prog_wf_no_rem"))
            return False

        if not status.needs_update and not force:
            self._log(
                "INFO", self._tr("log_wf_uptodate", reason=status.reason)
            )
            if (
                status.container_needs_recreate
                and self.config.auto_recreate_container
            ):
                self._log("INFO", self._tr("log_wf_cont_catchup"))
                ok = self.recreate_container_on_nas()
                return ok
            self._progress(1.0, self._tr("prog_wf_uptodate"))
            return True

        if force and not status.needs_update:
            self._log("INFO", self._tr("log_wf_force"))
        else:
            self._log("INFO", self._tr("log_wf_new_ver", reason=status.reason))

        if not status.local_docker_ok:
            self._log(
                "ERROR",
                self._tr("log_wf_no_docker", err=status.local_docker_error),
            )
            self._progress(0.0, self._tr("prog_wf_no_docker"))
            return False

        if not status.smb_dir_ok:
            self._log(
                "ERROR", self._tr("log_wf_no_smb", err=status.smb_dir_error)
            )
            self._progress(0.0, self._tr("prog_wf_no_smb"))
            return False

        try:
            image = self.pull_image_locally()
            self.export_image_to_smb(image)
            self.load_and_cleanup_on_nas()

            if self.config.auto_recreate_container:
                self._log(
                    "INFO",
                    self._tr(
                        "log_wf_auto_recreate",
                        name=self.config.ha_container_name,
                    ),
                )
                recreate_ok = self.recreate_container_on_nas()
                if not recreate_ok:
                    return False
        except Exception as exc:
            self._log("ERROR", self._tr("log_wf_failed", exc=exc))
            self._progress(0.0, self._tr("prog_wf_failed"))
            return False

        elapsed = time.time() - start_ts
        self._log("INFO", self._tr("log_wf_done", elapsed=elapsed))
        self._progress(1.0, self._tr("prog_wf_done"))
        return True


# ==============================================================================
# Flag Icon Helper & Graphical User Interface (CustomTkinter)
# ==============================================================================
def _create_flag_ctk_images(ctk_module: Any) -> dict[str, Any]:
    """
    Create crisp 20x14 UK (EN) and Netherlands (NL) flag icons using Pillow so
    colorful flags render reliably on Windows (where Segoe UI does not render
    regional indicator flag emojis).
    """
    try:
        from PIL import Image, ImageDraw

        # 1. Dutch flag (NL): Red (#AE1C28), White (#FFFFFF), Blue (#21468B)
        w, h = 40, 28
        nl_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d_nl = ImageDraw.Draw(nl_img)
        d_nl.rectangle([0, 0, w - 1, h // 3 - 1], fill="#AE1C28")
        d_nl.rectangle([0, h // 3, w - 1, (2 * h) // 3 - 1], fill="#FFFFFF")
        d_nl.rectangle([0, (2 * h) // 3, w - 1, h - 1], fill="#21468B")
        d_nl.rectangle([0, 0, w - 1, h - 1], outline="#475569", width=1)

        # 2. UK Union Jack (EN): Navy (#012169), White diagonals & cross, Red (#C8102E) cross
        en_img = Image.new("RGBA", (w, h), "#012169")
        d_en = ImageDraw.Draw(en_img)
        # White diagonals
        d_en.line([(0, 0), (w - 1, h - 1)], fill="#FFFFFF", width=5)
        d_en.line([(0, h - 1), (w - 1, 0)], fill="#FFFFFF", width=5)
        # Red diagonals
        d_en.line([(0, 0), (w - 1, h - 1)], fill="#C8102E", width=2)
        d_en.line([(0, h - 1), (w - 1, 0)], fill="#C8102E", width=2)
        # White central cross
        cx, cy = w // 2, h // 2
        d_en.rectangle([cx - 5, 0, cx + 5, h - 1], fill="#FFFFFF")
        d_en.rectangle([0, cy - 5, w - 1, cy + 5], fill="#FFFFFF")
        # Red central cross
        d_en.rectangle([cx - 3, 0, cx + 3, h - 1], fill="#C8102E")
        d_en.rectangle([0, cy - 3, w - 1, cy + 3], fill="#C8102E")
        d_en.rectangle([0, 0, w - 1, h - 1], outline="#475569", width=1)

        return {
            "en": ctk_module.CTkImage(
                light_image=en_img, dark_image=en_img, size=(20, 14)
            ),
            "nl": ctk_module.CTkImage(
                light_image=nl_img, dark_image=nl_img, size=(20, 14)
            ),
        }
    except Exception:
        return {"en": None, "nl": None}


def launch_gui(config: AppConfig) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import customtkinter as ctk

    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

    class HAUpdaterApp(ctk.CTk):
        def __init__(self, cfg: AppConfig) -> None:
            super().__init__()
            self.cfg = cfg
            self._lang = cfg.language if cfg.language in ("en", "nl") else "en"
            self.title(tr(self._lang, "window_title"))
            self.geometry("1120x780")
            self.minsize(980, 680)

            self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
            self.worker_thread: Optional[threading.Thread] = None
            self.is_busy = False
            self._last_status: Optional[UpdateStatus] = None
            self._flag_images = _create_flag_ctk_images(ctk)

            self._build_layout()
            self._update_language_toggle_styles()
            self.after(100, self._process_ui_queue)
            self.after(300, self.start_status_check)

        def _t(self, key: str, **kwargs: Any) -> str:
            return tr(self._lang, key, **kwargs)

        def _build_layout(self) -> None:
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(3, weight=1)

            # 1. Header Bar
            header = ctk.CTkFrame(self, corner_radius=12, fg_color=("#E2E8F0", "#1E293B"))
            header.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
            header.grid_columnconfigure(0, weight=1)

            title_box = ctk.CTkFrame(header, fg_color="transparent")
            title_box.grid(row=0, column=0, padx=16, pady=12, sticky="w")

            self.lbl_title = ctk.CTkLabel(
                title_box,
                text=self._t("header_title"),
                font=ctk.CTkFont(size=21, weight="bold"),
            )
            self.lbl_title.pack(anchor="w")

            self.lbl_subtitle = ctk.CTkLabel(
                title_box,
                text=self._format_subtitle(),
                font=ctk.CTkFont(size=12),
                text_color=("#475569", "#94A3B8"),
            )
            self.lbl_subtitle.pack(anchor="w", pady=(2, 0))

            right_header = ctk.CTkFrame(header, fg_color="transparent")
            right_header.grid(row=0, column=1, padx=16, pady=12, sticky="e")

            self.badge_status = ctk.CTkLabel(
                right_header,
                text=self._t("badge_checking"),
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#334155",
                text_color="#F8FAFC",
                corner_radius=8,
                padx=12,
                pady=6,
            )
            self.badge_status.pack(side="left", padx=(0, 8))

            self.btn_help = ctk.CTkButton(
                right_header,
                text=self._t("btn_help"),
                width=165,
                height=34,
                fg_color="#334155",
                hover_color="#475569",
                command=self.open_permissions_help_window,
            )
            self.btn_help.pack(side="left", padx=(0, 8))

            self.btn_settings = ctk.CTkButton(
                right_header,
                text=self._t("btn_settings"),
                width=145,
                height=34,
                fg_color="#334155",
                hover_color="#475569",
                command=self.open_settings_window,
            )
            self.btn_settings.pack(side="left", padx=(0, 10))

            # Compact Bilingual Flag Switcher in Top-Right Corner (🇬🇧 EN | 🇳🇱 NL)
            self.lang_switch_frame = ctk.CTkFrame(
                right_header,
                corner_radius=8,
                fg_color="#0F172A",
                border_width=1,
                border_color="#334155",
            )
            self.lang_switch_frame.pack(side="left")

            self.btn_lang_en = ctk.CTkButton(
                self.lang_switch_frame,
                text="EN",
                image=self._flag_images.get("en"),
                compound="left",
                width=56,
                height=28,
                corner_radius=6,
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda: self.set_language("en"),
            )
            self.btn_lang_en.pack(side="left", padx=(3, 1), pady=3)

            self.btn_lang_nl = ctk.CTkButton(
                self.lang_switch_frame,
                text="NL",
                image=self._flag_images.get("nl"),
                compound="left",
                width=56,
                height=28,
                corner_radius=6,
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda: self.set_language("nl"),
            )
            self.btn_lang_nl.pack(side="left", padx=(1, 3), pady=3)

            # 2. Four Status Cards
            cards_frame = ctk.CTkFrame(self, fg_color="transparent")
            cards_frame.grid(row=1, column=0, padx=16, pady=6, sticky="ew")
            cards_frame.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="cards")

            self.card_conn = self._create_info_card(
                cards_frame, 0, self._t("card_conn_title")
            )
            self.card_remote = self._create_info_card(
                cards_frame, 1, self._t("card_remote_title")
            )
            self.card_nas = self._create_info_card(
                cards_frame, 2, self._t("card_nas_title")
            )
            self.card_container = self._create_info_card(
                cards_frame, 3, self._t("card_container_title")
            )

            # 3. Action Bar & Progress Bar
            action_frame = ctk.CTkFrame(self, corner_radius=12, fg_color=("#E2E8F0", "#1E293B"))
            action_frame.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
            action_frame.grid_columnconfigure(0, weight=1)

            btn_row = ctk.CTkFrame(action_frame, fg_color="transparent")
            btn_row.grid(row=0, column=0, padx=14, pady=(12, 6), sticky="ew")

            self.btn_check = ctk.CTkButton(
                btn_row,
                text=self._t("btn_check"),
                width=155,
                height=38,
                fg_color="#0284C7",
                hover_color="#0369A1",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=self.start_status_check,
            )
            self.btn_check.pack(side="left", padx=(0, 8))

            self.btn_update = ctk.CTkButton(
                btn_row,
                text=self._t("btn_update"),
                width=185,
                height=38,
                fg_color="#16A34A",
                hover_color="#15803D",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda: self.start_update(force=False),
            )
            self.btn_update.pack(side="left", padx=(0, 8))

            self.btn_force = ctk.CTkButton(
                btn_row,
                text=self._t("btn_force"),
                width=175,
                height=38,
                fg_color="#D97706",
                hover_color="#B45309",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=lambda: self.start_update(force=True),
            )
            self.btn_force.pack(side="left", padx=(0, 8))

            self.btn_recreate = ctk.CTkButton(
                btn_row,
                text=self._t("btn_recreate"),
                width=165,
                height=38,
                fg_color="#7C3AED",
                hover_color="#6D28D9",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=self.start_recreate_container,
            )
            self.btn_recreate.pack(side="left", padx=(0, 8))

            self.btn_restart = ctk.CTkButton(
                btn_row,
                text=self._t("btn_restart"),
                width=130,
                height=38,
                fg_color="#475569",
                hover_color="#334155",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=self.start_restart_container,
            )
            self.btn_restart.pack(side="left", padx=(0, 8))

            self.btn_prune = ctk.CTkButton(
                btn_row,
                text=self._t("btn_prune"),
                width=165,
                height=38,
                fg_color="#475569",
                hover_color="#334155",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=self.start_prune_images,
            )
            self.btn_prune.pack(side="left")

            self.lbl_progress = ctk.CTkLabel(
                action_frame,
                text=self._t("prog_ready"),
                font=ctk.CTkFont(size=12),
                text_color=("#475569", "#CBD5E1"),
            )
            self.lbl_progress.grid(row=1, column=0, padx=16, pady=(2, 2), sticky="w")

            self.progress_bar = ctk.CTkProgressBar(action_frame, height=12)
            self.progress_bar.grid(row=2, column=0, padx=14, pady=(2, 12), sticky="ew")
            self.progress_bar.set(0.0)

            # 4. Live Log Panel
            log_frame = ctk.CTkFrame(self, corner_radius=12, fg_color=("#E2E8F0", "#1E293B"))
            log_frame.grid(row=3, column=0, padx=16, pady=(6, 16), sticky="nsew")
            log_frame.grid_columnconfigure(0, weight=1)
            log_frame.grid_rowconfigure(1, weight=1)

            log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
            log_header.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")

            self.lbl_log_header = ctk.CTkLabel(
                log_header,
                text=self._t("log_header_fmt", name=Path(self.cfg.log_file).name),
                font=ctk.CTkFont(size=14, weight="bold"),
            )
            self.lbl_log_header.pack(side="left")

            self.btn_clear_log = ctk.CTkButton(
                log_header,
                text=self._t("btn_clear_log"),
                width=95,
                height=26,
                fg_color="#334155",
                hover_color="#475569",
                command=self._clear_log_box,
            )
            self.btn_clear_log.pack(side="right")

            self.log_box = ctk.CTkTextbox(
                log_frame,
                font=ctk.CTkFont(family="Consolas", size=12),
                fg_color=("#0F172A", "#090D16"),
                text_color="#E2E8F0",
                corner_radius=8,
            )
            self.log_box.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="nsew")
            self.log_box.configure(state="disabled")

        def _update_language_toggle_styles(self) -> None:
            if self._lang == "en":
                self.btn_lang_en.configure(
                    fg_color="#2563EB",
                    hover_color="#1D4ED8",
                    text_color="#FFFFFF",
                )
                self.btn_lang_nl.configure(
                    fg_color="transparent",
                    hover_color="#1E293B",
                    text_color="#94A3B8",
                )
            else:
                self.btn_lang_nl.configure(
                    fg_color="#2563EB",
                    hover_color="#1D4ED8",
                    text_color="#FFFFFF",
                )
                self.btn_lang_en.configure(
                    fg_color="transparent",
                    hover_color="#1E293B",
                    text_color="#94A3B8",
                )

        def set_language(self, lang: str) -> None:
            """Switch language live between 'en' and 'nl' and persist to .env."""
            lang_norm = lang if lang in ("en", "nl") else "en"
            if lang_norm == self._lang:
                return
            self._lang = lang_norm
            self.cfg.save_language_preference(lang_norm)
            self._update_language_toggle_styles()
            self._retranslate_ui()

        def _retranslate_ui(self) -> None:
            self.title(self._t("window_title"))
            self.lbl_title.configure(text=self._t("header_title"))
            self.lbl_subtitle.configure(text=self._format_subtitle())
            self.btn_help.configure(text=self._t("btn_help"))
            self.btn_settings.configure(text=self._t("btn_settings"))

            self.card_conn["title"].configure(text=self._t("card_conn_title"))
            self.card_remote["title"].configure(text=self._t("card_remote_title"))
            self.card_nas["title"].configure(text=self._t("card_nas_title"))
            self.card_container["title"].configure(text=self._t("card_container_title"))

            self.btn_check.configure(text=self._t("btn_check"))
            self.btn_update.configure(text=self._t("btn_update"))
            self.btn_force.configure(text=self._t("btn_force"))
            self.btn_recreate.configure(text=self._t("btn_recreate"))
            self.btn_restart.configure(text=self._t("btn_restart"))
            self.btn_prune.configure(text=self._t("btn_prune"))

            self.lbl_log_header.configure(
                text=self._t("log_header_fmt", name=Path(self.cfg.log_file).name)
            )
            self.btn_clear_log.configure(text=self._t("btn_clear_log"))

            if self._last_status is not None:
                self._apply_status(self._last_status)
            elif not self.is_busy:
                self.badge_status.configure(text=self._t("badge_checking"))
                self.lbl_progress.configure(text=self._t("prog_ready"))

        def _create_info_card(
            self, parent: ctk.CTkFrame, col: int, title: str
        ) -> dict[str, Any]:
            card = ctk.CTkFrame(parent, corner_radius=12, fg_color=("#E2E8F0", "#1E293B"))
            card.grid(row=0, column=col, padx=5, pady=4, sticky="nsew")

            lbl_head = ctk.CTkLabel(
                card, text=title, font=ctk.CTkFont(size=14, weight="bold")
            )
            lbl_head.pack(anchor="w", padx=12, pady=(10, 4))

            lbl_body = ctk.CTkLabel(
                card,
                text=self._t("card_checking"),
                font=ctk.CTkFont(family="Consolas", size=11),
                justify="left",
                anchor="w",
                text_color=("#334155", "#CBD5E1"),
            )
            lbl_body.pack(anchor="w", padx=12, pady=(0, 12), fill="x")
            return {"frame": card, "title": lbl_head, "body": lbl_body}

        def _format_subtitle(self) -> str:
            return self._t(
                "subtitle_fmt",
                user=self.cfg.ssh_user or "?",
                ip=self.cfg.nas_ip or "?",
                port=self.cfg.ssh_port,
                smb=self.cfg.smb_path,
                container=self.cfg.ha_container_name or "-",
            )

        def _clear_log_box(self) -> None:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")

        def append_log(self, level: str, message: str) -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] [{level}] {message}\n"
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        def _set_busy(self, busy: bool) -> None:
            self.is_busy = busy
            state = "disabled" if busy else "normal"
            self.btn_check.configure(state=state)
            self.btn_update.configure(state=state)
            self.btn_force.configure(state=state)
            self.btn_recreate.configure(state=state)
            self.btn_restart.configure(state=state)
            self.btn_prune.configure(state=state)
            self.btn_settings.configure(state=state)

        def _process_ui_queue(self) -> None:
            try:
                while True:
                    event_type, payload = self.ui_queue.get_nowait()
                    if event_type == "log":
                        lvl, msg = payload
                        self.append_log(lvl, msg)
                    elif event_type == "progress":
                        frac, text = payload
                        self.progress_bar.set(frac)
                        self.lbl_progress.configure(text=text)
                    elif event_type == "status_result":
                        self._apply_status(payload)
                        self._set_busy(False)
                    elif event_type == "workflow_done":
                        self._set_busy(False)
                        self.start_status_check()
            except queue.Empty:
                pass
            finally:
                self.after(80, self._process_ui_queue)

        def _apply_status(self, st: UpdateStatus) -> None:
            self._last_status = st
            d_txt = self._t("conn_docker_ok") if st.local_docker_ok else self._t("conn_docker_err")
            s_txt = self._t("conn_smb_ok") if st.smb_dir_ok else self._t("conn_smb_err")
            h_txt = self._t("conn_ssh_ok") if st.ssh_ok else self._t("conn_ssh_err")
            sudo_txt = self._t("conn_sudo_on") if self.cfg.use_sudo else self._t("conn_sudo_off")
            self.card_conn["body"].configure(
                text=self._t(
                    "conn_body_fmt",
                    docker=d_txt,
                    smb=s_txt,
                    ssh=h_txt,
                    sudo=sudo_txt,
                )
            )

            if st.remote_digest:
                rem_id_short = st.remote_image_id[:22] if st.remote_image_id else "n/a"
                self.card_remote["body"].configure(
                    text=self._t(
                        "remote_body_fmt",
                        image=self.cfg.image_name,
                        platform=self.cfg.image_platform,
                        digest=st.remote_digest[:22],
                        image_id=rem_id_short,
                    )
                )
            else:
                self.card_remote["body"].configure(text=self._t("remote_err"))

            if st.ssh_ok and st.nas_image_exists:
                dig_short = (
                    st.nas_repo_digests[0].split("@")[-1][:22] + "..."
                    if st.nas_repo_digests
                    else self._t("nas_digest_none")
                )
                created_short = st.nas_created[:19].replace("T", " ")
                self.card_nas["body"].configure(
                    text=self._t(
                        "nas_body_fmt",
                        image_id=st.nas_image_id[:22],
                        digest=dig_short,
                        created=created_short,
                    )
                )
            elif st.ssh_ok:
                self.card_nas["body"].configure(text=self._t("nas_not_loaded"))
            else:
                self.card_nas["body"].configure(
                    text=self._t(
                        "nas_unavail_fmt",
                        err=(st.ssh_error[:38] if st.ssh_error else self._t("nas_ssh_err")),
                    )
                )

            if st.ssh_ok and st.nas_container_exists:
                c_img_short = (
                    st.nas_container_image_id[:19] + "..."
                    if st.nas_container_image_id
                    else "?"
                )
                sync_txt = (
                    self._t("cont_sync_old")
                    if st.container_needs_recreate
                    else self._t("cont_sync_ok")
                )
                self.card_container["body"].configure(
                    text=self._t(
                        "cont_body_fmt",
                        name=self.cfg.ha_container_name,
                        status=st.nas_container_status,
                        image_id=c_img_short,
                        sync=sync_txt,
                    )
                )
            elif st.ssh_ok:
                self.card_container["body"].configure(
                    text=self._t("cont_not_found_fmt", name=self.cfg.ha_container_name)
                )
            else:
                self.card_container["body"].configure(text=self._t("cont_no_conn"))

            if st.ssh_ok and st.remote_digest and not st.needs_update:
                if st.container_needs_recreate:
                    self.badge_status.configure(
                        text=self._t("badge_container_update"),
                        fg_color="#7C3AED",
                    )
                else:
                    self.badge_status.configure(
                        text=self._t("badge_up_to_date"),
                        fg_color="#15803D",
                    )
            elif st.ssh_ok and st.remote_digest and st.needs_update:
                self.badge_status.configure(
                    text=self._t("badge_update_avail"),
                    fg_color="#B45309",
                )
            else:
                self.badge_status.configure(
                    text=self._t("badge_check_req"),
                    fg_color="#B91C1C",
                )

            self.lbl_progress.configure(
                text=self._t("prog_last_check", ts=st.checked_at)
            )
            self.progress_bar.set(
                1.0
                if (st.ssh_ok and not st.needs_update and not st.container_needs_recreate)
                else 0.0
            )

        def start_status_check(self) -> None:
            if self.is_busy:
                return
            self._set_busy(True)
            self.badge_status.configure(
                text=self._t("badge_checking"), fg_color="#334155"
            )
            self.lbl_progress.configure(text=self._t("prog_checking_all"))
            self.progress_bar.set(0.2)

            def _worker() -> None:
                updater = HAUpdater(
                    self.cfg,
                    log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    progress_callback=lambda f, t: self.ui_queue.put(("progress", (f, t))),
                )
                st = updater.check_status()
                self.ui_queue.put(
                    ("log", ("INFO", self._t("log_status_check", reason=st.reason)))
                )
                self.ui_queue.put(("status_result", st))

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()

        def start_update(self, force: bool = False) -> None:
            if self.is_busy:
                return
            self._set_busy(True)

            def _worker() -> None:
                updater = HAUpdater(
                    self.cfg,
                    log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    progress_callback=lambda f, t: self.ui_queue.put(("progress", (f, t))),
                )
                ok = updater.run_update_workflow(force=force)
                self.ui_queue.put(("workflow_done", ok))

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()

        def start_recreate_container(self) -> None:
            if self.is_busy:
                return
            cname = self.cfg.ha_container_name.strip() or "home-assistant"
            if not messagebox.askyesno(
                self._t("recreate_confirm_title"),
                self._t(
                    "recreate_confirm_msg",
                    cname=cname,
                    image=self.cfg.image_name,
                ),
                parent=self,
            ):
                return

            self._set_busy(True)

            def _worker() -> None:
                updater = HAUpdater(
                    self.cfg,
                    log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    progress_callback=lambda f, t: self.ui_queue.put(("progress", (f, t))),
                )
                ok = updater.recreate_container_on_nas()
                self.ui_queue.put(("workflow_done", ok))

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()

        def start_restart_container(self) -> None:
            if self.is_busy:
                return
            self._set_busy(True)

            def _worker() -> None:
                updater = HAUpdater(
                    self.cfg,
                    log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    progress_callback=lambda f, t: self.ui_queue.put(("progress", (f, t))),
                )
                ok = updater.restart_container_on_nas()
                self.ui_queue.put(("workflow_done", ok))

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()

        def start_prune_images(self) -> None:
            if self.is_busy:
                return
            if not messagebox.askyesno(
                self._t("prune_confirm_title"),
                self._t("prune_confirm_msg"),
                parent=self,
            ):
                return

            self._set_busy(True)

            def _worker() -> None:
                updater = HAUpdater(
                    self.cfg,
                    log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    progress_callback=lambda f, t: self.ui_queue.put(("progress", (f, t))),
                )
                ok, _ = updater.prune_old_images_on_nas()
                self.ui_queue.put(("workflow_done", ok))

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()

        def open_settings_window(self) -> None:
            win = ctk.CTkToplevel(self)
            win.title(self._t("settings_title"))
            win.geometry("740x690")
            win.transient(self)
            win.grab_set()
            win.grid_columnconfigure(1, weight=1)

            entries: dict[str, ctk.CTkEntry] = {}

            def add_row(
                row_idx: int,
                label_txt: str,
                key: str,
                val: str,
                show: str = "",
                browse_cmd: Optional[Callable[[], None]] = None,
                browse_txt: Optional[str] = None,
            ) -> None:
                lbl = ctk.CTkLabel(
                    win, text=label_txt, font=ctk.CTkFont(size=12, weight="bold")
                )
                lbl.grid(row=row_idx, column=0, padx=16, pady=5, sticky="w")

                ent = ctk.CTkEntry(win, show=show)
                ent.insert(0, val)
                ent.grid(row=row_idx, column=1, padx=(0, 12), pady=5, sticky="ew")
                entries[key] = ent

                if browse_cmd:
                    btn = ctk.CTkButton(
                        win,
                        text=browse_txt or self._t("btn_browse"),
                        width=105,
                        command=browse_cmd,
                    )
                    btn.grid(row=row_idx, column=2, padx=(0, 16), pady=5)

            def browse_key() -> None:
                p = filedialog.askopenfilename(
                    parent=win, title=self._t("dlg_select_key")
                )
                if p:
                    entries["ssh_key_path"].delete(0, "end")
                    entries["ssh_key_path"].insert(0, os.path.normpath(p))

            def browse_smb() -> None:
                p = filedialog.asksaveasfilename(
                    parent=win,
                    title=self._t("dlg_select_tar"),
                    initialfile="ha_latest.tar",
                    defaultextension=".tar",
                    filetypes=[
                        (self._t("filetype_tar"), "*.tar"),
                        (self._t("filetype_all"), "*.*"),
                    ],
                )
                if p:
                    entries["smb_path"].delete(0, "end")
                    entries["smb_path"].insert(0, os.path.normpath(p))

            add_row(0, self._t("lbl_nas_ip"), "nas_ip", self.cfg.nas_ip)
            add_row(1, self._t("lbl_ssh_port"), "ssh_port", str(self.cfg.ssh_port))
            add_row(2, self._t("lbl_ssh_user"), "ssh_user", self.cfg.ssh_user)
            add_row(
                3,
                self._t("lbl_ssh_key"),
                "ssh_key_path",
                self.cfg.ssh_key_path,
                browse_cmd=browse_key,
                browse_txt=self._t("btn_browse"),
            )
            add_row(
                4,
                self._t("lbl_ssh_pw"),
                "ssh_password",
                self.cfg.ssh_password,
                show="*",
            )
            add_row(
                5,
                self._t("lbl_smb_path"),
                "smb_path",
                self.cfg.smb_path,
                browse_cmd=browse_smb,
                browse_txt=self._t("btn_choose_path"),
            )
            add_row(6, self._t("lbl_nas_tar"), "nas_tar_path", self.cfg.nas_tar_path)
            add_row(
                7, self._t("lbl_nas_docker"), "nas_docker_bin", self.cfg.nas_docker_bin
            )
            add_row(8, self._t("lbl_image_name"), "image_name", self.cfg.image_name)
            add_row(
                9, self._t("lbl_image_plat"), "image_platform", self.cfg.image_platform
            )
            add_row(
                10,
                self._t("lbl_cont_name"),
                "ha_container_name",
                self.cfg.ha_container_name,
            )
            add_row(
                11,
                self._t("lbl_recreate_mode"),
                "recreate_mode",
                self.cfg.recreate_mode,
            )
            add_row(
                12,
                self._t("lbl_compose_path"),
                "nas_compose_path",
                self.cfg.nas_compose_path,
            )

            sudo_var = tk.BooleanVar(value=self.cfg.use_sudo)
            chk_sudo = ctk.CTkSwitch(
                win,
                text=self._t("sw_use_sudo"),
                variable=sudo_var,
            )
            chk_sudo.grid(
                row=13, column=0, columnspan=3, padx=16, pady=(10, 4), sticky="w"
            )

            auto_rec_var = tk.BooleanVar(value=self.cfg.auto_recreate_container)
            chk_auto_rec = ctk.CTkSwitch(
                win,
                text=self._t("sw_auto_recreate"),
                variable=auto_rec_var,
            )
            chk_auto_rec.grid(
                row=14, column=0, columnspan=3, padx=16, pady=(4, 4), sticky="w"
            )

            strict_host_var = tk.BooleanVar(value=self.cfg.ssh_strict_host_key)
            chk_strict_host = ctk.CTkSwitch(
                win,
                text=self._t("sw_strict_host"),
                variable=strict_host_var,
            )
            chk_strict_host.grid(
                row=15, column=0, columnspan=3, padx=16, pady=(4, 8), sticky="w"
            )

            def save_settings() -> None:
                try:
                    port_val = int(entries["ssh_port"].get().strip())
                    if not (1 <= port_val <= 65535):
                        raise ValueError("Port out of range")
                except ValueError:
                    messagebox.showerror(
                        self._t("err_invalid_port_title"),
                        self._t("err_invalid_port_msg"),
                        parent=win,
                    )
                    return

                self.cfg.language = self._lang
                self.cfg.nas_ip = entries["nas_ip"].get().strip()
                self.cfg.ssh_port = port_val
                self.cfg.ssh_user = entries["ssh_user"].get().strip()
                self.cfg.ssh_key_path = entries["ssh_key_path"].get().strip()
                self.cfg.ssh_password = entries["ssh_password"].get().strip()
                self.cfg.smb_path = entries["smb_path"].get().strip()
                self.cfg.nas_tar_path = entries["nas_tar_path"].get().strip()
                self.cfg.nas_docker_bin = entries["nas_docker_bin"].get().strip()
                self.cfg.image_name = entries["image_name"].get().strip()
                self.cfg.image_platform = entries["image_platform"].get().strip()
                self.cfg.ha_container_name = entries["ha_container_name"].get().strip()
                rec_mode = entries["recreate_mode"].get().strip().lower()
                self.cfg.recreate_mode = (
                    rec_mode if rec_mode in ("inspect", "compose") else "inspect"
                )
                self.cfg.nas_compose_path = entries["nas_compose_path"].get().strip()
                self.cfg.use_sudo = bool(sudo_var.get())
                self.cfg.auto_recreate_container = bool(auto_rec_var.get())
                self.cfg.ssh_strict_host_key = bool(strict_host_var.get())

                self.cfg.save_to_env()
                self.lbl_subtitle.configure(text=self._format_subtitle())
                self.append_log("INFO", self._t("log_cfg_reloaded"))
                win.destroy()
                self.start_status_check()

            btn_bar = ctk.CTkFrame(win, fg_color="transparent")
            btn_bar.grid(row=16, column=0, columnspan=3, padx=16, pady=14, sticky="e")

            ctk.CTkButton(
                btn_bar,
                text=self._t("btn_cancel"),
                fg_color="#475569",
                width=110,
                command=win.destroy,
            ).pack(side="right", padx=(8, 0))
            ctk.CTkButton(
                btn_bar,
                text=self._t("btn_save"),
                fg_color="#16A34A",
                hover_color="#15803D",
                width=205,
                command=save_settings,
            ).pack(side="right")

        def open_permissions_help_window(self) -> None:
            win = ctk.CTkToplevel(self)
            win.title(self._t("perm_win_title"))
            win.geometry("820x660")
            win.transient(self)
            win.grab_set()
            win.grid_columnconfigure(0, weight=1)
            win.grid_rowconfigure(1, weight=1)

            lbl = ctk.CTkLabel(
                win,
                text=self._t("perm_header"),
                font=ctk.CTkFont(size=16, weight="bold"),
            )
            lbl.grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")

            txt = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12))
            txt.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="nsew")
            txt.insert("1.0", self._t("guide_text"))
            txt.configure(state="disabled")

            auto_frame = ctk.CTkFrame(win, corner_radius=10, fg_color="#1E293B")
            auto_frame.grid(row=2, column=0, padx=16, pady=(0, 14), sticky="ew")
            auto_frame.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                auto_frame,
                text=self._t("perm_pw_label"),
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")

            pw_entry = ctk.CTkEntry(
                auto_frame,
                show="*",
                placeholder_text=self._t("perm_pw_placeholder"),
            )
            pw_entry.grid(row=0, column=1, padx=8, pady=12, sticky="ew")

            btn_auto_setup = ctk.CTkButton(
                auto_frame,
                text=self._t("perm_auto_btn"),
                fg_color="#16A34A",
                hover_color="#15803D",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            btn_auto_setup.grid(row=0, column=2, padx=8, pady=12)

            def run_auto_setup() -> None:
                pw = pw_entry.get().strip()
                if not pw:
                    messagebox.showwarning(
                        self._t("perm_pw_req_title"),
                        self._t("perm_pw_req_msg"),
                        parent=win,
                    )
                    return

                btn_auto_setup.configure(
                    state="disabled", text=self._t("perm_auto_busy")
                )

                def _prov_worker() -> None:
                    updater = HAUpdater(
                        self.cfg,
                        log_callback=lambda l, m: self.ui_queue.put(("log", (l, m))),
                    )
                    ok, msg = updater.provision_synology_nas(pw)

                    def _finish() -> None:
                        if win.winfo_exists():
                            btn_auto_setup.configure(
                                state="normal",
                                text=self._t("perm_auto_btn"),
                            )
                        if ok:
                            messagebox.showinfo(
                                self._t("perm_done_title"), msg, parent=win
                            )
                            if win.winfo_exists():
                                win.destroy()
                            self.start_status_check()
                        else:
                            messagebox.showerror(
                                self._t("perm_fail_title"), msg, parent=win
                            )

                    self.after(0, _finish)

                threading.Thread(target=_prov_worker, daemon=True).start()

            btn_auto_setup.configure(command=run_auto_setup)

            ctk.CTkButton(
                auto_frame,
                text=self._t("perm_close_btn"),
                width=90,
                fg_color="#475569",
                command=win.destroy,
            ).grid(row=0, column=3, padx=(0, 12), pady=12)

    app = HAUpdaterApp(config)
    app.mainloop()


# ==============================================================================
# Logging & CLI / Scheduled Task Entrypoint
# ==============================================================================
def setup_logging(log_file: str) -> None:
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parent / log_path

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Home Assistant Synology DSM 7.1 Image & Container Updater (GUI & CLI)"
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run unattended without GUI (ideal for Windows Task Scheduler).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force pull, tar export, and docker load even if the digest matches.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check remote vs. Synology NAS status in the console and exit.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Explicitly launch the Graphical Desktop Interface.",
    )
    parser.add_argument(
        "--lang",
        choices=["en", "nl"],
        help="Override interface/log language ('en' = English, 'nl' = Dutch).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = AppConfig.from_env()
    if args.lang in ("en", "nl"):
        config.language = args.lang
    setup_logging(config.log_file)

    if args.check:
        updater = HAUpdater(config)
        st = updater.check_status()
        logger.info("Checked at        : %s", st.checked_at)
        logger.info("Local Docker OK   : %s", st.local_docker_ok)
        logger.info("SMB Directory OK  : %s", st.smb_dir_ok)
        logger.info("Synology SSH OK   : %s", st.ssh_ok)
        logger.info("Remote Digest     : %s", st.remote_digest or "N/A")
        logger.info("Remote Image ID   : %s", st.remote_image_id or "N/A")
        logger.info("NAS Image ID      : %s", st.nas_image_id or "N/A")
        logger.info("NAS Digests       : %s", ", ".join(st.nas_repo_digests) or "None")
        logger.info(
            "HA Container      : exists=%s, running=%s, status=%s, image=%s, needs_recreate=%s",
            st.nas_container_exists,
            st.nas_container_running,
            st.nas_container_status or "N/A",
            (st.nas_container_image_id[:19] if st.nas_container_image_id else "N/A"),
            st.container_needs_recreate,
        )
        logger.info("Update Needed     : %s (%s)", st.needs_update, st.reason)
        return 0 if st.ssh_ok else 1

    if args.auto:
        updater = HAUpdater(config)
        success = updater.run_update_workflow(force=args.force)
        return 0 if success else 1

    launch_gui(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())


