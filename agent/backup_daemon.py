"""Backup daemon — scans whitelisted folders, computes diff vs Google Drive,
uploads when the machine is idle. Auto-pauses on user activity.

CLI:
    python -m agent.backup_daemon status     # show idle seconds, last scan
    python -m agent.backup_daemon scan       # scan local + write index
    python -m agent.backup_daemon dry-run    # scan + diff + print what would upload
    python -m agent.backup_daemon serve      # run forever: idle-wait, scan, (upload — when OAuth ready)

State files (next to agent.log):
    backup_index.json   — local file index {path: {size, mtime, sha64}}
    backup.log          — operations log
    google_token.json   — OAuth token (created on first auth) — SEPARATE SESSION B

Whitelist / blacklist live in DEFAULT_WHITELIST / DEFAULT_BLACKLIST below.
Override via config.json → "backup": {"whitelist": [...], "blacklist": [...]}.
"""
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

# ── Paths (resolved against DATA_DIR — same logic as Electron main.js) ────────

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "backup_index.json"
CONFIG_PATH = ROOT / "config.json"
BACKUP_LOG = ROOT / "backup.log"

logger = logging.getLogger("backup")
if not logger.handlers:
    # Rotate at 5 MB, keep 3 old files (backup.log, backup.log.1 … .3).
    h = RotatingFileHandler(BACKUP_LOG, maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# ── Defaults ──────────────────────────────────────────────────────────────────

HOME = Path.home()

DEFAULT_WHITELIST: list[str] = [
    str(HOME / "Documents"),
    str(HOME / "Desktop"),
    str(ROOT / "config.json"),
    str(ROOT / "workflow_fixed.json"),
    str(ROOT / "agent.log"),
    # vault from obsidian config (added dynamically in load_config_whitelist)
]

DEFAULT_BLACKLIST: list[str] = [
    # System
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "C:\\ProgramData",
    # Caches / build artifacts
    "**/node_modules/**", "**/__pycache__/**", "**/.venv/**", "**/venv/**",
    "**/dist/**", "**/build/**", "**/*.egg-info/**",
    "**/.git/**", "**/.idea/**", "**/.vscode/**",
    # Trash / temp
    "**/~$*", "**/*.tmp", "**/*.bak", "**/Thumbs.db", "**/.DS_Store",
    # Downloads — explicitly skipped (huge + temporary)
    str(HOME / "Downloads"),
]

# Don't try to back up these patterns inside whitelisted folders either
SKIP_EXTENSIONS = {".iso", ".vhd", ".vhdx", ".vmdk"}

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB — anything larger requires explicit opt-in

# ── Idle detection (Windows) ──────────────────────────────────────────────────

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last user input (keyboard/mouse). Windows-only.

    Returns 0.0 on non-Windows or if the syscall fails — calling code should
    treat that as "not idle" to be safe.
    """
    if sys.platform != "win32":
        return 0.0
    try:
        info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        return 0.0


# ── Config-driven whitelist/blacklist ─────────────────────────────────────────

def load_config_whitelist() -> tuple[list[str], list[str]]:
    """Read config.json, return (whitelist, blacklist) — defaults + overrides + obsidian vault."""
    wl = list(DEFAULT_WHITELIST)
    bl = list(DEFAULT_BLACKLIST)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return wl, bl

    backup = cfg.get("backup") or {}
    wl.extend(p for p in backup.get("whitelist", []) if p)
    bl.extend(p for p in backup.get("blacklist", []) if p)

    # auto-include obsidian vault if configured
    obs_path = (cfg.get("obsidian") or {}).get("vault_path")
    if obs_path:
        wl.append(obs_path)

    # dedupe, preserve order
    return _dedupe(wl), _dedupe(bl)


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        nx = str(Path(x)) if not any(c in x for c in "*?[") else x
        if nx not in seen:
            seen.add(nx)
            out.append(x)
    return out


# ── Path matching ─────────────────────────────────────────────────────────────

def _is_under(path: Path, root: str) -> bool:
    """True if `path` is the same as or under `root` (no glob)."""
    try:
        rp = Path(root).resolve()
        ap = path.resolve()
        return ap == rp or rp in ap.parents
    except Exception:
        return False


def _matches_any(path_str: str, patterns: Iterable[str]) -> bool:
    for pat in patterns:
        if any(c in pat for c in "*?["):
            if fnmatch.fnmatch(path_str.replace("\\", "/"), pat.replace("\\", "/")):
                return True
        else:
            try:
                if _is_under(Path(path_str), pat):
                    return True
            except Exception:
                pass
    return False


def is_skipped(path: Path, blacklist: list[str]) -> bool:
    if path.suffix.lower() in SKIP_EXTENSIONS:
        return True
    return _matches_any(str(path), blacklist)


# ── Scanner ───────────────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: str
    size: int
    mtime: float
    sha64: str          # SHA-256 of first 64 KB — fast fingerprint

    @classmethod
    def from_path(cls, p: Path) -> "FileEntry | None":
        try:
            st = p.stat()
            if not p.is_file():
                return None
            # quick partial hash for change detection (full hash only at upload time)
            with open(p, "rb") as fh:
                head = fh.read(64 * 1024)
            sha = hashlib.sha256(head).hexdigest()[:16]
            return cls(
                path=str(p),
                size=st.st_size,
                mtime=st.st_mtime,
                sha64=sha,
            )
        except (PermissionError, FileNotFoundError, OSError):
            return None


def walk(root: Path, blacklist: list[str]) -> Iterable[Path]:
    """Yield files under `root`, skipping blacklisted dirs as we go."""
    if root.is_file():
        if not is_skipped(root, blacklist):
            yield root
        return

    if not root.exists() or not root.is_dir():
        return

    # os.walk is faster than rglob and lets us prune the tree
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        # prune blacklisted subdirs in place
        dirnames[:] = [d for d in dirnames if not is_skipped(dp / d, blacklist)]
        for name in filenames:
            f = dp / name
            if not is_skipped(f, blacklist):
                yield f


def scan(whitelist: list[str], blacklist: list[str], max_files: int | None = None) -> dict[str, FileEntry]:
    """Build {path: FileEntry} index for all whitelisted roots."""
    index: dict[str, FileEntry] = {}
    seen = 0
    for root in whitelist:
        for f in walk(Path(root), blacklist):
            entry = FileEntry.from_path(f)
            if entry is None:
                continue
            if entry.size > MAX_FILE_BYTES:
                logger.info("skip (>100 MB): %s", entry.path)
                continue
            index[entry.path] = entry
            seen += 1
            if max_files and seen >= max_files:
                logger.warning("scan: hit max_files=%d cap, truncating", max_files)
                return index
    return index


# ── Index persistence ─────────────────────────────────────────────────────────

def load_index() -> dict[str, FileEntry]:
    if not INDEX_PATH.exists():
        return {}
    try:
        raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return {k: FileEntry(**v) for k, v in raw.items()}
    except Exception as e:
        logger.error("index load failed: %s", e)
        return {}


def save_index(index: dict[str, FileEntry]) -> None:
    payload = {k: asdict(v) for k, v in index.items()}
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── Diff ──────────────────────────────────────────────────────────────────────

@dataclass
class Diff:
    new: list[str]           # in current, not in previous (or in Drive)
    changed: list[str]       # different size or sha64
    unchanged: list[str]     # same
    removed_local: list[str] # in previous, not in current
    total_bytes_new: int

    def summary(self) -> str:
        return (
            f"new: {len(self.new)} ({self.total_bytes_new/1_048_576:.1f} MB) · "
            f"changed: {len(self.changed)} · "
            f"unchanged: {len(self.unchanged)} · "
            f"removed locally: {len(self.removed_local)}"
        )


def diff(current: dict[str, FileEntry], previous: dict[str, FileEntry]) -> Diff:
    new, changed, unchanged, removed_local = [], [], [], []
    total_new_bytes = 0
    for path, cur in current.items():
        prev = previous.get(path)
        if prev is None:
            new.append(path)
            total_new_bytes += cur.size
        elif prev.size != cur.size or prev.sha64 != cur.sha64:
            changed.append(path)
            total_new_bytes += cur.size
        else:
            unchanged.append(path)
    for path in previous:
        if path not in current:
            removed_local.append(path)
    return Diff(new=new, changed=changed, unchanged=unchanged,
                removed_local=removed_local, total_bytes_new=total_new_bytes)


# ── Google Drive integration (Session B) ──────────────────────────────────────

DRIVE_FOLDER_NAME = "Dispatch"
DRIVE_TOKEN_PATH  = ROOT / "google_token.json"
DRIVE_CREDS_PATH  = ROOT / "drive_credentials.json"
DRIVE_SCOPES      = ["https://www.googleapis.com/auth/drive.file"]


def drive_authenticated() -> bool:
    """True if OAuth token file exists. Real validity check happens at upload time."""
    return DRIVE_TOKEN_PATH.exists()


def drive_setup_instructions() -> str:
    return (
        "Google Drive OAuth not yet configured. One-time setup:\n"
        "  1. console.cloud.google.com -> create project -> enable Drive API.\n"
        "  2. Create OAuth client (Desktop application), download client_secret.json.\n"
        f"  3. Save it as: {DRIVE_CREDS_PATH}\n"
        "  4. pip install google-auth-oauthlib google-api-python-client\n"
        "  5. python -m agent.backup_daemon auth   # opens browser for login\n"
        f"  6. Folder '{DRIVE_FOLDER_NAME}/' will be auto-created on Drive root."
    )


def _drive_service():
    """Build authenticated Drive v3 service. Returns None if not configured."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("missing deps: pip install google-auth-oauthlib google-api-python-client")
        return None

    creds = None
    if DRIVE_TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_PATH), DRIVE_SCOPES)
        except Exception as e:
            logger.error("token load failed: %s", e)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            DRIVE_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            logger.info("refreshed Drive token")
        except Exception as e:
            logger.error("token refresh failed: %s", e)
            creds = None

    if not creds or not creds.valid:
        if not DRIVE_CREDS_PATH.exists():
            logger.warning("no Drive credentials at %s", DRIVE_CREDS_PATH)
            return None
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(DRIVE_CREDS_PATH), DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
            DRIVE_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            logger.info("OAuth flow complete, token saved")
        except Exception as e:
            logger.error("OAuth flow failed: %s", e)
            return None

    try:
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error("build service failed: %s", e)
        return None


