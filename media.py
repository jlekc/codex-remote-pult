"""Download a single Telegram attachment with bounded size and private storage."""
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid
from notify import CONFIG, api, tls_context

MAX_BYTES = 20 * 1024 * 1024
INCOMING = CONFIG.with_name('incoming')


def attachment(message):
    if message.get('photo'):
        return max(message['photo'], key=lambda p: p.get('width', 0)*p.get('height', 0)), True
    if message.get('document'):
        item = message['document']
        return item, item.get('mime_type') in ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
    return None, False


def download(config, message):
    item, is_image = attachment(message)
    if not item:
        return None, False
    if item.get('file_size', 0) > MAX_BYTES:
        raise RuntimeError('Вложение больше 20 МБ. Отправь файл меньшего размера.')
    info = api(config['token'], 'getFile', {'file_id': item['file_id']})
    if info.get('file_size', 0) > MAX_BYTES:
        raise RuntimeError('Вложение больше 20 МБ. Отправь файл меньшего размера.')
    remote = info.get('file_path', '')
    if not remote or any(p in ('', '.', '..') for p in remote.split('/')) or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-' for c in remote):
        raise RuntimeError('Telegram вернул неожиданный путь вложения.')
    INCOMING.mkdir(mode=0o700, exist_ok=True)
    if INCOMING.is_symlink():
        raise RuntimeError('Каталог вложений не должен быть символической ссылкой.')
    INCOMING.chmod(0o700)
    folder = INCOMING / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    name = Path(str(item.get('file_name') or remote).replace(chr(92), '/')).name
    name = ''.join(c for c in name if c.isprintable())[:160]
    if name in ('', '.', '..'):
        name = 'attachment'
    path = folder / name
    try:
        url = 'https://api.telegram.org/file/bot' + config['token'] + '/' + remote
        with urllib.request.urlopen(url, timeout=30, context=tls_context()) as response:
            with path.open('xb') as target:
                path.chmod(0o600)
                size = 0
                while True:
                    part = response.read(65536)
                    if not part:
                        break
                    size += len(part)
                    if size > MAX_BYTES:
                        raise RuntimeError('Вложение больше 20 МБ. Отправь файл меньшего размера.')
                    target.write(part)
        if not size or (info.get('file_size') is not None and size != info['file_size']):
            raise RuntimeError('Файл скачался не полностью. Отправь вложение ещё раз.')
    except (OSError, ValueError, RuntimeError) as error:
        path.unlink(missing_ok=True)
        folder.rmdir()
        if isinstance(error, RuntimeError):
            raise
        # Network exceptions include the secret bot token in their URL.
        raise RuntimeError('Не удалось скачать вложение из Telegram. Проверь сеть и повтори отправку.') from None
    return str(path), is_image


def file_prompt(text, path):
    return (text or 'Посмотри приложенный файл и кратко опиши его содержимое.') + '\n\n' + 'Прикреплённый пользователем файл на Mac (путь в JSON): ' + json.dumps(path, ensure_ascii=False)
