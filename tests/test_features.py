import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
import chat_store
import features
import notify
import outgoing
import ui
from vscode_ipc import IpcError

class FakeIPC:
    def __init__(self): self.sent=[]; self.followed=[]
    def follow(self,t): self.followed.append(t)
    def owner(self,t): return 'owner'
    def request(self,m,p,**kw):
        self.sent.append((m,p,kw))
        return {'result':{'result':{'turn':{'id':'turn'}}}}

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.patches=[patch.object(chat_store,'DB',self.root/'messages.sqlite3'),patch.dict(os.environ,{'CODEX_HOME':str(self.root)}),patch.object(features,'enabled',return_value=True)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def bot(self):
        b=bridge.Bridge.__new__(bridge.Bridge)
        b.config={'chat_id':123,'token':'dummy'};b.state={'thread':'A'};b.active=None;b.approvals={};b.save=lambda:None;b.said=[];b.say=lambda t,**kw:b.said.append((t,kw));b.call=lambda m,p: {'thread':{'id':p.get('threadId'),'cwd':str(self.root)}}
        b.init_features();ipc=FakeIPC();b.connect_ipc=lambda:ipc
        return b,ipc
    def snapshot(self,b,thread='A',requests=None,status='idle'):
        state={'id':thread,'title':'Чат '+thread,'cwd':str(self.root),'turns':[],'requests':requests or [],'threadRuntimeStatus':{'type':status}}
        b.stream_event({'type':'broadcast','method':'thread-stream-state-changed','version':11,'sourceClientId':'owner','params':{'hostId':'local','conversationId':thread,'change':{'type':'snapshot','conversationState':state}}})
    def msg(self,text='test',**kw):return {'chat':{'id':123,'type':'private'},'text':text,**kw}
    def test_all_reply_chunks_route_and_preserve_answer(self):
        answer='Я🙂'*4200; sent=[]
        def api(token,method,data):sent.append(data);return {'message_id':len(sent)}
        with patch.object(notify,'api',side_effect=api),patch.object(notify.time,'sleep'):
            notify.send({'token':'dummy','chat_id':123},{'thread-id':'A','cwd':str(self.root),'thread-name':'Мой чат','last-assistant-message':answer},force=True)
        self.assertGreater(len(sent),2)
        for i,data in enumerate(sent):
            self.assertIn('Беседа: Мой чат',data['text'])
            self.assertIn('\nЧат: A\n',data['text'])
            self.assertLessEqual(len(data['text'].encode('utf-16-le'))//2,4096)
            self.assertEqual(chat_store.reply_thread(123,{'message_id':i+1}), 'A')
        self.assertEqual(''.join(d['text'].split('\n\n',1)[1] for d in sent),answer)
    def test_queue_chat_is_fixed_and_controls_work_while_busy(self):
        b,ipc=self.bot();b.active={'threadId':'A'}
        chat_store.bind(123,99,'B')
        b.message(self.msg(reply_to_message={'message_id':99,'text':'wrong'}))
        self.assertEqual(b.queue_items[0]['thread'],'B')
        b.state['thread']='C'
        self.assertEqual(b.queue_items[0]['thread'],'B')
        b.message(self.msg('/use D'))
        self.assertEqual(b.state['thread'],'D')
        b.message(self.msg('/queue'))
        self.assertIn('Очередь:',b.said[-1][0])
    def test_busy_state_then_idle_dispatch_once(self):
        b,ipc=self.bot();b.enqueue(self.msg())
        self.snapshot(b,status='active');b.drain_queue()
        self.assertFalse(ipc.sent);self.assertEqual(len(b.queue_items),1)
        self.snapshot(b);b.queue_poll=0;b.drain_queue()
        self.assertEqual(len(ipc.sent),1);self.assertEqual(len(b.queue_items),0)
        self.assertEqual(b.active['turnId'],'turn')
        b.queue_poll=0;b.drain_queue();self.assertEqual(len(ipc.sent),1)
    def test_off_and_queue_pause_do_not_dispatch(self):
        b,ipc=self.bot();b.enqueue(self.msg());self.snapshot(b)
        with patch.object(features,'enabled',return_value=False): b.drain_queue()
        self.assertFalse(ipc.sent)
        b.state['queue_paused']=True;b.drain_queue();self.assertFalse(ipc.sent)
        b.queue_control('qctl','resume');b.queue_poll=0;b.drain_queue();self.assertEqual(len(ipc.sent),1)
    def test_missing_snapshot_no_dispatch(self):
        b,ipc=self.bot();b.enqueue(self.msg());b.drain_queue()
        self.assertFalse(ipc.sent);self.assertEqual(ipc.followed,['A'])
    def test_restart_never_replays_unconfirmed_dispatch(self):
        b,ipc=self.bot();b.queue_items.append({'id':'q','thread':'A','message':self.msg(),'status':'dispatching'})
        b.init_features();self.assertEqual(b.queue_items[0]['status'],'uncertain');self.snapshot(b);b.drain_queue();self.assertFalse(ipc.sent)
    def test_restart_reconciles_known_active_turn(self):
        b,ipc=self.bot();b.queue_items.append({'id':'q','thread':'A','message':self.msg(),'status':'dispatching'})
        b.active={'queueId':'q'};b.init_features();self.assertFalse(b.queue_items)
    def test_timeout_blocks_automatic_retry(self):
        b,ipc=self.bot();b.enqueue(self.msg());self.snapshot(b)
        ipc.request=lambda *a,**k: (_ for _ in ()).throw(IpcError('request-timeout'))
        b.drain_queue();self.assertEqual(b.queue_items[0]['status'],'uncertain')
        b.queue_poll=0;b.drain_queue();self.assertIsNone(b.active)
    def test_command_and_file_approval_exact_route_single_use(self):
        for method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
            b,ipc=self.bot();r={'id':7,'method':method,'params':{'command':'echo test','cwd':str(self.root)}}
            self.snapshot(b,requests=[r]);key=next(iter(b.approvals));self.snapshot(b,requests=[r]);self.assertEqual(len(b.approvals),1)
            b.decide_approval(key,True)
            self.assertEqual(ipc.sent[0][0],features.METHODS[method]);self.assertEqual(ipc.sent[0][1],{'conversationId':'A','requestId':7,'decision':'accept'});self.assertEqual(ipc.sent[0][2]['target'],'owner')
            b.decide_approval(key,True);self.assertEqual(len(ipc.sent),1)
    def test_permissions_are_exact_and_turn_scoped(self):
        b,ipc=self.bot();perms={'network':{'enabled':True}};r={'id':'request','method':'item/permissions/requestApproval','params':{'permissions':perms}}
        self.snapshot(b,requests=[r]);b.decide_approval(next(iter(b.approvals)),True)
        self.assertEqual(ipc.sent[0][1]['response'],{'permissions':perms,'scope':'turn'})
    def test_desktop_resolved_and_mode_off_approvals_are_not_sent(self):
        b,ipc=self.bot();r={'id':'x','method':'item/fileChange/requestApproval','params':{}}
        self.snapshot(b,requests=[r]);key=next(iter(b.approvals));self.snapshot(b,requests=[]);b.decide_approval(key,True);self.assertFalse(ipc.sent)
        self.snapshot(b,requests=[r]);key=next(iter(b.approvals))
        with patch.object(features,'enabled',return_value=False):b.decide_approval(key,True)
        self.assertFalse(ipc.sent)
    def test_file_candidates_boundaries_and_multipart_caption_routes(self):
        p=self.root/'результат.txt';p.write_text('hello')
        bad=self.root/'credentials.json';bad.write_text('{}')
        outside=self.root.parent/'pult-outside.txt';outside.write_text('external')
        link=self.root/'outside.txt';link.symlink_to(outside)
        try:
            text=f'[готово]({p}:12) [secret]({bad}) [external]({link}) [web](https://example.com/doc.pdf)'
            self.assertEqual(outgoing.files_in(text,str(self.root)),[str(p)])
            captured=[]
            def urlopen(req,**kw):captured.append(req);return io.BytesIO(json.dumps({'ok':True,'result':{'message_id':123}}).encode())
            with patch.object(outgoing.urllib.request,'urlopen',side_effect=urlopen):outgoing.send_document({'chat_id':123,'token':'dummy'},p,str(self.root),'A','Чат A')
            payload=captured[0].data
            self.assertIn(b'name="document"',payload);self.assertIn(b'hello',payload);self.assertIn('Чат: A'.encode(),payload)
            self.assertEqual(chat_store.reply_thread(123,{'message_id':123}), 'A')
        finally:outside.unlink(missing_ok=True)
    def test_file_network_error_has_no_token(self):
        p=self.root/'result.txt';p.write_text('hi')
        with patch.object(outgoing.urllib.request,'urlopen',side_effect=OSError('secret token')):
            with self.assertRaisesRegex(RuntimeError,'Не удалось отправить') as e:outgoing.send_document({'chat_id':123,'token':'super-secret'},p,str(self.root),'A','Title')
            self.assertNotIn('secret',str(e.exception))
    def test_queued_image_and_file_are_downloaded_only_when_ready(self):
        for image in (True,False):
            b,ipc=self.bot()
            message=self.msg('',document={'file_id':'file'},caption='Посмотри')
            b.enqueue(message)
            with patch.object(bridge,'download',return_value=(str(self.root/'result.png'),image)) as dl:
                self.snapshot(b,status='active');b.drain_queue();dl.assert_not_called()
                self.snapshot(b);b.queue_poll=0;b.drain_queue();self.assertEqual(dl.call_count,1)
                inputs=ipc.sent[0][1]['turnStart']['request']['input']
                self.assertEqual(inputs[0]['text_elements'],[])
                if image:
                    self.assertEqual(inputs[1]['type'],'localImage')
                else:
                    self.assertEqual(len(inputs),1);self.assertIn('result.png',inputs[0]['text'])
    def test_stale_approval_requires_refresh(self):
        b,ipc=self.bot();r={'id':'x','method':'item/fileChange/requestApproval','params':{}}
        self.snapshot(b,requests=[r]);key=next(iter(b.approvals));b.snapshots['A']['received']-=20
        b.decide_approval(key,True);self.assertFalse(ipc.sent);self.assertEqual(ipc.followed,['A'])
    def test_off_keeps_vscode_approval_in_owner(self):
        b,ipc=self.bot();r={'id':'x','method':'item/fileChange/requestApproval','params':{}}
        self.snapshot(b,requests=[r]);b.write=lambda _:self.fail('must not respond on standalone server')
        with patch.object(bridge,'set_enabled'):
            b.message(self.msg('/off'))
        self.assertFalse(ipc.sent);self.assertFalse(b.approvals)

    def test_guide_is_available_off_and_not_queued(self):
        b,ipc=self.bot()
        with patch.object(features,'enabled',return_value=False):
            b.message(self.msg('/guide'))
        self.assertIn('инструкция пользователя',b.said[-1][0])
        self.assertIn('/queue',b.said[-1][0]);self.assertFalse(b.queue_items);self.assertFalse(ipc.sent)
    def test_help_guide_button_and_callback(self):
        b,ipc=self.bot();b.message(self.msg('/help'))
        self.assertIn('/guide',b.said[-1][0])
        self.assertEqual(b.said[-1][1]['markup']['inline_keyboard'][0][0]['callback_data'],'guide:open')
        with patch.object(bridge,'api',return_value={}):
            b.callback({'id':'callback','from':{'id':123},'message':{'chat':{'id':123}},'data':'guide:open'})
        self.assertIn('инструкция пользователя',b.said[-1][0]);self.assertFalse(b.queue_items)

    def test_new_general_chat_needs_no_project_lookup_and_is_single_use(self):
        b,ipc=self.bot();folder=self.root/'general-chat'
        b.call=lambda *a:self.fail('general chat must not query projects')
        with patch.object(bridge,'enabled',return_value=True), patch.object(bridge,'GENERAL_CHAT',folder), patch.object(bridge,'create_chat',return_value={'id':'new-general'}) as create, patch.object(bridge,'open_in_vscode',return_value=True):
            b.new_chat_choices();self.assertFalse(folder.exists())
            keys=b.state['new_chat_kinds'];self.assertEqual(set(keys.values()),{'general','project'})
            key=next(k for k,v in keys.items() if v=='general')
            b.new_chat_kind(key)
            create.assert_called_once_with(str(folder));self.assertEqual(b.state['thread'],'new-general')
            self.assertIn('Общий чат создан',b.said[-1][0]);self.assertNotIn(str(folder),b.said[-1][0])
            b.new_chat_kind(key);self.assertEqual(create.call_count,1)
    def test_new_project_filters_general_and_duplicate_folders(self):
        b,ipc=self.bot();general=self.root/'general';general.mkdir();sub=general/'sub';sub.mkdir();project=self.root/'work';project.mkdir()
        b.call=lambda m,p:{'data':[{'cwd':str(general)},{'cwd':str(project)},{'cwd':str(project)},{'cwd':str(sub)}]}
        with patch.object(bridge,'enabled',return_value=True),patch.object(bridge,'GENERAL_CHAT',general),patch.object(bridge,'create_chat',return_value={'id':'new-project'}) as create,patch.object(bridge,'open_in_vscode',return_value=True):
            b.new_chat_choices();key=next(k for k,v in b.state['new_chat_kinds'].items() if v=='project')
            b.new_chat_kind(key);self.assertEqual(list(b.state['new_chat_choices'].values()),[str(project)])
            self.assertIn('Выбери проект',b.said[-1][0]);create.assert_not_called()
            b.new_chat_selected(next(iter(b.state['new_chat_choices'])))
            create.assert_called_once_with(str(project));self.assertEqual(b.state['thread'],'new-project')
    def test_new_chat_cancel_off_and_busy(self):
        b,ipc=self.bot()
        with patch.object(bridge,'enabled',return_value=True),patch.object(bridge,'create_chat') as create:
            b.new_chat_choices();key=next(iter(b.state['new_chat_kinds']))
            b.new_chat_selected('cancel');self.assertNotIn('new_chat_kinds',b.state)
            b.new_chat_kind(key);create.assert_not_called()
            b.active={'turnId':'busy'};b.new_chat_choices();self.assertNotIn('new_chat_kinds',b.state)
        b.active=None
        with patch.object(bridge,'enabled',return_value=False):
            b.new_chat_choices();self.assertNotIn('new_chat_kinds',b.state)

    def test_mode_button_switches_and_repeated_old_press_is_idempotent(self):
        b,ipc=self.bot();state={'enabled':False}
        def set_mode(value):state['enabled']=value
        with patch.object(bridge,'enabled',side_effect=lambda:state['enabled']),patch.object(bridge,'set_enabled',side_effect=set_mode):
            b.message(self.msg(ui.MODE_OFF));self.assertTrue(state['enabled'])
            b.message(self.msg(ui.MODE_OFF));self.assertTrue(state['enabled'])
            b.message(self.msg(ui.MODE_ON));self.assertFalse(state['enabled'])
            b.message(self.msg(ui.MODE_ON));self.assertFalse(state['enabled'])
        self.assertFalse(b.queue_items)
    def test_reply_keyboard_shows_current_mode_as_single_button(self):
        b,ipc=self.bot();b.say=bridge.Bridge.say.__get__(b)
        for value,label in [(False,ui.MODE_OFF),(True,ui.MODE_ON)]:
            with patch.object(bridge,'enabled',return_value=value),patch.object(bridge,'api',return_value={'message_id':42}) as api:
                b.say('test')
                markup=api.call_args.args[2]['reply_markup']
                self.assertEqual(markup['keyboard'][0],[label])
                self.assertEqual(ui.BUTTONS[label],'/off' if value else '/on')

    def test_unauthorized_messages_and_callbacks(self):
        b,ipc=self.bot();b.message({'chat':{'id':999,'type':'private'},'text':'run'});self.assertFalse(b.queue_items)
        with patch.object(bridge,'api') as api:
            b.callback({'from':{'id':999},'message':{'chat':{'id':123}},'data':'qctl:clear'})
            api.assert_not_called()

if __name__=='__main__':unittest.main()
