import copy,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from chat_menu import ChatMenu
import bridge

class ChatMenuTests(unittest.TestCase):
    def setUp(self):
        api=patch('chat_menu.api',return_value=True)
        self.api=api.start();self.addCleanup(api.stop)

    def bot(self,n=50):
        b=SimpleNamespace(config={'token':'dummy','chat_id':123},state={},reply_topic=42,save=Mock(),call=Mock(return_value={'data':[{'id':str(i),'name':f'Чат {i}'} for i in range(n)]}),sent=[])
        def say(text,**kwargs):
            result={'message_id':len(b.sent)+1,'message_thread_id':b.reply_topic};b.sent.append((text,kwargs,result));return result
        b.say=say
        return b
    def message(self,b,index=-1):return b.sent[index][2]
    def more_key(self,b):return b.sent[-1][1]['markup']['inline_keyboard'][-1][0]['callback_data'].split(':',1)[1]
    def pick_key(self,b,row=0,page=-1):return b.sent[page][1]['markup']['inline_keyboard'][row][0]['callback_data'].split(':',1)[1]
    def test_four_blocks_and_old_choice_survives(self):
        b=self.bot();m=ChatMenu(b);m.open();first=b.sent[0][2];pick=self.pick_key(b)
        self.assertEqual(len(b.sent[0][1]['markup']['inline_keyboard']),6)
        self.assertEqual(b.call.call_args.args[1]['limit'],20)
        for _ in range(3):m.more(self.more_key(b),self.message(b))
        self.assertEqual(len(b.sent),4);self.assertEqual(len(b.sent[-1][1]['markup']['inline_keyboard']),5)
        current_key=self.pick_key(b,4);current_message=self.message(b)
        self.assertEqual(m.choose(pick,first),'0');self.assertEqual(m.choose(current_key,current_message),'19')
        self.assertEqual(b.call.call_count,1)
    def test_duplicate_click_does_not_emit_second_block(self):
        b=self.bot();m=ChatMenu(b);m.open();key=self.more_key(b);msg=self.message(b);m.more(key,msg);m.more(key,msg)
        menus=[x for x in b.sent if 'markup' in x[1]];self.assertEqual(len(menus),2)
    def test_short_list_has_no_more_button(self):
        b=self.bot(4);ChatMenu(b).open();self.assertEqual(len(b.sent[-1][1]['markup']['inline_keyboard']),4)
    def test_foreign_message_or_topic_cannot_choose(self):
        b=self.bot();m=ChatMenu(b);m.open();key=self.pick_key(b)
        self.assertIsNone(m.choose(key,{'message_id':999,'message_thread_id':42}))
        self.assertIsNone(m.choose(key,{'message_id':1,'message_thread_id':43}))
    def test_snapshot_survives_restart_and_newer_menu(self):
        b=self.bot();m=ChatMenu(b);m.open();key=self.more_key(b);msg=self.message(b);pick=self.pick_key(b)
        state=copy.deepcopy(b.state);b.state=state;m=ChatMenu(b);b.call.return_value={'data':[{'id':'new','name':'Новая'}]};m.open()
        self.assertEqual(m.choose(pick,msg),'0');m.more(key,msg);self.assertIn('6–10',b.sent[-1][0])
    def test_unknown_negative_and_not_yet_shown_indices_refused(self):
        b=self.bot();m=ChatMenu(b);m.open();token=next(iter(m.menus));msg=self.message(b)
        for key in [token+':-1',token+':30','unknown:0',token+':bad']:
            self.assertIsNone(m.choose(key,msg))
    def test_bridge_callback_keeps_topic_and_selected_id(self):
        fake=self.bot();ChatMenu(fake).open();b=bridge.Bridge.__new__(bridge.Bridge);b.config={'chat_id':123,'token':'dummy'};b.state=fake.state;b.save=Mock();b.say=Mock();b.message=Mock()
        cb={'id':'cb','from':{'id':123},'message':{'chat':{'id':123},**self.message(fake)},'data':'chatpick:'+self.pick_key(fake)}
        with patch.object(bridge,'api',return_value=True):b.callback(cb)
        self.assertEqual(b.message.call_args.args[0]['text'],'/use 0');self.assertEqual(b.message.call_args.args[0]['message_thread_id'],42)
        b.message.reset_mock();cb['from']['id']=999
        with patch.object(bridge,'api') as api:b.callback(cb);api.assert_not_called()
        b.message.assert_not_called()

    def test_duplicates_are_skipped_and_next_api_page_fills_list(self):
        b=self.bot();b.call.side_effect=[{'data':[{'id':'A','name':'Newest'},{'id':'A','name':'Duplicate'}],'nextCursor':'next'},{'data':[{'id':'A'},{'id':'B'},{'id':'C'}],'nextCursor':None}]
        m=ChatMenu(b);m.open();menu=next(iter(m.menus.values()))
        self.assertEqual([c['id'] for c in menu['chats']],['A','B','C'])
        self.assertEqual(menu['chats'][0]['label'],'Newest');self.assertEqual(b.call.call_args.args[1]['cursor'],'next')
    def test_repeating_cursor_does_not_loop(self):
        b=self.bot();b.call.return_value={'data':[{'id':'A'}],'nextCursor':'repeat'};m=ChatMenu(b);m.open()
        self.assertEqual(b.call.call_count,2);self.assertEqual(len(next(iter(m.menus.values()))['chats']),1)

    def test_previously_saved_fifty_chat_menu_is_limited_to_twenty(self):
        b=self.bot();b.state={'chat_menus':{'old':{'chats':[{'id':str(i),'label':str(i)} for i in range(50)],'shown':10,'messages':[1],'topic':42}}}
        b.sent=[('old',{}, {'message_id':1,'message_thread_id':42})]
        m=ChatMenu(b);m.more('old:10',self.message(b))
        self.assertEqual(len(m.menus['old']['chats']),20)
        self.assertEqual(len(b.sent[-1][1]['markup']['inline_keyboard']),6)
        self.assertIsNone(m.choose('old:25',self.message(b)))


    def test_menu_uses_compact_header_and_never_deletes_choices(self):
        b=self.bot();m=ChatMenu(b);m.open();key=self.more_key(b);msg=self.message(b)
        m.more(key,msg);m.sweep()
        self.assertTrue(b.sent[0][1]['compact'])
        self.assertEqual(b.sent[0][0],'💬 Чаты 1–5')
        self.assertEqual(next(iter(m.menus.values()))['messages'],[1,2])
        self.api.assert_not_called()

    def test_old_header_shrinks_without_changing_keyboard_or_binding(self):
        b=self.bot();b.state={'chat_menus':{'old':{'chats':[{'id':'A','label':'A'}], 'shown':1,'messages':[7],'topic':42,'closed':True,'pending_delete':[7]}}}
        m=ChatMenu(b);m.sweep()
        self.assertEqual(self.api.call_args.args[1],'editMessageText')
        self.assertNotIn('reply_markup',self.api.call_args.args[2])
        self.assertEqual(m.choose('old:0',{'message_id':7,'message_thread_id':42}),'A')
        self.assertEqual(m.menus['old']['messages'],[7])
