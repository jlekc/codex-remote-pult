import tempfile,time,unittest,queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import native_queue

class NativeQueueTests(unittest.TestCase):
    def bot(self):
        ipc=SimpleNamespace(follow=Mock(),owner=Mock(return_value='owner'))
        return SimpleNamespace(state={},queue_items=[],snapshots={},save=Mock(),mirror=SimpleNamespace(phone=Mock()),say=Mock(),call=Mock(return_value={'queuedSubmission':{'id':'native'}}),connect_ipc=lambda:ipc,config={'chat_id':123})
    def item(self,thread,key):
        return {'id':key,'thread':thread,'status':'pending','message':{'text':key}}
    def snap(self,b,thread):
        b.snapshots[thread]={'received':time.monotonic(),'owner':'owner','state':{}}
    def run_flush(self,q):
        with patch.object(native_queue,'enabled',return_value=True),patch.object(native_queue.progress,'before_start'):
            q.flush()
    def test_atomic_admission_and_no_execution(self):
        b=self.bot();b.queue_items=[self.item('A','one')];self.snap(b,'A');q=native_queue.NativeQueue(b);self.run_flush(q)
        self.assertFalse(b.queue_items);self.assertEqual(b.call.call_args.args[0],'thread/queue/add')
        params=b.call.call_args.args[1];self.assertEqual(params['threadId'],'A');self.assertEqual(params['input'][0]['text'],'one')
        self.assertEqual(b.mirror.phone.call_args.args[1],params['clientUserMessageId'])
        self.assertFalse(b.say.called)
    def test_uncertain_not_replayed_and_blocks_same_conversation(self):
        b=self.bot();b.queue_items=[self.item('A','one'),self.item('A','two')];self.snap(b,'A');b.call.side_effect=queue.Empty();q=native_queue.NativeQueue(b)
        self.run_flush(q);self.assertEqual(b.queue_items[0]['status'],'uncertain');q.poll=0;self.run_flush(q)
        self.assertEqual(b.call.call_count,1);self.assertEqual(b.queue_items[1]['status'],'pending')
    def test_independent_conversation_can_progress(self):
        b=self.bot();b.queue_items=[self.item('A','one'),self.item('B','two')];self.snap(b,'B');q=native_queue.NativeQueue(b);self.run_flush(q)
        self.assertEqual([i['thread'] for i in b.queue_items],['A']);self.assertEqual(b.call.call_args.args[1]['threadId'],'B')
    def test_paused_preserves_every_item(self):
        b=self.bot();b.queue_items=[self.item('A','one')];self.snap(b,'A');b.state['queue_paused']=True;q=native_queue.NativeQueue(b);self.run_flush(q)
        b.call.assert_not_called();self.assertEqual(len(b.queue_items),1)
    def test_follow_failure_after_add_does_not_repeat_submission(self):
        b=self.bot();b.queue_items=[self.item('A','one')];self.snap(b,'A');b.connect_ipc().follow.side_effect=RuntimeError('lost');q=native_queue.NativeQueue(b);self.run_flush(q);q.poll=0;self.run_flush(q)
        self.assertFalse(b.queue_items);self.assertEqual(b.call.call_count,1)
    def test_pagination(self):
        b=self.bot();b.call.side_effect=[{'data':[{'id':'one'}],'nextCursor':'next'},{'data':[{'id':'two'}],'nextCursor':None}];q=native_queue.NativeQueue(b)
        self.assertEqual([i['id'] for i in q.items('A')],['one','two']);self.assertEqual(b.call.call_args.args[1]['cursor'],'next')

    def test_observe_links_exact_client_id_in_canonical_history(self):
        b=self.bot();q=native_queue.NativeQueue(b);q.receipts['native']={'thread':'A','client_id':'cid','queue_id':'local'}
        state={'turnHistory':{'history':{'entitiesByKey':{'turn':{'turnId':'turn','items':[{'type':'userMessage','id':'cid'}]}}}}}
        with patch.object(native_queue.progress,'link') as link,patch.object(native_queue.progress,'before_start') as clean:
            q.observe('B',state);link.assert_not_called()
            q.observe('A',state);q.observe('A',state)
            link.assert_called_once_with(b.config,'local','A','turn');clean.assert_called_once()
        self.assertEqual(q.receipts['native']['turn_id'],'turn')
    def test_finals_are_only_from_admitted_turns(self):
        b=self.bot();q=native_queue.NativeQueue(b);q.receipts['native']={'thread':'A','client_id':'cid','queue_id':'local','turn_id':'own'}
        b.call.return_value={'thread':{'cwd':'/tmp','turns':[{'id':'other','status':'completed','items':[{'type':'agentMessage','text':'wrong'}]},{'id':'own','status':'completed','items':[{'type':'agentMessage','phase':'final_answer','text':'right'}]}]}}
        with patch.object(native_queue,'enabled',return_value=True),patch('notify.send') as send,patch.object(native_queue.progress,'confirmed',return_value=True):
            q.poll_finished();send.assert_called_once();self.assertEqual(send.call_args.args[1]['last-assistant-message'],'right')
        self.assertFalse(q.receipts)
    def test_live_tail_interrupted_is_not_final(self):
        b=self.bot();q=native_queue.NativeQueue(b);q.receipts['native']={'thread':'A','client_id':'cid','queue_id':'local','turn_id':'own'}
        b.call.return_value={'thread':{'turns':[{'id':'own','status':'interrupted','items':[]}]}}
        with patch.object(native_queue,'enabled',return_value=True),patch('notify.send') as send:
            q.poll_finished();send.assert_not_called()
        self.assertIn('native',q.receipts)
    def test_stop_uses_selected_conversation_and_original_owner(self):
        b=self.bot();q=native_queue.NativeQueue(b);self.snap(b,'A');b.snapshots['A']['state']={'turns':[{'turnId':'own','status':'inProgress'}]}
        ipc=b.connect_ipc();ipc.request=Mock();q.stop('A')
        self.assertEqual(ipc.request.call_args.args[1]['conversationId'],'A');self.assertEqual(ipc.request.call_args.kwargs['target'],'owner')
    def test_stop_refuses_stale_snapshot(self):
        b=self.bot();q=native_queue.NativeQueue(b);self.snap(b,'A');b.snapshots['A']['received']-=20
        b.connect_ipc().request=Mock();q.stop('A');b.connect_ipc().request.assert_not_called()

    def test_inflight_notify_claim_does_not_lose_final_retry(self):
        b=self.bot();q=native_queue.NativeQueue(b);q.receipts['native']={'thread':'A','client_id':'cid','queue_id':'local','turn_id':'own'}
        b.call.return_value={'thread':{'turns':[{'id':'own','status':'completed','items':[{'type':'agentMessage','text':'right'}]}]}}
        with patch.object(native_queue,'enabled',return_value=True),patch('notify.send') as send,patch.object(native_queue.progress,'confirmed',side_effect=[False,True]):
            q.poll_finished();self.assertIn('native',q.receipts)
            q.finish_poll=0;q.poll_finished();self.assertFalse(q.receipts);self.assertEqual(send.call_count,2)
