"""WexFlow — десктопное приложение (раздаваемая сборка).

Два режима работы одного и того же .exe:

1. ОКНО (обычный запуск без аргументов): поднимает локальные серверы и
   открывает их в нативном окне (pywebview / WebView2).
2. ВОРКЕР (запуск с аргументом --worker-...): exe запускает сам себя, чтобы
   выполнить отдельную задачу (сервер модуля или подачу анкеты). Это нужно,
   потому что на чужом ПК нет ни Python, ни .venv — exe должен уметь всё сам.

Пользовательские данные хранятся в %AppData%\\WexFlow (а не рядом с программой),
поэтому у каждого они свои и на старте пустые. Браузер Chromium для автоподачи
скачивается при первом запуске в %AppData%\\WexFlow\\ms-playwright.

Запуск без сборки (dev):  .venv\\Scripts\\pythonw.exe desktop_app.py
Сборка дистрибутива:      СОБРАТЬ_ДИСТРИБУТИВ.bat
"""
import os
import sys
import time
import json
import socket
import subprocess
import pathlib
import shutil
import tempfile
import threading
import urllib.request
import zipfile

APP_NAME = "WexFlow"

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# ── пути ───────────────────────────────────────────────────────────────
if is_frozen():
    BUNDLE_DIR = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).resolve().parent))
    APP_ROOT = pathlib.Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = pathlib.Path(__file__).resolve().parent
    APP_ROOT = BUNDLE_DIR

# Код 7-Eleven: в сборке — отдельная папка seven11/ внутри бандла; в dev — старое место.
SEVEN_DIR = (BUNDLE_DIR / "seven11") if is_frozen() else pathlib.Path(r"C:\seven11-apply")

# dev-интерпретаторы (только для запуска без сборки)
PY = APP_ROOT / ".venv" / "Scripts" / "python.exe"
SEVEN_PY = SEVEN_DIR / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = pathlib.Path(sys.executable if not is_frozen() else "python")
if not SEVEN_PY.exists():
    SEVEN_PY = PY

SALLING_PORT = 8000
HUB_PORT = 8080
SEVEN_PORT = 7111
HUB_URL = f"http://127.0.0.1:{HUB_PORT}/__app/salling?next=/hub"
CREATE_NO_WINDOW = 0x08000000  # фоновые серверы — без чёрных консолей

_started = []  # дочерние процессы, которые запустило именно это приложение


# ── общие хелперы окружения ────────────────────────────────────────────
def appdata_root() -> pathlib.Path:
    base = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
    d = pathlib.Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def browsers_dir() -> pathlib.Path:
    return appdata_root() / "ms-playwright"


def set_playwright_env() -> None:
    """Браузер храним в %AppData%, чтобы он пережил обновления приложения."""
    if is_frozen():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir()))


def chromium_installed() -> bool:
    d = browsers_dir()
    try:
        return any(p.name.startswith("chromium") for p in d.iterdir())
    except OSError:
        return False


def _self_cmd(*args: str) -> list:
    """Команда «запусти меня же» — в сборке это сам exe, в dev — python + этот файл."""
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, str(pathlib.Path(__file__).resolve()), *args]


def _updater_bat(src: pathlib.Path, target: pathlib.Path) -> str:
    """Сценарий апдейтера: ждёт выхода приложения, заменяет файлы, перезапускает.

    Запускается отдельным cmd-процессом, поэтому переживает закрытие приложения.
    """
    return (
        "@echo off\r\n"
        "rem WexFlow auto-updater\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "taskkill /F /IM WexFlow.exe >nul 2>&1\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        f'robocopy "{src}" "{target}" /E /NFL /NDL /NJH /NJS /NP /R:5 /W:1 >nul\r\n'
        "ping -n 2 127.0.0.1 >nul\r\n"
        f'start "" "{target}\\WexFlow.exe"\r\n'
    )


def _hidden_updater_vbs(bat: pathlib.Path) -> str:
    path = str(bat).replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run Chr(34) & "{path}" & Chr(34), 0, False\r\n'
    )


