#!/usr/bin/env python3
"""Local Telegram/Codex app-server bridge, standard library only."""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import signal
import uuid
from pathlib import Path
from notify import CONFIG, api, chunks, send
from mode import enabled, set_enabled
from ui import BUTTONS, keyboard, format_limits
from runtime import acquire
from features import Features, WaitForChat
from chat_store import remember, title_for, bind
from new_chat import create as create_chat, open_in_vscode
from media import attachment, download, file_prompt
from vscode_ipc import VSCodeIPC, IpcError, turn_start_params

STATE = CONFIG.with_name('state.json')
GENERAL_CHAT = Path.home() / 'Library/Application Support/CodexRemotePult/general-chat'


class Bridge(Features):
    def __init__(self, config):
        self.config = config
        self.state = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.pending, self.approvals, self.answers = {}, {}, {}
        self.events = queue.Queue()
        self.sequence = 0
        self.active = self.state.get('active')
        if not isinstance(self.active, dict) or self.active.get('transport') != 'vscode':
            self.active = None
        self.ipc = None
        self.last_poll = 0
        binary = shutil.which('codex')
        if not binary:
            raise RuntimeError('Codex не найден')
        self.log = CONFIG.with_name('server.log').open('a')
        self.proc = subprocess.Popen([binary, '-c', 'notify=[]', 'app-server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
        threading.Thread(target=self.read, daemon=True).start()
        self.call('initialize', {'clientInfo': {
            'name': 'telegram_bridge', 'title': 'Telegram Bridge', 'version': '0.1'}})
        self.write({'method': 'initialized', 'params': {}})
        self.init_features()

    def write(self, data):
        self.proc.stdin.write(json.dumps(data) + '\n')
        self.proc.stdin.flush()

    def read(self):
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
                if 'method' in event:
                    self.events.put(event)
                elif event.get('id') in self.pending:
                    self.pending[event['id']].put(event)
            except ValueError:
                pass

    def call(self, method, params):
        self.sequence += 1
        key = self.sequence
        result = queue.Queue()
        self.pending[key] = result
        self.write({'id': key, 'method': method, 'params': params})
        try:
            response = result.get(timeout=45)
        finally:
            self.pending.pop(key, None)
        if 'error' in response:
            detail = str(response['error'].get('message', ''))
            if 'active writer' in detail or 'thread-store conflict' in detail:
                raise RuntimeError('Этот чат занят процессом VS Code. Отдельный мост не может продолжить его, пока VS Code удерживает чат. Нужна передача управления или подключение к тому же процессу Codex.')
            raise RuntimeError('Codex отклонил запрос (' + method + '). Проверь server.log.')
        return response.get('result', {})

    def save(self):
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state))
        temp.chmod(0o600)
        temp.replace(STATE)

    def say(self, text, notification=False, markup=None, thread=None):
        for index, part in enumerate(chunks(text)):
            if notification and not enabled():
                break
            if index:
                time.sleep(1.1)
            result = api(self.config['token'], 'sendMessage', {'chat_id': self.config['chat_id'],
                'text': part, 'link_preview_options': {'is_disabled': True},
                'reply_markup': markup if markup is not None else keyboard(enabled())})
            bind(self.config['chat_id'], result.get('message_id'), thread)

    def event(self, event):
        if event.get('type') == 'broadcast':
            self.stream_event(event)
            return
        method, params = event['method'], event.get('params', {})
        if 'id' in event:
            if not enabled():
                if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    self.write({'id': event['id'], 'result': {'decision': 'decline'}})
                else:
                    self.write({'id': event['id'], 'error': {'code': -32601, 'message': 'Remote mode is off'}})
                return
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                key = str(event['id'])
                self.approvals[key] = event
                self.say('Запрос разрешения:\n' + str(params.get('command') or params.get('reason') or params.get('grantRoot') or method)
                    + '\n/approve ' + key + '\n/decline ' + key, notification=True,
                    markup={'inline_keyboard': [[{'text': '✅ Разрешить', 'callback_data': 'approve:'+key},
                        {'text': '❌ Отклонить', 'callback_data': 'decline:'+key}]]})
            else:
                self.write({'id': event['id'], 'error': {'code': -32601,
                    'message': 'Interactive request is not supported by Telegram Bridge'}})
                self.say('Этот интерактивный запрос пока не поддерживается: ' + method, notification=True)
        elif method == 'item/completed':
            item = params.get('item', {})
            if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                self.answers[params.get('turnId')] = item.get('text', '')
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            self.active = None
            self.approvals.clear()
            answer = self.answers.pop(turn.get('id'), '') or 'Последний ответ отсутствует.'
            if turn.get('status') != 'completed':
                answer = 'Статус: ' + str(turn.get('status')) + '\n' + answer
            send(self.config, {'thread-id': params.get('threadId'), 'turn-id': turn.get('id'),
                'cwd': params.get('cwd') or self.state.get('cwd') or '.',
                'last-assistant-message': answer})


    def message(self, message):
        chat = message.get('chat', {})
        if str(chat.get('id')) != str(self.config['chat_id']) or chat.get('type') != 'private':
            return
        media_item, _ = attachment(message)
        if not media_item and not message.get('text'):
            if 'voice' in message or 'audio' in message:
                self.say('Используй микрофон клавиатуры телефона: продиктуй промпт и отправь получившийся текст. Голосовые сообщения Telegram пока не распознаются.')
            elif any(key in message for key in ('video', 'video_note', 'sticker', 'animation')):
                self.say('Этот формат пока не поддерживается. Отправь текст, изображение или файл.')
            return
        text = (message.get('caption', '') if media_item else message.get('text', '')).strip()
        if media_item:
            # Captions are prompts, even if they look like a bot command.
            command, argument = '', ''
        else:
            text = BUTTONS.get(text, text)
            command, _, argument = text.partition(' ')
        if command in ('/on', '/off'):
            value = command == '/on'
            set_enabled(value)
            if not value:
                for event in self.approvals.values():
                    if event.get('transport') != 'vscode':
                        self.write({'id': event['id'], 'result': {'decision': 'decline'}})
                self.approvals.clear()
            self.say('Удалённый режим включён. Ответы Codex будут приходить сюда.' if value else
                     'Удалённый режим выключен. Уведомления и новые промпты отключены. Текущий запрос, если есть, продолжает работу.')
        elif command in ('/start', '/help'):
            self.say('/on — включить удалённый режим\n/off — выключить\n/new — новый чат\n/chats — последние чаты\n/use ID — выбрать чат\n/status — состояние\n/limits — лимиты Codex\n/stop — остановить\n/approve ID или /decline ID — разрешение\n/queue — очередь и пауза\n/files — файлы последнего ответа\n/file_ID — скачать конкретный файл\n/guide — подробная инструкция\nПромпт: текст, одно изображение или файл до 20 МБ. Подпись — задание. Голос: диктовка клавиатуры телефона.\nНе запускай тот же чат одновременно в VS Code.',
                markup={'inline_keyboard': [[{'text': '📖 Инструкция', 'callback_data': 'guide:open'}]]} if command == '/help' else None)
        elif command == '/guide':
            try:
                guide = CONFIG.with_name('USER_GUIDE.md').read_text(encoding='utf-8')
            except OSError:
                self.say('Инструкция недоступна. Проверь USER_GUIDE.md в папке проекта.')
            else:
                self.say(guide)
        elif command == '/new':
            self.new_chat_choices()
        elif command == '/chats':
            threads = self.call('thread/list', {'limit': 10, 'sortKey': 'updated_at', 'sortDirection': 'desc'}).get('data', [])
            self.state['chat_choices'] = {t['id']: t['id'] for t in threads}
            self.save()
            buttons = [[{'text': (t.get('name') or t.get('preview') or t['id'])[:60],
                         'callback_data': 'chat:'+t['id']}] for t in threads]
            self.say('Выбери чат:' if threads else 'Чатов нет.',
                     markup={'inline_keyboard': buttons} if buttons else None)
        elif command == '/limits':
            try:
                self.say(format_limits(self.call('account/rateLimits/read', {})))
            except (RuntimeError, queue.Empty):
                self.say('Не удалось получить лимиты. Проверь вход Codex через ChatGPT и подключение к сети.')
        elif command == '/status':
            self.say('Удалённый режим: ' + ('включён' if enabled() else 'выключен') + '\nЧат: ' + self.state.get('thread', 'не выбран') + '\n' + ('Работает' if self.active else 'Ожидает') + '\nВ очереди: ' + str(len(self.queue_items)))
        elif command in ('/approve', '/decline'):
            self.decide_approval(argument, command == '/approve')
        elif command == '/queue':
            self.show_queue()
        elif re.fullmatch(r'/file_[0-9a-f]{16}(?:@[A-Za-z0-9_]+)?', command):
            self.deliver_file(command.split('@',1)[0][6:])
        elif command == '/files':
            self.show_files(message)
        elif command == '/stop':
            self.state['queue_paused'] = True
            self.save()
            if self.active:
                if self.active.get('transport') == 'vscode':
                    self.connect_ipc().request('thread-follower-interrupt-turn', {
                        'conversationId': self.active['threadId'],
                        'expectedTurnId': self.active['turnId'], 'mode': 'user-stop'},
                        target=self.active['owner'])
                    self.active['stop_requested'] = True
                    self.state['active'] = self.active
                    self.save()
                else:
                    self.call('turn/interrupt', self.active)
            else:
                self.say('Активного запроса нет.')
            self.say('Очередь приостановлена. Для продолжения нажми «Очередь» → «Продолжить».')
        elif command == '/use':
            thread = self.call('thread/read', {'threadId': argument})['thread']
            self.state['thread'] = thread['id']
            self.save()
            remember(thread['id'], thread.get('name') or thread.get('preview'), thread.get('cwd'))
            self.say('Выбрана беседа: ' + title_for(thread['id']) + '\nЧат: ' + thread['id'], thread=thread['id'])
        elif command.startswith('/'):
            self.say('Неизвестная команда. /help')
        else:
            self.enqueue(message)

    def start_prompt(self, thread_id, message, queued):
        ipc = self.connect_ipc()
        snap = self.snapshots.get(thread_id)
        if not snap or time.monotonic()-snap['received']>10:
            ipc.follow(thread_id)
            raise WaitForChat('Нужно живое состояние чата. Открой его в Codex VS Code.')
        state = snap['state']
        turns = list(state.get('turns', []))
        turns += list((state.get('turnHistory') or {}).get('history', {}).get('entitiesByKey', {}).values())
        if (state.get('threadRuntimeStatus') or {}).get('type') == 'active' or any(t.get('status') == 'inProgress' for t in turns):
            raise WaitForChat('В выбранном чате ещё выполняется запрос.')
        if any(not r.get('completed') for r in state.get('requests', [])):
            raise WaitForChat('В выбранном чате ожидается ответ на интерактивный запрос.')
        thread = self.call('thread/read', {'threadId': thread_id})['thread']
        cwd = thread.get('cwd')
        if not cwd or not Path(cwd).is_dir():
            raise RuntimeError('Папка чата недоступна.')
        owner = ipc.owner(thread_id)
        if not owner or owner != snap['owner']:
            raise WaitForChat('Открой выбранный чат в Codex VS Code: его владелец недоступен.')
        media_item, _ = attachment(message)
        text = (message.get('caption', '') if media_item else message.get('text', '')).strip()
        images = []
        if media_item:
            path, is_image = download(self.config, message)
            if is_image:
                images.append(path)
                text = text or 'Посмотри изображение и кратко опиши, что на нём.'
            else:
                text = file_prompt(text, path)
        # Record dispatch before sending; a crash/timeout must not replay a task.
        queued['status'] = 'dispatching'
        self.save()
        response = ipc.request('thread-follower-start-turn',
            turn_start_params(thread_id, text, images), target=owner, timeout=40)
        turn = response.get('result', {}).get('result', {}).get('turn', {})
        if not turn.get('id'):
            raise RuntimeError('VS Code не подтвердил ID запроса. Проверь историю перед повтором.')
        self.active = {'threadId': thread_id, 'turnId': turn['id'],
                       'transport': 'vscode', 'owner': owner, 'queueId': queued['id']}
        self.last_poll = 0
        self.state['active'] = self.active
        self.state['cwd'] = cwd
        self.save()
        remember(thread_id, state.get('title'), cwd)

    def new_chat_allowed(self):
        if not enabled():
            self.say('Сначала включи удалённый режим: /on.')
            return False
        if self.active:
            self.say('Дождись завершения текущего запроса или нажми «Остановить запрос».')
            return False
        return True

    def new_chat_choices(self):
        if not self.new_chat_allowed():
            return
        self.state.pop('new_chat_choices', None)
        kinds = {uuid.uuid4().hex[:16]: kind for kind in ('general', 'project')}
        self.state['new_chat_kinds'] = kinds
        self.save()
        buttons = [[{'text': 'Общий чат' if kind == 'general' else 'Чат в проекте',
                     'callback_data': 'newmode:' + key}] for key, kind in kinds.items()]
        buttons.append([{'text': 'Отмена', 'callback_data': 'new:cancel'}])
        self.say('Какой чат создать?\nОбщий чат — для вопросов и задач без рабочего проекта.\nЧат в проекте — для работы с его файлами.',
                 markup={'inline_keyboard': buttons})

    def new_chat_kind(self, key):
        if not self.new_chat_allowed():
            return
        kind = self.state.get('new_chat_kinds', {}).get(key)
        if kind not in ('general', 'project'):
            self.say('Меню устарело. Нажми «Новый чат» ещё раз.')
            return
        self.state.pop('new_chat_kinds', None)
        self.save()
        if kind == 'project':
            self.new_chat_projects()
            return
        try:
            GENERAL_CHAT.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise RuntimeError('Не удалось подготовить общий чат. Проверь доступ к папке приложения на Mac.') from None
        # Keep the general workspace away from the bot/project AGENTS.md.
        choice = uuid.uuid4().hex[:16]
        self.state['new_chat_choices'] = {choice: str(GENERAL_CHAT.resolve())}
        self.save()
        self.new_chat_selected(choice)

    def new_chat_projects(self):
        if not self.new_chat_allowed():
            return
        threads = self.call('thread/list', {'limit': 50, 'sortKey': 'updated_at', 'sortDirection': 'desc'}).get('data', [])
        paths = [t.get('cwd') for t in threads]
        choices, buttons = {}, []
        for cwd in paths:
            if not cwd or not Path(cwd).is_dir():
                continue
            cwd = str(Path(cwd).resolve())
            if Path(cwd).is_relative_to(GENERAL_CHAT.resolve()) or cwd in choices.values():
                continue
            key = uuid.uuid4().hex[:16]
            choices[key] = cwd
            label = str(Path(cwd).relative_to(Path.home())) if Path(cwd).is_relative_to(Path.home()) else cwd
            buttons.append([{'text': label[-60:], 'callback_data': 'new:'+key}])
            if len(choices) >= 10:
                break
        self.state['new_chat_choices'] = choices
        self.save()
        if not choices:
            self.say('Доступных папок в недавних чатах нет. Сначала открой проект в VS Code.')
            return
        buttons.append([{'text': 'Отмена', 'callback_data': 'new:cancel'}])
        self.say('Выбери проект для нового чата:',
                 markup={'inline_keyboard': buttons})

    def new_chat_selected(self, key):
        if key == 'cancel':
            self.state.pop('new_chat_choices', None)
            self.state.pop('new_chat_kinds', None)
            self.save()
            self.say('Создание чата отменено.')
            return
        if not enabled():
            self.say('Сначала включи удалённый режим: /on.')
            return
        if self.active:
            self.say('Дождись завершения текущего запроса или /stop.')
            return
        cwd = self.state.get('new_chat_choices', {}).get(key)
        if not cwd:
            self.say('Выбор папки устарел. Нажми «Новый чат» ещё раз.')
            return
        # A repeated click must not create a duplicate thread.
        self.state.pop('new_chat_choices', None)
        self.save()
        thread = create_chat(cwd)
        self.state['thread'], self.state['cwd'] = thread['id'], cwd
        self.save()
        opened = open_in_vscode(thread['id'])
        description = ('➕ Общий чат создан и выбран.' if Path(cwd).resolve() == GENERAL_CHAT.resolve()
                       else '➕ Чат в проекте создан и выбран.\nПроект: ' + Path(cwd).name)
        self.say(description+'\nЧат: '+thread['id']+
                 ('\nОтправлена команда открытия в VS Code. Теперь отправь первый промпт.' if opened else
                  '\nНе удалось открыть VS Code автоматически. Открой этот чат в Codex, затем отправь промпт.'))

    def connect_ipc(self):
        if self.ipc is None or self.ipc.closed:
            if self.ipc:
                self.ipc.close()
            try:
                self.ipc = VSCodeIPC(self.events.put)
            except (OSError, IpcError):
                raise RuntimeError('Не удалось подключиться к VS Code. Проверь, что Codex открыт, и попробуй ещё раз.') from None
        return self.ipc

    def poll_vscode_turn(self):
        if not self.active or self.active.get('transport') != 'vscode':
            return
        if time.monotonic()-self.last_poll < 3:
            return
        self.last_poll = time.monotonic()
        thread_id, turn_id = self.active['threadId'], self.active['turnId']
        try:
            thread = self.call('thread/read', {'threadId': thread_id, 'includeTurns': True})['thread']
        except (RuntimeError, KeyError, queue.Empty):
            return
        turn = next((t for t in thread.get('turns', []) if t.get('id') == turn_id), None)
        if not turn or turn.get('status') not in ('completed', 'failed', 'interrupted'):
            return
        # The read-only history reports an unfinished/live tail as interrupted.
        # Only an acknowledged stop requested by this bridge proves interruption.
        if turn.get('status') == 'interrupted' and not self.active.get('stop_requested'):
            snap = self.snapshots.get(thread_id, {}).get('state', {})
            live_turns = list(snap.get('turns', [])) + list((snap.get('turnHistory') or {}).get('history', {}).get('entitiesByKey', {}).values())
            if not any(t.get('turnId') == turn_id and t.get('status') == 'interrupted' for t in live_turns):
                return
        if turn.get('status') == 'failed' and not turn.get('error'):
            return
        answers = [item.get('text', '') for item in turn.get('items', [])
                   if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary']
        answer = answers[-1] if answers else 'Последний ответ отсутствует.'
        if turn.get('status') != 'completed':
            answer = 'Статус: ' + str(turn.get('status')) + '\n' + answer
        send(self.config, {'thread-id': thread_id, 'turn-id': turn_id,
            'cwd': thread.get('cwd') or '.', 'status': turn.get('status'),
            'last-assistant-message': answer})
        self.active = None
        self.state.pop('active', None)
        self.save()

    def callback(self, callback):
        message = callback.get('message', {})
        if (str(callback.get('from', {}).get('id')) != str(self.config['chat_id']) or
                str(message.get('chat', {}).get('id')) != str(self.config['chat_id'])):
            return
        api(self.config['token'], 'answerCallbackQuery', {'callback_query_id': callback['id']})
        action, _, key = callback.get('data', '').partition(':')
        if action == 'guide':
            self.message({'chat': {'id': self.config['chat_id'], 'type': 'private'}, 'text': '/guide'})
            return
        if action in ('qdel', 'qctl'):
            self.queue_control(action, key)
            return
        if action == 'file':
            self.deliver_file(key)
            return
        if action == 'newmode':
            self.new_chat_kind(key)
            return
        if action == 'new':
            self.new_chat_selected(key)
            return
        if action == 'chat':
            thread = self.state.get('chat_choices', {}).get(key)
            if not thread:
                self.say('Список устарел. Нажми «Выбрать чат» ещё раз.')
                return
            text = '/use ' + thread
        elif action in ('approve', 'decline'):
            text = '/' + action + ' ' + key
        else:
            return
        self.message({'chat': {'id': self.config['chat_id'], 'type': 'private'}, 'text': text})

    def run(self):
        self.say('Мост Codex подключён. /help', notification=True)
        while True:
            self.poll_snapshots()
            while not self.events.empty():
                self.event(self.events.get())
            self.poll_vscode_turn()
            self.drain_queue()
            if self.proc.poll() is not None:
                raise RuntimeError('Codex остановился')
            try:
                updates = api(self.config['token'], 'getUpdates', {
                    'offset': self.state.get('offset', 0), 'timeout': 2,
                    'allowed_updates': ['message', 'callback_query']})
            except RuntimeError:
                time.sleep(3)
                continue
            for update in updates:
                # Prevent replaying a prompt after a restart.
                self.state['offset'] = update['update_id'] + 1
                self.save()
                try:
                    if 'callback_query' in update:
                        self.callback(update['callback_query'])
                    else:
                        self.message(update.get('message', {}))
                except IpcError as error:
                    detail = str(error)
                    if detail in ('request-timeout', 'connection-closed', 'client-disconnected'):
                        self.say('Подтверждение от VS Code не получено. Проверь историю чата перед повтором: промпт мог уже запуститься.')
                    elif detail == 'request-version-mismatch':
                        self.say('Версия канала Codex изменилась. Нужна адаптация моста под установленное расширение.')
                    else:
                        self.say('VS Code не принял запрос. Проверь, что выбранный чат открыт и предыдущая работа завершена.')
                except RuntimeError as error:
                    self.say('Запрос не выполнен. ' + str(error))
                except (KeyError, queue.Empty):
                    self.say('Запрос не выполнен: ответ Codex отсутствует или имеет неожиданный формат. Проверь server.log.')


if __name__ == '__main__':
    bridge = None
    instance_lock = None
    signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        instance_lock = acquire()
        bridge = Bridge(json.loads(CONFIG.read_text()))
        bridge.run()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, queue.Empty) as error:
        detail = str(error) if isinstance(error, RuntimeError) else 'Проверь credentials.json, доступ к Telegram и вход в Codex.'
        print('Мост остановлен. ' + detail, flush=True)
    finally:
        if bridge:
            if bridge.ipc:
                bridge.ipc.close()
            bridge.proc.terminate()
            bridge.proc.wait(timeout=10)
            bridge.log.close()
        if instance_lock:
            instance_lock.close()
