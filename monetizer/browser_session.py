"""Keep native Chrome sessions on disk and close Chrome without terminating it."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import time

from .diagnostics import logger
from .export import atomic_json


class BrowserStillRunning(RuntimeError):
    pass


def configure_session_restore(profile):
    """Called only under the browser lock, before Chrome starts; no cookie edits."""
    prefs = Path(profile) / 'Default' / 'Preferences'
    data = json.loads(prefs.read_text(encoding='utf-8')) if prefs.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get('session', {}), dict):
        raise ValueError('Не удалось прочитать настройки профиля Chrome; исходный файл сохранён без изменений')
    if data.get('session', {}).get('restore_on_startup') != 1:
        if prefs.exists():
            backup = prefs.with_name('Preferences.before-session-restore-v1.json')
            if not backup.exists():
                shutil.copy2(prefs, backup)
        data.setdefault('session', {})['restore_on_startup'] = 1
        atomic_json(prefs, data)


@contextmanager
def command_timeout(driver, seconds=8):
    """Bound local WebDriver commands, including commands after a closed window."""
    config = getattr(getattr(driver, 'command_executor', None), '_client_config', None)
    previous = config.timeout if config is not None else None
    if config is not None:
        config.timeout = seconds
    try:
        yield
    finally:
        if config is not None:
            config.timeout = previous


class BrowserExit:
    """Hold a Windows process handle so PID reuse cannot affect the wait."""
    def __init__(self, pid):
        self.pid, self.handle, self.api = pid, None, None
        self.already_exited = not pid
        if os.name == 'nt' and pid:
            from ctypes import wintypes
            self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.api.OpenProcess.restype = wintypes.HANDLE
            self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.api.WaitForSingleObject.restype = wintypes.DWORD
            self.api.CloseHandle.argtypes = [wintypes.HANDLE]
            self.api.CloseHandle.restype = wintypes.BOOL
            self.handle = self.api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
            if not self.handle:
                if ctypes.get_last_error() == 87:  # PID no longer exists.
                    self.already_exited = True
                else:
                    raise BrowserStillRunning('Не удалось подтвердить завершение Chrome. Закройте его вручную и повторите закрытие в панели.')

    def wait(self, seconds):
        if self.already_exited:
            return True
        if self.handle:
            result = self.api.WaitForSingleObject(self.handle, int(seconds * 1000))
            if result == 0xFFFFFFFF:
                raise BrowserStillRunning('Не удалось дождаться завершения Chrome; профиль остаётся занят')
            return result == 0
        until = time.monotonic() + seconds
        while True:
            try:
                os.kill(self.pid, 0)  # Query existence; never terminate a process.
            except ProcessLookupError:
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(.1)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def graceful_quit(driver):
    """UC.quit kills Chrome on Windows. Request Browser.close and wait instead."""
    if getattr(driver, '_monetizer_quit_done', False):
        return
    watcher = BrowserExit(getattr(driver, 'browser_pid', None))
    close_kind = 'already_closed' if watcher.already_exited else 'graceful'
    try:
        if not watcher.already_exited:
            try:
                with command_timeout(driver):
                    driver.execute_cdp_cmd('Browser.close', {})
            except Exception as exc:
                # A disconnect is normal when Chrome closes. The process handle
                # determines whether closing actually succeeded.
                logger().info('browser close command ended type=%s', type(exc).__name__)
            if not watcher.wait(15):
                raise BrowserStillRunning('Chrome ещё работает. Закройте его вручную и повторите закрытие в панели. '
                                          'Переключение профиля отложено, чтобы сохранить данные.')
    finally:
        watcher.close()
    driver._monetizer_quit_done = True
    driver._monetizer_close_kind = close_kind
    try:
        reactor = getattr(driver, 'reactor', None)
        if reactor is not None:
            reactor.event.set()
        executor = getattr(driver, 'command_executor', None)
        if executor is not None:
            executor.close()
        service = getattr(driver, 'service', None)
        if service is not None:
            service.stop()
    except Exception as exc:
        logger().warning('browser exited; driver cleanup type=%s', type(exc).__name__)
    driver.patcher = None


def login_state(driver):
    """Inspect only auth controls in the current TGStat page, never cookie values."""
    unknown = {'auth_status': 'unknown', 'auth_reason': 'Не удалось проверить вход в открытом Chrome'}
    try:
        with command_timeout(driver):
            state = driver.execute_script(r"""
                const host = location.hostname;
                if (!(host === 'tgstat.ru' || host.endsWith('.tgstat.ru'))) return {site:false};
                const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
                const links = [...document.querySelectorAll('a[href],form[action]')];
                const path = el => {try {
                    const url = new URL(el.href || el.action,location.href);
                    return url.origin === location.origin ? url.pathname : '';
                } catch {return '';}};
                const inMenu = el => !!el.closest('header,nav,.navbar,.dropdown-menu,.user-menu');
                const logout = links.some(el => /\/(logout|signout)(\/|$)/i.test(path(el)) ||
                    (inMenu(el) && /^(выйти( из аккаунта)?|выход|log\s*out|sign\s*out)$/i.test(el.textContent.trim())));
                const login = links.some(el => visible(el) && (/\/(login|signin)(\/|$)/i.test(path(el)) ||
                    (inMenu(el) && /^(войти( в tgstat)?|вход|log\s*in|sign\s*in)$/i.test(el.textContent.trim()))));
                const challenge = /just a moment|checking your browser|проверка браузера|security verification/i.test(document.title);
                return {site:true,logout,login,challenge};
            """)
    except Exception:
        return unknown
    if not isinstance(state, dict) or not state.get('site'):
        return {**unknown, 'auth_reason': 'В активной вкладке нет страницы TGStat'}
    if state.get('challenge'):
        return {**unknown, 'auth_reason': 'Сначала пройдите проверку сайта в Chrome'}
    if state.get('logout') and not state.get('login'):
        return {'auth_status': 'authenticated', 'auth_reason': 'На странице найдено управление выходом из аккаунта TGStat'}
    if state.get('login') and not state.get('logout'):
        return {'auth_status': 'not_authenticated', 'auth_reason': 'TGStat показывает кнопку входа'}
    return {**unknown, 'auth_reason': 'На странице нет однозначного признака входа. Проверьте аккаунт в Chrome вручную'}