def drive_get_or_create_folder(svc, name: str = DRIVE_FOLDER_NAME, parent: str = "root") -> str:
    """Find folder by exact name under parent, or create it. Returns folder ID."""
    safe_name = name.replace("'", "\\'")
    q = (f"name='{safe_name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent}' in parents and trashed=false")
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    if res.get("files"):
        return res["files"][0]["id"]
    folder = svc.files().create(body={
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent],
    }, fields="id").execute()
    logger.info("created Drive folder %s (id=%s)", name, folder["id"])
    return folder["id"]


def drive_list_recursive(svc, folder_id: str) -> dict[str, dict]:
    """Recursively list files under folder_id. Returns {relative_path: {id, size, md5, modified}}."""
    out: dict[str, dict] = {}

    def walk(fid: str, path_prefix: str) -> None:
        page_token = None
        while True:
            res = svc.files().list(
                q=f"'{fid}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,size,md5Checksum,modifiedTime)",
                pageToken=page_token, pageSize=1000,
            ).execute()
            for f in res.get("files", []):
                child_path = f"{path_prefix}/{f['name']}" if path_prefix else f["name"]
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    walk(f["id"], child_path)
                else:
                    out[child_path] = {
                        "id":       f["id"],
                        "size":     int(f.get("size") or 0),
                        "md5":      f.get("md5Checksum"),
                        "modified": f.get("modifiedTime"),
                    }
            page_token = res.get("nextPageToken")
            if not page_token:
                break

    walk(folder_id, "")
    return out


