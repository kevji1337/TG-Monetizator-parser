"""Open the existing local app, or start it using this Python interpreter."""
import socket
import subprocess
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request
import webbrowser

URL = "http://localhost:8501"
ROOT = Path(__file__).resolve().parent


def healthy():
    try:
        with urllib.request.urlopen(URL + "/_stcore/health", timeout=1) as response:
            return response.status == 200 and response.read(100).strip() == b"ok"
    except (OSError, urllib.error.URLError):
        return False


def main():
    if healthy():
        webbrowser.open(URL)
        return 0
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8501)) == 0:
            print("Port 8501 is in use by another application. Close it and retry.")
            return 1
    process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "monetizer_app.py"),
                                "--server.address=127.0.0.1", "--server.port=8501", "--server.headless=true",
                                "--browser.gatherUsageStats=false"], cwd=ROOT)
    try:
        for _ in range(60):
            if process.poll() is not None:
                return process.returncode
            if healthy():
                webbrowser.open(URL)
                break
            time.sleep(.5)
        else:
            print("Startup is taking longer than expected. Open " + URL + " when ready.")
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=15)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
