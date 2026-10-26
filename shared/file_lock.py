"""Process-scoped advisory locks for one local runtime and its publications."""

import os
import time
from pathlib import Path


class FileLockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path, *, timeout=0):
        self.path = path
        self.handle = None
        self.timeout=timeout

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline=time.monotonic()+self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic()>=deadline:
                    handle.close()
                    raise FileLockBusy(f"runtime lock busy: {self.path}") from None
                time.sleep(.005)
        self.handle = handle
        return self

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
