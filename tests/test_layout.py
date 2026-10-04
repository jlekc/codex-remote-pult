import io
import json
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import layout
import notify
import bridge
import chat_store


def entity_text(data, entity):
    raw=data['text'].encode('utf-16-le')
    return raw[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le')


class LayoutTests(unittest.TestCase):
    def test_body_is_highlighted_and_footer_keeps_all_metadata(self):
        body='Главный ответ 🙂\nПроект: это текст агента\nЧат: это текст агента'
        d=layout.format_message('',{'title':'Мой чат 🧪','body':body,
            'event':'✅ Ответ готов','meta':[('Чат','A'),('Проект','каталог')]})
        quoted=[entity_text(d,e) for e in d['entities'] if e['type']=='blockquote']
        self.assertEqual(quoted,[body])
        self.assertIn('Мой чат 🧪',[entity_text(d,e) for e in d['entities'] if e['type']=='bold'])
        self.assertEqual([entity_text(d,e) for e in d['entities'] if e['type']=='italic'],['Чат: A\nПроект: каталог'])
        self.assertLess(d['text'].index(body),d['text'].index('⚙️ Технические данные'))
    def test_emoji_offsets_code_fences_markdown_and_html_are_safe(self):
        d=layout.format_message('',{'title':'🙂','body':'## Проверка\n**Готово** `x < 2`\n```python\nprint("<b>😀</b>")\n```\n[Сайт](https://example.com)','meta':[]})
        self.assertIn('<b>😀</b>',d['text'])
        selected={e['type']:entity_text(d,e) for e in d['entities']}
        self.assertEqual(selected['pre'],'print("<b>😀</b>")\n')
        self.assertEqual(selected['code'],'x < 2')
        self.assertEqual(selected['text_link'],'Сайт')
        for e in d['entities']:
            self.assertLessEqual(e['offset']+e['length'],layout.units(d['text']))
    def test_api_applies_layout_and_preserves_buttons(self):
        seen=[]
        def urlopen(req,**kwargs):
            seen.append(json.loads(req.data));return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        markup={'inline_keyboard':[[{'text':'Скачать','callback_data':'file:key'}]]}
        with patch.object(notify.urllib.request,'urlopen',side_effect=urlopen):
            notify.api('dummy','sendMessage',{'chat_id':123,'text':'Вопрос агента\nБеседа: Тест\nЧат: A\n\nВаш ответ?','reply_markup':markup})
        self.assertEqual(seen[0]['reply_markup'],markup)
        self.assertNotIn('_presentation',seen[0])
        self.assertTrue(any(e['type']=='blockquote' for e in seen[0]['entities']))
        self.assertTrue(seen[0]['text'].startswith('💬 Тест'))
    def test_explicit_preview_format_is_unchanged(self):
        d={'text':'<b>Preview</b>','parse_mode':'HTML'}
        self.assertEqual(layout.message_data(d),d)
    def test_long_reply_each_part_keeps_title_footer_and_routing(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(chat_store,'DB',Path(folder)/'messages.sqlite3'):
            sent=[]
            def urlopen(req,**kwargs):
                sent.append(json.loads(req.data))
                return io.BytesIO(json.dumps({'ok':True,'result':{'message_id':len(sent)}}).encode())
            answer='Текст 🙂 < & >\n'*1000
            with patch.object(notify.urllib.request,'urlopen',side_effect=urlopen),patch.object(notify.time,'sleep'):
                notify.send({'chat_id':123,'token':'dummy'}, {'thread-id':'A','thread-name':'Длинный чат','cwd':folder,'last-assistant-message':answer},force=True)
            reconstructed=[]
            for index,d in enumerate(sent):
                self.assertLessEqual(layout.units(d['text']),4096)
                self.assertIn('Длинный чат',d['text'])
                self.assertIn('\nЧат: A\n',d['text'])
                self.assertEqual(chat_store.reply_thread(123,{'message_id':index+1}),'A')
                reconstructed.extend(entity_text(d,e) for e in d['entities'] if e['type']=='blockquote')
            self.assertEqual(''.join(reconstructed),answer)
    def test_service_metadata_and_chunks_keep_original_question_context(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(chat_store,'DB',Path(folder)/'messages.sqlite3'):
            b=bridge.Bridge.__new__(bridge.Bridge);b.config={'chat_id':123,'token':'dummy'}
            sent=[]
            def api(t,m,d):
                sent.append(layout.message_data(d));return {'message_id':len(sent)}
            markup={'force_reply':True}
            with patch.object(bridge,'api',side_effect=api),patch.object(bridge.time,'sleep'):
                result=b.say('Вопрос агента\nБеседа: Тест\nЧат: B\n\n'+'🙂'*4000,thread='B',markup=markup)
            self.assertGreater(len(sent),1)
            for d in sent:
                self.assertTrue(d['text'].startswith('💬 Тест'))
                self.assertIn('Чат: B',d['text'])
                self.assertEqual(d['reply_markup'],markup)
                self.assertLessEqual(layout.units(d['text']),4096)
            self.assertEqual(result['message_id'],len(sent))
