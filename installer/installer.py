"""WexFlow web installer — красивое графическое окно установки (для друзей).

Качает последний релиз с GitHub и устанавливает надёжно:
- ставит в C:\\Users\\Public\\WexFlow (ASCII-путь, без админа, обходит баг
  pythonnet с кириллическими путями вроде «Новая папка»);
- ПЕРЕД установкой полностью удаляет старую папку приложения (версии не копятся);
- настройки/данные пользователя в %AppData%\\WexFlow НЕ трогает (адрес, фильтры,
  вход, уже скачанный браузер сохраняются);
- после установки чистит временный zip из %TEMP% (чтобы не забивать память);
- снимает mark-of-the-web, чтобы .NET загрузил неподписанный Python.Runtime.dll;
- доустанавливает .NET Framework 4.8 + Edge WebView2 Runtime при необходимости;
- делает ярлык на рабочем столе и запускает приложение.

Установщик всегда ставит самую свежую версию, поэтому ОДИН и тот же файл
никогда не устаревает.

Окно оформлено в стиле главного экрана WexFlow (тёмная карточка, логотип,
шрифт Space Grotesk, неон-зелёный акцент, фирменные кнопки-точки). tkinter
входит в стандартный Python; шрифт и логотип кладутся в exe через --add-data.
Если графика не запустится — есть консольный фолбэк.

Build:  PyInstaller --onefile --windowed --icon app.ico --name WexFlow-Setup
        --add-data "installer/assets;assets" installer/installer.py
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "wexwxee/wexflow"
INSTALL_ROOT = r"C:\Users\Public"
APP_DIR = os.path.join(INSTALL_ROOT, "WexFlow")
EXE = os.path.join(APP_DIR, "WexFlow.exe")
PS_HIDE = 0x08000000  # CREATE_NO_WINDOW

# Фирменные цвета (как в главном экране: тёмная тема + неон-зелёный).
C_WIN = "#0d0e0e"        # фон окна (низ градиента)
C_GRAD_TOP = "#121313"   # верх градиента
C_CARD = "#1c1d1d"       # карточка
C_CARD_LINE = "#2b2c2c"  # рамка карточки
C_TRACK = "#282929"      # дорожка прогресса
C_ACCENT = "#1ed760"     # неон-зелёный
C_ACCENT_DK = "#16a34a"
C_TXT = "#f6f7f6"
C_TXT2 = "#e6e8e6"
C_MUTED = "#9ba19d"
C_ERR = "#fca5a5"
# фирменные «светофорные» точки окна
C_DOT_MIN = "#f7c544"
C_DOT_MAX = "#2dd4a8"
C_DOT_CLOSE = "#ff6fae"


def asset(name: str) -> str:
    """Путь к ресурсу (assets/) и в dev, и в собранном exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


# ---------------------------------------------------------------------------
# Логика установки (без UI). Сообщает о прогрессе через callback report().
# report(fraction_0_to_1, status_text)
# ---------------------------------------------------------------------------
def ps(command: str, capture: bool = False):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=capture, text=True, creationflags=PS_HIDE,
    )


def ensure_dotnet48(report):
    r = ps(r"(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' "
           r"-ErrorAction SilentlyContinue).Release", capture=True)
    try:
        if int((r.stdout or "0").strip() or "0") >= 528040:
            return
    except ValueError:
        pass
    report(0.06, "Устанавливаю .NET Framework 4.8 (один раз, 1–2 мин)…")
    dst = os.path.join(tempfile.gettempdir(), "ndp48-web.exe")
    ps("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
       f"Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/?linkid=2088631' -OutFile '{dst}'")
    if os.path.exists(dst):
        subprocess.run([dst, "/q", "/norestart"], creationflags=PS_HIDE)
        _rm(dst)


