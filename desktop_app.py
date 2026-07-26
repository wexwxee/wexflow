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
import re
import socket
# encodings.idna нужен сокетам: socket.getaddrinfo кодирует имя хоста кодеком
# "idna" ДАЖЕ для числового IP (127.0.0.1). В собранном exe этот кодек не
# подхватывается автоматически — импортируем явно, иначе ЛЮБОЕ сетевое обращение
# падает с «LookupError: unknown encoding: idna» (крэш на старте серверов).
import encodings.idna  # noqa: F401
import subprocess
import pathlib
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
import zipfile
import hashlib

import candidate_profiles

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
BETA_PORT = 8078  # устаревший порт: сохраняем только для очистки старого процесса
HUB_URL = f"http://127.0.0.1:{HUB_PORT}/__app/salling?next=/hub"
CREATE_NO_WINDOW = 0x08000000  # фоновые серверы — без чёрных консолей

_started = []  # дочерние процессы, которые запустило именно это приложение
_started_lock = threading.Lock()
_stopping = False
_profile_restart_requested = False
_autopilot_win = None  # мини-окно автопилота (чтобы не открывать дубликаты)


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


def _norm_sha(value) -> str:
    """Привести контрольную сумму к нижнему регистру hex или вернуть '' (невалидна)."""
    s = str(value or "").strip().lower()
    return s if re.fullmatch(r"[0-9a-f]{64}", s) else ""


