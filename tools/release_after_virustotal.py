"""Последний шаг выпуска: ссылка VirusTotal от человека → публикация релиза.

Скрипт ничего не ослабляет. Он только помогает Ивану быстро сделать шаг,
который может сделать только человек: загрузить установщик на VirusTotal и
принести ссылку. Проверку «ссылка относится именно к этому файлу» выполняет
publish_release.py — здесь она повторяется заранее, чтобы ошибку было видно
сразу, а не после долгого подсчёта хэшей.

Файл никуда не отправляется автоматически: открывается только страница
загрузки VirusTotal и папка dist, дальше человек перетаскивает файл сам.
"""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import publish_release  # noqa: E402
import version  # noqa: E402

UPLOAD_PAGE = "https://www.virustotal.com/gui/home/upload"


def _ask(prompt: str) -> str:
    """Спросить человека; при запуске без консоли вернуть пустой ответ."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def main() -> int:
    ver = version.__version__
    zip_path = ROOT / "dist" / f"WexFlow-{ver}.zip"
    setup_path = ROOT / "dist" / "WexFlow-Setup.exe"
    marker = setup_path.with_name(setup_path.name + ".virustotal.txt")

    print("=" * 62)
    print(f"  Выпуск WexFlow {ver} — остался один твой шаг.")
    print("=" * 62)
    print()
    if not zip_path.exists():
        print(f"Архив не найден: {zip_path}")
        print("Значит, сборки нет. Собери дистрибутив: СОБРАТЬ_ДИСТРИБУТИВ.bat")
        return 1
    if not setup_path.exists():
        print(f"Установщик не найден: {setup_path}")
        print("Собери его: СОБРАТЬ_УСТАНОВЩИК.bat")
        return 1

    setup_hash = publish_release._sha256_of(setup_path)
    print("Всё уже собрано и проверено, пересобирать ничего не нужно:")
    print(f"  {zip_path.name}")
    print(f"  {setup_path.name}")
    print()
    print("Осталось проверить установщик на VirusTotal и вставить сюда ссылку")
    print("на отчёт. Без неё релиз не публикуется — это защита, чтобы в")
    print("заметках к релизу не было пустых обещаний.")
    print()
    print("Сейчас откроются страница загрузки VirusTotal и папка dist.")
    print("Перетащи на сайт файл WexFlow-Setup.exe, дождись отчёта и скопируй")
    print("адрес страницы из браузера.")
    _ask("Нажми Enter, когда будешь готов… ")

    try:
        webbrowser.open(UPLOAD_PAGE)
        os.startfile(str(setup_path.parent))  # noqa: S606 — открыть папку в Проводнике
    except Exception as exc:  # noqa: BLE001 — не смогли открыть, не беда
        print(f"(не удалось открыть автоматически: {exc})")
        print(f"Открой вручную: {UPLOAD_PAGE}")
        print(f"Файл лежит здесь: {setup_path}")

    print()
    link = _ask("Вставь ссылку на отчёт VirusTotal и нажми Enter: ").strip()
    if not link:
        print("Ссылка пустая. Ничего не менял, релиз не опубликован.")
        return 1

    previous = marker.read_text(encoding="utf-8") if marker.exists() else None
    marker.write_text(link + "\n", encoding="utf-8")
    if not publish_release._virustotal_url(setup_path, setup_hash):
        # Не оставляем чужую ссылку в маркере: иначе следующий запуск решит,
        # что проверка от другого файла — это «испорченный маркер».
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(previous, encoding="utf-8")
        print()
        print("Эта ссылка не подходит. Ожидается отчёт именно про этот файл:")
        print(f"  https://www.virustotal.com/gui/file/{setup_hash}")
        print("Проверь, что загрузил WexFlow-Setup.exe из папки dist, и запусти")
        print("кнопку ещё раз. Релиз не опубликован.")
        return 1

    print()
    print("Ссылка подходит: отчёт про этот самый установщик. Публикую…")
    print()
    return subprocess.call(
        [sys.executable, str(ROOT / "publish_release.py")], cwd=str(ROOT)
    )


if __name__ == "__main__":
    sys.exit(main())
