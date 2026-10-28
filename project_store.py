"""Crash-safe files and per-album transactions, shared by HTTP and direct callers."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading


class ProjectConflict(ValueError):
    pass


_locks = {}
_guard = threading.Lock()
_held = threading.local()


@contextmanager
def project_lock(path: Path):
    key = str(path.resolve())
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "paths", set())
        if key in held:
            yield
            return
        # Lock a stable sibling: the JSON inode changes on each atomic replace.
        with path.with_suffix(".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            _held.paths = held | {key}
            try:
                yield
            finally:
                _held.paths = held
                fcntl.flock(stream, fcntl.LOCK_UN)


def write_json(path: Path, value: dict):
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            filename = stream.name
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if filename:
            Path(filename).unlink(missing_ok=True)
