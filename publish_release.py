"""Публикация релиза WexFlow на GitHub (надёжно, без капризов .bat).

Берёт версию из version.py, ищет собранный dist/WexFlow-<версия>.zip,
спрашивает подтверждение и публикует релиз через GitHub CLI (gh).
Ничего не выкладывает без твоего ответа «y».
"""
import subprocess
import sys
from pathlib import Path

import version

ROOT = Path(__file__).resolve().parent


def main() -> int:
    ver = version.__version__
    repo = (getattr(version, "GITHUB_REPO", "") or "").strip()
    zip_path = ROOT / "dist" / f"WexFlow-{ver}.zip"
    setup_path = ROOT / "dist" / f"WexFlow-Setup-{ver}.exe"

    print(f"Версия:      {ver}")
    print(f"Репозиторий: {repo or '(не задан)'}")
    print(f"Архив:       {zip_path}")
    print(f"Setup:        {setup_path}")
    print()

    if not repo:
        print("В version.py не задан GITHUB_REPO. Впиши, например: wexwxee/wexflow")
        return 1
    if not zip_path.exists():
        print("Архив не найден. Сначала собери дистрибутив (СОБРАТЬ_ДИСТРИБУТИВ.bat).")
        return 1

    ans = input(f"Опубликовать релиз v{ver} на GitHub сейчас? (y/n): ").strip().lower()
    if not setup_path.exists():
        print("Setup exe not found. Build it with installer/build_setup.ps1 first.")
        return 1

    if ans != "y":
        print(f"Отменено. Архив лежит здесь: {zip_path}")
        return 0

    cmd = [
        "gh", "release", "create", f"v{ver}", str(setup_path), str(zip_path),
        "--repo", repo,
        "--title", f"WexFlow {ver}",
        "--notes", f"WexFlow {ver}\n\nDownload and run WexFlow-Setup-{ver}.exe. The zip is kept for in-app auto-update.",
    ]
    print("Публикую…")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print()
        print("Не получилось опубликовать. Частые причины:")
        print(f"  • Релиз v{ver} уже существует — подними номер версии в version.py.")
        print("  • Не выполнен вход: запусти 'gh auth login'.")
        return result.returncode

    print()
    print(f"Готово! Релиз v{ver} опубликован: https://github.com/{repo}/releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
