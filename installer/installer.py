"""WexFlow web installer — красивое графическое окно установки (для друзей).

Качает последний релиз с GitHub и устанавливает надёжно:
- ставит в C:\\Users\\Public\\WexFlow (ASCII-путь, без админа, обходит баг
  pythonnet с кириллическими путями вроде «Новая папка»);
- снимает mark-of-the-web, чтобы .NET загрузил неподписанный Python.Runtime.dll;
- доустанавливает .NET Framework 4.8 + Edge WebView2 Runtime при необходимости;
- делает ярлык на рабочем столе и запускает приложение.

Установщик всегда ставит самую свежую версию, поэтому ОДИН и тот же файл
никогда не устаревает.

Окно установки — на tkinter (входит в стандартный Python, доп. зависимостей нет).
Если графика почему-то не запустится — есть консольный фолбэк.

Build:  PyInstaller --onefile --windowed --icon app.ico --name WexFlow-Setup
        installer/installer.py
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

REPO = "wexwxee/wexflow"
INSTALL_ROOT = r"C:\Users\Public"
APP_DIR = os.path.join(INSTALL_ROOT, "WexFlow")
EXE = os.path.join(APP_DIR, "WexFlow.exe")
PS_HIDE = 0x08000000  # CREATE_NO_WINDOW

# Фирменные цвета (как в самом приложении: тёмная тема + неон-зелёный).
C_BG = "#141515"
C_CARD = "#1c1d1d"
C_TRACK = "#282929"
C_ACCENT = "#1ed760"
C_ACCENT_DK = "#16a34a"
C_TXT = "#f6f7f6"
C_MUTED = "#9ba19d"
C_ERR = "#fca5a5"


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
    if os.path.exists(APP_DIR):
        shutil.rmtree(APP_DIR, ignore_errors=True)
    os.makedirs(INSTALL_ROOT, exist_ok=True)
    with zipfile.ZipFile(tmp_zip) as z:
        z.extractall(INSTALL_ROOT)  # архив содержит верхнюю папку WexFlow
    if not os.path.exists(EXE):
        raise RuntimeError("WexFlow.exe не найден после распаковки")

    report(0.90, "Завершаю установку…")
    ps(f"Get-ChildItem -LiteralPath '{APP_DIR}' -Recurse -File | "
       "Unblock-File -ErrorAction SilentlyContinue")
    ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
       "[Environment]::GetFolderPath('Desktop') + '\\WexFlow.lnk'); "
       f"$s.TargetPath='{EXE}'; $s.WorkingDirectory='{APP_DIR}'; "
       f"$s.IconLocation='{EXE}'; $s.Save()")

    report(1.0, "Готово!")
    return tag


# ---------------------------------------------------------------------------
# Графическое окно установки (tkinter).
# ---------------------------------------------------------------------------
def gui_main():
    import tkinter as tk
    from tkinter import font as tkfont

    msgs: "queue.Queue" = queue.Queue()

    root = tk.Tk()
    root.title("Установка WexFlow")
    root.overrideredirect(True)  # без стандартной рамки — чистое фирменное окно
    root.configure(bg=C_BG)
    W, H = 560, 380
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x, y = (sw - W) // 2, (sh - H) // 3
    root.geometry(f"{W}x{H}+{x}+{y}")
    try:
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    f_logo = tkfont.Font(family="Segoe UI Semibold", size=30)
    f_sub = tkfont.Font(family="Segoe UI", size=11)
    f_status = tkfont.Font(family="Segoe UI", size=10)
    f_pct = tkfont.Font(family="Segoe UI Semibold", size=10)
    f_btn = tkfont.Font(family="Segoe UI Semibold", size=11)
    f_close = tkfont.Font(family="Segoe UI", size=13)

    # тонкая зелёная полоска сверху
    tk.Frame(root, bg=C_ACCENT, height=3).pack(fill="x", side="top")

    # верхняя строка с кнопкой закрытия
    top = tk.Frame(root, bg=C_BG)
    top.pack(fill="x")
    close_btn = tk.Label(top, text="✕", bg=C_BG, fg=C_MUTED, font=f_close,
                         cursor="hand2", padx=14, pady=8)
    close_btn.pack(side="right")
    close_btn.bind("<Enter>", lambda e: close_btn.config(fg=C_TXT))
    close_btn.bind("<Leave>", lambda e: close_btn.config(fg=C_MUTED))
    close_btn.bind("<Button-1>", lambda e: root.destroy())

    # перетаскивание окна за верхнюю область
    drag = {"x": 0, "y": 0}
    def start_drag(e):
        drag["x"], drag["y"] = e.x, e.y
    def on_drag(e):
        root.geometry(f"+{root.winfo_x() + e.x - drag['x']}+{root.winfo_y() + e.y - drag['y']}")
    for w in (top,):
        w.bind("<Button-1>", start_drag)
        w.bind("<B1-Motion>", on_drag)

    body = tk.Frame(root, bg=C_BG)
    body.pack(fill="both", expand=True, padx=44)

    tk.Frame(body, bg=C_BG, height=10).pack()
    logo = tk.Label(body, text="WexFlow", bg=C_BG, fg=C_TXT, font=f_logo)
    logo.pack(anchor="w")
    # подчёркивание-акцент в слове
    sub = tk.Label(body, text="Автоматическая подача заявок", bg=C_BG, fg=C_MUTED, font=f_sub)
    sub.pack(anchor="w", pady=(2, 0))

    tk.Frame(body, bg=C_BG, height=34).pack()

    # статус-строка
    status_var = tk.StringVar(value="Готов к установке")
    status = tk.Label(body, textvariable=status_var, bg=C_BG, fg=C_TXT,
                      font=f_status, anchor="w", justify="left", wraplength=W - 88)
    status.pack(anchor="w", fill="x")

    tk.Frame(body, bg=C_BG, height=10).pack()

    # кастомный прогресс-бар на canvas (надёжно зелёный, в отличие от ttk)
    bar_w = W - 88
    canvas = tk.Canvas(body, width=bar_w, height=8, bg=C_BG, highlightthickness=0)
    canvas.pack(anchor="w")
    canvas.create_rectangle(0, 0, bar_w, 8, fill=C_TRACK, outline="")
    fill_id = canvas.create_rectangle(0, 0, 0, 8, fill=C_ACCENT, outline="")

    pct_var = tk.StringVar(value="")
    pct = tk.Label(body, textvariable=pct_var, bg=C_BG, fg=C_MUTED, font=f_pct)
    pct.pack(anchor="e", pady=(6, 0))

    # нижняя зона с кнопкой
    btn_holder = tk.Frame(root, bg=C_BG)
    btn_holder.pack(fill="x", side="bottom", pady=(0, 24))

    state = {"phase": "idle"}  # idle | running | done | error

    def set_progress(frac):
        frac = max(0.0, min(1.0, frac))
        canvas.coords(fill_id, 0, 0, int(bar_w * frac), 8)
        pct_var.set(f"{int(frac * 100)}%")

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
        btn.config(text="Устанавливаю…", state="disabled", bg=C_TRACK, fg=C_MUTED)
        threading.Thread(target=worker, daemon=True).start()

    def launch_and_close():
        try:
            os.startfile(EXE)
        except OSError:
            pass
        root.destroy()

    def make_button(text, command, enabled=True):
        for c in btn_holder.winfo_children():
            c.destroy()
        bg = C_ACCENT if enabled else C_TRACK
        fg = "#062e14" if enabled else C_MUTED
        b = tk.Label(btn_holder, text=text, bg=bg, fg=fg, font=f_btn,
                     padx=28, pady=11, cursor="hand2" if enabled else "arrow")
        b.pack()
        if enabled:
            b.bind("<Enter>", lambda e: b.config(bg=C_ACCENT_DK))
            b.bind("<Leave>", lambda e: b.config(bg=C_ACCENT))
            b.bind("<Button-1>", lambda e: command())
        return b

    btn = make_button("Установить WexFlow", start_install)

    def poll():
        try:
            while True:
                kind, *rest = msgs.get_nowait()
                if kind == "progress":
                    fr, txt = rest
                    set_progress(fr)
                    status_var.set(txt)
                elif kind == "done":
                    tag = rest[0]
                    state["phase"] = "done"
                    set_progress(1.0)
                    status_var.set(f"WexFlow {tag or ''} установлен. Ярлык на рабочем столе.")
                    sub.config(text="Установка завершена", fg=C_ACCENT)
                    make_button("Запустить WexFlow", launch_and_close)
                    # авто-запуск приложения и закрытие установщика через 1.5 сек
                    root.after(1500, lambda: launch_and_close()
                              if state["phase"] == "done" else None)
                elif kind == "error":
                    state["phase"] = "error"
                    status_var.set("Не удалось установить:\n" + rest[0])
                    status.config(fg=C_ERR)
                    sub.config(text="Ошибка установки", fg=C_ERR)
                    pct_var.set("")
                    make_button("Закрыть", root.destroy)
        except queue.Empty:
            pass
        root.after(80, poll)

    # Самопроверка интерфейса: построить окно и закрыть, без реальной установки.
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
