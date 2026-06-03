"""File-safety guard (SAFETY.1) — no hard deletes.

Instead of deleting, files/dirs are MOVED into a trash backup folder with a
manifest, so anything an agent "deletes" is recoverable. Two parts:

  safe_delete(path, reason)  — move into TRASH_DIR/<timestamp>/… + manifest line
  detect_destructive(cmd)    — flag shell commands that would hard-delete, so the
                               terminal handler can refuse and route here instead

TRASH_DIR: config "trash_dir" if set (point it at a backup disk), else
DATA_DIR/trash.

Limitation: this protects the agent's own paths (terminal handler, /rm). It can
NOT intercept deletes done inside external processes (claude -p, computer-use
clicks) — those are gated separately by the SonnetReviewer and FAILSAFE.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent.paths import DATA_DIR


def _trash_root() -> Path:
    root = None
    try:
        from agent.config import load_config
        cfg_val = (load_config().get("trash_dir") or "").strip()
        if cfg_val:
            root = Path(cfg_val)
    except Exception:
        root = None
    if root is None:
        root = DATA_DIR / "trash"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_path() -> Path:
    return _trash_root() / "manifest.jsonl"


def _next_id() -> int:
    mp = _manifest_path()
    if not mp.exists():
        return 1
    try:
        with mp.open(encoding="utf-8") as f:
            return sum(1 for _ in f) + 1
    except Exception:
        return int(time.time())


def safe_delete(path: str | Path, reason: str = "") -> dict:
    """Move `path` into the trash backup instead of deleting. Returns the
    manifest entry. Raises FileNotFoundError if the path doesn't exist."""
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(str(src))

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bucket = _trash_root() / stamp
    # Preserve the original location under the bucket: drop the drive colon so
    # "E:\proj\a.txt" -> "<bucket>\E\proj\a.txt".
    rel = str(src).replace(":", "", 1)
    rel = rel.lstrip("\\/")
    dst = bucket / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    entry = {
        "id": _next_id(),
        "original": str(src),
        "trashed": str(dst),
        "ts": time.time(),
        "reason": reason,
    }
    with _manifest_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_trash(limit: int = 20) -> list[dict]:
    mp = _manifest_path()
    if not mp.exists():
        return []
    out: list[dict] = []
    try:
        with mp.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        return []
    return out[-limit:][::-1]  # newest first


def restore(trash_id: int) -> Optional[str]:
    """Move a trashed item back to its original location. Returns the restored
    path, or None if not found / original location is occupied."""
    for entry in list_trash(limit=100000):
        if entry.get("id") == trash_id:
            trashed = Path(entry["trashed"])
            original = Path(entry["original"])
            if not trashed.exists():
                return None
            if original.exists():
                return None  # don't clobber a file that's back in place
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trashed), str(original))
            return str(original)
    return None


# Shell tokens that hard-delete or wipe. Tuned to catch real deletes without
# false-flagging benign commands like "git format-patch" or "npm rm".
_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-[a-z]*[rf]",      # rm -rf / -r / -f / -fr
    r"\brmdir\b", r"\brd\s+/s",  # rmdir, rd /s
    r"\bdel\s", r"\berase\s",    # del/erase <something>
    r"\bremove-item\b", r"\bremove-itemproperty\b",
    r"\bformat\s+[a-z]:",        # format c:
    r"\bmkfs", r"\bclear-content\b", r"\bcipher\s+/w", r"\bdiskpart\b",
]
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def detect_destructive(cmd: str) -> Optional[str]:
    """Return the matched destructive token if `cmd` looks like a hard-delete /
    wipe, else None."""
    if not cmd:
        return None
    m = _DESTRUCTIVE_RE.search(cmd)
    return m.group(0) if m else None
