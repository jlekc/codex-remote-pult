"""Atomic Codex queue operations; local queue is only an offline outbox.

No thread/resume or model execution by the bridge app-server. Actual turns
remain owned/executed by VS Code. Do not replace an entire desktop queue.
"""
import time
import queue
import uuid
from mode import enabled
from chat_store import title_for
from media import attachment, download, file_prompt
import progress


class NativeQueue:
    def __init__(self, bridge):
        self.bridge=bridge
        self.receipts=bridge.state.setdefault('native_queue_receipts',{})
        self.poll=0
        self.finish_poll=0
    def items(self, thread):
        result=[];cursor=None
        while True:
            page=self.bridge.call('thread/queue/list',{'threadId':thread,'limit':100,'cursor':cursor})
            result.extend(page['data']);cursor=page.get('nextCursor')
            if not cursor:return result
    def flush(self):
        b=self.bridge
        if not enabled() or b.state.get('queue_paused') or time.monotonic()-self.poll<3:return
        self.poll=time.monotonic()
        # Preserve FIFO within each conversation, while a blocked conversation
        # does not stop independent conversations. An uncertain add blocks only
        # its own thread and is never replayed automatically.
        blocked=set()
        for item in list(b.queue_items):
            thread=item['thread']
            if thread in blocked:continue
            blocked.add(thread)
            if item['status']!='pending':continue
            try:
                ipc=b.connect_ipc();snap=b.snapshots.get(thread)
                if not snap or time.monotonic()-snap['received']>10:
                    ipc.follow(thread);continue
                owner=ipc.owner(thread)
                if not owner or owner!=snap['owner']:continue
            except (RuntimeError,OSError):
                continue
            cid=item.setdefault('client_id',str(uuid.uuid4()));b.save()
            media,_=attachment(item['message'])
            text=(item['message'].get('caption','') if media else item['message'].get('text','')).strip()
            inputs=[{'type':'text','text':text,'text_elements':[]}]
            try:
                if media:
                    path,image=download(b.config,item['message'])
                    if image:
                        inputs[0]['text']=text or 'Посмотри изображение и кратко опиши, что на нём.'
                        inputs.append({'type':'localImage','path':path})
                    else:inputs[0]['text']=file_prompt(text,path)
                b.mirror.phone(thread,cid)
                item['status']='dispatching';b.save()
                result=b.call('thread/queue/add',{'threadId':thread,'clientUserMessageId':cid,'input':inputs})['queuedSubmission']
                self.receipts[result['id']]={'thread':thread,'client_id':cid,'queue_id':item['id']}
                b.queue_items.remove(item);b.save()
            except (RuntimeError,OSError,KeyError,queue.Empty):
                item['status']='uncertain' if item['status']=='dispatching' else 'failed'
                item['error']='Добавление не подтверждено. Проверь очередь и историю VS Code перед повтором.'
                b.save()
                try:b.say(item['error'],thread=thread)
                except (RuntimeError,OSError):pass
                continue
            blocked.discard(thread)
            # Notification failures must not change a confirmed admission into
            # an uncertain item or cause a second add.
            try:ipc.follow(thread)
            except (RuntimeError,OSError):pass

    def show(self, thread):
        b=self.bridge
        if not thread:b.say('Сначала выберите беседу для очереди.');return
        items=self.items(thread);lines=['Общая очередь: '+title_for(thread)]
        snap=b.snapshots.get(thread)
        if snap and (snap['state'].get('threadRuntimeStatus') or {}).get('type')=='active':lines.append('Агент сейчас работает.')
        buttons=[]
        choices={}
        for n,item in enumerate(items,1):
            source='Telegram' if item['id'] in self.receipts else 'Codex / VS Code'
            text=' '.join(x.get('text','') for x in item.get('input',[]) if x.get('type')=='text').replace('\n',' ')[:100] or 'Вложение'
            lines.append(str(n)+'. ['+source+'] '+text)
            key=uuid.uuid4().hex[:16];choices[key]={'thread':thread,'id':item['id']}
            buttons.append([{'text':'Удалить №'+str(n),'callback_data':'nativeq:'+key}])
        if not items:lines.append('Ожидающих заданий в Codex нет.')
        local=[i for i in b.queue_items if i['thread']==thread]
        if local:
            lines.append('Ещё не переданы с телефона: '+str(len(local)))
            for item in local:
                lines.append('• '+str(item['message'].get('text') or 'Вложение')[:80]+' ['+item['status']+']')
                if item.get('error'):lines.append(item['error'])
                label='Убрать локальную запись' if item['status']=='uncertain' else 'Удалить непереданный запрос'
                if item['status']=='uncertain':lines.append('Уборка локальной записи не отменяет возможный запрос в Codex.')
                buttons.append([{'text':label,'callback_data':'qdel:'+item['id']}])
        b.state['native_queue_choices']=choices;b.save()
        lines.append('Отправка из Telegram: '+('приостановлена' if b.state.get('queue_paused') or not enabled() else 'включена'))
        buttons.append([{'text':'⏸ Пауза отправки из Telegram','callback_data':'qctl:pause'},{'text':'▶ Продолжить','callback_data':'qctl:resume'}])
        b.say('\n'.join(lines),thread=thread,markup={'inline_keyboard':buttons})
    def remove(self,key):
        b=self.bridge;choice=b.state.get('native_queue_choices',{}).get(key)
        if not choice:b.say('Кнопка устарела. Откройте очередь заново.');return
        result=b.call('thread/queue/delete',{'threadId':choice['thread'],'queuedSubmissionId':choice['id']})
        if result.get('deleted'):
            receipt=self.receipts.pop(choice['id'],None)
            if receipt:progress.before_start(b.config,receipt['queue_id'])
            b.save()
        if not result.get('deleted'):
            b.say('Запрос уже отсутствует в очереди: возможно, начал выполняться.',thread=choice['thread'])
        self.show(choice['thread'])

    def observe(self, thread, state):
        turns={t.get('turnId') or t.get('id'):t for t in state.get('turns',[])}
        turns.update({t.get('turnId') or t.get('id') or k:t for k,t in (state.get('turnHistory') or {}).get('history',{}).get('entitiesByKey',{}).items()})
        changed=False
        for receipt in self.receipts.values():
            if receipt['thread']!=thread or receipt.get('turn_id'):continue
            cid=receipt['client_id']
            for turn_id,turn in turns.items():
                if turn_id and any(x.get('type') in ('userMessage','steeringUserMessage') and cid in (x.get('id'),x.get('clientId'),x.get('clientUserMessageId')) for x in turn.get('items',[])):
                    receipt['turn_id']=turn_id;changed=True
                    progress.link(self.bridge.config,receipt['queue_id'],thread,turn_id)
                    progress.before_start(self.bridge.config,receipt['queue_id'])
                    break
        if changed:self.bridge.save()

    def poll_finished(self):
        if not enabled() or time.monotonic()-self.finish_poll<3:return
        self.finish_poll=time.monotonic();b=self.bridge
        from notify import send
        threads={r['thread'] for r in self.receipts.values() if r.get('turn_id')}
        for thread in threads:
            try:
                history=b.call('thread/read',{'threadId':thread,'includeTurns':True})['thread']
            except (RuntimeError,OSError,KeyError,queue.Empty):continue
            turns={t.get('id'):t for t in history.get('turns',[])}
            for ident,receipt in list(self.receipts.items()):
                if receipt['thread']!=thread:continue
                turn=turns.get(receipt.get('turn_id'),{})
                status=turn.get('status')
                if status not in ('completed','failed','interrupted'):continue
                # History can temporarily show an unfinished live tail as
                # interrupted. Require the owner's completed snapshot too.
                state=b.snapshots.get(thread,{}).get('state',{})
                live=list(state.get('turns',[]))+list((state.get('turnHistory') or {}).get('history',{}).get('entitiesByKey',{}).values())
                if status=='interrupted' and not any((t.get('turnId') or t.get('id'))==receipt['turn_id'] and t.get('status')=='interrupted' for t in live):continue
                if status=='failed' and not turn.get('error'):continue
                answers=[i.get('text','') for i in turn.get('items',[]) if i.get('type')=='agentMessage' and i.get('phase')!='commentary']
                try:
                    send(b.config,{'thread-id':thread,'turn-id':receipt['turn_id'],'cwd':history.get('cwd') or '.', 'status':status,'last-assistant-message':answers[-1] if answers else 'Последний ответ отсутствует.'})
                except (RuntimeError,OSError):continue
                if progress.confirmed(b.config,thread,receipt['turn_id']):
                    self.receipts.pop(ident,None);b.save()

    def stop(self, thread):
        b=self.bridge
        if not thread:b.say('Сначала выбери беседу.');return
        snap=b.snapshots.get(thread)
        if not snap or time.monotonic()-snap['received']>10:
            b.connect_ipc().follow(thread)
            b.say('Нет свежего состояния беседы. Открой её в VS Code и повтори остановку.',thread=thread);return
        state=snap['state'];turns=list(state.get('turns',[]))+list((state.get('turnHistory') or {}).get('history',{}).get('entitiesByKey',{}).values())
        active=next((t for t in turns if t.get('status')=='inProgress'),None)
        if active:
            ipc=b.connect_ipc();owner=ipc.owner(thread)
            if not owner or owner!=snap['owner']:
                b.say('Владелец беседы изменился. Дождись обновления состояния.',thread=thread);return
            ipc.request('thread-follower-interrupt-turn',{'conversationId':thread,'expectedTurnId':active.get('turnId') or active.get('id'),'mode':'user-stop'},target=owner)
            b.say('Остановка отправлена владельцу беседы.',thread=thread)
        else:b.say('Активного запроса в этой беседе нет.',thread=thread)
        b.say('Отправка новых запросов из Telegram приостановлена. Уже переданные задания смотри в общей очереди.',thread=thread)

    def status(self, thread):
        b=self.bridge
        if not thread:b.say('Беседа не выбрана.');return
        snap=b.snapshots.get(thread)
        fresh=bool(snap and time.monotonic()-snap['received']<=10)
        active=fresh and (snap['state'].get('threadRuntimeStatus') or {}).get('type')=='active'
        b.say('Удалённый режим: '+('включён' if enabled() else 'выключен')+'\n'+('Агент работает' if active else 'Беседа ожидает' if fresh else 'Нет свежего состояния VS Code')+'\nВ общей очереди: '+str(len(self.items(thread)))+'\nЕщё не переданы из Telegram: '+str(sum(i['thread']==thread for i in b.queue_items)),thread=thread)
