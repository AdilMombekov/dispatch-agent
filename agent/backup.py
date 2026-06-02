"""Google Drive backup service.

Architecture
------------
Three independent pieces tied together by `BackupService`:

  IdleDetector  — polls Windows `GetLastInputInfo` to know when the user
                  is away from keyboard. Backup only runs while idle.
  FileScanner   — walks configured roots, applies exclude patterns,
                  builds an in-memory manifest (path → size, mtime).
  DriveClient   — OAuth via desktop installed-app flow; ensures a target
                  folder; uploads files with resumable chunks.

The service alternates between two cadences:
  - **scan**:   once per `scan_interval_hours` (default 24h) — rebuilds local
                manifest and diffs against the *remote* manifest written by the
                last upload pass; new/changed files enter the upload queue.
  - **upload**: while idle and queue non-empty — uploads one file at a time
                with resumable chunks. On user-activity, pause the chunk loop
                and wait until idle again. Already-uploaded chunks are not
                re-sent thanks to Drive's resumable session URL.

Disabled by default (`config.backup.enabled = false`). To turn on:
  1. Fill `backup.google_oauth.client_id` and `client_secret` from your
     desktop OAuth client (GCP Console → APIs & Services → Credentials).
  2. Add at least one path to `backup.roots`.
  3. Set `backup.enabled = true`.
  4. On first start the service opens a browser for consent and stores the
     refresh token at `DATA_DIR/drive_token.json`.

Trigger a backup manually via `BackupService.trigger()` (wired to the
"Запустить сейчас" button in Control Center) — that skips the idle gate.
"""
import ctypes
import fnmatch
import hashlib
import json
import logging
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from agent.paths import DATA_DIR

logger = logging.getLogger("backup")

# Files / dir patterns we never back up. Matched against any path component
# (so `.git` anywhere in the tree is skipped, not just at the root).
DEFAULT_EXCLUDES = [
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs",
    "dist", "build", "*.pyc", "*.tmp", "*.log", "AppData", "Cookies",
    "Local Settings", "$RECYCLE.BIN", "System Volume Information",
]

# What constitutes "idle" — no keyboard/mouse activity for at least N minutes.
DEFAULT_IDLE_MIN = 10
# Don't rebuild the local manifest more often than this when nothing triggered.
DEFAULT_SCAN_HOURS = 24
# Cap on a single file — anything bigger is skipped (Drive accepts huge files
# but we don't want a 50GB game install to monopolise the queue).
DEFAULT_MAX_MB = 200

MANIFEST_PATH = DATA_DIR / "backup_manifest.json"  # remote state (what's in Drive)
TOKEN_PATH    = DATA_DIR / "drive_token.json"      # OAuth refresh token


# ─── Idle detection (Windows) ─────────────────────────────────────────────────

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_seconds() -> float:
    """Seconds since the last keyboard / mouse event. 0 on non-Windows."""
    if sys.platform != "win32":
        return 0.0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (tick - info.dwTime) / 1000.0)


# ─── File scanner + diff ──────────────────────────────────────────────────────

