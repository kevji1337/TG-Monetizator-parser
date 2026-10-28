"""Open the existing local app, or start it using this Python interpreter."""
import socket
import subprocess
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request
import webbrowser
import argparse
import os
import threading

from monetizer.diagnostics import logger, redact

URL = "http://127.0.0.1:8501"
ROOT = Path(__file__).resolve().parent
HEALTH_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy():
    try:
        with HEALTH_OPENER.open(URL + "/_stcore/health", timeout=.4) as response:
            return response.status == 200 and response.read(100).strip() == b"ok"
    except (OSError, urllib.error.URLError):
        return False


def server_command(dev=False):
    return [sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'monetizer_app.py'),
            '--server.address=127.0.0.1', '--server.port=8501', '--server.headless=true',
            '--browser.gatherUsageStats=false', '--server.fileWatcherType=poll', '--server.runOnSave=true',
            '--logger.level=' + ('info' if dev else 'warning')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true', help='Console logs and Streamlit debug logging')
    args = parser.parse_args()
    started = time.perf_counter()
    logger().info('launcher started dev=%s python=%s', args.dev, sys.version.split()[0])
    if healthy():
        logger().info('existing app healthy; opening browser')
        webbrowser.open(URL)
        return 0
    with socket.socket() as probe:
        probe.settimeout(.4)
        if probe.connect_ex(("127.0.0.1", 8501)) == 0:
            print("Port 8501 is in use by another application. Close it and retry.")
            return 1
    environment = os.environ.copy()
    environment['PYTHONUNBUFFERED'] = '1'
    if args.dev:
        environment['MONETIZER_DEV'] = '1'
    process = subprocess.Popen(server_command(args.dev), cwd=ROOT,
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace')
    def capture():
        for line in process.stdout:
            logger().info('streamlit %s', line.rstrip())
            if args.dev:
                print(redact(line.rstrip()), flush=True)
    reader = threading.Thread(target=capture, daemon=True, name='server-output')
    reader.start()
    try:
        for _ in range(300):
            if process.poll() is not None:
                return process.returncode
            if healthy():
                logger().info('server healthy elapsed=%.3fs', time.perf_counter() - started)
                webbrowser.open(URL)
                break
            time.sleep(.1)
        else:
            print("Startup is taking longer than expected. Open " + URL + " when ready.")
        result = process.wait()
        reader.join(2)
        logger().info('server exited returncode=%s', result)
        return result
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=15)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
