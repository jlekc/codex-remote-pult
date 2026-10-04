"""Telegram controls and Codex quota presentation."""
from datetime import datetime
from zoneinfo import ZoneInfo

MODE_ON = '🟢 Включён · выключить'
MODE_OFF = '🔴 Выключен · включить'

BUTTONS = {
    MODE_ON: '/off', MODE_OFF: '/on',
    '🔕 Выключен · включить': '/on',
    '🟢 Включить': '/on', '🔕 Выключить': '/off',
    '📊 Статус': '/status', '📈 Лимиты': '/limits',
    '💬 Выбрать чат': '/chats', '⏹ Остановить запрос': '/stop',
    '📋 Очередь': '/queue', '📎 Файлы': '/files',
    '➕ Новый чат': '/new', '❓ Помощь': '/help',
}
def keyboard(is_enabled):
    return {'keyboard': [
        [MODE_ON if is_enabled else MODE_OFF], ['📊 Статус', '📈 Лимиты'],
        ['💬 Выбрать чат', '⏹ Остановить запрос'], ['➕ Новый чат', '📋 Очередь'],
        ['📎 Файлы', '❓ Помощь']],
        'resize_keyboard': True, 'is_persistent': True,
        'input_field_placeholder': 'Промпт или кнопка управления'}


def format_limits(result):
    buckets = result.get('rateLimitsByLimitId') or {}
    if not buckets and result.get('rateLimits'):
        buckets = {'codex': result['rateLimits']}
    lines = ['📈 Лимиты Codex']
    if not buckets:
        return 'Лимиты сейчас недоступны. Для них нужен вход Codex через ChatGPT.'
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        lines.append('\n' + str(bucket.get('limitName') or bucket.get('limitId') or key))
        if bucket.get('planType'):
            lines.append('План: ' + str(bucket['planType']))
        has_window = False
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            has_window = True
            minutes = window.get('windowDurationMins')
            if isinstance(minutes, (int, float)) and minutes > 0:
                duration = (f'{minutes / 1440:g} дн.' if minutes >= 1440 else
                            f'{minutes / 60:g} ч.' if minutes >= 60 else f'{minutes:g} мин.')
            else:
                duration = 'длительность неизвестна'
            used = window.get('usedPercent')
            usage = (f'осталось {max(0, min(100, 100-used)):g}% (использовано {used:g}%)'
                     if isinstance(used, (int, float)) else 'остаток неизвестен')
            lines.append(f'Окно {duration}: {usage}')
            reset = window.get('resetsAt')
            if isinstance(reset, (int, float)):
                try:
                    date = datetime.fromtimestamp(reset, ZoneInfo('Europe/Moscow'))
                    lines.append('Обновление: ' + date.strftime('%d.%m.%Y %H:%M') + ' МСК')
                except (ValueError, OverflowError, OSError):
                    lines.append('Время обновления недоступно')
        if not has_window:
            lines.append('Данные по окнам пока недоступны')
        if bucket.get('rateLimitReachedType'):
            lines.append('⚠️ Лимит достигнут: ' + str(bucket['rateLimitReachedType']))
    lines.append('\nДанные на момент запроса. Проценты не определяют точное число оставшихся промптов.')
    return '\n'.join(lines)