def _local_to_drive_path(local_path: str, whitelist: list[str]) -> str:
    """Map E:\\dispatch\\config.json -> 'dispatch/config.json'; Documents/notes -> 'Documents/notes'."""
    p = Path(local_path).resolve()
    for root in whitelist:
        try:
            rp = Path(root).resolve()
        except Exception:
            continue
        if rp.is_file() and p == rp:
            return rp.name
        try:
            rel = p.relative_to(rp)
            return f"{rp.name}/{rel.as_posix()}"
        except ValueError:
            continue
    return p.name


def drive_upload_one(svc, dispatch_folder_id: str, local_path: str, drive_rel_path: str):
    """Upload one file as drive_rel_path inside Dispatch/. Creates subfolders as needed."""
    from googleapiclient.http import MediaFileUpload

    parts = drive_rel_path.split("/")
    folders, filename = parts[:-1], parts[-1]
    parent = dispatch_folder_id
    for fname in folders:
        if fname:
            parent = drive_get_or_create_folder(svc, fname, parent=parent)

    safe_fn = filename.replace("'", "\\'")
    q = f"name='{safe_fn}' and '{parent}' in parents and trashed=false"
    existing = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])

    media = MediaFileUpload(local_path, resumable=True)
    if existing:
        return svc.files().update(fileId=existing[0]["id"], media_body=media, fields="id").execute()
    return svc.files().create(
        body={"name": filename, "parents": [parent]},
        media_body=media, fields="id",
    ).execute()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli_status() -> int:
    wl, bl = load_config_whitelist()
    print(f"Idle: {idle_seconds():.1f} s")
    print(f"Whitelist ({len(wl)}):")
    for w in wl:
        print(f"  - {w}")
    print(f"Blacklist ({len(bl)} patterns)")
    print(f"Index:  {INDEX_PATH}  ({'exists' if INDEX_PATH.exists() else 'missing'})")
    print(f"Drive token: {DRIVE_TOKEN_PATH}  ({'present' if drive_authenticated() else 'missing'})")
    if not drive_authenticated():
        print()
        print(drive_setup_instructions())
    return 0


