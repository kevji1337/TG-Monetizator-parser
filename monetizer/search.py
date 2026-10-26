"""One visible Chrome, resumable query/page positions, no background server."""
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from .audit import Cancelled, normalize_username
from .keywords import unique_queries
from .settings import Settings
from .categories import (CACHE_PATH, category_key, category_id_from_key, validate_categories,
                         load_categories)
from .export import atomic_json
from .tgstat_filters import filter_spec
from .diagnostics import diagnosed, logger, safe_url, retain, retained_sessions, BrowserControl
from .tgstat_profiles import profile_directory, ROOT as PROFILE_ROOT


class RetryAfterLogin(Exception):
    manual_retry = True


class SearchBudgetExceeded(Exception):
    pass


class TGStatSearch:
    def __init__(self, settings, stop=None, log=print, deadline=None, control=None):
        self.settings, self.stop, self.log = settings, stop or threading.Event(), log
        self.deadline, self.driver = deadline, None
        self._chrome_guard = None
        self.current_query, self.current_page = None, -1
        self.applied_filters = None
        self._unavailable_urls = set()
        self.session_id, self.stage = uuid4().hex[:12], 'created'
        self.preserve_on_close = False
        self.control = control or BrowserControl()

    def location(self):
        try:
            return safe_url(self.driver.current_url) if self.driver else ''
        except Exception:
            return '[browser unavailable]'

    def _progress(self, message, waiting=False):
        logger().info('session=%s progress=%s', self.session_id, message)
        self.log(message)
        self.control.update(session_id=self.session_id, stage=self.stage, message=message, waiting=waiting)

    def _check(self):
        if self.stop.is_set():
            raise Cancelled()
        if self.deadline and time.monotonic() >= self.deadline:
            raise SearchBudgetExceeded()

    def _wait(self, predicate, seconds=40):
        from selenium.webdriver.support.ui import WebDriverWait
        started, reported = time.monotonic(), [0]
        def checked(driver):
            self._check()
            elapsed = time.monotonic() - started
            if elapsed >= reported[0] + 10:
                reported[0] = elapsed
                self.log(f'Ожидаю TGStat: {int(elapsed)} с. Если видна проверка или вход, выполните их в Chrome.')
            return predicate(driver)
        return WebDriverWait(self.driver, seconds, poll_frequency=.3).until(checked)

    def __enter__(self):
        return self  # Chrome is created only for an actual search page.

    @diagnosed('browser_start')
    def _open(self):
        from .runner import RunGuard
        self._check()
        if retained_sessions(include_url=False):
            raise RuntimeError('Chrome этого проекта уже оставлен открытым. Закройте его кнопкой в «Управление профилями TGStat» '
                               'или в Dev-диагностике, затем запустите поиск.')
        profile = profile_directory(self.settings.tgstat_profile_id)
        guard = RunGuard(PROFILE_ROOT / 'chrome_browser.session')
        try:
            guard.__enter__()
        except RuntimeError:
            raise RuntimeError('Chrome этого проекта уже используется. Закройте его окно в панели перед новым открытием.') from None
        self._chrome_guard = guard
        try:
            self._launch_browser(profile)
        except Exception:
            self.close(force=True)
            raise

    def _launch_browser(self, profile):
        import undetected_chromedriver as uc
        from .browser import prepare_driver
        class QuietChrome(uc.Chrome):
            def __del__(self):
                try:
                    super().__del__()
                except OSError:
                    pass
        self._check()
        browser_path = uc.find_chrome_executable()
        if not browser_path:
            raise RuntimeError('Chrome не найден. Установите Google Chrome и повторите запуск.')
        self._progress('Подготавливаю ChromeDriver…')
        driver_path, major = prepare_driver(browser_path, self.log)
        self._check()
        options = uc.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--no-first-run")
        logger().info('session=%s TGStat profile=%s', self.session_id, self.settings.tgstat_profile_id)
        self.log("Открываю Chrome для TGStat; при необходимости пройдите проверку в его окне.")
        self.driver = QuietChrome(options=options, user_data_dir=str(profile), driver_executable_path=driver_path,
                                  browser_executable_path=browser_path, version_main=major)
        self.driver.set_page_load_timeout(35)
        self._progress('Chrome открыт. Перехожу к TGStat.')
        if self.settings.login_wait_seconds:
            self.driver.get("https://tgstat.ru/channels/search")
            self.log(f"Пауза {self.settings.login_wait_seconds} с для ручного входа.")
            until = time.monotonic() + self.settings.login_wait_seconds
            while time.monotonic() < until:
                self._check()
                self.stop.wait(.25)

    @diagnosed('submit_or_next_page')
    def _request(self, button):
        self._check()
        self._verify_filters()
        self.driver.execute_script("""
            window.__monetizerRequest = null;
            jQuery(document).off('ajaxComplete.monetizer').on('ajaxComplete.monetizer', function(e,x,o) {
                if(o.url.split('?')[0].endsWith('/channels/search')) {
                    window.__monetizerRequest={http:x.status,status:x.responseJSON && x.responseJSON.status};
                }
            });
            arguments[0].click();
        """, button)
        self._wait(lambda d: d.execute_script("return window.__monetizerRequest !== null"))
        response = self.driver.execute_script("return window.__monetizerRequest")
        logger().info('session=%s AJAX http=%s status=%s', self.session_id, response.get('http'), response.get('status'))
        if response["http"] != 200:
            raise RuntimeError(f"TGStat HTTP {response['http']}")
        if response.get("status") == "restricted":
            self.preserve_on_close = True
            if self._wait_for_login():
                raise RetryAfterLogin()
            return False
        if response.get("status") not in (None, "ok", "success"):
            raise RuntimeError(f"TGStat: {response['status']}")
        return True

    def _wait_for_login(self):
        self.control.resume.clear()
        message = 'TGStat требует авторизацию или ограничил выдачу. Войдите в открытом Chrome и нажмите «Продолжить после входа» в панели.'
        self._progress(message, waiting=True)
        logger().warning('session=%s waiting for manual login timeout=%s', self.session_id, self.settings.browser_wait_seconds)
        until = time.monotonic() + self.settings.browser_wait_seconds
        try:
            while time.monotonic() < until:
                self._check()
                if self.control.resume.wait(.25):
                    return True
            self.log('Время ожидания входа истекло. Получение новых страниц этой тематики остановлено; '
                     'аудит уже найденных каналов может продолжаться. Chrome будет сохранён, если включена диагностика.')
            return False
        finally:
            self.control.update(session_id=self.session_id, stage=self.stage, waiting=False, message='Ожидание входа завершено')

    @diagnosed('load_search_form')
    def _load_form(self):
        from selenium.webdriver.common.by import By
        self._progress('Загружаю форму TGStat. При проверке сайта используйте открытый Chrome.')
        self.driver.get("https://tgstat.ru/channels/search")
        self._wait(lambda d: d.find_element(By.ID, "q").is_displayed() and
                   d.execute_script("return document.readyState==='complete' && window.jQuery && jQuery.active===0"), 90)

    @diagnosed('read_categories')
    def read_categories(self):
        """Read actual option IDs/labels; no search is submitted."""
        self._check()
        if self.driver is None:
            self._open()
        self._load_form()
        options = self.driver.execute_script("""
            return Object.fromEntries(['categories','countries','languages'].map(id => {
                const field = document.getElementById(id);
                return [id, field ? Array.from(field.options).filter(o => o.value && !o.disabled)
                    .map(o => ({id:o.value, title:o.textContent.trim()})) : []];
            }));
        """)
        for name in ('categories', 'countries', 'languages'):
            seen = set()
            options[name] = [item for item in options[name] if not (item['id'] in seen or seen.add(item['id']))]
            validate_categories({'schema_version': 1, 'categories': options[name]})
        snapshot = validate_categories({'schema_version': 1, 'source': 'https://tgstat.ru/channels/search',
                                       'observed_at': datetime.now(timezone.utc).isoformat(), **options})
        atomic_json(CACHE_PATH, snapshot)
        logger().info('session=%s category options=%s countries=%s languages=%s', self.session_id,
                      len(options['categories']), len(options['countries']), len(options['languages']))
        self.current_query, self.current_page = None, -1
        return snapshot

    def _apply_filters(self, spec):
        errors = self.driver.execute_script("""
            const spec = arguments[0], errors = [];
            for (const [id,value] of Object.entries(spec.text)) {
                const field = document.getElementById(id);
                if (!field) { errors.push(id + ': field missing'); continue; }
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(field,value);
                field.dispatchEvent(new Event('input',{bubbles:true}));
                field.dispatchEvent(new Event('change',{bubbles:true}));
            }
            for (const [id,values] of Object.entries(spec.selects)) {
                const field = document.getElementById(id);
                if (!field || values.some(value => !Array.from(field.options).some(o => o.value === value && !o.disabled))) {
                    errors.push(id + ': option missing'); continue;
                }
                Array.from(field.options).forEach(o => o.selected = values.includes(o.value));
                jQuery(field).trigger('change');
            }
            for (const [id,limits] of Object.entries(spec.sliders)) {
                const field = document.getElementById(id), slider = field && jQuery(field).data('ionRangeSlider');
                if (!slider) { errors.push(id + ': slider missing'); continue; }
                slider.update(limits);
            }
            for (const [id,value] of Object.entries(spec.checks)) {
                const field = document.getElementById(id);
                if (!field) { errors.push(id + ': checkbox missing'); continue; }
                field.checked = value;
                field.dispatchEvent(new Event('change',{bubbles:true}));
            }
            return errors;
        """, spec)
        if errors:
            raise RuntimeError('Не удалось применить фильтры TGStat: ' + '; '.join(errors))
        self.applied_filters = spec
        self._verify_filters()

    def _verify_filters(self):
        if self.applied_filters is None:
            raise RuntimeError('Фильтры TGStat не подготовлены; поиск остановлен')
        errors = self.driver.execute_script("""
            const spec = arguments[0], errors = [];
            for (const [id,value] of Object.entries(spec.text)) {
                const field = document.getElementById(id);
                if (!field || field.value !== value) errors.push(id);
            }
            for (const [id,values] of Object.entries(spec.selects)) {
                const field = document.getElementById(id);
                const actual = field ? Array.from(field.selectedOptions).map(o => o.value).sort() : null;
                if (JSON.stringify(actual) !== JSON.stringify([...values].sort())) errors.push(id);
            }
            for (const [id,limits] of Object.entries(spec.sliders)) {
                const field = document.getElementById(id), slider = field && jQuery(field).data('ionRangeSlider');
                if (!slider || Object.entries(limits).some(([key,value]) => Number(slider.result[key]) !== Number(value))) errors.push(id);
            }
            for (const [id,value] of Object.entries(spec.checks)) {
                const field = document.getElementById(id);
                if (!field || field.checked !== value) errors.push(id);
            }
            return errors;
        """, self.applied_filters)
        if errors:
            raise RuntimeError('Фильтры TGStat изменились или не применились: ' + ', '.join(errors) + '. Поиск остановлен.')

    @diagnosed('apply_search_filters')
    def _reset(self, query):
        from selenium.webdriver.common.by import By
        self._load_form()
        category_id = category_id_from_key(query) if self.settings.search_mode == 'categories' else None
        if category_id is not None and category_id not in self.settings.category_ids:
            raise ValueError('Тематика не входит в выбранные для запуска')
        spec = filter_spec(self.settings, category_id)
        if category_id is None:
            spec['text']['q'] = query
        self._apply_filters(spec)
        self._progress('Фильтры применены. Запрашиваю каналы выбранной тематики…')
        self.current_query, self.current_page = query, 0
        return self._request(self.driver.find_element(By.ID, "search-form-submit-btn"))

    def _names(self):
        from selenium.webdriver.common.by import By
        result = []
        for link in self.driver.find_elements(By.CSS_SELECTOR, ".channels-list a[href*='/channel/@']"):
            raw = (link.get_attribute("href") or "").split("/channel/@")[-1].split("/")[0].split("?")[0]
            try:
                name = normalize_username(raw)
                if name not in result:
                    result.append(name)
            except ValueError:
                pass
        return result

    def _channel_urls(self):
        from selenium.webdriver.common.by import By
        return {link.get_attribute('href') for link in self.driver.find_elements(By.CSS_SELECTOR, ".channels-list a[href*='/channel/']")
                if link.get_attribute('href')}

    def _more(self):
        from selenium.webdriver.common.by import By
        return next((b for b in self.driver.find_elements(By.CSS_SELECTOR, ".lm-button") if b.is_displayed() and b.is_enabled()), None)

    @diagnosed('fetch_category_page')
    def fetch(self, query, page=0):
        for attempt in range(3):
            try:
                result = self._fetch(query, page)
                logger().info('session=%s category=%s page=%s found=%s exhausted=%s restricted=%s',
                              self.session_id, query, page, len(result['usernames']), result['exhausted'], result.get('restricted'))
                return result
            except RetryAfterLogin:
                self.current_query, self.current_page = None, -1
                self._progress('Повторяю поиск после ручного входа…')
        return {'usernames': [], 'exhausted': True, 'restricted': True}

    def _fetch(self, query, page=0):
        self._check()
        before_unavailable = len(self._unavailable_urls)
        if self.driver is None:
            self._open()
        if self.current_query != query or self.current_page > page:
            if not self._reset(query):
                return {"usernames": [], "exhausted": True, "restricted": True}
        result = self._names()
        self._unavailable_urls.update(url for url in self._channel_urls() if '/channel/@' not in url)
        while self.current_page < page:
            before = set(self._names())
            before_urls = self._channel_urls()
            button = self._more()
            if button is None:
                return {"usernames": [], "exhausted": True, "restricted": False}
            if not self._request(button):
                return {"usernames": [], "exhausted": True, "restricted": True}
            self.current_page += 1
            result = [name for name in self._names() if name not in before]
            after_urls = self._channel_urls()
            self._unavailable_urls.update(url for url in after_urls if '/channel/@' not in url)
            if not result and not after_urls - before_urls:
                return {"usernames": [], "exhausted": True, "restricted": False}
        return {"usernames": result, "exhausted": self._more() is None, "restricted": False,
                'without_public_username': len(self._unavailable_urls) - before_unavailable}

    def close(self, force=False):
        if self.driver:
            if self.preserve_on_close and self.settings.keep_browser_on_error and not force:
                retain(self)
                self._progress('Chrome оставлен открытым для диагностики. Закрыть его можно в разделе Dev-диагностики.')
                return
            try:
                self.driver.quit()
            except Exception:
                logger().exception('session=%s browser close failed', self.session_id)
            self.driver = None
            logger().info('session=%s browser closed', self.session_id)
        if self._chrome_guard is not None:
            self._chrome_guard.__exit__()
            self._chrome_guard = None

    def __exit__(self, *args):
        self.close()


