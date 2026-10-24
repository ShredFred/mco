"""OS-released lock for one daemon per session (not provider write ownership)."""
from contextlib import contextmanager
import os


@contextmanager
def daemon_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as stream:
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                stream.write(b'0')
                stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def lifecycle_serialized(function):
    from functools import wraps
    from inspect import signature
    from .state import session_dir

    @wraps(function)
    def guarded(*args, **kwargs):
        bound = signature(function).bind(*args, **kwargs)
        bound.apply_defaults()
        name = bound.arguments['name']
        if name is None:
            return function(*args, **kwargs)  # auto-generated names are independent
        with daemon_lock(session_dir(bound.arguments['repo_root'], name) / 'lifecycle.lock') as acquired:
            if not acquired:
                raise ValueError('Session lifecycle change in progress; retry ensure later')
            return function(*args, **kwargs)
    return guarded
