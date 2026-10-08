"""Forward real request_user_input forms to their original VS Code owner."""
import json
import time
import uuid
from chat_store import bind_question, question_reply, title_for
from mode import enabled
from vscode_ipc import turn_start_params

QUESTION_METHOD = 'item/tool/requestUserInput'


def async_turns(state):
    turns = {t.get('turnId') or t.get('id'): t for t in state.get('turns', [])}
    canonical = (state.get('turnHistory') or {}).get('history', {}).get('entitiesByKey', {})
    turns.update({t.get('turnId') or t.get('id') or k: t for k, t in canonical.items()})
    return sorted(turns.values(), key=lambda t: t.get('turnStartedAtMs') or 0)


def live_questions(state):
    """Async questions are agent items, not blocking app-server requests.

    Match the installed UI's questionItemId and reply envelope. Only accepted
    steering messages close questions; no inference from ordinary user text.
    """
    live = [r for r in state.get('requests', [])
            if not r.get('completed') and r.get('method') == QUESTION_METHOD]
    answered = set()
    turns = async_turns(state)
    for turn in turns:
        for item in turn.get('items', []):
            kind = item.get('type')
            if kind not in ('userMessage', 'steeringUserMessage'):
                continue
            if kind == 'steeringUserMessage' and item.get('status') != 'accepted':
                continue
            content = item.get('content') or item.get('input') or []
            if len(content) != 1 or content[0].get('type') != 'text':
                continue
            text = content[0].get('text', '').strip()
            start, end = '<send_user_message_question_reply>', '</send_user_message_question_reply>'
            if not (text.startswith(start) and text.endswith(end)):
                continue
            try:
                replies = json.loads(text[len(start):-len(end)])
                if isinstance(replies, dict): replies = [replies]
                if isinstance(replies, list):
                    answered.update(r['questionItemId'] for r in replies
                                    if isinstance(r, dict) and isinstance(r.get('questionItemId'), str))
            except (ValueError, TypeError):
                continue
    for turn in turns:
        for item in turn.get('items', []):
            if item.get('type') != 'agentMessage' or not item.get('id'):
                continue
            questions = []
            for index, q in enumerate(item.get('questions') or []):
                ident = json.dumps(['request_user_input_async', item['id'], index], ensure_ascii=False, separators=(',', ':'))
                if ident in answered or not isinstance(q, dict) or not q.get('title'):
                    continue
                questions.append({'id': ident, 'question': q['title'], 'isSecret': q.get('isSecret', False),
                    'options': [{'label': o, 'description': ''} for o in q.get('options') or [] if isinstance(o, str)]})
            if questions:
                live.append({'id': 'async:' + item['id'], 'method': 'async-question', 'async': True,
                    'params': {'questions': questions, 'turnId': turn.get('turnId') or turn.get('id')}})
    return live


class Questions:
    def init_questions(self):
        self.question_groups = {}
        self.question_keys = {}
        self.async_baselines = {}
        self.async_submitted = set()

    def observe_questions(self, thread, state, owner):
        live = live_questions(state)
        for group_key, group in list(self.question_groups.items()):
            if group['thread'] == thread and not any(
                    r.get('id') == group['request']['id'] and r.get('params') == group['request'].get('params')
                    for r in live):
                self.drop_questions(group_key)
        if not enabled():
            return
        if thread not in self.async_baselines:
            turns = async_turns(state)
            latest = (turns[-1].get('turnId') or turns[-1].get('id')) if turns else None
            self.async_baselines[thread] = {r['id'] for r in live if r.get('async') and r['params']['turnId'] != latest}
        for request in live:
            if request.get('async') and (request['id'] in self.async_baselines[thread] or (thread, request['id']) in self.async_submitted):
                continue
            if any(g['thread'] == thread and g['owner'] == owner
                   and g['request'].get('id') == request.get('id')
                   and g['request'].get('params') == request.get('params')
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
                   and not r.get('completed') for r in live_questions(snap['state'])):
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
        if group['request'].get('async'):
            replies = [{'questionItemId': q['id'], 'question': q['question'],
                        'answer': answers[q['id']]['answers'][0]} for q in group['questions']]
            text = '<send_user_message_question_reply>\n' + json.dumps(replies, ensure_ascii=False) + '\n</send_user_message_question_reply>'
            params = turn_start_params(group['thread'], text)
            client_id = params['turnStart']['request']['clientUserMessageId']
            self.mirror.phone(group['thread'], client_id)
            turns = async_turns(self.snapshots[group['thread']]['state'])
            if turns and turns[-1].get('status') == 'inProgress':
                self.connect_ipc().request('thread-follower-steer-turn', {
                    'conversationId': group['thread'], 'input': params['turnStart']['request']['input'],
                    'clientUserMessageId': client_id, 'attachments': [],
                    'restoreMessage': {'id': client_id, 'text': text,
                        'cwd': self.snapshots[group['thread']]['state'].get('cwd'), 'createdAt': int(time.time()*1000),
                        'context': {'prompt': text, 'turnTrigger': 'send_user_message_async_question',
                            'addedFiles': [], 'fileAttachments': [], 'ideContext': None, 'imageAttachments': [],
                            'workspaceRoots': [self.snapshots[group['thread']]['state'].get('cwd') or '/']}}}, target=group['owner'])
            else:
                self.connect_ipc().request('thread-follower-start-turn', params, target=group['owner'])
            self.async_submitted.add((group['thread'], group['request']['id']))
        else:
            self.connect_ipc().request('thread-follower-submit-user-input', {
                'conversationId': group['thread'], 'requestId': group['request']['id'],
                'response': {'answers': answers}}, target=group['owner'])
        self.drop_questions(entry[0])
        self.say('Ответ отправлен агенту.', thread=group['thread'])