def discover_channels(queries, settings=None, stop=None, log=print, on_candidate=None, initial=None):
    """Compatibility helper returning raw usernames; the runner qualifies candidates."""
    settings = settings or Settings()
    candidates = {u: list(q) for u, q in (initial or {}).items()}
    with TGStatSearch(settings, stop, log) as search:
        for query in unique_queries(queries):
            page = 0
            while len(candidates) < settings.target_count:
                response = search.fetch(query, page)
                for username in response["usernames"]:
                    keys = candidates.setdefault(username, [])
                    if query not in keys:
                        keys.append(query)
                        if on_candidate:
                            on_candidate(username, query)
                    if len(candidates) >= settings.target_count:
                        break
                if response["exhausted"]:
                    break
                page += 1
    return candidates


def search_tgstat_by_methodology(query_word="", category="", min_subs=3000, max_subs=30000, min_reach=300, target_count=40):
    if category:
        options = load_categories()['categories']
        selected = next((c for c in options if str(category) == c['id'] or str(category).casefold() == c['title'].casefold()), None)
        if selected is None:
            raise ValueError('Неизвестная тематика TGStat: укажите ID или название из каталога')
        if query_word:
            raise ValueError('Для поиска по тематике оставьте текстовый запрос пустым')
        queries = [category_key(selected['id'])]
        settings = Settings(min_subs=min_subs, max_subs=max_subs, min_daily_reach=min_reach,
                            target_count=target_count, search_mode='categories', category_ids=(selected['id'],))
    else:
        if not query_word:
            raise ValueError('Выберите тематику TGStat')
        queries = [query_word]  # Compatibility for explicitly requested historical keyword searches.
        settings = Settings(min_subs=min_subs, max_subs=max_subs, min_daily_reach=min_reach, target_count=target_count)
    return list(discover_channels(queries, settings))


def refresh_categories(settings=None):
    with TGStatSearch(settings or Settings()) as search:
        return search.read_categories()