def _is_excluded(path: Path, root: Path, patterns: list[str]) -> bool:
    """True iff any path component (relative to root) matches an exclude pattern."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = rel.parts
    for pat in patterns:
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
        # Patterns with separators (e.g. "AppData/Local/Temp") match contiguous segments
        if "/" in pat or "\\" in pat:
            norm = pat.replace("\\", "/")
            rel_norm = "/".join(parts)
            if norm in rel_norm:
                return True
    return False


def scan_root(root: Path, excludes: list[str], max_size: int) -> dict[str, dict]:
    """Walk `root` and return `{abs_path: {size, mtime}}`.

    Skips files matching excludes or over `max_size` bytes. Hashes are computed
    lazily on demand (the diff doesn't need them in the common case).
    """
    out: dict[str, dict] = {}
    if not root.exists():
        logger.warning(f"backup: root {root} not found, skipping")
        return out
    for p in root.rglob("*"):
        try:
            if _is_excluded(p, root, excludes):
                # Skip the whole subtree if a directory matched
                continue
            if not p.is_file():
                continue
            st = p.stat()
            if st.st_size > max_size:
                continue
            out[str(p)] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except (OSError, PermissionError):
            continue
    return out


def diff(local: dict[str, dict], remote: dict[str, dict]) -> list[str]:
    """Return paths that need upload — new locally, or size/mtime drifted.

    `remote` is the manifest of what we've already uploaded (DATA_DIR/backup_manifest.json),
    NOT a live Drive listing — we trust our own record so we don't pay a Drive list call
    every cycle. The cost of being wrong is a one-off re-upload, not data loss.
    """
    todo = []
    for path, meta in local.items():
        rem = remote.get(path)
        if not rem:
            todo.append(path)
            continue
        # mtime is in seconds; allow 2s slop for filesystems that round
        if rem.get("size") != meta["size"] or abs(rem.get("mtime", 0) - meta["mtime"]) > 2:
            todo.append(path)
    return todo


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 — used only as a tie-breaker on suspicious diffs."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


# ─── Drive client (OAuth + upload) ────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveAuthError(RuntimeError):
    pass


class DriveClient:
    """Thin wrapper over the Google Drive API.

    Lazily imports `googleapiclient` etc. so importing this module doesn't
    require those libs to be installed (the service no-ops with a clear log
    line when deps are missing).
    """

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise DriveAuthError(
                "Google OAuth client_id/client_secret not configured "
                "(config.backup.google_oauth).")
        self._client_id = client_id
        self._client_secret = client_secret
        self._service = None
        self._folder_cache: dict[tuple, str] = {}

    def authenticate(self) -> None:
        """Load creds from disk, refresh if expired, or run interactive flow."""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if TOKEN_PATH.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            except Exception as e:
                logger.warning(f"backup: token load failed, re-auth: {e}")
                creds = None
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"backup: refresh failed, re-auth: {e}")
                creds = None
        if not creds or not creds.valid:
            # First-run interactive consent. Opens a local browser. The user
            # has to click through once; the resulting refresh_token covers
            # the lifetime of the install.
            cfg = {
                "installed": {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
            creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            logger.info(f"backup: stored refresh token at {TOKEN_PATH}")
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find folder by name+parent, create if missing, return its Drive id."""
        key = (name, parent_id)
        if key in self._folder_cache:
            return self._folder_cache[key]
        q_parts = [
            f"name = '{name.replace(chr(39), chr(92)+chr(39))}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
        ]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        resp = self._service.files().list(
            q=" and ".join(q_parts), fields="files(id, name)", pageSize=10,
        ).execute()
        files = resp.get("files", [])
        if files:
            fid = files[0]["id"]
        else:
            body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if parent_id:
                body["parents"] = [parent_id]
            created = self._service.files().create(body=body, fields="id").execute()
            fid = created["id"]
            logger.info(f"backup: created Drive folder '{name}' ({fid})")
        self._folder_cache[key] = fid
        return fid

    def upload(self, local_path: Path, parent_id: str,
               on_progress=None, abort_event: threading.Event | None = None) -> str | None:
        """Resumable upload. Returns Drive file id, or None on abort."""
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(local_path), resumable=True, chunksize=8 * 1024 * 1024)
        body = {"name": local_path.name, "parents": [parent_id]}
        request = self._service.files().create(body=body, media_body=media, fields="id")
        response = None
        while response is None:
            if abort_event and abort_event.is_set():
                logger.info(f"backup: upload aborted mid-file ({local_path.name})")
                return None
            status, response = request.next_chunk()
            if status and on_progress:
                try:
                    on_progress(status.resumable_progress, status.total_size)
                except Exception:
                    pass
        return response.get("id")


# ─── Manifest persistence ─────────────────────────────────────────────────────

def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"backup: manifest load failed: {e}")
        return {}


