"""Layout B using Telegram entities, never interpreting input as HTML."""
import re

TOP = '━━━━━━━━━━━━━━━━'
BOTTOM = '┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄'
TOKENS = re.compile(r'```([^\n`]*)\n([\s\S]*?)```|\*\*([^*]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s)]+)\)')


def units(text):
    return len(text.encode('utf-16-le')) // 2


class Text:
    def __init__(self):
        self.parts, self.entities, self.size = [], [], 0

    def add(self, value, kind=None, **attrs):
        value = str(value)
        length = units(value)
        if kind and length:
            self.entities.append(dict(type=kind, offset=self.size, length=length, **attrs))
        self.parts.append(value)
        self.size += length

    def plain(self, value):
        for line in value.splitlines(keepends=True):
            match = re.match(r'^#{1,6} (.*?)(\n?)$', line)
            if match:
                self.add(match[1], 'bold')
                self.add(match[2])
            else:
                self.add(line)

    def markdown(self, value):
        pos = 0
        for m in TOKENS.finditer(value):
            self.plain(value[pos:m.start()])
            lang, code, bold, inline, label, url = m.groups()
            if code is not None:
                self.add(code, 'pre', **({'language':lang.strip()} if lang.strip() else {}))
            elif bold is not None:
                self.add(bold, 'bold')
            elif inline is not None:
                self.add(inline, 'code')
            else:
                self.add(label, 'text_link', url=url)
            pos = m.end()
        self.plain(value[pos:])

    def result(self):
        return {'text': ''.join(self.parts), 'entities': sorted(self.entities, key=lambda e:(e['offset'],-e['length']))}


def service_parts(text):
    """Extract controlled service metadata; never use on the agent's final answer."""
    body, title, meta = [], None, []
    for line in text.split('\n'):
        m = re.match(r'^(Беседа|Чат|Проект|Папка|Статус|Часть): (.+)$', line)
        if not m:
            body.append(line)
        elif m[1] == 'Беседа':
            title = m[2]
        else:
            meta.append((m[1], m[2]))
    return '\n'.join(body).strip(), title, meta


def format_message(text, presentation=None):
    if presentation is None:
        body, title, meta = service_parts(text)
        event = None
    else:
        body, title = presentation.get('body',text), presentation.get('title')
        meta, event = presentation.get('meta',[]), presentation.get('event')
    if not event:
        first, sep, rest = body.partition('\n')
        events = {'Вопрос агента':'❓ Вопрос агента',
                  'Разрешение требуется':'🛡 Запрос разрешения',
                  'Запрос разрешения:':'🛡 Запрос разрешения',
                  'Скачать готовые файлы:':'📎 Готовые файлы',
                  'Кодекс Пульт — помощь':'❓ Помощь'}
        if first in events:
            event, body = events[first], rest.lstrip('\n')
    t = Text()
    t.add('💬 ' if title else '🤖 ')
    t.add(title or 'Кодекс Пульт', 'bold')
    t.add('\n')
    if event:
        t.add(event, 'bold')
        t.add('\n')
    t.add(TOP+'\n\n')
    start = t.size
    t.markdown(body)
    end = t.size
    # Code blocks have their own background; keep them outside quote entities.
    cursor = start
    for e in list(t.entities):
        if e['type'] == 'pre' and e['offset'] >= start:
            if e['offset'] > cursor:
                t.entities.append(dict(type='blockquote',offset=cursor,length=e['offset']-cursor))
            cursor = e['offset']+e['length']
    if end > cursor:
        t.entities.append(dict(type='blockquote',offset=cursor,length=end-cursor))
    t.add('\n\n'+BOTTOM)
    if meta:
        t.add('\n⚙️ Технические данные\n')
        t.add('\n'.join(str(k)+': '+str(v) for k,v in meta), 'italic')
    return t.result()


def message_data(data):
    data = dict(data)
    presentation = data.pop('_presentation',None)
    if 'text' in data and 'parse_mode' not in data and 'entities' not in data:
        data.update(format_message(data['text'],presentation))
    return data
