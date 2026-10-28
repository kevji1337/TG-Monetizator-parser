"""Sequential TGStat profiles with a durable, finite list per search run."""
import copy
from dataclasses import replace
from datetime import datetime, timezone

from .search import TGStatSearch
from .tgstat_profiles import TGStatProfiles


def profile_plan(repo, settings):
    profiles = TGStatProfiles(repo).list()
    names = {item['id']: item['name'] for item in profiles}
    if not names:
        raise ValueError('Добавьте или восстановите профиль в разделе «Профили TGStat»')
    first = settings.tgstat_profile_id
    if first not in names:
        raise ValueError('Выбранный профиль TGStat не найден')
    return {'order': [first] + [item['id'] for item in profiles if item['id'] != first],
            'names': names, 'active': first, 'skipped': {}, 'exhausted': False}


def reconcile_profiles(repo, plan):
    """Deleted profiles must never reappear through a historical checkpoint."""
    current = {item['id']: item['name'] for item in TGStatProfiles(repo).list()}
    if not current:
        raise ValueError('Нет активных профилей TGStat. Добавьте или восстановите профиль перед продолжением.')
    plan = copy.deepcopy(plan)
    plan['order'] = [item for item in plan['order'] if item in current]
    if not plan['order']:
        raise ValueError('Все профили этого запуска удалены. Восстановите нужный профиль или начните новый поиск.')
    plan['names'].update(current)
    if plan['active'] not in plan['order']:
        plan['active'] = next((item for item in plan['order'] if item not in plan['skipped']), plan['order'][0])
    plan['exhausted'] = all(item in plan['skipped'] for item in plan['order'])
    return plan


class ProfileSearch:
    def __init__(self, settings, plan, stop, log, deadline, control, on_change, search_factory=None):
        self.settings, self.plan = settings, copy.deepcopy(plan)
        self.stop, self.log, self.deadline, self.control = stop, log, deadline, control
        self.on_change, self.search = on_change, None
        self.search_factory = search_factory or TGStatSearch

    def _save(self):
        self.on_change(copy.deepcopy(self.plan))

    def fetch(self, query, page=0):
        if self.settings.tgstat_auto_switch and self.plan['exhausted']:
            return {'usernames': [], 'exhausted': True, 'restricted': True}
        while True:
            if self.search is None:
                current = replace(self.settings, tgstat_profile_id=self.plan['active'])
                # The explicit initial login pause applies only to the starting profile.
                if self.plan['skipped']:
                    current = replace(current, login_wait_seconds=0)
                self.search = self.search_factory(current, self.stop, self.log, self.deadline, self.control)
            response = self.search.fetch(query, page)
            if not response.get('restricted') or not self.settings.tgstat_auto_switch:
                if not response.get('restricted') and self.plan['active'] in self.plan['skipped']:
                    # A successful manual login makes this profile available again.
                    self.plan['skipped'].pop(self.plan['active'])
                    self.plan['exhausted'] = False
                    self._save()
                return {**response, 'page_number': page}
            active = self.plan['active']
            reason = self.search.last_restriction or 'TGStat ограничил выдачу'
            self.plan['skipped'][active] = {'reason': reason, 'at': datetime.now(timezone.utc).isoformat()}
            label = self.plan['names'].get(active, active)
            self.log(f'Профиль TGStat «{label}» пропущен: {reason}')
            next_id = next((item for item in self.plan['order'] if item not in self.plan['skipped']), None)
            if next_id is None:
                self.plan['exhausted'] = True
                self._save()
                self.log('Доступных профилей TGStat не осталось. Сохраняю прогресс; аудит найденных каналов продолжается.')
                return {**response, 'page_number': page}
            # Quit the previous Chrome before opening another; its session stays on disk.
            self.search.close(force=True)
            self.search = None
            self.plan['active'] = next_id
            self.plan['exhausted'] = False
            self._save()
            self.log('Переключаю TGStat на профиль «' + self.plan['names'].get(next_id, next_id) +
                     '». Повторяю тематику с первой страницы; найденные каналы сохраняются, дубли отсеиваются.')
            page = 0

    def close(self):
        if self.search is not None:
            self.search.close()
