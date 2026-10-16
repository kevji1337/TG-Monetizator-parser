"""Reuse a compatible local driver before attempting a verified HTTPS download."""
import ctypes
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from .diagnostics import logger

DRIVER_DIR = Path(__file__).resolve().parents[1] / 'results/monetizer/drivers'
MANIFEST = 'https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json'


def file_version(path):
    if os.name == 'nt':
        from ctypes import wintypes
        api = ctypes.WinDLL('version', use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        length = api.GetFileVersionInfoSizeW(str(path), None)
        if length:
            data = ctypes.create_string_buffer(length)
            pointer, size = ctypes.c_void_p(), wintypes.UINT()
            if api.GetFileVersionInfoW(str(path), 0, length, data) and api.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(size)):
                words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
                return '.'.join(map(str, (words[2] >> 16, words[2] & 65535, words[3] >> 16, words[3] & 65535)))
        raise RuntimeError('Не удалось определить версию установленного Chrome')
    result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=8, check=True)
    return re.search(r'\d+(?:\.\d+){3}', result.stdout).group()


def driver_version(path):
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        match = re.search(r'ChromeDriver\s+(\d+(?:\.\d+){3})', result.stdout)
        return match.group(1) if match and result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def local_driver_candidates(major):
    suffix = '.exe' if os.name == 'nt' else ''
    yield DRIVER_DIR / f'chromedriver-{major}{suffix}'
    if os.name == 'nt':
        yield Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'undetected_chromedriver/undetected_chromedriver.exe'
    else:
        yield Path.home() / '.local/share/undetected_chromedriver/undetected_chromedriver'
    cache = Path.home() / '.cache/selenium/chromedriver'
    if cache.exists():
        yield from cache.glob('**/chromedriver' + suffix)


def prepare_driver(browser_path, log=print):
    version = file_version(browser_path)
    major = int(version.split('.')[0])
    DRIVER_DIR.mkdir(parents=True, exist_ok=True)
    destination = DRIVER_DIR / ('chromedriver-' + str(major) + ('.exe' if os.name == 'nt' else ''))
    for candidate in local_driver_candidates(major):
        if not candidate.is_file():
            continue
        cached = driver_version(candidate)
        if cached and cached.split('.')[0] == str(major):
            if candidate.resolve() != destination.resolve():
                shutil.copy2(candidate, destination)
            log(f'Chrome {version}; совместимый ChromeDriver {cached} найден в кэше. Скачивание не требуется.')
            logger().info('driver cache hit chrome=%s driver=%s path=%s', version, cached, destination)
            return str(destination), major
    log(f'Ищу ChromeDriver для Chrome {version}; локального совместимого драйвера нет.')
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    with requests.Session() as session:
        session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=.5, status_forcelist=[429, 502, 503, 504])))
        try:
            response = session.get(MANIFEST, timeout=(8, 20))
            response.raise_for_status()
            milestone = response.json()['milestones'][str(major)]
            platform = 'win64' if os.name == 'nt' and sys.maxsize > 2**32 else 'win32' if os.name == 'nt' else 'linux64'
            url = next(v['url'] for v in milestone['downloads']['chromedriver'] if v['platform'] == platform)
            if not url.startswith('https://storage.googleapis.com/chrome-for-testing-public/'):
                raise ValueError('Неожиданный источник ChromeDriver')
            package = session.get(url, timeout=(8, 45))
            package.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                name = next(n for n in archive.namelist() if n.endswith('/chromedriver.exe' if os.name == 'nt' else '/chromedriver'))
                if archive.getinfo(name).file_size > 100_000_000:
                    raise ValueError('Неожиданный размер ChromeDriver')
                temporary = destination.with_name(destination.stem + '.download' + ('.exe' if os.name == 'nt' else ''))
                temporary.write_bytes(archive.read(name))
            temporary.chmod(0o700)
            downloaded = driver_version(temporary)
            if not downloaded or downloaded.split('.')[0] != str(major):
                raise ValueError('Скачанный драйвер не соответствует Chrome')
            temporary.replace(destination)
            logger().info('driver downloaded chrome=%s driver=%s', version, downloaded)
            return str(destination), major
        except Exception as exc:
            logger().exception('driver preparation failed chrome=%s', version)
            raise RuntimeError('Не удалось подготовить ChromeDriver: ' + type(exc).__name__ + '. '
                               'Проверьте сеть/VPN. Проверка TLS остаётся включённой. Подробности — в Dev-логах.') from exc