def _cli_scan(write: bool = True) -> int:
    wl, bl = load_config_whitelist()
    t0 = time.time()
    cur = scan(wl, bl)
    dt = time.time() - t0
    total_bytes = sum(e.size for e in cur.values())
    print(f"Scanned {len(cur)} files ({total_bytes/1_048_576:.1f} MB) in {dt:.1f}s")
    if write:
        save_index(cur)
        print(f"Saved index → {INDEX_PATH}")
    return 0


def _cli_dryrun() -> int:
    wl, bl = load_config_whitelist()
    previous = load_index()
    print(f"Previous index: {len(previous)} files")
    print("Scanning…")
    current = scan(wl, bl)
    d = diff(current, previous)
    print(d.summary())
    print()
    if d.new:
        print(f"NEW ({len(d.new)}):")
        for p in d.new[:20]:
            print(f"  + {p}")
        if len(d.new) > 20:
            print(f"  … +{len(d.new) - 20} more")
    if d.changed:
        print(f"CHANGED ({len(d.changed)}):")
        for p in d.changed[:20]:
            print(f"  ~ {p}")
        if len(d.changed) > 20:
            print(f"  … +{len(d.changed) - 20} more")
    if not drive_authenticated():
        print()
        print("⚠ Drive OAuth not set up — files would NOT actually upload.")
        print(drive_setup_instructions())
    return 0


def _cli_auth() -> int:
    """Run OAuth flow: opens browser, saves token, creates Dispatch/ folder."""
    print("Starting Google Drive OAuth flow...")
    svc = _drive_service()
    if svc is None:
        print()
        print(drive_setup_instructions())
        return 1
    fid = drive_get_or_create_folder(svc)
    print(f"OK. Token saved: {DRIVE_TOKEN_PATH}")
    print(f"Dispatch/ folder ID: {fid}")
    return 0


def _cli_drive_list() -> int:
    """List files currently in Dispatch/ on Drive."""
    svc = _drive_service()
    if svc is None:
        print(drive_setup_instructions())
        return 1
    fid = drive_get_or_create_folder(svc)
    files = drive_list_recursive(svc, fid)
    print(f"Drive Dispatch/ contains {len(files)} files")
    for p in list(files)[:20]:
        f = files[p]
        size_kb = f["size"] / 1024
        print(f"  {p}  {size_kb:>8.1f} KB")
    if len(files) > 20:
        print(f"  ... +{len(files) - 20} more")
    return 0


