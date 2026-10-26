"""User-scheduled contact actions, independent of automated channel snapshots."""
from datetime import date

ACTIONS = {'first_message': 'Первое сообщение', 'follow_up': 'Повторный контакт',
           'reply': 'Ответить автору', 'call': 'Созвон', 'other': 'Другое действие'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS contact_plans(
    username TEXT PRIMARY KEY, due_date TEXT NOT NULL, action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)'''


def validate_plan(due_date, action, note):
    if not isinstance(due_date, str):
        raise ValueError('Укажите дату следующего контакта')
    try:
        day = date.fromisoformat(due_date)
    except ValueError:
        raise ValueError('Дата следующего контакта: ГГГГ-ММ-ДД') from None
    if day.isoformat() != due_date or not 1900 <= day.year <= 2100:
        raise ValueError('Дата следующего контакта: 1900–2100 год')
    if action not in ACTIONS:
        raise ValueError('Выберите следующее действие')
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError('Пояснение к действию: до 2000 символов')
    return due_date, action, note.strip()


def plan_state(due_date, today=None):
    if not due_date:
        return 'Без плана'
    today = today or date.today()
    planned = date.fromisoformat(due_date)
    return 'Просрочено' if planned < today else 'Сегодня' if planned == today else 'Запланировано'
