"""Forward real request_user_input forms to their original VS Code owner."""
import json
import time
import uuid
from chat_store import bind_question, question_reply, title_for
from mode import enabled

QUESTION_METHOD = 'item/tool/requestUserInput'


class Questions:
    def init_questions(self):
        self.question_groups = {}
        self.question_keys = {}

    def observe_questions(self, thread, state, owner):
        live = [r for r in state.get('requests', [])
                if not r.get('completed') and r.get('method') == QUESTION_METHOD]
        for group_key, group in list(self.question_groups.items()):
            if group['thread'] == thread and not any(
                    r.get('id') == group['request']['id'] and r.get('params') == group['request'].get('params')
                    for r in live):
                self.drop_questions(group_key)
        if not enabled():
            return
        for request in live:
            if any(g['thread'] == thread and g['owner'] == owner and g['request'] == request
                   for g in self.question_groups.values()):
                continue
            questions = request.get('params', {}).get('questions', [])
            if not questions:
                continue
            group_key = uuid.uuid4().hex[:16]
            group = {'thread': thread, 'owner': owner, 'request': request,
                     'questions': questions, 'answers': {}}
            self.question_groups[group_key] = group
            for index, question in enumerate(questions):
                key = uuid.uuid4().hex[:16]
                self.question_keys[key] = (group_key, index)
                if question.get('isSecret'):
                    self.say('Вопрос требует конфиденциального ответа. Ответь в VS Code.\nЧат: '+thread,
                             notification=True, thread=thread)
                    continue
                options = question.get('options') or []
                text = 'Вопрос агента\nБеседа: '+title_for(thread)+'\nЧат: '+thread+'\n\n'+question['question']
                buttons = []
                for option_index, option in enumerate(options):
                    label = option['label']
                    text += '\n\n'+str(option_index+1)+'. '+label+'\n'+option.get('description', '')
                    buttons.append([{'text': label[:60], 'callback_data': f'question:{key}:{option_index}'}])
                buttons.append([{'text': '✍️ Свой ответ', 'callback_data': f'question:{key}:custom'}])
                text += '\n\nМожно ответить своим текстом через «Ответить» на это сообщение.'
                result = self.say(text, notification=True, thread=thread, markup={'inline_keyboard': buttons})
                if result:
                    bind_question(self.config['chat_id'], result.get('message_id'), key)

    def drop_questions(self, group_key):
        self.question_groups.pop(group_key, None)
        self.question_keys = {k: v for k, v in self.question_keys.items() if v[0] != group_key}

    def current_question(self, key):
        if not enabled():
            self.say('Удалённый режим выключен. Включи /on или ответь в VS Code.')
            return None
        entry = self.question_keys.get(key)
        group = self.question_groups.get(entry[0]) if entry else None
        if not group:
            self.say('Этот вопрос уже закрыт или кнопка устарела. Используй новое сообщение с вопросом.')
            return None
        question = group['questions'][entry[1]]
        if question.get('isSecret'):
            self.say('Конфиденциальный ответ введи в VS Code.')
            return None
        snap = self.snapshots.get(group['thread'])
        if not snap or snap['owner'] != group['owner'] or time.monotonic()-snap['received'] > 10:
            self.connect_ipc().follow(group['thread'])
            self.say('Проверяю актуальность вопроса. Повтори ответ через несколько секунд.')
            return None
        if not any(r.get('id') == group['request']['id'] and r.get('params') == group['request'].get('params')
                   and not r.get('completed') for r in snap['state'].get('requests', [])):
            self.drop_questions(entry[0])
            self.say('На этот вопрос уже ответили в VS Code.')
            return None
        if question['id'] in group['answers']:
            self.say('Этот ответ уже выбран. Ответь на остальные вопросы.')
            return None
        return entry, group, question

    def question_callback(self, data):
        key, _, choice = data.partition(':')
        if choice == 'custom':
            current = self.current_question(key)
            if not current:
                return
            _, group, question = current
            result = self.say('✍️ Напиши свой ответ на вопрос:\n'+question['question']+
                              '\n\nОтветь на это сообщение или отправь /answer '+key+' твой текст',
                              thread=group['thread'], markup={'force_reply': True, 'selective': True})
            if result:
                bind_question(self.config['chat_id'], result.get('message_id'), key)
        else:
            self.answer_question(key, choice=choice)

    def question_message(self, message):
        # Commands and attachments keep their usual meaning; text replies answer the form.
        text = message.get('text', '').strip()
        if not text or text.startswith('/'):
            return False
        key = question_reply(self.config['chat_id'], message.get('reply_to_message', {}).get('message_id'))
        if key:
            self.answer_question(key, answer=text)
            return True
        return False

    def answer_question(self, key, answer=None, choice=None):
        current = self.current_question(key)
        if not current:
            return
        entry, group, question = current
        if choice is not None:
            options = question.get('options') or []
            if not choice.isdecimal() or not 0 <= int(choice) < len(options):
                self.say('Вариант ответа недоступен.')
                return
            answer = options[int(choice)]['label']
        if not answer or not answer.strip():
            self.say('Напиши ответ: /answer '+key+' твой текст')
            return
        answers = dict(group['answers'])
        answers[question['id']] = {'answers': [answer.strip()]}
        if len(answers) < len(group['questions']):
            group['answers'] = answers
            self.say('Ответ сохранён. Ответь на остальные вопросы.', thread=group['thread'])
            return
        self.connect_ipc().request('thread-follower-submit-user-input', {
            'conversationId': group['thread'], 'requestId': group['request']['id'],
            'response': {'answers': answers}}, target=group['owner'])
        self.drop_questions(entry[0])
        self.say('Ответ отправлен агенту.', thread=group['thread'])
