"""Лаунчер беты «WexFlow — подача» для запуска из .bat (открывает браузер).
Вся логика — в connectors/webapp.py (чтобы попадать в сборку)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from connectors.webapp import serve  # noqa: E402

if __name__ == "__main__":
    serve(open_browser=True)
