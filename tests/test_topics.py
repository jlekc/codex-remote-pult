import io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import chat_store,topics,notify,bridge

class TopicsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=patch.object(chat_store,'DB',Path(self.temp.name)/'messages.sqlite3');self.db.start();self.addCleanup(self.db.stop)
        self.lock=patch.object(topics,'LOCK',Path(self.temp.name)/'topics.lock');self.lock.start();self.addCleanup(self.lock.stop)
        topics._cache.clear()
    def test_binding_persists_and_cannot_change_conversation(self):
        self.assertTrue(topics.bind_topic(123,10,'A'))
        self.assertTrue(topics.bind_topic(123,10,'A'))
        self.assertFalse(topics.bind_topic(123,10,'B'))
        self.assertFalse(topics.bind_topic(123,11,'A'))
        self.assertEqual(topics.thread_for(123,10),'A')
        self.assertIsNone(topics.thread_for(456,10))
    def test_creation_once(self):
        calls=[]
        def api(token,method,data):
            calls.append(method)
            return {'has_topics_enabled':True} if method=='getMe' else {'message_thread_id':42}
        with patch.object(topics,'title_for',return_value='Test'):
            self.assertEqual(topics.ensure_topic('dummy',123,'A',api),42)
            self.assertEqual(topics.ensure_topic('dummy',123,'A',api),42)
        self.assertEqual(calls,['getMe','createForumTopic'])
    def test_disabled_keeps_plain_chat(self):
        self.assertIsNone(topics.ensure_topic('dummy',123,'A',lambda *args:{}))
    def test_topic_overrides_global_selection_and_unknown_does_not_fall_back(self):
        b=bridge.Bridge.__new__(bridge.Bridge);b.config={'chat_id':123};b.state={'thread':'B'}
        topics.bind_topic(123,10,'A')
        self.assertEqual(b.target_thread({'message_thread_id':10}),'A')
        self.assertIsNone(b.target_thread({'message_thread_id':11}))
        self.assertEqual(b.target_thread({}),'B')
    def test_wire_routing_and_private_keys_removed(self):
        topics.bind_topic(123,10,'A');seen=[]
        def urlopen(req,**kw):
            seen.append(json.loads(req.data));return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch.object(notify.urllib.request,'urlopen',side_effect=urlopen):
            notify.api('dummy','sendMessage',{'chat_id':123,'text':'Test','_thread':'A','_topic':99})
        self.assertEqual(seen[0]['message_thread_id'],10)
        self.assertNotIn('_thread',seen[0]);self.assertNotIn('_topic',seen[0])
    def test_incoming_context_restored_even_on_failure(self):
        b=bridge.Bridge.__new__(bridge.Bridge);b.reply_topic=5
        def fail(message):
            self.assertEqual(b.reply_topic,10);raise RuntimeError('test')
        b._message=fail
        with self.assertRaises(RuntimeError):b.message({'message_thread_id':10})
        self.assertEqual(b.reply_topic,5)