# ── режим ВОРКЕРА (frozen): exe выполняет одну задачу и выходит ─────────
def _add_seven_path() -> None:
    p = str(SEVEN_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def run_worker(mode: str, rest: list) -> None:
    set_playwright_env()

    if mode == "--worker-salling-server":
        import uvicorn
        import app as salling_app
        uvicorn.run(salling_app.app, host="127.0.0.1",
                    port=int(rest[0]) if rest else SALLING_PORT, log_level="warning")

    elif mode == "--worker-hub-server":
        import uvicorn
        import hub as hub_app
        uvicorn.run(hub_app.app, host="127.0.0.1",
                    port=int(rest[0]) if rest else HUB_PORT, log_level="warning")

    elif mode == "--worker-7e-server":
        _add_seven_path()
        import web_app  # из seven11/ (изолированный sys.path)
        web_app.serve(int(rest[0]) if rest else SEVEN_PORT, open_browser=False)

    elif mode == "--worker-salling-apply":
        import apply as salling_apply
        salling_apply.main(rest)

    elif mode == "--worker-7e-apply":
        _add_seven_path()
        # apply.py 7-Eleven грузим по пути под уникальным именем — чтобы не
        # столкнуться с apply.py Salling, который уже внутри сборки.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "seven11_apply", str(SEVEN_DIR / "apply.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["seven11_apply"] = mod
        spec.loader.exec_module(mod)
        sys.argv = ["apply"] + list(rest)
        mod.app()  # typer-приложение

    elif mode == "--worker-pwinstall":
        install_browser_blocking()


def install_browser_blocking() -> int:
    """Скачать Chromium для Playwright (вызывается в воркере --worker-pwinstall)."""
    set_playwright_env()
    try:
        from playwright.__main__ import main as pw_main
    except Exception as exc:  # noqa: BLE001
        print(f"[WexFlow] playwright недоступен: {exc}")
        return 1
    sys.argv = ["playwright", "install", "chromium"]
    original_popen = subprocess.Popen

    def hidden_popen(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        kwargs.setdefault("stderr", subprocess.DEVNULL)
        return original_popen(*args, **kwargs)

    try:
        subprocess.Popen = hidden_popen
        pw_main()
    except SystemExit as e:  # playwright CLI зовёт sys.exit
        return int(e.code or 0)
    finally:
        subprocess.Popen = original_popen
    return 0


def ensure_browser_async() -> None:
    """Если браузера ещё нет — тихо скачать его в фоне отдельным процессом."""
    if not is_frozen() or chromium_installed():
        return

    def _run():
        try:
            _started.append(("pwinstall", subprocess.Popen(
                _self_cmd("--worker-pwinstall"),
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )))
        except Exception as exc:  # noqa: BLE001
            print(f"[WexFlow] не удалось запустить загрузку браузера: {exc}")

    threading.Thread(target=_run, daemon=True).start()


# ── управление окном (js_api) ──────────────────────────────────────────
class WindowControls:
    def __init__(self):
        self._maximized = False
        self._fullscreen = False
        self.update_info = None  # заполняется фоновой проверкой обновлений

    def _window(self):
        import webview
        return webview.windows[0] if webview.windows else None

    def minimize(self):
        window = self._window()
        if window:
            window.minimize()
        return True

    def toggle_maximize(self):
        window = self._window()
        if window:
            try:
                window.toggle_fullscreen()
                self._fullscreen = not self._fullscreen
            except Exception:  # noqa: BLE001
                if self._maximized:
                    window.restore()
                else:
                    window.maximize()
                self._maximized = not self._maximized
        return True

    def get_location(self):
        script = r"""
Add-Type -AssemblyName System.Device
$watcher = New-Object System.Device.Location.GeoCoordinateWatcher
$started = $watcher.TryStart($false, [TimeSpan]::FromSeconds(10))
$coord = $watcher.Position.Location
if ($started -and -not $coord.IsUnknown) {
  [pscustomobject]@{
    ok = $true
    lat = $coord.Latitude
    lng = $coord.Longitude
    accuracy = $coord.HorizontalAccuracy
  } | ConvertTo-Json -Compress
} else {
  [pscustomobject]@{
    ok = $false
    error = "Windows location unavailable or denied"
  } | ConvertTo-Json -Compress
}
"""
        try:
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                timeout=14,
                creationflags=CREATE_NO_WINDOW,
                stderr=subprocess.DEVNULL,
            )
            return json.loads(output.decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:240]}

    def resize_window(self, width, height, anchor="nw"):
        """Растянуть окно (frameless-окно своих рамок не имеет — тянем через API).

        anchor — какой угол остаётся на месте: nw/ne/sw/se. Это позволяет тянуть
        за любой край/угол, а противоположная сторона стоит неподвижно.
        """
        window = self._window()
        if not window:
            return False
        w = max(900, int(width))
        h = max(600, int(height))
        try:
            import webview
            fp = getattr(webview, "FixPoint", None)
            if fp is None:
                from webview.window import FixPoint as fp  # noqa: N813
            amap = {
                "nw": fp.NORTH | fp.WEST,
                "ne": fp.NORTH | fp.EAST,
                "sw": fp.SOUTH | fp.WEST,
                "se": fp.SOUTH | fp.EAST,
            }
            window.resize(w, h, amap.get(anchor, fp.NORTH | fp.WEST))
        except Exception:  # noqa: BLE001 — на крайний случай без якоря
            try:
                window.resize(w, h)
            except Exception:  # noqa: BLE001
                return False
        return True

    def move_window_by(self, dx, dy):
        window = self._window()
        if not window:
            return False
        try:
            left = int(getattr(window, "x", 0)) + int(dx)
            top = int(getattr(window, "y", 0)) + int(dy)
            window.move(left, top)
            return True
        except Exception:  # noqa: BLE001
            return False

    def app_info(self):
        """Версия и доступное обновление — для баннера в интерфейсе."""
        try:
            import version
            ver = version.__version__
        except Exception:  # noqa: BLE001
            ver = "dev"
        return {"version": ver, "update": self.update_info}

    def open_external(self, url):
        """Открыть ссылку в системном браузере (а не внутри окна приложения)."""
        try:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return False
            import webbrowser
            webbrowser.open(url)
            return True
        except Exception:  # noqa: BLE001
            return False

    def install_update(self, url):
        """Автообновление: скачать новую версию, закрыть приложение, подменить
        файлы и снова открыть. Работает только в собранном приложении."""
        if not is_frozen():
            return {"ok": False, "error": "Автообновление доступно только в собранном приложении."}
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            return {"ok": False, "error": "bad url"}
        threading.Thread(target=self._run_update, args=(url,), daemon=True).start()
        return {"ok": True}

    def _run_update(self, url):
        try:
            work = pathlib.Path(tempfile.gettempdir()) / "wexflow_update"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)

            zpath = work / "update.zip"
            self._set_update_status("Скачиваю обновление…")
            if not self._download(url, zpath):
                # сеть/таймаут/антивирус — не вешаемся навсегда, а даём
                # пользователю рабочий запасной путь (страница загрузки)
                self._fallback_to_browser(url)
                return

            self._set_update_status("Распаковываю…")
            extracted = work / "new"
            with zipfile.ZipFile(zpath) as z:
                z.extractall(extracted)

            # внутри архива папка WexFlow/ (или exe лежит глубже — найдём)
            src = extracted / "WexFlow"
            if not (src / "WexFlow.exe").exists():
                found = list(extracted.glob("**/WexFlow.exe"))
                if not found:
                    print("[WexFlow] в архиве нет WexFlow.exe")
                    self._fallback_to_browser(url)
                    return
                src = found[0].parent

            target = APP_ROOT  # папка, где лежит текущий WexFlow.exe
            bat = work / "apply_update.bat"
            bat.write_text(_updater_bat(src, target), encoding="ascii")
            vbs = work / "run_update_hidden.vbs"
            vbs.write_text(_hidden_updater_vbs(bat), encoding="ascii")

            self._set_update_status("Устанавливаю, приложение перезапустится…")
            # запускаем апдейтер отдельным, не зависящим от нас процессом
            # (он сам убьёт все WexFlow.exe и подменит файлы)
            DETACHED = 0x00000008
            NEW_GROUP = 0x00000200
            try:
                subprocess.Popen(["wscript.exe", str(vbs)],
                                 creationflags=CREATE_NO_WINDOW | DETACHED | NEW_GROUP,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
            except Exception:
                subprocess.Popen(["cmd", "/c", str(bat)],
                                 creationflags=CREATE_NO_WINDOW | DETACHED | NEW_GROUP,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
            # закрываем приложение — апдейтер дождётся выхода и подменит файлы
            stop_started()
            window = self._window()
            if window:
                window.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"[WexFlow] обновление не удалось: {exc}")
            self._fallback_to_browser(url)

    def _download(self, url, dest, attempts=3):
        """Скачать файл с таймаутом, прогрессом и повтором. True — успех.

        Ключевое отличие от urllib.urlretrieve: на каждое чтение действует
        таймаут (timeout=30), поэтому зависший канал больше НЕ вешает обновление
        навсегда — попытка обрывается, делается повтор, а затем (если совсем не
        вышло) уходим в браузерный фолбэк. Прогресс показываем в баннере.
        """
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "WexFlow-Updater"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    last_pct = -1
                    with open(dest, "wb") as f:
                        while True:
                            chunk = r.read(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done * 100 // total
                                if pct >= last_pct + 5:
                                    last_pct = pct
                                    self._set_update_status(f"Скачиваю обновление… {pct}%")
                if dest.exists() and dest.stat().st_size > 0:
                    return True
            except Exception as exc:  # noqa: BLE001 — нет сети/таймаут/обрыв
                print(f"[WexFlow] загрузка обновления, попытка {attempt}/{attempts}: {exc}")
                if attempt < attempts:
                    self._set_update_status("Связь прервалась, пробую снова…")
                    time.sleep(2)
        return False

    def _set_update_status(self, text):
        """Показать текст в баннере обновления (из фонового потока, без падений)."""
        window = self._window()
        if not window:
            return
        try:
            window.evaluate_js(
                "(function(t){var e=document.getElementById('hubUpdateText');"
                "if(e){e.textContent=t;}})(" + json.dumps(str(text)) + ")"
            )
        except Exception:  # noqa: BLE001
            pass

    def _fallback_to_browser(self, url):
        """Автообновление не удалось — открыть страницу загрузки в браузере.

        Открываем именно страницу релизов (а не 70-МБ zip): там пользователь
        возьмёт лёгкий WexFlow-Setup.exe. Кнопку возвращаем в кликабельное
        состояние, чтобы можно было попробовать ещё раз.
        """
        target = url
        try:
            import version
            repo = (getattr(version, "GITHUB_REPO", "") or "").strip()
            if repo:
                target = f"https://github.com/{repo}/releases/latest"
        except Exception:  # noqa: BLE001
            pass
        self._set_update_status("Не вышло автоматически — открываю страницу загрузки")
        self.open_external(target)
        window = self._window()
        if window:
            try:
                window.evaluate_js(
                    "(function(){var b=document.getElementById('hubUpdate');"
                    "if(b){b.disabled=false;}"
                    "var c=document.querySelector('.hub-update-cta');"
                    "if(c){c.textContent='Скачать \\u2192';}})()"
                )
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        window = self._window()
        if window:
            window.destroy()
        return True


_native_frame_procs = {}


def _install_native_frame_hit_test(window=None):
    """Install a Win32 hit-test hook for the frameless pywebview window."""
    if os.name != "nt" or window is None:
        return
    native = getattr(window, "native", None)
    if native is None:
        return
    try:
        hwnd = int(native.Handle.ToInt64())
    except Exception:  # noqa: BLE001
        try:
            hwnd = int(native.Handle.ToInt32())
        except Exception:  # noqa: BLE001
            return
    if not hwnd or hwnd in _native_frame_procs:
        return

    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    SetWindowLongPtr = user32.SetWindowLongPtrW
    SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    SetWindowLongPtr.restype = ctypes.c_void_p
    CallWindowProc = user32.CallWindowProcW
    CallWindowProc.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    CallWindowProc.restype = LRESULT
    GetWindowRect = user32.GetWindowRect
    GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    GetWindowRect.restype = wintypes.BOOL

    GWLP_WNDPROC = -4
    WM_NCHITTEST = 0x0084
    HTCAPTION = 2
    HTLEFT = 10
    HTRIGHT = 11
    HTTOP = 12
    HTTOPLEFT = 13
    HTTOPRIGHT = 14
    HTBOTTOM = 15
    HTBOTTOMLEFT = 16
    HTBOTTOMRIGHT = 17

    def signed_word(value):
        value &= 0xFFFF
        return value - 0x10000 if value & 0x8000 else value

    def proc(h, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            rect = RECT()
            if GetWindowRect(h, ctypes.byref(rect)):
                x = signed_word(int(lparam))
                y = signed_word(int(lparam) >> 16)
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                cx = x - rect.left
                cy = y - rect.top
                scale = 1.0
                try:
                    scale = max(1.0, float(user32.GetDpiForWindow(h)) / 96.0)
                except Exception:  # noqa: BLE001
                    pass
                border = max(7, int(7 * scale))
                titlebar = max(34, int(34 * scale))
                traffic_width = max(96, int(96 * scale))

                left = cx <= border
                right = cx >= width - border
                top = cy <= border
                bottom = cy >= height - border

                if top and left:
                    return HTTOPLEFT
                if top and right:
                    return HTTOPRIGHT
                if bottom and left:
                    return HTBOTTOMLEFT
                if bottom and right:
                    return HTBOTTOMRIGHT
                if left:
                    return HTLEFT
                if right:
                    return HTRIGHT
                if top:
                    return HTTOP
                if bottom:
                    return HTBOTTOM
                if border < cy <= titlebar and cx > traffic_width:
                    return HTCAPTION

        old_proc = _native_frame_procs.get(hwnd, {}).get("old_proc")
        if old_proc:
            return CallWindowProc(old_proc, h, msg, wparam, lparam)
        return user32.DefWindowProcW(h, msg, wparam, lparam)

    callback = WNDPROC(proc)
    old_proc = SetWindowLongPtr(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
    if old_proc:
        _native_frame_procs[hwnd] = {"callback": callback, "old_proc": old_proc}


# ── проверка обновлений (фон) ──────────────────────────────────────────
def check_updates_async(controls: "WindowControls") -> None:
    def _run():
        try:
            import update_check
            controls.update_info = update_check.check()
        except Exception:  # noqa: BLE001
            controls.update_info = None
    threading.Thread(target=_run, daemon=True).start()


# ── запуск серверов ────────────────────────────────────────────────────
def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.4):
            return True
    except OSError:
        return False


def _spawn(args, cwd=None):
    return subprocess.Popen(
        [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure(port, args, cwd, name):
    if _port_up(port):
        return
    try:
        _started.append((name, _spawn(args, cwd)))
    except Exception as exc:  # noqa: BLE001
        print(f"[WexFlow] не удалось запустить {name}: {exc}")


def _free_our_ports():
    """Освободить порты 8000/8080/7111 от ЛЮБЫХ зависших процессов прошлого
    запуска (WexFlow.exe или python.exe-серверы), чтобы новая версия всегда
    поднимала свои свежие серверы. Иначе окно показывает старый интерфейс со
    старого сервера на том же порту — главная причина «обновление не применилось».
    """
    if not is_frozen():
        return
    import re
    self_pid = os.getpid()
    # снять старые окна/воркеры WexFlow прошлого запуска (они не на портах)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "WexFlow.exe", "/FI", f"PID ne {self_pid}"],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass
    our_ports = {SALLING_PORT, HUB_PORT, SEVEN_PORT}
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            creationflags=CREATE_NO_WINDOW, text=True, errors="ignore",
        )
    except Exception:  # noqa: BLE001
        return
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        if not any(f"127.0.0.1:{p} " in line or f":{p} " in line for p in our_ports):
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if m:
            pids.add(int(m.group(1)))
    killed = False
    for pid in pids:
        if pid and pid != self_pid:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed = True
    if killed:
        time.sleep(1.2)  # дать портам освободиться


def start_servers():
    if is_frozen():
        # каждый сервер — отдельный процесс самого exe (изоляция и без Python снаружи)
        _ensure(SEVEN_PORT, _self_cmd("--worker-7e-server", str(SEVEN_PORT)), None, "7-Eleven")
        _ensure(SALLING_PORT, _self_cmd("--worker-salling-server", str(SALLING_PORT)), None, "Salling")
        _ensure(HUB_PORT, _self_cmd("--worker-hub-server", str(HUB_PORT)), None, "Hub")
    else:
        # dev — как раньше: внешние интерпретаторы
        if SEVEN_DIR.exists():
            _ensure(SEVEN_PORT, [SEVEN_PY, "web_app.py"], SEVEN_DIR, "7-Eleven")
        _ensure(SALLING_PORT, [PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                               "--port", str(SALLING_PORT)], APP_ROOT, "Salling")
        _ensure(HUB_PORT, [PY, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
                           "--port", str(HUB_PORT)], APP_ROOT, "Hub")


def wait_for_hub(timeout=60) -> bool:
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


# ── окно приложения ────────────────────────────────────────────────────
def run_window():
    # Полностью выключаем HTTP-кэш WebView2 — иначе окно показывает страницу,
    # закэшированную от прошлой версии (старый интерфейс/баннер не исчезает).
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--disable-http-cache --disk-cache-size=1 --disable-application-cache"
    )
    set_playwright_env()
    _free_our_ports()
    start_servers()
    ensure_browser_async()
    ready = wait_for_hub()

    import webview

    controls = WindowControls()
    check_updates_async(controls)

    # уникальный URL на каждый запуск — чтобы WebView2 не показал страницу,
    # закэшированную от прошлой версии (иначе остаётся старый интерфейс/баннер).
    _next = urllib.request.quote(f"/hub?_cb={int(time.time())}")
    fresh_hub = f"http://127.0.0.1:{HUB_PORT}/__app/salling?next={_next}"

    start_url = fresh_hub if ready else "data:text/html;charset=utf-8," + urllib.request.quote(
        "<body style='font:16px system-ui;background:#101111;color:#e6e8e6;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;"
        "text-align:center'>"
        "<div><h2>WexFlow не смог запустить серверы</h2>"
        "<p>Закрой это окно и запусти заново.</p></div></body>"
    )

    native_window = webview.create_window(
        "WexFlow",
        start_url,
        js_api=controls,
        width=1280,
        height=860,
        min_size=(900, 600),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#101111",
    )
    # перетаскивание — через pywebview drag-region (класс .desktop-drag),
    # ресайз — через window_chrome.js + resize_window. Нативный WndProc-хук на
    # родительском окне для WebView2 не работает (хиты ловит дочернее окно),
    # поэтому его не вешаем.
    _ = native_window
    # Постоянное хранилище WebView2 (cookie/localStorage) в %AppData%\WexFlow —
    # иначе по умолчанию private_mode=True держит всё в памяти и стирает при
    # закрытии, и сохранённые фильтры/тема слетают после перезапуска.
    storage = appdata_root() / "webview"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    except Exception as exc:  # noqa: BLE001
        try:
            fallback_log = appdata_root() / "native_window_error.log"
            fallback_log.write_text(str(exc), encoding="utf-8", errors="replace")
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "WexFlow не смог открыть окно приложения на этом ПК.\n\n"
                "Запусти WexFlow Setup заново: он установит нужные компоненты Windows "
                "(.NET Framework 4.8 и WebView2 Runtime), после этого приложение должно открыться.\n\n"
                f"Технический лог: {fallback_log}",
                "WexFlow",
                0x10,
            )
        except Exception:
            raise
    finally:
        stop_started()


def _harden_stdio():
    """В windowed-сборке sys.stdout/stderr могут быть None, а на не-UTF-8 локали
    print датских/русских символов падает. Делаем потоки безопасными и UTF-8."""
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        try:
            if st is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            elif hasattr(st, "reconfigure"):
                st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main():
    _harden_stdio()
    args = sys.argv[1:]
    if args and args[0].startswith("--worker-"):
        run_worker(args[0], args[1:])
        return
    run_window()


if __name__ == "__main__":
    main()
