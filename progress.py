"""Temporary queue/start notices; replace statuses before the next stage.

Persist IDs per queue/turn so hooks, bridge polling and restart share ownership.
No deletion based on message text, current selected thread or neighboring IDs.
"""
import sqlite3
import time
from chat_store import connection

_last_sweep = 0


def notice(config, queue_id, result):
    if not isinstance(result,dict) or not result.get('message_id'):
        return
    try:
        with connection() as c:
            c.execute('INSERT OR IGNORE INTO progress_notices VALUES (?,?,?)',
                (str(config['chat_id']),queue_id,result['message_id']))
            run=c.execute('SELECT thread,turn FROM progress_runs WHERE chat=? AND queue=?',
                (str(config['chat_id']),queue_id)).fetchone()
        if run:
            cleanup(config,*run)
    except (OSError,sqlite3.Error):
        pass


def link(config, queue_id, thread, turn):
    try:
        with connection() as c:
            c.execute('INSERT OR REPLACE INTO progress_runs VALUES (?,?,?,?)',
                (str(config['chat_id']),queue_id,thread,turn))
        cleanup(config,thread,turn)
    except (OSError,sqlite3.Error):
        pass


def delivered(config, event):
    thread=event.get('thread-id') or event.get('session_id')
    turn=event.get('turn-id') or event.get('turn_id')
    if not thread or not turn:
        return
    try:
        with connection() as c:
            c.execute('INSERT OR IGNORE INTO progress_finals VALUES (?,?,?)',
                (str(config['chat_id']),thread,turn))
        cleanup(config,thread,turn)
    except (OSError,sqlite3.Error):
        pass


def retry(config, event):
    thread=event.get('thread-id') or event.get('session_id')
    turn=event.get('turn-id') or event.get('turn_id')
    if thread and turn:
        cleanup(config,thread,turn)


def cleanup(config, thread, turn, before=False):
    try:
        chat=str(config['chat_id'])
        with connection() as c:
            if not before and not c.execute('SELECT 1 FROM progress_finals WHERE chat=? AND thread=? AND turn=?',
                (chat,thread,turn)).fetchone():
                return
            rows=c.execute('SELECT n.message FROM progress_notices n JOIN progress_runs r ON n.chat=r.chat AND n.queue=r.queue WHERE r.chat=? AND r.thread=? AND r.turn=?',
                (chat,thread,turn)).fetchall()
        # Lazy import avoids notify/progress cycle; notify's API sanitizes failures.
        from notify import api
        for start in range(0,len(rows),100):
            ids=[r[0] for r in rows[start:start+100]]
            api(config['token'],'deleteMessages',{'chat_id':config['chat_id'],'message_ids':ids})
            with connection() as c:
                c.executemany('DELETE FROM progress_notices WHERE chat=? AND message=?',[(chat,m) for m in ids])
    except (RuntimeError,OSError,sqlite3.Error,ValueError):
        # Keep IDs for retry; failed housekeeping must not resend the final answer.
        pass


def sweep(config):
    global _last_sweep
    if time.monotonic()-_last_sweep<30:
        return
    _last_sweep=time.monotonic()
    try:
        with connection() as c:
            rows=c.execute('SELECT DISTINCT r.thread,r.turn FROM progress_runs r JOIN progress_finals f ON f.chat=r.chat AND f.thread=r.thread AND f.turn=r.turn JOIN progress_notices n ON n.chat=r.chat AND n.queue=r.queue WHERE r.chat=?',(str(config['chat_id']),)).fetchall()
        for thread,turn in rows:
            cleanup(config,thread,turn)
    except (OSError,sqlite3.Error):
        pass


def before_answer(config,event):
    thread=event.get('thread-id') or event.get('session_id')
    turn=event.get('turn-id') or event.get('turn_id')
    if thread and turn:
        cleanup(config,thread,turn,before=True)


def before_start(config,queue_id):
    try:
        chat=str(config['chat_id'])
        with connection() as c:
            rows=c.execute('SELECT message FROM progress_notices WHERE chat=? AND queue=?',(chat,queue_id)).fetchall()
        from notify import api
        for start in range(0,len(rows),100):
            ids=[r[0] for r in rows[start:start+100]]
            api(config['token'],'deleteMessages',{'chat_id':config['chat_id'],'message_ids':ids})
            with connection() as c:
                c.executemany('DELETE FROM progress_notices WHERE chat=? AND message=?',[(chat,m) for m in ids])
    except (RuntimeError,OSError,sqlite3.Error,ValueError):
        pass


def confirmed(config, thread, turn):
    """True only after the entire final answer reached this Telegram chat."""
    try:
        with connection() as c:
            return bool(c.execute('SELECT 1 FROM progress_finals WHERE chat=? AND thread=? AND turn=?',
                (str(config['chat_id']),thread,turn)).fetchone())
    except (OSError,sqlite3.Error):
        return False
