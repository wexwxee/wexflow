"""Фоновая установка офлайн-переводчика из веб-интерфейса."""
import json
import subprocess
import sys
import threading
from datetime import datetime

import config

STATUS_PATH = config.BASE_DIR / "translator_install_status.json"
_lock = threading.Lock()


def _write(status: str, message: str = ""):
    STATUS_PATH.write_text(
        json.dumps(
            {
                "status": status,
                "message": message,
                "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def status() -> dict:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"status": "idle", "message": "", "updated_at": ""}


def _run_install():
    try:
        _write("running", "Ставлю пакет argostranslate...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "argostranslate"],
            cwd=str(config.BASE_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
        _write("running", "Скачиваю и ставлю модель датский → русский...")
        subprocess.run(
            [sys.executable, str(config.BASE_DIR / "install_offline_translator.py")],
            cwd=str(config.BASE_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
        _write("done", "Готово. Обнови страницу вакансии и нажми «Перевести на русский».")
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        _write("error", output.strip()[-1200:] or str(exc))
    except Exception as exc:
        _write("error", str(exc))


def start_install() -> bool:
    """Запускает установку, если она ещё не идёт. True если стартовала."""
    with _lock:
        current = status()
        if current.get("status") == "running":
            return False
        _write("running", "Запускаю установку офлайн-переводчика...")
        thread = threading.Thread(target=_run_install, daemon=True)
        thread.start()
        return True