def save_manifest(manifest: dict) -> None:
    try:
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"backup: manifest save failed: {e}")


# ─── Backup service (orchestrator) ────────────────────────────────────────────

class BackupService:
    """Drives the scan → diff → upload loop. Runs in a daemon thread.

    Public surface (called from main.py / Telegram bot / IPC):
      start() / stop()
      trigger()         — request a scan+upload cycle now (skips idle gate)
      authenticate()    — force a re-auth (e.g. after user edits client_id)
      status()          — snapshot dict for the Control Center UI
    """

    def __init__(self, get_config):
        # `get_config` is a zero-arg callable returning the latest config dict.
        # Reading it fresh on each tick lets the user toggle backup.enabled or
        # edit roots without restarting the agent.
        self._get_config = get_config
        self._stop = threading.Event()
        self._activity = threading.Event()  # set when user is active → pause upload
        self._force = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._status = "stopped"
        self._last_scan_at: float = 0.0
        self._last_upload_at: float = 0.0
        self._queue: list[str] = []
        self._current: str | None = None
        self._progress = {"bytes_done": 0, "bytes_total": 0}
        self._counts = {"uploaded": 0, "failed": 0, "skipped": 0, "total_to_upload": 0}
        self._last_error: str | None = None

        self._drive: DriveClient | None = None
        self._target_folder_id: str | None = None

    # ── public surface ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="BackupService")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._force.set()  # unblock any wait
        self._activity.set()

    def trigger(self) -> None:
        """Force a scan+upload cycle now."""
        self._force.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "queue_len": len(self._queue),
                "current_file": self._current,
                "progress": dict(self._progress),
                "counts": dict(self._counts),
                "last_scan_at": self._last_scan_at,
                "last_upload_at": self._last_upload_at,
                "last_error": self._last_error,
                "authenticated": TOKEN_PATH.exists(),
            }

    def authenticate(self) -> dict:
        """Run/refresh OAuth flow. Returns {ok, error?}."""
        try:
            cfg = (self._get_config().get("backup") or {})
            oauth = cfg.get("google_oauth") or {}
            client = DriveClient(oauth.get("client_id", ""), oauth.get("client_secret", ""))
            client.authenticate()
            self._drive = client
            return {"ok": True}
        except DriveAuthError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.error(f"backup auth failed: {e}")
            return {"ok": False, "error": str(e)}

    # ── main loop ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._set_status("idle")
        while not self._stop.is_set():
            cfg = (self._get_config().get("backup") or {})
            if not cfg.get("enabled"):
                # Wait quietly until enabled — recheck every 30s.
                if self._stop.wait(30):
                    break
                continue

            now = time.time()
            scan_interval_s = float(cfg.get("scan_interval_hours", DEFAULT_SCAN_HOURS)) * 3600
            need_scan = self._force.is_set() or (now - self._last_scan_at) > scan_interval_s

            if need_scan:
                try:
                    self._do_scan(cfg)
                except Exception as e:
                    logger.error(f"backup scan failed: {e}")
                    self._last_error = f"scan: {e}"

            # Upload phase — gated on idleness unless force was set.
            idle_threshold = float(cfg.get("idle_minutes", DEFAULT_IDLE_MIN)) * 60
            forced = self._force.is_set()
            idle = get_idle_seconds() >= idle_threshold
            if self._queue and (forced or idle):
                try:
                    self._do_upload_pass(cfg)
                except Exception as e:
                    logger.error(f"backup upload failed: {e}")
                    self._last_error = f"upload: {e}"

            self._force.clear()

            # Sleep responsively — the trigger can wake us up early.
            self._set_status("idle" if not self._queue else "queued")
            self._force.wait(timeout=60)
            self._force.clear()

        self._set_status("stopped")

    def _do_scan(self, cfg: dict) -> None:
        self._set_status("scanning")
        roots = [Path(r) for r in (cfg.get("roots") or []) if r]
        excludes = list(cfg.get("exclude_patterns") or DEFAULT_EXCLUDES)
        max_size = int(cfg.get("max_file_mb", DEFAULT_MAX_MB)) * 1024 * 1024
        if not roots:
            logger.info("backup: no roots configured — skip scan")
            self._last_scan_at = time.time()
            return
        local: dict[str, dict] = {}
        for root in roots:
            local.update(scan_root(root, excludes, max_size))
        remote = load_manifest()
        todo = diff(local, remote)
        with self._lock:
            self._queue = todo
            self._counts["total_to_upload"] = len(todo)
            self._counts["uploaded"] = 0
            self._counts["failed"] = 0
            self._last_scan_at = time.time()
        logger.info(f"backup: scanned {len(local)} files, {len(todo)} to upload")

    def _do_upload_pass(self, cfg: dict) -> None:
        # Need an authenticated client + a target folder before we start a pass.
        if self._drive is None:
            r = self.authenticate()
            if not r.get("ok"):
                self._last_error = r.get("error") or "auth failed"
                logger.warning(f"backup: auth needed before upload: {self._last_error}")
                return
        if self._target_folder_id is None:
            try:
                folder_name = cfg.get("drive_folder_name") or "Dispatch"
                # Optional: parent folder by id (from config) for nesting under a shared drive
                parent = cfg.get("drive_folder_id") or None
                self._target_folder_id = self._drive.ensure_folder(folder_name, parent)
            except Exception as e:
                self._last_error = f"folder: {e}"
                logger.error(f"backup: ensure_folder failed: {e}")
                return

        self._set_status("uploading")
        idle_threshold = float(cfg.get("idle_minutes", DEFAULT_IDLE_MIN)) * 60
        abort = threading.Event()

        # While in this pass, watch user activity in a side thread; flip abort
        # when the user starts using the machine again (unless this was forced).
        forced = self._force.is_set()
        watcher_stop = threading.Event()
        def _watch_activity():
            while not watcher_stop.is_set():
                if not forced and get_idle_seconds() < idle_threshold:
                    abort.set()
                    return
                time.sleep(2)
        threading.Thread(target=_watch_activity, daemon=True, name="BackupIdleWatcher").start()

        try:
            remote = load_manifest()
            with self._lock:
                queue_snapshot = list(self._queue)
            for path_s in queue_snapshot:
                if self._stop.is_set() or abort.is_set():
                    logger.info("backup: pass interrupted")
                    break
                path = Path(path_s)
                if not path.exists():
                    with self._lock:
                        self._counts["skipped"] += 1
                        self._queue.remove(path_s)
                    continue
                with self._lock:
                    self._current = path_s
                    try:
                        st = path.stat()
                        self._progress = {"bytes_done": 0, "bytes_total": st.st_size}
                    except OSError:
                        self._progress = {"bytes_done": 0, "bytes_total": 0}

                def _prog(done, total):
                    with self._lock:
                        self._progress = {"bytes_done": int(done), "bytes_total": int(total or 0)}

                try:
                    fid = self._drive.upload(path, self._target_folder_id, _prog, abort)
                except Exception as e:
                    logger.warning(f"backup: upload {path.name} failed: {e}")
                    with self._lock:
                        self._counts["failed"] += 1
                    continue
                if fid is None:
                    # aborted mid-file; leave it in the queue for the next idle window
                    break
                # Successful upload — record in remote manifest and remove from queue
                try:
                    st = path.stat()
                    remote[path_s] = {"size": st.st_size, "mtime": int(st.st_mtime),
                                       "drive_id": fid, "uploaded_at": int(time.time())}
                except OSError:
                    pass
                with self._lock:
                    self._counts["uploaded"] += 1
                    if path_s in self._queue:
                        self._queue.remove(path_s)
                    self._last_upload_at = time.time()
                    self._current = None
                save_manifest(remote)
        finally:
            watcher_stop.set()
            self._set_status("idle" if not self._queue else "queued")

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