def _sha256_file(path: pathlib.Path) -> str:
    """SHA-256 файла (потоково, чтобы не держать 70 МБ в памяти). '' при ошибке."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _safe_extract_zip(zip_path: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a release zip without allowing absolute or parent paths."""
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = (info.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or "\x00" in name or re.match(r"^[a-zA-Z]:", name):
                raise ValueError(f"bad zip entry: {info.filename!r}")
            target = (dest / name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe zip entry: {info.filename!r}")
        z.extractall(dest)


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

    elif mode == "--worker-beta-server":
        # модуль подачи (БЕТА) — коннекторы ATS, отдельный сервер
        from connectors.webapp import serve as beta_serve
        beta_serve(int(rest[0]) if rest else BETA_PORT, open_browser=False)

    elif mode == "--worker-connector-apply":
        # ассистированная подача по ссылке (открывает видимый браузер, без отправки)
        from connectors.apply_dispatch import run as connector_apply
        connector_apply(rest[0], keep_open=True, job_id=rest[1] if len(rest) > 1 else "")

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

    elif mode == "--worker-selftest":
        # Release smoke test: import the frozen application and verify critical
        # bundled resources without starting schedulers, browsers or network IO.
        import app as salling_app  # noqa: F401
        import cloud_auth as cloud  # noqa: F401
        import version
        # These protocols are imported lazily only when Uvicorn starts serving.
        # Import them here so the release smoke test catches missing frozen
        # WebSocket/HTTP dependencies before an update is published.
        import uvicorn.protocols.http.auto  # noqa: F401
        import uvicorn.protocols.websockets.auto  # noqa: F401
        import websockets.legacy  # noqa: F401
        required = [
            BUNDLE_DIR / "templates" / "settings.html",
            BUNDLE_DIR / "static",
            SEVEN_DIR / "web_app.py",
            SEVEN_DIR / "web_static",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError("missing bundled resources: " + ", ".join(missing))
        if not version.__version__:
            raise RuntimeError("application version is empty")
        # Трей обязан работать в сборке: без pystray/Pillow закрытие окна
        # снова молча убивало бы фоновый поиск.
        import pystray  # noqa: F401
        from pystray import _win32  # noqa: F401
        from PIL import Image
        Image.open(BUNDLE_DIR / "app.ico").close()


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
            _remember_started("pwinstall", subprocess.Popen(
                _self_cmd("--worker-pwinstall"),
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"[WexFlow] не удалось запустить загрузку браузера: {exc}")

    threading.Thread(target=_run, daemon=True).start()


# ── управление окном (js_api) ──────────────────────────────────────────
class WindowControls:
    def __init__(self):
        self._maximized = False
        self._fullscreen = False
        self._window_transition_lock = threading.Lock()
        self._last_window_toggle = 0.0
        self.update_info = None  # заполняется фоновой проверкой обновлений

    def _window(self):
        import webview
        return webview.windows[0] if webview.windows else None

    def minimize(self):
        window = self._window()
        if window:
            window.minimize()
        return True

    def _restart_for_candidate(self):
        """Close cleanly; main() starts a fresh process after workers stop."""
        global _profile_restart_requested, _tray_quit
        _profile_restart_requested = True
        _tray_quit = True
        window = self._window()
        if window:
            threading.Timer(0.25, window.destroy).start()

    def switch_candidate_profile(self, profile_id):
        try:
            profile = candidate_profiles.set_active(str(profile_id or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self._restart_for_candidate()
        return {"ok": True, "profile": profile}

    def create_candidate_profile(self, name):
        try:
            profile = candidate_profiles.create_profile(str(name or ""), activate=True)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self._restart_for_candidate()
        return {"ok": True, "profile": profile}

    def _native_fullscreen_state(self, window) -> bool | None:
        """Read pywebview's real state when its WinForms window is available."""
        native = getattr(window, "native", None)
        state = getattr(native, "is_fullscreen", None)
        return None if state is None else bool(state)

    def window_state(self):
        window = self._window()
        native_state = self._native_fullscreen_state(window) if window else None
        if native_state is not None:
            self._fullscreen = native_state
            self._maximized = native_state
        return {"ok": True, "fullscreen": bool(self._fullscreen)}

    def toggle_maximize(self):
        # JS bridge methods run on worker threads. Two quick clicks used to enter
        # SetWindowPos concurrently and could leave WinForms/WebView2 in a broken
        # state. Only one UI transition may exist at a time.
        if not self._window_transition_lock.acquire(blocking=False):
            return {"ok": True, "fullscreen": bool(self._fullscreen), "busy": True}
        try:
            now = time.monotonic()
            if now - self._last_window_toggle < 0.35:
                return {"ok": True, "fullscreen": bool(self._fullscreen), "busy": True}

            window = self._window()
            if not window:
                return {"ok": False, "fullscreen": bool(self._fullscreen)}

            native_state = self._native_fullscreen_state(window)
            before = self._fullscreen if native_state is None else native_state
            try:
                # pywebview marshals this operation onto the WinForms UI thread,
                # fits the current monitor and disables DWM rounding/border.
                # The old direct SetWindowPos path caused the thin left line and
                # intermittent crashes during overlapping transitions.
                window.toggle_fullscreen()
                after = self._native_fullscreen_state(window)
                self._fullscreen = (not before) if after is None else after
                self._maximized = self._fullscreen
                self._last_window_toggle = time.monotonic()
                return {"ok": True, "fullscreen": bool(self._fullscreen)}
            except Exception:  # noqa: BLE001 — safe fallback on other platforms
                try:
                    if self._maximized:
                        window.restore()
                    else:
                        window.maximize()
                    self._maximized = not self._maximized
                    self._fullscreen = self._maximized
                    self._last_window_toggle = time.monotonic()
                    return {"ok": True, "fullscreen": bool(self._fullscreen)}
                except Exception:  # noqa: BLE001
                    return {"ok": False, "fullscreen": bool(self._fullscreen)}
        finally:
            self._window_transition_lock.release()

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
        if self._fullscreen or self._window_transition_lock.locked():
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

    def open_autopilot_monitor(self):
        """Открыть мини-окно автопилота (живой монитор) отдельным небольшим окном.

        Если оно уже открыто — ничего не делаем (не плодим копии). Окно обычное,
        с рамкой ОС: его легко двигать и закрывать, и не нужна своя «шапка».
        """
        global _autopilot_win
        try:
            import webview
            if _autopilot_win is not None and _autopilot_win in webview.windows:
                return True  # уже открыто
            url = f"http://127.0.0.1:{HUB_PORT}/autopilot/mini"
            _autopilot_win = webview.create_window(
                "Автопилот — WexFlow", url,
                width=470, height=660, min_size=(380, 480),
                resizable=True, background_color="#0e0f10",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[WexFlow] не удалось открыть окно автопилота: {exc}")
            return False

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

    def install_update(self, url, sha256=""):
        """Автообновление: скачать новую версию, закрыть приложение, подменить
        файлы и снова открыть. Работает только в собранном приложении.

        Только https и только с проверкой контрольной суммы (см. _run_update)."""
        if not is_frozen():
            return {"ok": False, "error": "Автообновление доступно только в собранном приложении."}
        if not (isinstance(url, str) and url.startswith("https://")):
            return {"ok": False, "error": "bad url"}
        threading.Thread(target=self._run_update, args=(url, sha256), daemon=True).start()
        return {"ok": True}

    def _expected_update_sha(self) -> str:
        """Ожидаемая контрольная сумма релиза из доверенного канала (GitHub API по https)."""
        try:
            import update_check
            return _norm_sha((update_check.check() or {}).get("sha256", ""))
        except Exception:  # noqa: BLE001 — нет сети/суммы — вернём пусто, установку не делаем
            return ""

    def _run_update(self, url, expected_sha=""):
        try:
            # Контрольная сумма из доверенного канала. Без совпадения НЕ ставим —
            # иначе подменённый архив мог бы установить чужой код (захват ПК).
            expected_sha = _norm_sha(expected_sha) or self._expected_update_sha()
            if not expected_sha:
                self._set_update_status(
                    "Не удалось проверить подлинность обновления — открываю страницу загрузки."
                )
                self._fallback_to_browser(url)
                return

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

            got_sha = _sha256_file(zpath)
            if got_sha != expected_sha:
                # архив повреждён или подменён — НЕ ставим, уводим в браузер
                print(f"[WexFlow] контрольная сумма не совпала: "
                      f"ждали {expected_sha}, получили {got_sha or '(ошибка чтения)'}")
                self._set_update_status(
                    "Проверка подлинности не пройдена — открываю страницу загрузки."
                )
                self._fallback_to_browser(url)
                return

            self._set_update_status("Распаковываю…")
            extracted = work / "new"
            _safe_extract_zip(zpath, extracted)

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


def _bind_free(port: int) -> bool:
    """True, если порт можно эксклюзивно занять нашим сервером.

    На Windows обычный bind иногда проходит рядом с wildcard/WSL-прокси, а
    последующий connect к только что освобождённому порту может ложно отвечать.
    SO_EXCLUSIVEADDRUSE проверяет именно то, что затем потребуется uvicorn.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _port_clean(port: int) -> bool:
    """Порт «чистый» для нашего сервера: можно занять (bind) И на нём НЕ отвечает
    чужой сервер (connect). Docker/WSL держат 8080 так, что bind на 127.0.0.1
    проходит, но connect отвечает им — на таком порту loopback-трафик может уйти
    не туда, поэтому берём порт, где никого нет вообще."""
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        return _bind_free(port)
    return _bind_free(port) and not _port_up(port)


def _pick_port(default: int, used: set) -> int:
    """Чистый порт: сам default, если чистый; иначе ближайший выше. Нужно, когда
    default занят ЧУЖИМ приложением (частый случай — Docker/WSL держат 8080, и
    Hub-сервер WexFlow не мог подняться → «серверы не запустились»)."""
    if default not in used and _port_clean(default):
        return default
    for p in range(default + 1, default + 60):
        if p not in used and _port_clean(p):
            return p
    return default  # не нашли — вернём дефолт, пусть падёт с понятной ошибкой


def _resolve_ports() -> None:
    """Выбрать реально свободные порты для трёх серверов и сообщить их Hub-у через
    окружение (дочерние воркеры наследуют env). Вызывать ОДИН раз перед первым
    start_servers(), после _free_our_ports() (тот уже снял наши зависшие процессы)."""
    global SALLING_PORT, HUB_PORT, SEVEN_PORT
    used: set = set()
    SALLING_PORT = _pick_port(8000, used); used.add(SALLING_PORT)
    SEVEN_PORT = _pick_port(7111, used); used.add(SEVEN_PORT)
    HUB_PORT = _pick_port(8080, used); used.add(HUB_PORT)
    # Hub проксирует на Salling/7-Eleven — он читает их порты из окружения.
    os.environ["WEXFLOW_SALLING_PORT"] = str(SALLING_PORT)
    os.environ["WEXFLOW_SEVEN_PORT"] = str(SEVEN_PORT)
    if (SALLING_PORT, SEVEN_PORT, HUB_PORT) != (8000, 7111, 8080):
        print(f"[WexFlow] порты заняты чужими — выбрал свободные: "
              f"salling={SALLING_PORT}, 7e={SEVEN_PORT}, hub={HUB_PORT}")


def _spawn(args, cwd=None):
    return subprocess.Popen(
        [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_stopped(proc):
    """Завершить один дочерний процесс и не оставить его висеть после окна."""
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _remember_started(name, proc):
    """Запомнить процесс или сразу погасить его, если окно уже закрывается."""
    with _started_lock:
        if not _stopping:
            _started.append((name, proc))
            return proc
    _wait_stopped(proc)
    return proc


def _ensure(port, args, cwd, name):
    # Решаем по BIND, а не по CONNECT. Чужой сервер (Docker/WSL на 8080) ОТВЕЧАЕТ
    # на connect — раньше из-за этого наш Hub считался «уже поднятым» и не
    # запускался («серверы не запустились»). Bind на 127.0.0.1:порт при этом
    # свободен, и наш сервер спокойно занимает loopback (в Windows более точный
    # 127.0.0.1-bind выигрывает у 0.0.0.0). Не свободен → порт держит НАШ сервер
    # (перезапуск) — второй не плодим.
    if not _port_clean(port):
        return
    try:
        _remember_started(name, _spawn(args, cwd))
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
    our_ports = {SALLING_PORT, HUB_PORT, SEVEN_PORT, BETA_PORT}
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
        if pid and pid != self_pid and _pid_looks_like_wexflow(pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed = True
    if killed:
        time.sleep(1.2)  # дать портам освободиться


def _pid_looks_like_wexflow(pid: int) -> bool:
    """Only kill stale WexFlow workers, never an unrelated dev server on the same port."""
    try:
        ps = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
            + str(int(pid))
            + "\"; if($p){$p.Name; $p.ExecutablePath; $p.CommandLine}"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=CREATE_NO_WINDOW,
            text=True,
            errors="ignore",
            timeout=3,
        ).lower()
    except Exception:  # noqa: BLE001
        return False
    return "wexflow.exe" in out


def start_servers():
    if is_frozen():
        # каждый сервер — отдельный процесс самого exe (изоляция и без Python снаружи)
        _ensure(SEVEN_PORT, _self_cmd("--worker-7e-server", str(SEVEN_PORT)), None, "7-Eleven")
        _ensure(SALLING_PORT, _self_cmd("--worker-salling-server", str(SALLING_PORT)), None, "Salling")
        _ensure(HUB_PORT, _self_cmd("--worker-hub-server", str(HUB_PORT)), None, "Hub")
    else:
        # dev — как раньше: внешние интерпретаторы
        if SEVEN_DIR.exists():
            _ensure(SEVEN_PORT, [SEVEN_PY, "web_app.py", str(SEVEN_PORT), "--no-browser"],
                    SEVEN_DIR, "7-Eleven")
        _ensure(SALLING_PORT, [PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                               "--port", str(SALLING_PORT)], APP_ROOT, "Salling")
        _ensure(HUB_PORT, [PY, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
                           "--port", str(HUB_PORT)], APP_ROOT, "Hub")

    # Отдельный beta-сервер на 8078 больше не запускается: подача по ссылке
    # встроена в основной интерфейс. BETA_PORT остаётся в очистке старых
    # процессов, чтобы после обновления закрыть воркер предыдущей версии.


def wait_for_hub(timeout=60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{HUB_PORT}/__health", timeout=1
            ) as response:
                hub = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{SALLING_PORT}/api/health", timeout=2
            ) as response:
                salling = json.loads(response.read().decode("utf-8"))
            if (hub.get("service") == "wexflow-hub"
                    and salling.get("service") == "wexflow-salling"):
                return True
            # Порт отвечает, но это ещё НЕ наш сервер (например, наш Hub только
            # поднимается, а на 8080 пока отвечает чужой Docker/WSL). Раньше здесь
            # был мгновенный выход — из-за него окно «серверы не запустились».
            # Теперь ждём: как только ответит наш сервис, вернём True.
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
            pass
        time.sleep(0.4)
    return False


def stop_started():
    global _stopping
    with _started_lock:
        _stopping = True
        processes = list(_started)
        _started.clear()
    # Сначала посылаем terminate всем, чтобы они завершались параллельно.
    for _name, proc in processes:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    for _name, proc in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


# ── трей: «закрыть окно = свернуть в фон» ──────────────────────────────
_tray_icon = None      # pystray.Icon, пока приложение живо
_tray_quit = False     # пользователь выбрал «Выйти» в меню трея
_tray_hint_shown = False  # уведомление «работаю в фоне» — только при первом сворачивании


def _start_tray(window) -> bool:
    """Иконка в области уведомлений: «Открыть» и «Выйти». False — библиотек
    нет (например, урезанная сборка) → закрытие окна работает по-старому."""
    global _tray_icon
    try:
        import pystray
        from PIL import Image
        icon_path = (BUNDLE_DIR if is_frozen() else APP_ROOT) / "app.ico"
        image = Image.open(icon_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[WexFlow] трей недоступен ({exc}) — закрытие окна завершает приложение")
        return False

    def _show(icon, item):
        try:
            window.show()
            window.restore()
        except Exception:  # noqa: BLE001
            pass

    def _quit(icon, item):
        global _tray_quit
        _tray_quit = True
        try:
            icon.visible = False
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.destroy()   # выход из webview.start → finally гасит серверы
        except Exception:  # noqa: BLE001
            pass

    _tray_icon = pystray.Icon(
        "WexFlow", image, "WexFlow — поиск вакансий работает в фоне",
        menu=pystray.Menu(
            pystray.MenuItem("Открыть WexFlow", _show, default=True),
            pystray.MenuItem("Выйти совсем", _quit),
        ),
    )
    threading.Thread(target=_tray_icon.run, daemon=True).start()
    return True


def _stop_tray():
    global _tray_icon
    icon, _tray_icon = _tray_icon, None
    if icon is not None:
        try:
            icon.visible = False
            icon.stop()
        except Exception:  # noqa: BLE001
            pass


def _on_window_closing(window):
    """Обработчик закрытия окна: True — закрыть по-настоящему, False — спрятать
    в трей (серверы и автопилот продолжают работать)."""
    global _tray_hint_shown
    if _tray_quit or _tray_icon is None:
        return True
    try:
        window.hide()
    except Exception:  # noqa: BLE001
        return True     # спрятать не вышло — честно закрываемся
    if not _tray_hint_shown:
        _tray_hint_shown = True
        try:
            _tray_icon.notify(
                "WexFlow продолжает искать вакансии в фоне. "
                "Открыть окно или выйти совсем — через иконку в трее.", "WexFlow")
        except Exception:  # noqa: BLE001
            pass
    return False


def _error_page(title: str, body_html: str, download_url: str) -> str:
    """HTML экрана ошибки с кнопкой «Скачать свежую версию» (открывает GitHub в
    браузере) и видимой ссылкой — на случай, если приложение сломалось и
    обновиться изнутри нельзя. Кнопки зовут js_api окна (open_external/close)."""
    safe_url = str(download_url or "").replace("'", "%27").replace('"', "%22")
    return (
        "<body style='font:16px system-ui;background:#101111;color:#e6e8e6;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;"
        "text-align:center'>"
        "<div style='max-width:480px;padding:24px'>"
        f"<h2 style='margin:0 0 12px'>{title}</h2>"
        f"<p style='color:#9aa0a6;line-height:1.6;text-align:left'>{body_html}</p>"
        "<div style='display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:18px'>"
        "<button onclick=\"try{window.pywebview.api.open_external('" + safe_url + "')}catch(e){}\" "
        "style='background:#1ed760;color:#0e1011;border:none;border-radius:10px;padding:12px 18px;"
        "font-weight:800;font-size:14px;cursor:pointer'>⬇ Скачать свежую версию</button>"
        "<button onclick=\"try{window.pywebview.api.close()}catch(e){}\" "
        "style='background:#2a2d2f;color:#fff;border:1px solid #3a3d3f;border-radius:10px;"
        "padding:12px 18px;font-weight:700;font-size:14px;cursor:pointer'>Закрыть</button>"
        "</div>"
        "<p style='color:#6b7075;font-size:12.5px;margin-top:16px;word-break:break-all'>"
        "Или открой вручную:<br>"
        f"<span style='color:#9aa0a6;user-select:all'>{download_url}</span></p>"
        "</div></body>"
    )


# ── окно приложения ────────────────────────────────────────────────────
def run_window(minimized: bool = False):
    # Полностью выключаем HTTP-кэш WebView2 — иначе окно показывает страницу,
    # закэшированную от прошлой версии (старый интерфейс/баннер не исчезает).
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--disable-http-cache --disk-cache-size=1 --disable-application-cache"
    )
    set_playwright_env()
    _free_our_ports()
    _resolve_ports()   # чужой софт на 8080 (Docker/WSL) и т.п. — берём свободные порты
    start_servers()
    ensure_browser_async()
    ready = wait_for_hub(90)
    if not ready:
        # Частый случай при первом запуске: антивирус сканирует свежие .exe-воркеры
        # (они стартуют дольше), либо остались процессы прошлой версии на портах.
        # Не сдаёмся сразу — чистим порты и поднимаем серверы ещё раз.
        print("[WexFlow] серверы не ответили за 90с — чищу порты и пробую снова")
        _free_our_ports()
        start_servers()
        ready = wait_for_hub(90)

    import webview

    controls = WindowControls()
    check_updates_async(controls)

    # уникальный URL на каждый запуск — чтобы WebView2 не показал страницу,
    # закэшированную от прошлой версии (иначе остаётся старый интерфейс/баннер).
    _next = urllib.request.quote(f"/hub?_cb={int(time.time())}")
    fresh_hub = f"http://127.0.0.1:{HUB_PORT}/__app/salling?next={_next}"

    win_kwargs = dict(
        js_api=controls,
        width=1280,
        height=860,
        min_size=(900, 600),
        resizable=True,
        frameless=True,
        easy_drag=False,
        # DWM's native shadow is drawn outside a frameless window. In real
        # fullscreen Windows leaves its 6–7 px offset visible at the left and
        # bottom edges even with the border colour disabled. Our UI already has
        # its own card shadows, so disabling the outer native shadow is cleaner.
        shadow=False,
        background_color="#101111",
    )

    if ready:
        native_window = webview.create_window("WexFlow", fresh_hub, **win_kwargs)
    else:
        # Серверы не поднялись. ВАЖНО: показываем HTML через html=, а НЕ через
        # data:-URL — иначе pywebview принимает его за локальный путь, поднимает
        # свой http-сервер и отдаёт «404 / File does not exist» (путанее некуда).
        try:
            import version as _v
            _repo = (getattr(_v, "GITHUB_REPO", "") or "").strip() or "wexwxee/wexflow"
        except Exception:  # noqa: BLE001
            _repo = "wexwxee/wexflow"
        _rel = f"https://github.com/{_repo}/releases/latest"
        native_window = webview.create_window(
            "WexFlow", html=_error_page(
                "WexFlow не смог запустить серверы",
                "Чаще всего это бывает при <b>первом запуске</b> (антивирус проверяет свежие файлы) "
                "или если порт занят другим приложением (например, Docker/WSL).<br><br>"
                "<b>Что сделать:</b><br>"
                "1. Закрой это окно полностью.<br>"
                "2. Подожди примерно минуту.<br>"
                "3. Запусти WexFlow снова — обычно со второго раза всё стартует.<br><br>"
                "Если повторяется — переустанови приложение свежей версией с GitHub "
                "(кнопка ниже) или добавь папку WexFlow в исключения антивируса.",
                _rel),
            **win_kwargs)
    # перетаскивание — через pywebview drag-region (класс .desktop-drag),
    # ресайз — через window_chrome.js + resize_window. Нативный WndProc-хук на
    # родительском окне для WebView2 не работает (хиты ловит дочернее окно),
    # поэтому его не вешаем.
    _ = native_window
    if minimized:
        # Автозапуск с Windows: окно сворачиваем сразу после показа — серверы и
        # автопилот работают, а окно ждёт в панели задач.
        def _minimize_on_show():
            try:
                native_window.minimize()
            except Exception:  # noqa: BLE001
                pass
        try:
            native_window.events.shown += _minimize_on_show
        except Exception:  # noqa: BLE001
            pass
    # Трей: закрытие окна прячет его, поиск продолжается. Если pystray
    # недоступен — обработчик не вешаем, закрытие работает как раньше.
    if _start_tray(native_window):
        try:
            native_window.events.closing += (lambda: _on_window_closing(native_window))
        except Exception:  # noqa: BLE001
            _stop_tray()
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
            try:
                import version as _v
                _repo = (getattr(_v, "GITHUB_REPO", "") or "").strip() or "wexwxee/wexflow"
            except Exception:  # noqa: BLE001
                _repo = "wexwxee/wexflow"
            _rel = f"https://github.com/{_repo}/releases/latest"
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "WexFlow не смог открыть окно приложения на этом ПК.\n\n"
                "Скачай свежую версию и запусти WexFlow-Setup.exe — он установит нужные "
                "компоненты Windows (.NET Framework 4.8 и WebView2 Runtime), после этого "
                "приложение должно открыться:\n\n"
                f"{_rel}\n\n"
                f"Технический лог: {fallback_log}",
                "WexFlow",
                0x10,
            )
        except Exception:
            raise
    finally:
        _stop_tray()
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
        try:
            run_worker(args[0], args[1:])
        except SystemExit:
            raise  # штатный выход (например, playwright CLI) — пропускаем
        except Exception as exc:  # noqa: BLE001
            # Воркеры запускаются скрыто, их вывод идёт в лог-файл и в живой
            # лог на странице. Пишем понятную ошибку туда, а не показываем
            # страшный системный диалог «Unhandled exception in script».
            import traceback
            try:
                (appdata_root() / "worker_error.log").write_text(
                    traceback.format_exc(), encoding="utf-8", errors="replace")
            except OSError:
                pass
            print(f"\n[WexFlow] Не получилось выполнить задачу: {exc}\n")
            traceback.print_exc()
            sys.stdout.flush()
            sys.exit(1)
        return
    run_window(minimized="--minimized" in args)
    if _profile_restart_requested:
        try:
            _spawn(_self_cmd(), APP_ROOT if not is_frozen() else None)
        except Exception as exc:  # noqa: BLE001
            print(f"[WexFlow] profile restart failed: {exc}")


if __name__ == "__main__":
    main()
