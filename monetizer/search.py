"""One visible Chrome, resumable query/page positions, no background server."""
import threading
import time
from pathlib import Path

from .audit import Cancelled, normalize_username
from .keywords import unique_queries
from .settings import Settings


class SearchBudgetExceeded(Exception):
    pass


class TGStatSearch:
    def __init__(self, settings, stop=None, log=print, deadline=None):
        self.settings, self.stop, self.log = settings, stop or threading.Event(), log
        self.deadline, self.driver = deadline, None
        self.current_query, self.current_page = None, -1

    def _check(self):
        if self.stop.is_set():
            raise Cancelled()
        if self.deadline and time.monotonic() >= self.deadline:
            raise SearchBudgetExceeded()

    def _wait(self, predicate, seconds=40):
        from selenium.webdriver.support.ui import WebDriverWait
        def checked(driver):
            self._check()
            return predicate(driver)
        return WebDriverWait(self.driver, seconds, poll_frequency=.3).until(checked)

    def __enter__(self):
        return self  # Chrome is created only for an actual search page.

    def _open(self):
        import undetected_chromedriver as uc
        class QuietChrome(uc.Chrome):
            def __del__(self):
                try:
                    super().__del__()
                except OSError:
                    pass
        self._check()
        options = uc.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--no-first-run")
        profile = Path(__file__).resolve().parents[1] / "results" / "monetizer" / "chrome_profile"
        profile.mkdir(parents=True, exist_ok=True)
        self.log("Открываю Chrome для TGStat; при необходимости пройдите проверку в его окне.")
        self.driver = QuietChrome(options=options, user_data_dir=str(profile))
        self.driver.set_page_load_timeout(35)
        if self.settings.login_wait_seconds:
            self.driver.get("https://tgstat.ru/channels/search")
            self.log(f"Пауза {self.settings.login_wait_seconds} с для ручного входа.")
            until = time.monotonic() + self.settings.login_wait_seconds
            while time.monotonic() < until:
                self._check()
                self.stop.wait(.25)

    def _request(self, button):
        self._check()
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
        if response["http"] != 200:
            raise RuntimeError(f"TGStat HTTP {response['http']}")
        if response.get("status") == "restricted":
            self.log("TGStat требует авторизацию; ограничение сохранено отдельно от пустой выдачи.")
            return False
        if response.get("status") not in (None, "ok", "success"):
            raise RuntimeError(f"TGStat: {response['status']}")
        return True

    def _reset(self, query):
        from selenium.webdriver.common.by import By
        self.driver.get("https://tgstat.ru/channels/search")
        self._wait(lambda d: d.find_element(By.ID, "q").is_displayed() and
                   d.execute_script("return document.readyState==='complete' && window.jQuery && jQuery.active===0"), 90)
        for field_id, value in {"q": query, "participantscountfrom": self.settings.min_subs,
                                "participantscountto": self.settings.max_subs, "avgreach24from": self.settings.min_daily_reach}.items():
            field = self.driver.find_element(By.ID, field_id)
            self.driver.execute_script("""
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(arguments[0],arguments[1]);
                arguments[0].dispatchEvent(new Event('input',{bubbles:true}));
                arguments[0].dispatchEvent(new Event('change',{bubbles:true}));
            """, field, str(value))
            if field.get_property("value") != str(value):
                raise RuntimeError(f"Не применён фильтр {field_id}")
        for field_id in ("nodead", "noscam"):
            field = self.driver.find_element(By.ID, field_id)
            self.driver.execute_script("arguments[0].checked=true;arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", field)
            if not field.is_selected():
                raise RuntimeError(f"Не применён фильтр {field_id}")
        self.driver.execute_script("document.getElementById('inabout').checked=false;document.getElementById('channeltype').value='public';")
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

    def _more(self):
        from selenium.webdriver.common.by import By
        return next((b for b in self.driver.find_elements(By.CSS_SELECTOR, ".lm-button") if b.is_displayed() and b.is_enabled()), None)

    def fetch(self, query, page=0):
        self._check()
        if self.driver is None:
            self._open()
        if self.current_query != query or self.current_page > page:
            if not self._reset(query):
                return {"usernames": [], "exhausted": True, "restricted": True}
        result = self._names()
        while self.current_page < page:
            before = set(self._names())
            button = self._more()
            if button is None:
                return {"usernames": [], "exhausted": True, "restricted": False}
            if not self._request(button):
                return {"usernames": [], "exhausted": True, "restricted": True}
            self.current_page += 1
            result = [name for name in self._names() if name not in before]
            if not result:
                return {"usernames": [], "exhausted": True, "restricted": False}
        return {"usernames": result, "exhausted": self._more() is None, "restricted": False}

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

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
    return list(discover_channels([query_word], Settings(min_subs=min_subs, max_subs=max_subs,
                                                       min_daily_reach=min_reach, target_count=target_count)))
