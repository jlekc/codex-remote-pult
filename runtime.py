"""Single instance guard for both terminal and LaunchAgent starts."""
import fcntl
from pathlib import Path


def acquire():
    stream = Path(__file__).resolve().with_name('bridge.lock').open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise RuntimeError('Мост уже запущен. Не запускай вторую копию.') from None
    return stream
