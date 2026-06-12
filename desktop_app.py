"""WexFlow — десктопное приложение.

Запускает локальные серверы (7-Eleven, Salling, единый Hub) в фоне и
открывает их в отдельном окне приложения (pywebview / WebView2), без
браузера и адресной строки. Закрытие окна останавливает только те
серверы, которые приложение запустило само (если ты уже открыл их через
JobApplyHub.bat — они продолжат работать).

Запуск без сборки:  .venv\\Scripts\\pythonw.exe desktop_app.py
Сборка в WexFlow.exe:  СОБРАТЬ_ПРИЛОЖЕНИЕ.bat
"""
import os
import sys
import time
import socket
import subprocess
import pathlib
import urllib.request

# ── пути ───────────────────────────────────────────────────────────────
# при сборке в .exe sys.executable указывает на сам exe — берём его папку
if getattr(sys, "frozen", False):
    APP_ROOT = pathlib.Path(sys.executable).resolve().parent
else:
    APP_ROOT = pathlib.Path(__file__).resolve().parent

SEVEN_ROOT = pathlib.Path(r"C:\seven11-apply")

PY = APP_ROOT / ".venv" / "Scripts" / "python.exe"
SEVEN_PY = SEVEN_ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = pathlib.Path(sys.executable if not getattr(sys, "frozen", False) else "python")
if not SEVEN_PY.exists():
    SEVEN_PY = PY

HUB_URL = "http://127.0.0.1:8080/"
CREATE_NO_WINDOW = 0x08000000  # фоновые серверы — без чёрных консолей

_started = []  # серверы, которые запустило именно это приложение


def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.4):
            return True
    except OSError:
        return False


def _spawn(args, cwd):
    return subprocess.Popen(
        [str(a) for a in args],
        cwd=str(cwd),
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure(port, args, cwd, name):
    """Поднять сервер, только если порт ещё не отвечает."""
    if _port_up(port):
        return
    try:
        _started.append((name, _spawn(args, cwd)))
    except Exception as exc:  # noqa: BLE001
        print(f"[WexFlow] не удалось запустить {name}: {exc}")


def start_servers():
    if SEVEN_ROOT.exists():
        _ensure(7111, [SEVEN_PY, "web_app.py"], SEVEN_ROOT, "7-Eleven")
    _ensure(8000, [PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
            APP_ROOT, "Salling")
    _ensure(8080, [PY, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1", "--port", "8080"],
            APP_ROOT, "Hub")


def wait_for_hub(timeout=45) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(HUB_URL, timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def stop_started():
    for _name, proc in _started:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def main():
    start_servers()
    ready = wait_for_hub()

    import webview

    start_url = HUB_URL if ready else "data:text/html;charset=utf-8," + urllib.request.quote(
        "<body style='font:16px system-ui;background:#101111;color:#e6e8e6;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;"
        "text-align:center'>"
        "<div><h2>WexFlow не смог запустить серверы</h2>"
        "<p>Закрой это окно и запусти заново. Если не помогает — открой JobApplyHub.bat.</p></div></body>"
    )

    webview.create_window(
        "WexFlow",
        start_url,
        width=1280,
        height=860,
        min_size=(900, 600),
        background_color="#101111",
    )
    try:
        webview.start()
    finally:
        stop_started()


if __name__ == "__main__":
    main()
