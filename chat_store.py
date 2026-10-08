"""Shared routing metadata for the notify hook and bridge (no credentials)."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parent
DB = ROOT/'messages.sqlite3'

@contextmanager
def connection():
    fd = os.open(DB, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    DB.chmod(0o600)
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS messages(chat TEXT, message TEXT, thread TEXT, PRIMARY KEY(chat,message))')
        conn.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, answer TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY, thread TEXT, cwd TEXT, path TEXT, UNIQUE(thread,cwd,path))')
        conn.execute('CREATE TABLE IF NOT EXISTS question_replies(chat TEXT, message TEXT, question_key TEXT, PRIMARY KEY(chat,message))')
        conn.execute('CREATE TABLE IF NOT EXISTS progress_runs(chat TEXT, queue TEXT, thread TEXT, turn TEXT, PRIMARY KEY(chat,queue))')
        conn.execute('CREATE TABLE IF NOT EXISTS progress_notices(chat TEXT, queue TEXT, message INTEGER, PRIMARY KEY(chat,message))')
        conn.execute('CREATE TABLE IF NOT EXISTS progress_finals(chat TEXT, thread TEXT, turn TEXT, PRIMARY KEY(chat,thread,turn))')
        yield conn
        conn.commit()
    finally:
        conn.close()


def remember(thread, title=None, cwd=None, answer=None):
    if not thread:
        return
    with connection() as c:
        c.execute('INSERT OR IGNORE INTO threads(id) VALUES (?)', (thread,))
        for column, value in [('title',title),('cwd',cwd),('answer',answer)]:
            if value is not None:
                c.execute('UPDATE threads SET '+column+'=? WHERE id=?', (value,thread))


def details(thread):
    with connection() as c:
        r=c.execute('SELECT title,cwd,answer FROM threads WHERE id=?',(thread,)).fetchone()
    return dict(zip(('title','cwd','answer'),r)) if r else {}


def title_for(thread):
    if not thread:
        return 'Без названия'
    home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    # Read the installed Codex local title, without opening a thread for writing.
    for path in sorted(home.glob('state_*.sqlite'), reverse=True):
        try:
            c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
            try:
                cols={r[1] for r in c.execute('PRAGMA table_info(threads)')}
                fields=[k for k in ('name','title','preview') if k in cols]
                if fields:
                    row=c.execute('SELECT '+','.join(fields)+' FROM threads WHERE id=?',(thread,)).fetchone()
                    if row:
                        for value in row:
                            if value and str(value).strip():
                                return ' '.join(str(value).split())[:120]
            finally:
                c.close()
        except (OSError,sqlite3.Error):
            pass
    data=details(thread)
    return ' '.join((data.get('title') or thread).split())[:120]


def bind(chat, message, thread):
    if thread and message is not None:
        with connection() as c:
            c.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?)',(str(chat),str(message),thread))


def reply_thread(chat, reply):
    with connection() as c:
        row=c.execute('SELECT thread FROM messages WHERE chat=? AND message=?',(str(chat),str(reply.get('message_id')))).fetchone()
    if row:
        return row[0]
    text=reply.get('text') or reply.get('caption') or ''
    match=re.search(r'^Чат: ([A-Za-z0-9_-]+)$',text,re.MULTILINE)
    return match.group(1) if match else None


def register_download(thread, cwd, path):
    with connection() as c:
        row = c.execute('SELECT id FROM downloads WHERE thread=? AND cwd=? AND path=?',
                        (thread, str(cwd), str(path))).fetchone()
        if row:
            return row[0]
        key = uuid.uuid4().hex[:16]
        c.execute('INSERT OR IGNORE INTO downloads VALUES (?,?,?,?)', (key, thread, str(cwd), str(path)))
        return c.execute('SELECT id FROM downloads WHERE thread=? AND cwd=? AND path=?',
                         (thread, str(cwd), str(path))).fetchone()[0]


def download_entry(key):
    with connection() as c:
        row = c.execute('SELECT thread,cwd,path FROM downloads WHERE id=?', (key,)).fetchone()
    return dict(zip(('thread','cwd','path'), row)) if row else None


def bind_question(chat, message, key):
    if message is not None:
        with connection() as c:
            c.execute('INSERT OR REPLACE INTO question_replies VALUES (?,?,?)',
                      (str(chat),str(message),key))


def question_reply(chat, message):
    with connection() as c:
        row=c.execute('SELECT question_key FROM question_replies WHERE chat=? AND message=?',
                      (str(chat),str(message))).fetchone()
    return row[0] if row else None
