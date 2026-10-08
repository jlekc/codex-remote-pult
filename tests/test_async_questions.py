import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import chat_store
from questions import Questions, live_questions

class Bot(Questions):
    def __init__(self):
        self.init_questions(); self.config={'chat_id':123}; self.snapshots={};self.said=[]
        self.ipc=Mock();self.mirror=Mock()
    def say(self,text,**kw):self.said.append((text,kw));return {'message_id':len(self.said)}
    def connect_ipc(self):return self.ipc
    def snapshot(self,state,owner='original-owner'):
        self.snapshots['thread']={'state':state,'owner':owner,'received':time.monotonic()}
        self.observe_questions('thread',state,owner)

class AsyncQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(chat_store,'DB',Path(self.tmp.name)/'messages.sqlite3');p.start();self.addCleanup(p.stop)
        p=patch('questions.enabled',return_value=True);p.start();self.addCleanup(p.stop)
        self.b=Bot()
    def state(self,status='inProgress',questions=None):
        return {'cwd':'/own/project','requests':[], 'turns':[{'turnId':'turn','status':status,'turnStartedAtMs':1,
            'items':[{'type':'agentMessage','id':'ask','questions':questions or [{'title':'Какой период?','options':['Месяц','Год']}]}]}]}
    def test_async_is_forwarded_once_with_options_and_free_text(self):
        s=self.state();self.b.snapshot(s);self.b.snapshot(s)
        self.assertEqual(len(self.b.said),1)
        self.assertIn('Какой период?',self.b.said[0][0]);self.assertEqual(len(self.b.said[0][1]['markup']['inline_keyboard']),3)
    def test_busy_answer_steers_original_thread_with_exact_question_identity(self):
        self.b.snapshot(self.state());key=next(iter(self.b.question_keys));self.b.answer_question(key,choice='1')
        args,kw=self.b.ipc.request.call_args;self.assertEqual(args[0],'thread-follower-steer-turn')
        self.assertEqual(args[1]['conversationId'],'thread');self.assertEqual(kw['target'],'original-owner')
        text=args[1]['input'][0]['text'];reply=json.loads(text.split('\n')[1])[0]
        self.assertEqual(reply['questionItemId'],'["request_user_input_async","ask",0]');self.assertEqual(reply['answer'],'Год')
        self.assertEqual(args[1]['restoreMessage']['context']['workspaceRoots'],['/own/project'])
        self.b.mirror.phone.assert_called_once();self.b.snapshot(self.state());self.assertEqual(len(self.b.said),2)
    def test_finished_turn_answer_starts_original_thread_not_new_chat(self):
        self.b.snapshot(self.state('completed'));key=next(iter(self.b.question_keys));self.b.answer_question(key,answer='Другой период')
        args,kw=self.b.ipc.request.call_args;self.assertEqual(args[0],'thread-follower-start-turn');self.assertEqual(args[1]['conversationId'],'thread')
        self.assertEqual(kw['target'],'original-owner')
    def test_desktop_answer_closes_question_only_after_accepted_steer(self):
        s=self.state();reply={'questionItemId':'["request_user_input_async","ask",0]','question':'Какой период?','answer':'Месяц'}
        item={'type':'steeringUserMessage','status':'pending','input':[{'type':'text','text':'<send_user_message_question_reply>\n'+json.dumps([reply])+'\n</send_user_message_question_reply>'}]}
        s['turns'][0]['items'].append(item);self.assertEqual(len(live_questions(s)),1)
        self.b.snapshot(s);key=next(iter(self.b.question_keys));item['status']='accepted';self.b.snapshot(s)
        self.b.answer_question(key,answer='Повтор');self.b.ipc.request.assert_not_called()
    def test_canonical_history_ignores_archived_questions_on_first_snapshot(self):
        old=self.state()['turns'][0];old['turnId']='old';old['status']='completed'
        s={'requests':[], 'turnHistory':{'history':{'entitiesByKey':{'old':old,'new':{'turnId':'new','turnStartedAtMs':2,'items':[]}}}}}
        self.b.snapshot(s);self.assertFalse(self.b.said)
        s['turnHistory']['history']['entitiesByKey']['new']['items']=[{'type':'agentMessage','id':'new-ask','questions':[{'title':'Новый вопрос'}]}]
        self.b.snapshot(s);self.assertEqual(len(self.b.said),1)
    def test_off_secret_and_stale_owner_do_not_send_an_answer(self):
        s=self.state(questions=[{'title':'PRIVATE_QUESTION','isSecret':True}]);self.b.snapshot(s)
        self.assertNotIn('PRIVATE_QUESTION',self.b.said[0][0]);self.b.answer_question(next(iter(self.b.question_keys)),answer='secret');self.b.ipc.request.assert_not_called()
        b=Bot()
        with patch('questions.enabled',return_value=False):b.snapshot(self.state())
        self.assertFalse(b.said)
        b.snapshot(self.state());key=next(iter(b.question_keys));b.snapshots['thread']['owner']='different-owner';b.answer_question(key,answer='no');b.ipc.request.assert_not_called()

if __name__=='__main__':unittest.main()
