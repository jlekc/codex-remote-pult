import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import chat_store
import delivery
import notify
import progress
import bridge


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.config={'chat_id':123,'token':'dummy'};self.calls=[]
        self.patches=[patch.object(chat_store,'DB',root/'messages.sqlite3'),
            patch.object(delivery,'ROOT',root/'receipts'),patch.object(notify,'enabled',return_value=True),
            patch.object(notify.time,'sleep'),patch.object(notify,'api',side_effect=self.api)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def api(self,token,method,data):
        self.calls.append((method,data))
        return {'message_id':100+len(self.calls)} if method=='sendMessage' else True
    def event(self,turn='t',text='Готово'):
        return {'thread-id':'A','turn-id':turn,'last-assistant-message':text}
    def register(self,q='q',turn='t',ids=(1,2)):
        progress.notice(self.config,q,{'message_id':ids[0]})
        progress.link(self.config,q,'A',turn)
        progress.notice(self.config,q,{'message_id':ids[1]})
    def test_delete_before_final_and_only_matching_request(self):
        self.register();self.register('other','other-turn',(3,4));self.assertFalse(self.calls)
        notify.send(self.config,self.event())
        self.assertEqual([m for m,d in self.calls],['deleteMessages','sendMessage'])
        self.assertEqual(self.calls[0][1]['message_ids'],[1,2])
        with chat_store.connection() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT message FROM progress_notices ORDER BY message')],[3,4])
    def test_send_failure_after_cleanup_can_be_retried(self):
        self.register()
        def failure(token,method,data):
            if method=='sendMessage' and any(m=='sendMessage' for m,d in self.calls):raise RuntimeError('No connection')
            return self.api(token,method,data)
        with patch.object(notify,'api',side_effect=failure):
            with self.assertRaises(RuntimeError):notify.send(self.config,self.event(text='x'*4000))
        self.assertEqual([m for m,d in self.calls],['deleteMessages','sendMessage'])
        notify.send(self.config,self.event(text='x'*4000))
        self.assertEqual(sum(m=='sendMessage' for m,d in self.calls),3)
    def test_delete_failure_never_resends_final_and_sweep_retries(self):
        self.register();real=self.api
        def failure(token,method,data):
            if method=='deleteMessages':raise RuntimeError('No connection')
            return real(token,method,data)
        with patch.object(notify,'api',side_effect=failure):notify.send(self.config,self.event())
        with patch.object(progress,'_last_sweep',0):progress.sweep(self.config)
        notify.send(self.config,self.event())
        self.assertEqual([m for m,d in self.calls],['sendMessage','deleteMessages'])
    def test_fast_final_before_link_and_start_notice_is_cleaned(self):
        progress.notice(self.config,'q',{'message_id':1})
        notify.send(self.config,self.event())
        progress.link(self.config,'q','A','t')
        progress.notice(self.config,'q',{'message_id':2})
        deletes=[d['message_ids'] for m,d in self.calls if m=='deleteMessages']
        self.assertEqual(deletes,[[1],[2]])
    def test_start_status_is_sent_after_queue_notice_deletion(self):
        progress.notice(self.config,'q',{'message_id':11})
        progress.notice(self.config,'other',{'message_id':99})
        progress.before_start(self.config,'q')
        self.api('dummy','sendMessage',{'text':'Промпт запущен'})
        self.assertEqual([m for m,d in self.calls],['deleteMessages','sendMessage'])
        self.assertEqual(self.calls[0][1]['message_ids'],[11])
    def test_persistent_links_and_receipts_allow_restart_cleanup(self):
        self.register()
        with chat_store.connection() as c:c.execute('INSERT INTO progress_finals VALUES (?,?,?)',('123','A','t'))
        # Another process can use the same SQLite metadata without memory state.
        progress.cleanup(dict(self.config),'A','t')
        self.assertEqual(self.calls[0][1]['message_ids'],[1,2])
    def test_no_deletion_for_other_chat_or_unconfirmed_final(self):
        self.register();progress.cleanup(self.config,'A','t');self.assertFalse(self.calls)
        progress.delivered({'token':'dummy','chat_id':999},self.event())
        self.assertFalse(self.calls)
    def test_enqueue_and_dispatch_register_real_notice_ids(self):
        b=bridge.Bridge.__new__(bridge.Bridge);b.config=self.config;b.state={'thread':'A'};b.active=None;b.approvals={};b.save=lambda:None
        b.init_features();n=[]
        def say(text,**kw):n.append(text);return {'message_id':10+len(n)}
        b.say=say
        with patch('features.enabled',return_value=True):
            b.enqueue({'text':'Вопрос'})
            def start(t,msg,item):
                progress.link(b.config,item['id'],t,'turn')
                b.active={'threadId':t,'turnId':'turn'}
            b.start_prompt=start;b.drain_queue()
        progress.delivered(self.config,self.event('turn'))
        self.assertEqual([d['message_ids'] for m,d in self.calls if m=='deleteMessages'],[[11],[12]])

if __name__=='__main__':unittest.main()
