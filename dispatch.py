#!/usr/bin/env python3
"""Preserve the previous Codex notification handler and invoke Telegram."""
import json
from pathlib import Path
import subprocess
import sys
import threading
ROOT = Path(__file__).resolve().parent

def invoke(command):
    try:
        subprocess.run(command + sys.argv[1:], timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass

if __name__ == '__main__':
    previous_path = ROOT / 'previous-notify.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else []
    worker = threading.Thread(target=invoke, args=(previous,)) if previous else None
    if worker:
        worker.start()
    invoke([sys.executable, str(ROOT / 'notify.py')])
    if worker:
        worker.join()
