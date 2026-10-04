"""Prevent duplicate final replies from hook and Telegram bridge."""
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().with_name('deliveries')


def claim(event):
    thread = event.get('thread-id') or event.get('session_id')
    turn = event.get('turn-id') or event.get('turn_id')
    if not thread or not turn:
        return True, None
    ROOT.mkdir(exist_ok=True, mode=0o700)
    key = str(thread)+':'+str(turn)
    status = event.get('status', 'completed')
    if status != 'completed':
        key += ':' + str(status)
    name = hashlib.sha256(key.encode()).hexdigest()
    path = ROOT/name
    try:
        fd = os.open(path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, path
    os.close(fd)
    return True, path
