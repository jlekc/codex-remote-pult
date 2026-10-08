import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import chat_store
import mirror


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.sent=[]
        self.patches=[patch.object(chat_store,'DB',Path(self.tmp.name)/'messages.sqlite3'),
            patch.object(mirror,'enabled',return_value=True),patch.object(mirror,'title_for',return_value='Тест'),
            patch.object(mirror.time,'sleep'),patch.object(mirror,'api',side_effect=lambda *a:(self.sent.append(a[2]) or {'message_id':len(self.sent)}))]
        for p in self.patches:p.start()
        self.bot=mirror.DesktopMirror({'token':'dummy','chat_id':123})
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def user(self,id='u',text='Вопрос',client=None):
        return {'id':id,'type':'userMessage','clientId':client,'content':[{'type':'text','text':text}]}
    def state(self,items,old=False):
        return {'turns':[{'turnId':'t','turnStartedAtMs':self.bot.cutoff+(-1000 if old else 1000),'items':items}]}
    def test_desktop_only_no_agent_and_no_duplicate(self):
        s=self.state([self.user(),{'id':'c','type':'agentMessage','phase':'commentary','text':'Progress'}, {'id':'f','type':'agentMessage','phase':'final_answer','text':'Final'}])
        self.bot.observe('A',s);self.bot.observe('A',s)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(chat_store.reply_thread(123,{'message_id':1}),'A')
    def test_phone_origin_survives_restart_and_same_text_on_desktop(self):
        self.bot.phone('A','phone');self.bot=mirror.DesktopMirror(self.bot.config)
        self.bot.observe('A',self.state([self.user('phone-item',client='phone'),self.user('desktop-item')]))
        self.assertEqual(len(self.sent),1)
    def test_context_removed_without_changing_request(self):
        text='# Context from my IDE setup:\n\n## Open tabs:\n- secret.html\n\n## My request:\nСравни **заявки**\n## My request:\nэто часть запроса'
        self.bot.observe('A',self.state([self.user(text=text)]))
        self.assertEqual(self.sent[0]['text'],'Сравни **заявки**\n## My request:\nэто часть запроса')
        self.assertEqual(mirror.prompt_text('Обычный текст\n## My request:\nпример'),'Обычный текст\n## My request:\nпример')
    def test_history_and_off_interval_not_replayed(self):
        self.bot.observe('A',self.state([self.user('old')],old=True))
        with patch.object(mirror,'enabled',return_value=False):self.bot.observe('A',self.state([self.user('off')]))
        self.bot.switch();self.bot.observe('A',self.state([self.user('off')]))
        self.assertFalse(self.sent)
        self.bot.observe('A',self.state([self.user('fresh')]))
        self.assertEqual(len(self.sent),1)
    def test_canonical_history_no_double_delivery(self):
        s=self.state([self.user()]);s['turnHistory']={'history':{'entitiesByKey':{'t':s['turns'][0]}}}
        self.bot.observe('A',s);self.assertEqual(len(self.sent),1)
    def test_successful_chunks_not_repeated_on_retry(self):
        s=self.state([self.user(text='🙂'*4000)]);calls=[]
        def fail(*args):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('Unavailable')
            self.sent.append(args[2]);return {'message_id':len(self.sent)}
        with patch.object(mirror,'api',side_effect=fail):
            with self.assertRaises(RuntimeError):self.bot.observe('A',s)
        self.bot.observe('A',s)
        self.assertEqual(''.join(d['text'] for d in self.sent),'🙂'*4000)

if __name__=='__main__':unittest.main()
