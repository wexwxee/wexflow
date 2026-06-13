"""Упаковать собранную папку dist/WexFlow в архив с номером версии.

Запускается из СОБРАТЬ_ДИСТРИБУТИВ.bat после PyInstaller. Делает
dist/WexFlow-<версия>.zip — это и есть файл, который ты кидаешь другу.
"""
import shutil
import sys
from pathlib import Path

import version

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "dist" / "WexFlow"


def main() -> int:
    if not SRC.exists():
        print(f"Нет папки сборки: {SRC}. Сначала собери PyInstaller.")
        return 1
    out_base = ROOT / "dist" / f"WexFlow-{version.__version__}"
    zip_path = ROOT / "dist" / f"WexFlow-{version.__version__}.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"Упаковываю {SRC} -> {zip_path} …")
    shutil.make_archive(str(out_base), "zip", root_dir=str(SRC.parent), base_dir="WexFlow")
    size_mb = zip_path.stat().st_size / 1_048_576
    print(f"Готово: {zip_path}  ({size_mb:.0f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
