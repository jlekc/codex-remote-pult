"""Frozen recent-chat lists, five buttons at a time; no model or task changes."""
import uuid
import time
from notify import api

PAGE_SIZE = 5
MAX_CHATS = 20
MAX_MENUS = 20
MAX_FETCH_PAGES = 10

class ChatMenu:
    def __init__(self, bridge):
        self.bridge = bridge
        self.menus = bridge.state.setdefault('chat_menus', {})
        changed = False
        for menu in self.menus.values():
            for field in ('closed', 'pending_delete'):
                if field in menu:
                    menu.pop(field)
                    changed = True
            if len(menu['chats']) > MAX_CHATS:
                menu['chats'] = menu['chats'][:MAX_CHATS]
                menu['shown'] = min(menu['shown'], MAX_CHATS)
                changed = True
        if changed:
            bridge.save()

    def open(self):
        b = self.bridge
        threads, seen, cursors = [], set(), set()
        cursor = None
        # Some Codex stores expose the same thread more than once. Preserve
        # newest-first order, count distinct IDs and fill from subsequent pages.
        for _ in range(MAX_FETCH_PAGES):
            params = {'limit': MAX_CHATS, 'sortKey': 'updated_at', 'sortDirection': 'desc'}
            if cursor:
                params['cursor'] = cursor
            page = b.call('thread/list', params)
            for thread in page.get('data', []):
                if thread['id'] not in seen:
                    seen.add(thread['id'])
                    threads.append(thread)
                    if len(threads) == MAX_CHATS:
                        break
            cursor = page.get('nextCursor')
            if len(threads) == MAX_CHATS or not cursor or cursor in cursors:
                break
            cursors.add(cursor)
        if not threads:
            b.say('Чатов нет.')
            return
        # Keep this list fixed while new activity changes the global ordering.
        # Store only routing IDs and labels, never the full thread/history.
        chats = [{'id': t['id'], 'label': (t.get('name') or t.get('preview') or t['id'])[:60]} for t in threads[:MAX_CHATS]]
        key = uuid.uuid4().hex[:16]
        menu = {'chats': chats, 'shown': 0, 'messages': [], 'topic': getattr(b, 'reply_topic', None)}
        self.menus[key] = menu
        while len(self.menus) > MAX_MENUS:
            self.menus.pop(next(iter(self.menus)))
        b.save()
        self._page(key, menu)

    def _page(self, key, menu):
        b = self.bridge
        start = menu['shown']
        end = min(start + PAGE_SIZE, len(menu['chats']))
        buttons = [[{'text': c['label'], 'callback_data': f'chatpick:{key}:{n}'}]
                   for n, c in enumerate(menu['chats'][start:end], start)]
        if end < len(menu['chats']):
            buttons.append([{'text': '⬇️ Вывести следующие 5 ⬇️', 'callback_data': f'chatmore:{key}:{end}'}])
        result = b.say(f'💬 Чаты {start+1}–{end}', markup={'inline_keyboard': buttons}, compact=True)
        menu['shown'] = end
        if isinstance(result, dict) and result.get('message_id'):
            menu['messages'].append(result['message_id'])
            menu.setdefault('compact_messages', []).append(result['message_id'])
            menu['topic'] = result.get('message_thread_id', menu['topic'])
        b.save()

    def _get(self, key, message):
        token, _, raw = key.partition(':')
        menu = self.menus.get(token)
        try:
            index = int(raw)
        except ValueError:
            menu = None
        if (not menu or message.get('message_id') not in menu['messages'] or
                message.get('message_thread_id') != menu.get('topic')):
            self.bridge.say('Список устарел. Нажми «Выбрать чат» ещё раз.')
            return None
        return token, menu, index

    def more(self, key, message):
        found = self._get(key, message)
        if not found:
            return
        token, menu, index = found
        if index != menu['shown'] or index >= len(menu['chats']):
            self.bridge.say('Следующие беседы уже выведены ниже.')
            return
        self._page(token, menu)

    def choose(self, key, message):
        found = self._get(key, message)
        if not found:
            return None
        _, menu, index = found
        if index < 0 or index >= menu['shown']:
            self.bridge.say('Этой беседы нет в доступной части списка. Открой список заново.')
            return None
        return menu['chats'][index]['id']


    def sweep(self):
        # Shrink old headers by editing text only; Telegram retains their
        # inline keyboards. Never delete chat-choice messages.
        b = self.bridge
        now = time.monotonic()
        if now - getattr(b, '_chat_menu_sweep', 0) < 30:
            return
        b._chat_menu_sweep = now
        for menu in self.menus.values():
            compact = menu.setdefault('compact_messages', [])
            for message in menu.get('messages', []):
                if message in compact:
                    continue
                try:
                    api(b.config['token'], 'editMessageText',
                        {'chat_id': b.config['chat_id'], 'message_id': message,
                         'text': '💬 Чаты', 'entities': []})
                except (RuntimeError, OSError):
                    continue
                compact.append(message)
                b.save()
