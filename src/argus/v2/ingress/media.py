"""Atomic blob store: temp write -> fsync -> checksum -> atomic rename -> caller
commits the row. A crash leaves at most an orphan temp file, never a row that
points at a missing/partial blob."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


def run_root() -> Path:
    return Path(os.environ.get("ARGUS_RUN_ROOT", "run")).expanduser()


def store_blob(src_path: str, *, event_id: str, kind: str) -> tuple[str, int, str]:
    media_dir = run_root() / "media" / event_id
    media_dir.mkdir(parents=True, exist_ok=True)
    data = Path(src_path).read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    final = media_dir / f"{checksum}-{Path(src_path).name}"
    fd, tmp = tempfile.mkstemp(dir=media_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)  # atomic on same filesystem
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return str(final), len(data), checksum