def _upload_plan(svc, fid: str, current: dict[str, FileEntry], whitelist: list[str]):
    """Compare local index with Drive listing. Returns list of (local_path, drive_rel_path, kind)."""
    drive_files = drive_list_recursive(svc, fid)
    plan: list[tuple[str, str, str]] = []
    for path, entry in current.items():
        rel = _local_to_drive_path(path, whitelist)
        existing = drive_files.get(rel)
        if existing is None:
            plan.append((path, rel, "new"))
        elif existing["size"] != entry.size:
            plan.append((path, rel, "changed"))
        # size match -> trust it (full md5 would re-read every file)
    return plan, drive_files


def _cli_upload(limit: int | None = None, dry: bool = False) -> int:
    """Upload local files to Drive Dispatch/. Pauses on user activity."""
    wl, bl = load_config_whitelist()
    svc = _drive_service()
    if svc is None:
        print(drive_setup_instructions())
        return 1

    fid = drive_get_or_create_folder(svc)
    print(f"Dispatch/ folder ID: {fid}")
    print("Scanning local...")
    current = scan(wl, bl)
    print(f"Local: {len(current)} files")
    print("Listing Drive...")
    plan, drive_files = _upload_plan(svc, fid, current, wl)
    print(f"Drive: {len(drive_files)} files")
    print(f"Need to upload: {len(plan)}")
    if dry:
        for path, rel, kind in plan[:30]:
            print(f"  {kind:>7}  {rel}")
        if len(plan) > 30:
            print(f"  ... +{len(plan) - 30} more")
        return 0

    n = limit or len(plan)
    uploaded = 0
    for i, (path, rel, kind) in enumerate(plan[:n], 1):
        # Idle-check every 5 files — pause if user is active.
        if i % 5 == 0 and idle_seconds() < 30:
            print(f"\nPaused: user active. Uploaded {uploaded}/{n}, will retry on next serve loop.")
            logger.info("upload paused at %d/%d (user activity)", uploaded, n)
            return 0
        try:
            drive_upload_one(svc, fid, path, rel)
            uploaded += 1
            logger.info("uploaded [%d/%d] %s: %s -> %s", uploaded, n, kind, path, rel)
            print(f"  [{uploaded}/{n}] {kind:>7}  {rel}")
        except Exception as e:
            logger.error("upload failed %s -> %s: %s", path, rel, e)
            print(f"  FAIL  {rel}: {e}")

    # Refresh local index — anything we uploaded is now "known"
    save_index(current)
    print(f"\nDone. Uploaded {uploaded} files. Local index refreshed.")
    return 0


def _cli_upload_dry() -> int:
    return _cli_upload(dry=True)


def _cli_serve() -> int:
    """Run forever: when idle > 5 min, scan + upload. Pause on activity."""
    print("Serving. Ctrl+C to stop.")
    IDLE_THRESHOLD = 300   # 5 min idle before starting
    while True:
        try:
            time.sleep(30)
            isec = idle_seconds()
            if isec < IDLE_THRESHOLD:
                continue
            print(f"[{time.strftime('%H:%M:%S')}] idle {isec:.0f}s -- starting cycle")
            logger.info("idle %.0fs -- starting cycle", isec)
            if not drive_authenticated():
                print("  skip: no Drive token (run 'auth' first)")
                logger.warning("serve: no token, skipping cycle")
                time.sleep(IDLE_THRESHOLD)  # don't busy-loop
                continue
            _cli_upload()
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as e:
            logger.error("serve loop error: %s", e)
            time.sleep(60)


COMMANDS = {
    "status":     _cli_status,
    "scan":       _cli_scan,
    "dry-run":    _cli_dryrun,
    "dryrun":     _cli_dryrun,
    "serve":      _cli_serve,
    "auth":       _cli_auth,
    "drive-list": _cli_drive_list,
    "upload":     _cli_upload,
    "upload-dry": _cli_upload_dry,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "status"
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(f"Unknown command: {cmd}\nAvailable: {', '.join(COMMANDS)}")
        return 1
    return fn()


if __name__ == "__main__":
    sys.exit(main())
