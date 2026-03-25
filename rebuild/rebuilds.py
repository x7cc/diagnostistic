#!/usr/bin/env python3
"""
Diagnostic Rebuild Tool
Reassembles a zip archive from chunk files using a manifest produced
by github_diagnose.py.

Usage:
    python rebuilds.py                          # looks for manifest.json in same directory
    python rebuilds.py manifest_20260325.json   # explicit manifest path
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# Must match BASE_OUTPUT in github_diagnose.py
BASE_OUTPUT = Path(os.getenv("PROGRAMDATA") or Path.home()) / "WinStore"


def _find_manifest(name: str) -> Optional[Path]:
    """Search for the manifest next to this script, cwd, then BASE_OUTPUT."""
    candidates = [
        Path(name).resolve(),                        # absolute or relative to cwd
        Path(__file__).parent / name,                # same folder as rebuilds.py
        BASE_OUTPUT / name,                          # where github_diagnose.py saves it
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def rebuild_zip(manifest_file: str = "manifest.json") -> bool:
    manifest_path = _find_manifest(manifest_file)

    if manifest_path is None:
        print(f"[ERROR] Manifest '{manifest_file}' not found.")
        print(f"        Searched: cwd, script folder, and {BASE_OUTPUT}")
        return False

    print(f"[INFO] Using manifest: {manifest_path}")

    # All paths are resolved relative to the manifest's directory
    base_dir = manifest_path.parent

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[ERROR] Failed to read manifest: {exc}")
        return False

    output_name = manifest.get("final_zip_name")
    chunks      = manifest.get("chunks")
    total       = manifest.get("total_chunks")

    if not output_name:
        print("[ERROR] Manifest is missing 'final_zip_name'.")
        return False
    if not chunks or not isinstance(chunks, list):
        print("[ERROR] Manifest is missing or has invalid 'chunks'.")
        return False

    # Warn if total_chunks doesn't match what we actually have listed
    if total is not None and total != len(chunks):
        print(f"[WARN] Manifest says {total} chunks but lists {len(chunks)} — may be incomplete.")

    # Verify all chunks exist before starting
    missing = [c for c in chunks if not (base_dir / c).exists()]
    if missing:
        for m in missing:
            print(f"[ERROR] Missing chunk: {m}")
        return False

    output_path = base_dir / output_name

    print(f"[INFO] Rebuilding {len(chunks)} chunk(s) → {output_path.name}")
    try:
        with open(output_path, "wb") as outfile:
            for chunk_name in chunks:
                chunk_path = base_dir / chunk_name
                print(f"  Merging {chunk_name} ...")
                with open(chunk_path, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)
    except Exception as exc:
        if output_path.exists():
            output_path.unlink()
        print(f"[ERROR] Rebuild failed: {exc}")
        return False

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n[OK] Rebuild complete: {output_path}  ({size_mb:.2f} MB)")
    return True


if __name__ == "__main__":
    manifest_arg = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    success = rebuild_zip(manifest_arg)
    raise SystemExit(0 if success else 1)
