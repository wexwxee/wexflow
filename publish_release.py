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


def _run_text(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _release_target() -> str:
    """Коммит, на который должен указывать GitHub-тег релиза."""
    return _run_text(["git", "rev-parse", "HEAD"])


def _setup_sources() -> list[Path]:
    sources = [
        ROOT / "installer" / "installer.py",
        ROOT / "СОБРАТЬ_УСТАНОВЩИК.bat",
        ROOT / "app.ico",
    ]
    assets = ROOT / "installer" / "assets"
    if assets.exists():
        sources.extend(p for p in assets.rglob("*") if p.is_file())
    return [p for p in sources if p.exists()]


def _setup_is_fresh(setup_path: Path) -> tuple[bool, str]:
    if not setup_path.exists():
        return False, "Установщик не найден. Сначала собери его: СОБРАТЬ_УСТАНОВЩИК.bat"
    setup_mtime = setup_path.stat().st_mtime
    stale = [p for p in _setup_sources() if p.stat().st_mtime > setup_mtime]
    if stale:
        newest = max(stale, key=lambda p: p.stat().st_mtime)
        return (
            False,
            "Установщик старее исходников. Сначала пересобери его: "
            f"СОБРАТЬ_УСТАНОВЩИК.bat\nНовее установщика: {newest.relative_to(ROOT)}",
        )
    return True, ""


def main() -> int:
    ver = version.__version__
    repo = (getattr(version, "GITHUB_REPO", "") or "").strip()
    zip_path = ROOT / "dist" / f"WexFlow-{ver}.zip"
    # Веб-установщик один и тот же для всех версий (скачивает последний релиз),
    # поэтому он без номера версии. Прикладываем к релизу, если он собран.
    setup_path = ROOT / "dist" / "WexFlow-Setup.exe"

    print(f"Версия:      {ver}")
    print(f"Репозиторий: {repo or '(не задан)'}")
    print(f"Архив:       {zip_path}")
    print(f"Установщик:  {setup_path} {'(есть)' if setup_path.exists() else '(нет)'}")
    target = _release_target()
    print(f"Коммит:      {target or '(не найден)'}")
    print()

    if not repo:
        print("В version.py не задан GITHUB_REPO. Впиши, например: wexwxee/wexflow")
        return 1
    if not zip_path.exists():
        print("Архив не найден. Сначала собери дистрибутив (СОБРАТЬ_ДИСТРИБУТИВ.bat).")
        return 1
    if not target:
        print("Не удалось определить текущий git-коммит. Релиз не опубликован.")
        return 1
    ok, setup_problem = _setup_is_fresh(setup_path)
    if not ok:
        print(setup_problem)
        return 1

    ans = input(f"Опубликовать релиз v{ver} на GitHub сейчас? (y/n): ").strip().lower()
    if ans != "y":
        print(f"Отменено. Архив лежит здесь: {zip_path}")
        return 0

    assets = [str(zip_path), str(setup_path)]

    cmd = [
        "gh", "release", "create", f"v{ver}", *assets,
        "--repo", repo,
        "--target", target,
        "--title", f"WexFlow {ver}",
        "--notes", f"WexFlow {ver}\n\nFor a new PC: download and run WexFlow-Setup.exe (it installs everything). The zip is for in-app auto-update.",
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
