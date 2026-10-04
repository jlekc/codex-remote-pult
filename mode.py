"""Shared, persistent notification switch; disabled by default."""
import json
import os
from pathlib import Path
import tempfile

MODE = Path(__file__).resolve().with_name('mode.json')


def enabled():
    try:
        return json.loads(MODE.read_text()).get('enabled') is True
    except (OSError, ValueError, AttributeError):
        return False


def set_enabled(value):
    fd, name = tempfile.mkstemp(prefix='mode-', suffix='.tmp', dir=MODE.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'enabled': bool(value)}, stream)
        os.replace(name, MODE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