def ensure_webview2(report):
    keys = (
        r"'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',"
        r"'HKLM:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',"
        r"'HKLM:\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'"
    )
    r = ps(f"$k=@({keys}); foreach($x in $k){{ if((Get-ItemProperty $x -ErrorAction "
           "SilentlyContinue).pv){ 'yes'; break } }", capture=True)
    if "yes" in (r.stdout or ""):
        return
    report(0.14, "Устанавливаю Edge WebView2 Runtime (один раз)…")
    dst = os.path.join(tempfile.gettempdir(), "wv2setup.exe")
    ps("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
       f"Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile '{dst}'")
    if os.path.exists(dst):
        subprocess.run([dst, "/silent", "/install"], creationflags=PS_HIDE)
        _rm(dst)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def safe_extract_zip(zip_path: str, dest: str) -> None:
    """Extract a release zip without allowing absolute or parent paths."""
    root = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = (info.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or "\x00" in name:
                raise RuntimeError(f"опасный путь в архиве: {info.filename!r}")
            if len(name) > 1 and name[1] == ":":
                raise RuntimeError(f"опасный путь в архиве: {info.filename!r}")
            target = (Path(dest) / name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"опасный путь в архиве: {info.filename!r}")
        z.extractall(dest)


def install_from_zip(tmp_zip: str) -> None:
    work = os.path.join(tempfile.gettempdir(), f"wexflow_install_{os.getpid()}")
    new_root = os.path.join(work, "new")
    backup = APP_DIR + ".old"
    if os.path.exists(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(new_root, exist_ok=True)
    try:
        safe_extract_zip(tmp_zip, new_root)
        new_app = os.path.join(new_root, "WexFlow")
        if not os.path.exists(os.path.join(new_app, "WexFlow.exe")):
            found = []
            for root, _dirs, files in os.walk(new_root):
                if "WexFlow.exe" in files:
                    found.append(root)
            if not found:
                raise RuntimeError("WexFlow.exe не найден после распаковки")
            new_app = found[0]

        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(APP_DIR):
            os.rename(APP_DIR, backup)
        try:
            shutil.move(new_app, APP_DIR)
        except Exception:
            if os.path.exists(backup) and not os.path.exists(APP_DIR):
                os.rename(backup, APP_DIR)
            raise
        if not os.path.exists(EXE):
            raise RuntimeError("WexFlow.exe не найден после установки")
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def latest_zip():
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"User-Agent": "WexFlow-Installer", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        if name.startswith("wexflow-") and name.endswith(".zip"):
            return a["browser_download_url"], a["name"], data.get("tag_name", "")
    raise RuntimeError("в последнем релизе на GitHub нет zip-файла WexFlow")


def do_install(report):
    """Полная установка. report(fraction, text). Возвращает тег версии."""
    report(0.02, "Проверяю компоненты Windows…")
    ensure_dotnet48(report)
    ensure_webview2(report)

    report(0.30, "Ищу последнюю версию WexFlow…")
    url, name, tag = latest_zip()
    tmp_zip = os.path.join(tempfile.gettempdir(), name)

    def hook(block, block_size, total):
        if total and total > 0:
            frac = min(block * block_size / total, 1.0)
            report(0.30 + frac * 0.45, f"Скачиваю {tag or 'WexFlow'}… {int(frac * 100)}%")
    report(0.32, f"Скачиваю {tag or 'WexFlow'}…")
    urllib.request.urlretrieve(url, tmp_zip, reporthook=hook)

    report(0.78, f"Устанавливаю {tag or ''}…")
    os.makedirs(INSTALL_ROOT, exist_ok=True)
    install_from_zip(tmp_zip)

    report(0.90, "Завершаю установку…")
    ps(f"Get-ChildItem -LiteralPath '{APP_DIR}' -Recurse -File | "
       "Unblock-File -ErrorAction SilentlyContinue")
    ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
       "[Environment]::GetFolderPath('Desktop') + '\\WexFlow.lnk'); "
       f"$s.TargetPath='{EXE}'; $s.WorkingDirectory='{APP_DIR}'; "
       f"$s.IconLocation='{EXE}'; $s.Save()")

    _rm(tmp_zip)  # подчищаем временный архив, чтобы не забивать память
    report(1.0, "Готово!")
    return tag


# ---------------------------------------------------------------------------
# Графическое окно установки (tkinter, стиль главного экрана WexFlow).
# ---------------------------------------------------------------------------
def _register_font():
    """Подключаем Space Grotesk из ресурсов. True, если получилось."""
    try:
        import ctypes
        path = asset("SpaceGrotesk-700.ttf")
        if os.path.exists(path):
            n = ctypes.windll.gdi32.AddFontResourceExW(ctypes.c_wchar_p(path), 0x10, 0)
            return bool(n)
    except Exception:  # noqa: BLE001
        pass
    return False


def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _round_window(root, w, h, r=22):
    """Скруглить углы самого окна (Win32), чтобы не было прямоугольной подложки."""
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            hwnd = root.winfo_id()
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:  # noqa: BLE001
        pass


def gui_main():
    import tkinter as tk
    from tkinter import font as tkfont

    msgs: "queue.Queue" = queue.Queue()
    W, H = 440, 384

    root = tk.Tk()
    root.title("Установка WexFlow")
    root.overrideredirect(True)
    root.configure(bg=C_CARD)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")
    try:
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    has_grotesk = _register_font()
    head_family = "Space Grotesk" if has_grotesk else "Segoe UI Semibold"
    f_logo = tkfont.Font(family=head_family, size=24, weight="bold")
    f_sub = tkfont.Font(family="Segoe UI", size=10)
    f_status = tkfont.Font(family="Segoe UI", size=10)
    f_pct = tkfont.Font(family="Segoe UI Semibold", size=9)
    f_btn = tkfont.Font(family="Segoe UI Semibold", size=11)

    cv = tk.Canvas(root, width=W, height=H, bg=C_CARD, highlightthickness=0, bd=0)
    cv.pack(fill="both", expand=True)

    cx = W // 2
    is_update = os.path.exists(EXE)  # приложение уже установлено?

    # фирменные кнопки-точки (минимизировать / на весь экран / закрыть)
    def dot(x, color, tag, r=6):
        cv.create_oval(x - r, 30 - r, x + r, 30 + r, fill=color, outline="", tags=tag)
    dot(W - 74, C_DOT_MIN, "min")
    dot(W - 52, C_DOT_MAX, "max")
    dot(W - 30, C_DOT_CLOSE, "close")
    cv.tag_bind("close", "<Button-1>", lambda e: root.destroy())
    cv.tag_bind("min", "<Button-1>", lambda e: _iconify(root))
    for d in ("close", "min", "max"):
        cv.tag_bind(d, "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind(d, "<Leave>", lambda e: cv.config(cursor=""))

    # логотип + заголовок
    images = []
    try:
        logo = tk.PhotoImage(file=asset("wexflow_mark_52.png"))
        images.append(logo)
        cv.create_image(cx, 100, image=logo)
    except tk.TclError:
        pass
    cv.create_text(cx, 152, text="WexFlow", font=f_logo, fill=C_TXT)
    sub_id = cv.create_text(cx, 178,
                            text="Обновление" if is_update else "Автоматическая подача заявок",
                            font=f_sub, fill=C_MUTED)

    # статус
    status_id = cv.create_text(
        cx, 224,
        text="Готов к обновлению — данные и заявки сохранятся"
             if is_update else "Готов к установке",
        font=f_status, fill=C_TXT2, width=W - 72, justify="center")

    # прогресс-бар
    bx1, bx2, by = 56, W - 56, 256
    _round_rect(cv, bx1, by, bx2, by + 8, 4, fill=C_TRACK, outline="")
    fill_state = {"id": None}
    pct_id = cv.create_text(cx, 280, text="", font=f_pct, fill=C_MUTED)

    def set_progress(frac):
        frac = max(0.0, min(1.0, frac))
        if fill_state["id"]:
            cv.delete(fill_state["id"])
            fill_state["id"] = None
        w = (bx2 - bx1) * frac
        if w > 0:
            w = max(w, 8)
            fill_state["id"] = _round_rect(cv, bx1, by, bx1 + w, by + 8, 4,
                                           fill=C_ACCENT, outline="")
        cv.itemconfig(pct_id, text=f"{int(frac * 100)}%" if frac > 0 else "")

    # кнопка (rounded pill на canvas)
    btn = {"rect": None, "text": None, "cmd": None, "enabled": True}
    bx_1, bx_2, bty1, bty2 = cx - 92, cx + 92, 308, 346

    def set_button(text, command, enabled=True):
        if btn["rect"]:
            cv.delete(btn["rect"])
            cv.delete(btn["text"])
        bg = C_ACCENT if enabled else C_TRACK
        fg = "#062e14" if enabled else C_MUTED
        btn["rect"] = _round_rect(cv, bx_1, bty1, bx_2, bty2, 10, fill=bg, outline="",
                                  tags="btn")
        btn["text"] = cv.create_text(cx, (bty1 + bty2) // 2, text=text, font=f_btn,
                                     fill=fg, tags="btn")
        btn["cmd"] = command
        btn["enabled"] = enabled

    def on_btn_enter(e):
        if btn["enabled"]:
            cv.itemconfig(btn["rect"], fill=C_ACCENT_DK)
            cv.config(cursor="hand2")

    def on_btn_leave(e):
        if btn["enabled"]:
            cv.itemconfig(btn["rect"], fill=C_ACCENT)
        cv.config(cursor="")

    def on_btn_click(e):
        if btn["enabled"] and btn["cmd"]:
            btn["cmd"]()

    cv.tag_bind("btn", "<Enter>", on_btn_enter)
    cv.tag_bind("btn", "<Leave>", on_btn_leave)
    cv.tag_bind("btn", "<Button-1>", on_btn_click)

    # перетаскивание окна за верхнюю полосу (где нет интерактивных элементов)
    drag = {"x": 0, "y": 0}
    def press(e):
        drag["x"], drag["y"] = e.x, e.y
    def move(e):
        if e.y < 120:  # тянем только за «шапку», чтобы не мешать кнопке
            root.geometry(f"+{root.winfo_x() + e.x - drag['x']}+{root.winfo_y() + e.y - drag['y']}")
    cv.bind("<Button-1>", press)
    cv.bind("<B1-Motion>", move)
    root.bind("<Escape>", lambda e: root.destroy())
    root.after(40, lambda: _round_window(root, W, H))

    state = {"phase": "idle"}

    def worker():
        try:
            tag = do_install(lambda fr, txt: msgs.put(("progress", fr, txt)))
            msgs.put(("done", tag))
        except Exception as exc:  # noqa: BLE001
            msgs.put(("error", str(exc)))

    def start_install():
        if state["phase"] == "running":
            return
        state["phase"] = "running"
        set_button("Устанавливаю…", None, enabled=False)
        threading.Thread(target=worker, daemon=True).start()

    def launch_and_close():
        try:
            os.startfile(EXE)
        except OSError:
            pass
        root.destroy()

    set_button("Обновить WexFlow" if is_update else "Установить WexFlow", start_install)

    def poll():
        try:
            while True:
                kind, *rest = msgs.get_nowait()
                if kind == "progress":
                    fr, txt = rest
                    set_progress(fr)
                    cv.itemconfig(status_id, text=txt)
                elif kind == "done":
                    tag = rest[0]
                    state["phase"] = "done"
                    set_progress(1.0)
                    cv.itemconfig(sub_id,
                                  text="Обновление завершено" if is_update else "Установка завершена",
                                  fill=C_ACCENT)
                    cv.itemconfig(
                        status_id,
                        text=(f"WexFlow {tag or ''} обновлён.\nВаши данные и заявки на месте."
                              if is_update else
                              f"WexFlow {tag or ''} установлен.\nЯрлык на рабочем столе."))
                    set_button("Запустить WexFlow", launch_and_close)
                    root.after(1600, lambda: launch_and_close()
                              if state["phase"] == "done" else None)
                elif kind == "error":
                    state["phase"] = "error"
                    cv.itemconfig(sub_id, text="Ошибка установки", fill=C_ERR)
                    cv.itemconfig(status_id, text=rest[0], fill=C_ERR)
                    set_progress(0)
                    set_button("Закрыть", root.destroy)
        except queue.Empty:
            pass
        root.after(80, poll)

    # Самопроверка: построить окно и закрыть, без реальной установки.
    _selftest = os.environ.get("WEXFLOW_SELFTEST")
    if _selftest:
        try:
            _ms = int(_selftest)
        except ValueError:
            _ms = 700
        root.after(_ms, root.destroy)

    root.after(80, poll)
    try:
        root.focus_force()
    except tk.TclError:
        pass
    root.mainloop()


def _iconify(root):
    try:
        root.overrideredirect(False)
        root.iconify()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Консольный фолбэк (если графика не запустится).
# ---------------------------------------------------------------------------
def console_main():
    print("==============================================")
    print("   Установка WexFlow")
    print("==============================================")

    def report(frac, text):
        print(f"[{int(frac * 100):3d}%] {text}", flush=True)

    try:
        tag = do_install(report)
        os.startfile(EXE)
        print(f"\nГотово! WexFlow {tag or ''} установлен и запущен.")
        print("Ярлык «WexFlow» теперь на рабочем столе.")
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        print("\n!!! Ошибка установки:", exc)
        input("Нажми Enter, чтобы закрыть…")
        sys.exit(1)


if __name__ == "__main__":
    try:
        gui_main()
    except Exception:  # noqa: BLE001 — графика не поднялась → консольный режим
        console_main()
