"""Offer only local files explicitly linked by the final answer; upload on click."""
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import unquote
import uuid
from notify import tls_context
from chat_store import bind

MAX_BYTES=50*1024*1024

def allowed(path,cwd):
    root=Path(cwd).resolve()
    p=Path(path).resolve()
    return p.is_relative_to(root) and p.is_file() and not any(x in ('.git','.secrets','.aws','.codex') for x in p.relative_to(root).parts) and p.name not in ('credentials.json','.env')


def files_in(answer,cwd):
    if not cwd:
        return []
    result=[]
    for match in re.finditer(r'\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]+)\)',answer or ''):
        value=match.group(1).strip('<>')
        if '://' in value and not value.startswith('file://'):
            continue
        value=unquote(value.removeprefix('file://'))
        value=re.sub(r':\d+(?::\d+)?$','',value).split('#',1)[0]
        p=Path(value)
        if not p.is_absolute():
            p=Path(cwd)/p
        if allowed(p,cwd) and str(p.resolve()) not in result:
            result.append(str(p.resolve()))
    return result[:20]


def send_document(config,path,cwd,thread,title):
    if not allowed(path,cwd):
        raise RuntimeError('Файл недоступен или находится вне папки проекта.')
    p=Path(path).resolve()
    with p.open('rb') as f:
        payload=f.read(MAX_BYTES+1)
    if len(payload)>MAX_BYTES:
        raise RuntimeError('Файл больше 50 МБ. Подготовь файл меньшего размера.')
    boundary='pult'+uuid.uuid4().hex
    caption='Беседа: '+title[:120]+'\nЧат: '+thread
    parts=[]
    for key,value in [('chat_id',str(config['chat_id'])),('caption',caption)]:
        parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode())
    filename=''.join(c for c in p.name if c not in '\r\n"\\') or 'file'
    parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="document"; filename="'+filename+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    parts.extend([payload,('\r\n--'+boundary+'--\r\n').encode()])
    request=urllib.request.Request('https://api.telegram.org/bot'+config['token']+'/sendDocument',data=b''.join(parts),headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    try:
        with urllib.request.urlopen(request,timeout=60,context=tls_context()) as response:
            data=json.load(response)
        if not data.get('ok'):
            raise RuntimeError('Telegram отклонил отправку файла.')
    except (OSError,ValueError):
        raise RuntimeError('Не удалось отправить файл в Telegram. Проверь сеть и повтори.') from None
    bind(config['chat_id'],data['result']['message_id'],thread)
