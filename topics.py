"""Persistent one-to-one Telegram topic ↔ Codex thread routing."""
import fcntl
from pathlib import Path
import time
from chat_store import connection, title_for

_cache = {}
LOCK = Path(__file__).resolve().with_name('topics.lock')


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS telegram_topics(chat TEXT, topic INTEGER, thread TEXT, PRIMARY KEY(chat,topic), UNIQUE(chat,thread))')


def thread_for(chat, topic):
    if not topic: return None
    with connection() as c:
        setup(c)
        row=c.execute('SELECT thread FROM telegram_topics WHERE chat=? AND topic=?',(str(chat),topic)).fetchone()
    return row[0] if row else None


def bind_topic(chat, topic, thread):
    if not isinstance(topic,int) or topic<=0 or not thread: raise ValueError('Invalid topic binding')
    with connection() as c:
        setup(c)
        rows=c.execute('SELECT topic,thread FROM telegram_topics WHERE chat=? AND (topic=? OR thread=?)',(str(chat),topic,thread)).fetchall()
        if any(t!=topic or r!=thread for t,r in rows):return False
        c.execute('INSERT OR IGNORE INTO telegram_topics VALUES (?,?,?)',(str(chat),topic,thread))
    return True


def topic_for(chat, thread):
    with connection() as c:
        setup(c)
        row=c.execute('SELECT topic FROM telegram_topics WHERE chat=? AND thread=?',(str(chat),thread)).fetchone()
    return row[0] if row else None


def ensure_topic(token, chat, thread, api):
    topic=topic_for(chat,thread)
    if topic:return topic
    cached=_cache.get(token)
    if not cached or time.monotonic()-cached[0]>60:
        on=bool(api(token,'getMe',{}).get('has_topics_enabled'))
        _cache[token]=(time.monotonic(),on)
    else:on=cached[1]
    if not on:return None
    # notify hook and bridge may create the same topic simultaneously.
    with LOCK.open('a') as lock:
        LOCK.chmod(0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        topic=topic_for(chat,thread)
        if topic:return topic
        result=api(token,'createForumTopic',{'chat_id':chat,'name':title_for(thread)[:128] or 'Беседа Codex'})
        topic=result['message_thread_id']
        if not bind_topic(chat,topic,thread):raise RuntimeError('Не удалось закрепить тему. Выберите беседу заново.')
        return topic
