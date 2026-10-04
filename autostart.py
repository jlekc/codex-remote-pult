#!/usr/bin/env python3
"""Install/manage this project's per-user macOS LaunchAgent."""
import argparse
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
LABEL = 'local.codex-remote-pult'
PLIST = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
DOMAIN = f'gui/{os.getuid()}'
SERVICE = DOMAIN+'/'+LABEL


def run(*args, check=True):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, check=check)


def stop_terminal_copies():
    # Migrate only this user's bridge.py in this project's directory.
    processes = subprocess.check_output(['ps', '-axo', 'pid=,uid=,command='], text=True)
    for line in processes.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or parts[1] != str(os.getuid()):
            continue
        pid, _, command = parts
        words = command.split()
        if 'bridge.py' not in words and str(ROOT/'bridge.py') not in words:
            continue
        cwd = subprocess.run(['lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn'], capture_output=True, text=True)
        if 'n'+str(ROOT) not in cwd.stdout.splitlines() and str(ROOT/'bridge.py') not in words:
            continue
        os.kill(int(pid), signal.SIGINT)
        for _ in range(50):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Старая копия моста не остановилась. Останови её через Ctrl+C.')
        print('Остановлена прежняя копия моста:', pid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'status', 'restart', 'stop', 'start', 'uninstall'])
    action = parser.parse_args().action
    if action == 'status':
        result = run('print', SERVICE, check=False)
        if result.returncode:
            print('Фоновый мост не загружен.')
        else:
            for line in result.stdout.splitlines():
                if any(key in line for key in ('state =', 'pid =', 'last exit code =')):
                    print(line.strip())
        return
    if PLIST.exists():
        existing = plistlib.loads(PLIST.read_bytes())
        if str(ROOT/'bridge.py') not in existing.get('ProgramArguments', []):
            raise RuntimeError('Этот LaunchAgent принадлежит другому проекту; файл не изменён.')
    if action == 'install':
        run('bootout', SERVICE, check=False)
        stop_terminal_copies()
        logs = ROOT/'logs'
        logs.mkdir(exist_ok=True)
        path = ':'.join([str(Path(sys.executable).parent), str(Path.home()/'.npm-global/bin'),
                         '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'])
        data = {'Label': LABEL, 'ProgramArguments': [sys.executable, '-u', str(ROOT/'bridge.py')],
                'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
                'ThrottleInterval': 15, 'ExitTimeOut': 15,
                'EnvironmentVariables': {'PATH': path},
                'StandardOutPath': str(logs/'bridge.out.log'), 'StandardErrorPath': str(logs/'bridge.err.log')}
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(data))
        PLIST.chmod(0o600)
        run('bootstrap', DOMAIN, str(PLIST))
        print('Автозапуск установлен; мост запущен в фоне.')
    elif action == 'restart':
        run('kickstart', '-k', SERVICE)
        print('Мост перезапущен.')
    elif action == 'stop':
        run('bootout', SERVICE, check=False)
        print('Фоновый мост остановлен до входа в macOS или команды start.')
    elif action == 'start':
        run('bootstrap', DOMAIN, str(PLIST))
        print('Фоновый мост запущен.')
    elif action == 'uninstall':
        run('bootout', SERVICE, check=False)
        PLIST.unlink(missing_ok=True)
        print('Автозапуск удалён; файлы проекта сохранены.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Ошибка управления автозапуском:', str(error), file=sys.stderr)
        sys.exit(1)
