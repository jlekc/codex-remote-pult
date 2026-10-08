"""Forward desktop user prompts only; Telegram prompts and agent items excluded."""
import re
import time
from chat_store import connection, title_for, bind
from notify import api, chunks
from mode import enabled


def prompt_text(text):
    # Remove only the known IDE envelope anchored at the start. Preserve ordinary
    # text, Markdown and anything after the actual user-request heading.
    match = re.match(r"\A\s*# Context from my IDE setup:\s*\n[\s\S]*?\n## My request:[ \t]*\r?\n", text)
    return text[match.end():].strip() if match else text


class DesktopMirror:
    def __init__(self, config):
        self.config = config
        self.cutoff = time.time()*1000
        self.observed = set()
        self.last_send = 0
        with connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS mirror_items(thread TEXT, item TEXT, PRIMARY KEY(thread,item))')
            c.execute('CREATE TABLE IF NOT EXISTS mirror_phone(thread TEXT, item TEXT, PRIMARY KEY(thread,item))')
            c.execute('CREATE TABLE IF NOT EXISTS mirror_parts(thread TEXT, item TEXT, part INTEGER, PRIMARY KEY(thread,item,part))')

    def switch(self):
        self.cutoff = time.time()*1000
        self.observed.clear()

    def phone(self, thread, client_id):
        # Save origin before IPC dispatch; survive timeout/restart and snapshots
        # arriving before confirmation. Never infer origin from matching text.
        with connection() as c:
            c.execute('INSERT OR IGNORE INTO mirror_phone VALUES (?,?)',(thread,client_id))

    def observe(self, thread, state):
        on = enabled()
        turns = {t.get('turnId') or t.get('id'):t for t in state.get('turns',[])}
        canonical = (state.get('turnHistory') or {}).get('history',{}).get('entitiesByKey',{})
        turns.update({t.get('turnId') or t.get('id') or k:t for k,t in canonical.items()})
        with connection() as c:
            seen = {r[0] for r in c.execute('SELECT item FROM mirror_items WHERE thread=?',(thread,))}
            phone = {r[0] for r in c.execute('SELECT item FROM mirror_phone WHERE thread=?',(thread,))}
        first = thread not in self.observed
        for turn in sorted(turns.values(),key=lambda t:t.get('turnStartedAtMs') or 0):
            old = first and (turn.get('turnStartedAtMs') or 0)<self.cutoff
            for item in turn.get('items',[]):
                ident = item.get('id')
                if not ident or ident in seen or item.get('type') not in ('userMessage','steeringUserMessage'):
                    continue
                if not old and on and ident not in phone and item.get('clientId') not in phone:
                    content = item.get('content') or item.get('input') or []
                    text = prompt_text('\n'.join(x.get('text','') for x in content if x.get('type')=='text'))
                    if any(x.get('type') in ('image','localImage') for x in content):
                        text += '\n[Изображение в VS Code]'
                    if text.strip():
                        self.deliver(thread,ident,text)
                with connection() as c:
                    c.execute('INSERT OR IGNORE INTO mirror_items VALUES (?,?)',(thread,ident))
                seen.add(ident)
        self.observed.add(thread)

    def deliver(self,thread,ident,text):
        for index,part in enumerate(chunks(text,limit=3000)):
            if not enabled():return
            with connection() as c:
                if c.execute('SELECT 1 FROM mirror_parts WHERE thread=? AND item=? AND part=?',(thread,ident,index)).fetchone():
                    continue
            pause = 1.1-(time.monotonic()-self.last_send)
            if pause>0:time.sleep(pause)
            if not enabled():return
            result = api(self.config['token'],'sendMessage',{
                'chat_id':self.config['chat_id'],'text':part,
                '_presentation':{'title':title_for(thread),'author':'user','event':'Ты · с компьютера','body':part},
                'link_preview_options':{'is_disabled':True}})
            self.last_send = time.monotonic()
            bind(self.config['chat_id'],result.get('message_id'),thread)
            with connection() as c:
                c.execute('INSERT OR IGNORE INTO mirror_parts VALUES (?,?,?)',(thread,ident,index))
