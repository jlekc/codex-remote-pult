"""Create an empty thread using a short-lived writer, then hand it to VS Code."""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import uuid
import time


def create(cwd):
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError('Папка проекта недоступна. Выбери другую папку.')
    binary = shutil.which('codex')
    if not binary:
        raise RuntimeError('Codex не найден.')
    replies = queue.Queue()
    proc = subprocess.Popen([binary, '-c', 'notify=[]', 'app-server'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    def read():
        try:
            for line in proc.stdout:
                replies.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            replies.put(None)
    threading.Thread(target=read, daemon=True).start()
    def call(key, method, params):
        proc.stdin.write(json.dumps({'id': key, 'method': method, 'params': params})+'\n')
        proc.stdin.flush()
        while True:
            response = replies.get(timeout=30)
            if response is None:
                raise RuntimeError('Codex остановился при создании чата.')
            if response.get('id') != key:
                continue
            if 'error' in response:
                raise RuntimeError('Codex не смог создать чат. Проверь вход и настройки проекта.')
            return response['result']
    try:
        call(1, 'initialize', {'clientInfo': {'name': 'telegram_new_chat', 'version': '0.1'},
                                'capabilities': {'experimentalApi': True}})
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        result = call(2, 'thread/start', {'cwd': str(path), 'historyMode': 'legacy'})
        thread = result['thread']
        uuid.UUID(thread['id'])
        # Empty threads are not persisted until an item is written.
        # Record only the origin; this does not run a model or a user task.
        call(3, 'thread/inject_items', {'threadId': thread['id'], 'items': [{
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text',
            'text': 'Чат создан из Telegram. Задача будет отправлена следующим сообщением.'}]}]})
        call(4, 'thread/unsubscribe', {'threadId': thread['id']})
        stored = Path(thread['path'])
        for _ in range(50):
            if stored.is_file() and stored.stat().st_size:
                return thread
            time.sleep(0.1)
        raise RuntimeError('Чат не сохранился. Проверь список чатов перед повтором.')
    except (queue.Empty, OSError, ValueError, KeyError):
        raise RuntimeError('Не удалось подтвердить создание чата. Проверь список чатов перед повтором.') from None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def open_in_vscode(thread):
    # The installed extension URI handler routes /local/<uuid> to the chat.
    uuid.UUID(thread)
    try:
        subprocess.run(['/usr/bin/open', '-g', 'vscode://openai.chatgpt/local/'+thread],
                       check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
