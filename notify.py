#!/usr/bin/env python3
"""Codex notify handler. Standard library only; credentials stay beside script."""
import argparse
import getpass
import json
import re
import socket
import ssl
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from mode import enabled
from delivery import claim
from chat_store import remember, title_for, bind

CONFIG = Path(__file__).resolve().with_name('credentials.json')


def tls_context():
    # python.org macOS installs may lack the default CA link.
    # Use an installed, maintained CA bundle; keep certificate verification on.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def api(token, method, data):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15, context=tls_context()) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Do not print the exception: its URL contains the bot token.
        messages = {
            401: 'Telegram не принял токен. Скопируй токен целиком из BotFather.',
            404: 'Telegram не нашёл бота. Проверь токен из BotFather.',
            403: 'Telegram запретил действие. Проверь, что бот не заблокирован.',
            409: 'Бот уже получает сообщения другим процессом или через webhook.',
            429: 'Слишком много запросов. Подожди и повтори.'}
        raise RuntimeError(messages.get(error.code, 'Telegram вернул HTTP ' + str(error.code))) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise RuntimeError('Python не доверяет HTTPS-сертификату. Установи сертификаты для этой версии Python (Install Certificates.command в папке Python в Applications).') from None
        if isinstance(error.reason, socket.gaierror):
            raise RuntimeError('Не удалось определить адрес api.telegram.org. Проверь интернет, DNS и VPN.') from None
        raise RuntimeError('Не удалось соединиться с Telegram. Проверь интернет и VPN.') from None
    except TimeoutError:
        raise RuntimeError('Telegram не ответил за 15 секунд. Проверь интернет и VPN.') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram отклонил запрос')
    return result['result']


def chunks(text, limit=3500):
    # UTF-16 units also accommodate astral characters such as emoji.
    part, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if size + width > limit:
            yield ''.join(part)
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        yield ''.join(part)


def send(config, event, force=False):
    if not force and not enabled():
        return
    project = Path(event.get('cwd') or '.').name
    answer = event.get('last-assistant-message') or event.get('last_assistant_message') or 'Последний ответ отсутствует.'
    status = event.get('status', 'completed')
    headline = {'completed': '✅ Codex завершил ответ',
                'failed': '❌ Запрос Codex завершился ошибкой',
                'interrupted': '⏹ Запрос Codex остановлен'}.get(status, 'Codex: ' + str(status))
    thread = event.get('thread-id') or event.get('session_id')
    remember(thread, event.get('thread-name'), event.get('cwd'), answer)
    header = headline + '\nПроект: ' + project[:80]
    if thread:
        header += '\nБеседа: ' + title_for(thread) + '\nЧат: ' + str(thread)
    # Every part has the same routing header, including older/copy messages.
    parts = list(chunks(answer, limit=3000))
    claimed, receipt = (True, None) if force else claim(event)
    if not claimed:
        return
    try:
        for index, part in enumerate(parts):
            if not force and not enabled():
                break
            if index:
                time.sleep(1.1)
            text = header + (f'\nЧасть {index+1}/{len(parts)}' if len(parts)>1 else '') + '\n\n' + part
            result = api(config['token'], 'sendMessage', {
                'chat_id': config['chat_id'], 'text': text,
                'link_preview_options': {'is_disabled': True}})
            bind(config['chat_id'], result.get('message_id'), thread)
        if force or enabled():
            from outgoing import file_offer
            offer = file_offer(answer, event.get('cwd'), thread)
            if offer:
                text, markup = offer
                text += '\nБеседа: ' + title_for(thread) + '\nЧат: ' + str(thread)
                result = api(config['token'], 'sendMessage', {'chat_id': config['chat_id'],
                    'text': text, 'reply_markup': markup})
                bind(config['chat_id'], result.get('message_id'), thread)
    except Exception:
        if receipt:
            receipt.unlink(missing_ok=True)
        raise


def setup():
    token = getpass.getpass('Токен от BotFather (скрытый ввод): ').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise RuntimeError('Неверный формат токена. Нужна строка вида 123456789:ABC..., без кавычек, имени бота и ссылки.')
    bot = api(token, 'getMe', {})
    print('Токен проверен. Бот: @' + bot.get('username', ''))
    input('Откройте вашего бота, нажмите Start и отправьте ему сообщение. Затем Enter здесь: ')
    updates = api(token, 'getUpdates', {'timeout': 0})
    chats = {}
    for update in updates:
        chat = update.get('message', {}).get('chat', {})
        if chat.get('type') == 'private':
            chats[str(chat['id'])] = chat.get('first_name', '')
    for chat_id, name in chats.items():
        print('Личный чат:', chat_id, name)
    chat_id = input('Введи chat_id своего личного чата: ').strip()
    if not chat_id.isdecimal():
        raise RuntimeError('Нужен числовой chat_id личного чата')
    config = {'token': token, 'chat_id': chat_id}
    # Restrict permissions from file creation, not only after writing.
    import os
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(CONFIG, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(config, handle)
    send(config, {'cwd': 'настройка', 'last-assistant-message': 'Тестовое уведомление. Telegram подключён.'}, force=True)
    print('Настройки сохранены. Тестовое сообщение отправлено.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('event', nargs='?')
    args = parser.parse_args()
    if args.setup:
        setup()
        return
    event = ({'type': 'agent-turn-complete', 'cwd': 'тест',
              'last-assistant-message': 'Проверка уведомления Codex.'} if args.test
             else json.loads(args.event if args.event else sys.stdin.read()))
    if event.get('type', event.get('hook_event_name')) not in ('agent-turn-complete', 'Stop'):
        return
    if args.dry_run:
        print(json.dumps(event, ensure_ascii=False))
        return
    send(json.loads(CONFIG.read_text()), event, force=args.test)
    if event.get('hook_event_name') == 'Stop':
        print('{}')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        # Only our controlled RuntimeError strings are safe to expose.
        detail = str(error) if isinstance(error, RuntimeError) else 'Ошибка файла настроек или данных. Проверь credentials.json.'
        print('Codex Telegram: ' + detail, file=sys.stderr)
        # Notification delivery must not fail the coding turn.
        if '--setup' in sys.argv or '--test' in sys.argv:
            sys.exit(1)
        if len(sys.argv) == 1:
            print('{}')
