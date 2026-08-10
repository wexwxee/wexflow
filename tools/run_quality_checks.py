"""One command for the checks that must pass before a WexFlow build."""
from __future__ import annotations

import os
import py_compile
import sqlite3
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git", ".venv", "build", "dist", "build_installer", "dist_installer",
    "_backups", "_dbrescue", "backup", "browser_profile", "connector_browser",
    "uploads", "logs", "__pycache__",
}


def _source_files():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".bak_") for part in relative.parts):
            continue
        yield path


def check_python() -> int:
    files = list(_source_files())
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(f"OK   Python syntax: {len(files)} files")
    return len(files)


def check_templates() -> int:
    root = ROOT / "templates"
    environment = Environment(loader=FileSystemLoader(str(root)))
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".html", ".jinja", ".j2"}
    )
    for name in files:
        source, _filename, _uptodate = environment.loader.get_source(environment, name)
        environment.parse(source)
    print(f"OK   Jinja templates: {len(files)} files")
    return len(files)


def check_database() -> None:
    path = ROOT / "jobs.db"
    if not path.exists():
        print("SKIP SQLite quick_check: jobs.db is absent")
        return
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result}")
    print("OK   SQLite quick_check")


def run_tests() -> int:
    """Прогнать ВЕСЬ набор через pytest.

    Раньше каждый файл запускался как скрипт (``python tests/test_x.py``).
    Так проверялись только те файлы, у которых есть блок ``__main__`` — сейчас
    это 64 из 104. Остальные (всё, что написано на фикстурах pytest) молча
    импортировались и «проходили», ничего не проверив. Ворота перед сборкой,
    которые пропускают 40 файлов тестов, — это не ворота.
    """
    tests = sorted((ROOT / "tests").glob("test_*.py"))
    test_env = os.environ.copy()
    previous_path = test_env.get("PYTHONPATH", "")
    test_env["PYTHONPATH"] = str(ROOT) + (os.pathsep + previous_path if previous_path else "")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")],
        cwd=str(ROOT),
        env=test_env,
    )
    if result.returncode:
        raise RuntimeError(f"pytest завершился с кодом {result.returncode}")
    print(f"OK   Test files: {len(tests)}")
    return len(tests)


def main() -> int:
    os.chdir(ROOT)
    try:
        check_python()
        check_templates()
        check_database()
        run_tests()
    except Exception as exc:
        print(f"\nQUALITY CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nALL QUALITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
