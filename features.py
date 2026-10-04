"""Telegram queue, VS Code approval snapshots, and result-file controls."""
import copy
import json
import time
import uuid
from chat_store import remember, title_for, details, reply_thread
from mode import enabled
from outgoing import files_in, send_document
from vscode_ipc import IpcError

METHODS={'item/commandExecution/requestApproval':'thread-follower-command-approval-decision',
         'item/fileChange/requestApproval':'thread-follower-file-approval-decision',
         'item/permissions/requestApproval':'thread-follower-permissions-request-approval-response'}

class WaitForChat(RuntimeError):
    pass

class Features:
    def init_features(self):
        self.snapshots={}
        self.snapshot_poll=0
        self.queue_poll=0
        self.queue_items=self.state.setdefault('queue',[])
        if self.active and self.active.get('queueId'):
            self.queue_items[:] = [i for i in self.queue_items if i['id'] != self.active['queueId']]
        for item in self.queue_items:
            if item.get('status')=='dispatching':
                item['status']='uncertain'
                item['error']='Мост перезапустился во время отправки. Проверь историю чата.'
        self.save()

    def target_thread(self,message):
        return reply_thread(self.config['chat_id'],message.get('reply_to_message',{})) or self.state.get('thread')

    def enqueue(self,message):
        if not enabled():
            self.say('Удалённый режим выключен. Отправь /on перед новым промптом.')
            return
        thread=self.target_thread(message)
        if not thread:
            self.say('Сначала выбери чат: /chats.')
            return
        if message.get('media_group_id'):
            self.say('Альбомы пока не поддерживаются. Отправь одно вложение отдельно.')
            return
        if len(self.queue_items)>=20:
            self.say('В очереди уже 20 промптов. Удали ненужные через /queue.')
            return
        item={'id':uuid.uuid4().hex[:16],'thread':thread,'message':copy.deepcopy(message),'status':'pending'}
        self.queue_items.append(item)
        self.save()
        self.say('В очереди: '+str(len(self.queue_items))+'.\nБеседа: '+title_for(thread)+'\nЧат: '+thread, thread=thread)

    def show_queue(self):
        buttons=[]
        lines=['Очередь: '+str(len(self.queue_items)), 'Приостановлена' if self.state.get('queue_paused') or not enabled() else 'Включена']
        for index,item in enumerate(self.queue_items):
            msg=item['message'];preview=(msg.get('text') or msg.get('caption') or 'Вложение').replace('\n',' ')[:80]
            lines.append(str(index+1)+'. '+title_for(item['thread'])+' — '+preview+' ['+item['status']+']')
            if item.get('error'):
                lines.append(item['error'])
            buttons.append([{'text':'Удалить №'+str(index+1),'callback_data':'qdel:'+item['id']}])
        buttons.append([{'text':'⏸ Пауза','callback_data':'qctl:pause'}, {'text':'▶ Продолжить','callback_data':'qctl:resume'}])
        if self.queue_items:
            buttons.append([{'text':'Очистить очередь','callback_data':'qctl:clear'}])
        self.say('\n'.join(lines),markup={'inline_keyboard':buttons})

    def queue_control(self,action,key):
        if action=='qdel':
            self.queue_items[:]=[i for i in self.queue_items if i['id']!=key]
        elif key=='clear':
            self.queue_items.clear()
        elif key in ('pause','resume'):
            self.state['queue_paused']=key=='pause'
        self.save()
        self.show_queue()

    def drain_queue(self):
        if self.active or not enabled() or self.state.get('queue_paused') or not self.queue_items:
            return
        if time.monotonic()-self.queue_poll<3:
            return
        self.queue_poll=time.monotonic()
        item=self.queue_items[0]
        if item['status']!='pending':
            return
        try:
            self.start_prompt(item['thread'],item['message'],item)
        except WaitForChat as error:
            detail=str(error)
            if item.get('error')!=detail:
                item['error']=detail
                self.save()
                self.say('Очередь ждёт. '+detail+'\nЧат: '+item['thread'],thread=item['thread'])
            return
        except (RuntimeError,KeyError,OSError) as error:
            uncertain=isinstance(error,IpcError) and str(error) in ('request-timeout','connection-closed','client-disconnected')
            item['status']='uncertain' if uncertain or item['status']=='dispatching' else 'failed'
            item['error']='Запуск не подтверждён. Проверь историю перед повтором.' if item['status']=='uncertain' else (str(error) if isinstance(error,RuntimeError) else 'Не удалось подготовить промпт.')
            self.save()
            self.say('Очередь остановилась: '+item['error']+'\n/queue')
            return
        self.queue_items.pop(0)
        self.save()
        self.say('Промпт запущен.\nБеседа: '+title_for(item['thread'])+'\nЧат: '+item['thread'], thread=item['thread'])

    def poll_snapshots(self):
        if not enabled():
            return
        if time.monotonic()-self.snapshot_poll<5:
            return
        self.snapshot_poll=time.monotonic()
        ids={self.state.get('thread')}
        if self.active:
            ids.add(self.active['threadId'])
        if self.queue_items:
            ids.add(self.queue_items[0]['thread'])
        # Subscribe to recent user chats as well, for requests launched on desktop.
        try:
            recent=self.call('thread/list',{'limit':10,'sortKey':'updated_at','sortDirection':'desc'}).get('data',[])
            ids.update(t['id'] for t in recent if not t.get('parentThreadId'))
            ipc=self.connect_ipc()
            for thread in ids-{None}:
                ipc.follow(thread)
        except (RuntimeError,OSError):
            return

    def stream_event(self,event):
        if event.get('method')!='thread-stream-state-changed' or event.get('version')!=11:
            return
        params=event.get('params',{})
        if params.get('hostId')!='local':
            return
        change=params.get('change',{})
        if change.get('type')!='snapshot':
            return
        thread=params.get('conversationId');state=change.get('conversationState',{})
        if state.get('id')!=thread:
            return
        self.snapshots[thread]={'state':state,'owner':event.get('sourceClientId'),'received':time.monotonic()}
        remember(thread,state.get('title'),state.get('cwd'))
        requests=[r for r in state.get('requests',[]) if not r.get('completed') and r.get('method') in METHODS]
        live={json.dumps(r.get('id'),sort_keys=True) for r in requests}
        for key,value in list(self.approvals.items()):
            if value.get('transport')=='vscode' and value['thread']==thread and json.dumps(value['request']['id'],sort_keys=True) not in live:
                self.approvals.pop(key,None)
        if not enabled():
            return
        for request in requests:
            if any(a.get('transport')=='vscode' and a['thread']==thread and a['request']['id']==request['id'] for a in self.approvals.values()):
                continue
            key=uuid.uuid4().hex[:16]
            value={'transport':'vscode','thread':thread,'request':request,'owner':event['sourceClientId']}
            self.approvals[key]=value
            p=request.get('params',{})
            action=p.get('command') or p.get('reason') or p.get('grantRoot') or request['method']
            description='Разрешение требуется\nБеседа: '+title_for(thread)+'\nЧат: '+thread+'\n\n'+str(action)
            if p.get('cwd'):
                description+='\nПапка: '+str(p['cwd'])
            if p.get('permissions'):
                description+='\nЗапрошенный доступ:\n'+json.dumps(p['permissions'],ensure_ascii=False)
            self.say(description,notification=True,thread=thread,markup={'inline_keyboard':[[{'text':'✅ Разрешить один раз','callback_data':'approve:'+key},{'text':'❌ Отклонить','callback_data':'decline:'+key}]]})

    def decide_approval(self,key,accept):
        if not enabled():
            self.say('Удалённый режим выключен. Разрешение можно дать в VS Code или после /on.')
            return
        value=self.approvals.get(key)
        if not value:
            self.say('Этот запрос уже решён или кнопка устарела.')
            return
        if value.get('transport')!='vscode':
            self.write({'id':value['id'],'result':{'decision':'accept' if accept else 'decline'}})
        else:
            thread=value['thread'];request=value['request'];ipc=self.connect_ipc()
            snap=self.snapshots.get(thread)
            if not snap or snap['owner']!=value['owner'] or time.monotonic()-snap['received']>10:
                ipc.follow(thread)
                self.say('Проверяю актуальность запроса. Повтори нажатие через несколько секунд.')
                return
            if not any(r.get('id')==request['id'] and not r.get('completed') for r in snap['state'].get('requests',[])):
                self.approvals.pop(key,None)
                self.say('Этот запрос уже решён в VS Code.')
                return
            params={'conversationId':thread,'requestId':request['id']}
            if request['method']=='item/permissions/requestApproval':
                params['response']={'permissions':request['params'].get('permissions',{}) if accept else {},'scope':'turn'}
            else:
                params['decision']='accept' if accept else 'decline'
            ipc.request(METHODS[request['method']],params,target=value['owner'])
        self.approvals.pop(key,None)
        self.say('Разрешение отправлено.' if accept else 'Отказ отправлен.')

    def show_files(self,message):
        thread=self.target_thread(message)
        if not thread:
            self.say('Выбери чат или ответь командой /files на уведомление.')
            return
        data=details(thread)
        paths=files_in(data.get('answer'),data.get('cwd'))
        if not paths:
            self.say('В последнем ответе этой беседы нет доступных ссылок на файлы. Попроси Codex дать ссылку на готовый файл.')
            return
        choices=self.state.setdefault('file_choices',{})
        buttons=[]
        from pathlib import Path
        for path in paths:
            key=uuid.uuid4().hex[:16]
            choices[key]={'path':path,'cwd':data['cwd'],'thread':thread}
            buttons.append([{'text':Path(path).name[:60],'callback_data':'file:'+key}])
        while len(choices)>100:
            choices.pop(next(iter(choices)))
        self.save()
        self.say('Файлы из последнего ответа: '+title_for(thread),thread=thread,markup={'inline_keyboard':buttons})

    def deliver_file(self,key):
        if not enabled():
            self.say('Сначала включи удалённый режим: /on.')
            return
        entry=self.state.get('file_choices',{}).get(key)
        if not entry:
            self.say('Список файлов устарел. Нажми «Файлы» ещё раз.')
            return
        send_document(self.config,entry['path'],entry['cwd'],entry['thread'],title_for(entry['thread']))
