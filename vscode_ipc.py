"""Adapter for the installed Codex extension's private thread-follower IPC.

Version-specific protocol. No second writer and no extension file modifications.
"""
import json
import os
from pathlib import Path
import queue
import socket
import stat
import struct
import threading
import uuid

VERSIONS = {'initialize': 0, 'thread-owner-discovery': 1,
    'thread-follower-start-turn': 2, 'thread-follower-load-complete-history': 1,
    'thread-follower-interrupt-turn': 4,
    'thread-follower-command-approval-decision': 1,
    'thread-follower-file-approval-decision': 1,
    'thread-follower-permissions-request-approval-response': 1}


def turn_start_params(thread, text, images=None):
    # This is the webview's internal input format, not app-server UserInput.
    # The installed UI calls text_elements.some() without a null guard.
    return {'conversationId': thread, 'turnStart': {
        'request': {'threadId': thread,
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}] +
                             [{'type': 'localImage', 'path': path} for path in (images or [])],
                    'clientUserMessageId': str(uuid.uuid4())},
        'context': {'inheritThreadSettings': True}}}


class IpcError(RuntimeError):
    pass


class VSCodeIPC:
    def __init__(self, emit):
        self.emit = emit
        self.pending = {}
        self.lock = threading.Lock()
        self.client_id = 'initializing-client'
        self.closed = False
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'ipc/ipc.sock'
        info = path.stat()
        parent = path.parent.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise IpcError('Небезопасный локальный сокет Codex')
        self.socket.connect(str(path))
        threading.Thread(target=self.read, daemon=True).start()
        response = self.request('initialize', {'clientType': 'telegram-bridge'})
        self.client_id = response['result']['clientId']

    def write(self, data):
        payload = json.dumps(data).encode()
        with self.lock:
            self.socket.sendall(struct.pack('<I', len(payload)) + payload)

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.socket.recv(size-len(data))
            if not part:
                raise ConnectionError('IPC disconnected')
            data.extend(part)
        return data

    def read(self):
        try:
            while not self.closed:
                size = struct.unpack('<I', self.exact(4))[0]
                if not 0 < size <= 256*1024*1024:
                    raise ValueError('Invalid IPC frame')
                event = json.loads(self.exact(size))
                kind = event.get('type')
                if kind == 'response':
                    pending = self.pending.get(event.get('requestId'))
                    if pending:
                        pending.put(event)
                elif kind == 'client-discovery-request':
                    self.write({'type': 'client-discovery-response', 'requestId': event['requestId'],
                                'response': {'canHandle': False}})
                elif kind == 'broadcast':
                    self.emit(event)
        except (OSError, ValueError):
            self.closed = True
            for pending in list(self.pending.values()):
                pending.put({'resultType': 'error', 'error': 'connection-closed'})

    def request(self, method, params, target=None, timeout=20):
        if self.closed:
            raise IpcError('connection-closed')
        key = str(uuid.uuid4())
        result = queue.Queue()
        self.pending[key] = result
        data = {'type': 'request', 'requestId': key, 'sourceClientId': self.client_id,
                'method': method, 'params': params, 'version': VERSIONS[method],
                'timeoutMs': int(timeout*1000)}
        if target:
            data['targetClientId'] = target
        try:
            self.write(data)
            response = result.get(timeout=timeout+1)
        except queue.Empty:
            raise IpcError('request-timeout') from None
        finally:
            self.pending.pop(key, None)
        if response.get('resultType') != 'success':
            raise IpcError(response.get('error', 'IPC request failed'))
        return response

    def follow(self, thread):
        self.write({'type': 'broadcast', 'method': 'thread-stream-following-changed',
                    'sourceClientId': self.client_id, 'version': 1,
                    'params': {'conversationId': thread, 'hostId': 'local', 'following': True}})

    def owner(self, thread):
        try:
            response = self.request('thread-owner-discovery',
                                    {'hostId': 'local', 'conversationId': thread})
            return response['handledByClientId']
        except IpcError as error:
            if str(error) == 'no-client-found':
                return None
            raise

    def close(self):
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
