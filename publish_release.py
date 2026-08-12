"""Публикация релиза WexFlow на GitHub (надёжно, без капризов .bat).

Берёт версию из version.py, ищет собранный dist/WexFlow-<версия>.zip,
спрашивает подтверждение и публикует релиз через GitHub CLI (gh).
Ничего не выкладывает без твоего ответа «y».
"""
import hashlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import changelog
import version

ROOT = Path(__file__).resolve().parent


def _sha256_of(path: Path) -> str:
    """SHA-256 файла (потоково). Прикладываем к релизу для проверки авто-обновления."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_sha256(path: Path) -> tuple[str, Path]:
    checksum = _sha256_of(path)
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text(f"{checksum}  {path.name}\n", encoding="ascii")
    return checksum, sidecar


def _files_match_hashes(expected: dict[Path, str]) -> bool:
    """Защититься от замены ассетов между расчётом хэша и ответом ``y``."""
    try:
        return all(_sha256_of(path) == digest for path, digest in expected.items())
    except OSError:
        return False


def _run_text(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _release_target() -> str:
    """Коммит, на который должен указывать GitHub-тег релиза."""
    return _run_text(["git", "rev-parse", "HEAD"])


def _tracked_worktree_clean() -> bool:
    """Релизный тег обязан указывать на тот же код, из которого собран ZIP."""
    unstaged = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=str(ROOT),
    )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "HEAD", "--"],
        cwd=str(ROOT),
    )
    return unstaged.returncode == 0 and staged.returncode == 0


def _release_notes(
    ver: str,
    hashes: dict[str, str] | None = None,
    virustotal_url: str = "",
) -> str:
    entry = next(
        (item for item in changelog.ENTRIES if str(item.get("version")) == ver),
        None,
    )
    lines = [f"WexFlow {ver}", ""]
    if entry:
        if entry.get("date"):
            lines.extend([str(entry["date"]), ""])
        lines.extend(f"- {text}" for text in entry.get("items", []))
        lines.append("")
    lines.extend([
        "Для нового компьютера скачай и запусти WexFlow-Setup.exe — он установит свежую версию целиком.",
        "ZIP предназначен для встроенного автообновления.",
    ])
    if hashes:
        lines.extend(["", "SHA-256:"])
        lines.extend(f"- `{name}`: `{digest}`" for name, digest in hashes.items())
    if virustotal_url:
        lines.extend(["", f"VirusTotal (точный WexFlow-Setup.exe): {virustotal_url}"])
    return "\n".join(lines)


def _virustotal_url(setup_path: Path, expected_sha256: str = "") -> str:
    """Вернуть только отчёт VirusTotal, привязанный к точному установщику.

    Маркер остаётся в ``dist`` между пересборками. Поэтому одной проверки домена
    недостаточно: ссылка от предыдущего EXE дала бы релизным заметкам ложное
    утверждение, что проверен текущий файл.
    """
    marker = setup_path.with_name(setup_path.name + ".virustotal.txt")
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not value or len(value.splitlines()) != 1:
        return ""

    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        try:
            expected = _sha256_of(setup_path)
        except OSError:
            return ""

    parsed = urlparse(value)
    if parsed.scheme.lower() != "https":
        return ""
    if parsed.netloc.lower() not in {"virustotal.com", "www.virustotal.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[:2] != ["gui", "file"]:
        return ""
    scanned_sha256 = parts[2].lower()
    if scanned_sha256 != expected:
        return ""
    return value


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
    if not _tracked_worktree_clean():
        print(
            "Есть незакоммиченные изменения в отслеживаемых файлах. "
            "Сначала зафиксируй код, заново собери ZIP и только потом публикуй релиз."
        )
        return 1
    ok, setup_problem = _setup_is_fresh(setup_path)
    if not ok:
        print(setup_problem)
        return 1

    zip_hash, zip_sha_path = _write_sha256(zip_path)
    setup_hash, setup_sha_path = _write_sha256(setup_path)
    hashes = {
        zip_path.name: zip_hash,
        setup_path.name: setup_hash,
    }
    print(f"SHA-256 ZIP:       {zip_hash}")
    print(f"SHA-256 installer: {setup_hash}")

    vt_url = _virustotal_url(setup_path, setup_hash)
    if not vt_url:
        marker = setup_path.with_name(setup_path.name + ".virustotal.txt")
        print()
        print(
            "Перед публикацией проверь ТОЧНЫЙ WexFlow-Setup.exe на VirusTotal "
            "и сохрани ссылку одной строкой в:"
        )
        print(f"  {marker}")
        print("Файл автоматически никуда не отправлялся. Релиз не опубликован.")
        return 1

    ans = input(f"Опубликовать релиз v{ver} на GitHub сейчас? (y/n): ").strip().lower()
    if ans != "y":
        print(f"Отменено. Архив лежит здесь: {zip_path}")
        return 0

    if not _files_match_hashes({zip_path: zip_hash, setup_path: setup_hash}):
        print("Ассеты изменились после расчёта SHA-256. Релиз не опубликован; пересобери и проверь их заново.")
        return 1

    assets = [
        str(zip_path),
        str(zip_sha_path),
        str(setup_path),
        str(setup_sha_path),
    ]

    cmd = [
        "gh", "release", "create", f"v{ver}", *assets,
        "--repo", repo,
        "--target", target,
        "--title", f"WexFlow {ver}",
        "--notes", _release_notes(ver, hashes=hashes, virustotal_url=vt_url),
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
