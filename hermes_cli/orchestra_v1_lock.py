"""同一项目控制写入的线程及跨进程互斥。"""

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os
import threading


_guard = threading.Lock()
_locks: dict[Path, threading.RLock] = {}
_local = threading.local()


@contextmanager
def project_lock(project, hermes_home=None):
    from hermes_cli.orchestra_v1_control import project_control_dir

    control = project_control_dir(project, hermes_home)
    path = control.parent / ".locks" / f"{control.name}.lock"
    with _guard:
        lock = _locks.setdefault(path, threading.RLock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("该项目已有活动 Orchestra 或 worker")
    held = getattr(_local, "held", set())
    _local.held = held
    handle = None
    try:
        if path in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该项目已有活动 Orchestra 或 worker") from exc
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)
    finally:
        if handle is not None:
            handle.close()
        lock.release()


def project_exclusive(function):
    @wraps(function)
    def wrapped(project, *args, **kwargs):
        with project_lock(project, kwargs.get("hermes_home")):
            return function(project, *args, **kwargs)

    return wrapped
