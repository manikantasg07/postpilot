"""Safe JSON read/write helpers."""
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class DataFileError(ValueError):
    """A data file exists but is not readable JSON."""


def read_json(path: Path) -> Any:
    """Read one JSON file, naming it when it turns out not to be JSON.

    A half-written or hand-edited file used to raise a bare JSONDecodeError
    from deep inside a loader, which reached the browser as a 500 with a
    traceback and no way back to setup. Knowing *which* file is broken is the
    whole of the fix.
    """
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise DataFileError(
                f"{path.name} is not valid JSON (line {exc.lineno}): {exc.msg}. "
                "Fix or delete the file and the app will rebuild it."
            ) from exc


def write_atomic(path: Path, text: str) -> None:
    """Write text by swapping a finished file in, never by editing in place.

    A crash, a full disk or a power cut mid-write leaves the old file intact
    rather than a half-written one. The fsync matters: without it the rename
    can reach the disk before the bytes do, and the file comes back empty.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _replace(tmp: str, path: Path, tries: int = 5) -> None:
    """os.replace, with patience for Windows.

    An anti-virus scanner or the search indexer can hold the destination open
    for a moment, and the swap then fails with PermissionError on a file that
    is perfectly fine a heartbeat later.
    """
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def write_json(path: Path, data: Any) -> None:
    """Write one JSON file atomically."""
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
