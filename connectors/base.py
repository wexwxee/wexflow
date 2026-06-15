"""Контракт коннектора и единый формат вакансии (Этап 0 плана).

Каждый коннектор — это один сайт/платформа с одинаковым набором «кнопок»:
- метаданные (key/name/icon/color);
- search() — найти вакансии и вернуть их в едином формате JobItem;
- (позже) apply() и login_state() — пока не нужны, Этап 1 только просмотр.

JobItem специально близок по полям к модели Salling (db.Job), чтобы потом
объединение в общую ленту было тривиальным, без переделок.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional


def catalog_path(filename: str) -> Path:
    """Путь к JSON-каталогу коннектора. В сборке (PyInstaller) файлы лежат в
    _MEIPASS/connectors/, в dev — рядом с модулем."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "connectors" / filename
    return Path(__file__).resolve().parent / filename


@dataclass
class JobItem:
    """Одна вакансия в едином формате (общий язык всех коннекторов)."""

    source: str                       # ключ коннектора, напр. "teamtailor"
    id: str                           # стабильный id в пределах источника
    title: str
    company: str = ""                 # работодатель (у Salling это brand)
    url: str = ""                     # ссылка на объявление / подачу
    city: Optional[str] = None
    street: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    published: Optional[str] = None   # ISO-дата публикации
    description: Optional[str] = None  # HTML

    def as_dict(self) -> dict:
        return asdict(self)


class Connector:
    """Базовый контракт. Конкретный коннектор наследуется и реализует search()."""

    key: str = ""        # уникальный идентификатор в реестре
    name: str = ""       # человекочитаемое имя
    icon: str = ""       # эмодзи/символ или имя SVG-иконки (для UI позже)
    color: str = ""      # фирменный цвет источника (для UI позже)

    def search(self) -> list[JobItem]:
        """Вернуть текущие вакансии источника. Не должен бросать наружу —
        отдельная упавшая компания/страница не валит весь список."""
        raise NotImplementedError

    # --- задел на будущее (Этап 2+), сейчас не используется ---
    def login_state(self) -> dict:
        return {"logged_in": None}


# --- Реестр коннекторов ---
_REGISTRY: dict[str, Connector] = {}


def register(conn: Connector) -> Connector:
    _REGISTRY[conn.key] = conn
    return conn


def all_connectors() -> list[Connector]:
    return list(_REGISTRY.values())


def get(key: str) -> Optional[Connector]:
    return _REGISTRY.get(key)


def search_companies(companies: list[dict], fetch_one: Callable[[dict], list],
                     workers: int = 12) -> list:
    """Опросить компании ПАРАЛЛЕЛЬНО (фирм бывают десятки → быстро). Пропускает
    enabled=false; упавшая компания не валит остальных. Используется коннекторами."""
    todo = [c for c in companies if c.get("enabled", True)]

    def _safe(c: dict) -> list:
        try:
            return fetch_one(c)
        except Exception as e:
            print(f"  пропуск {c.get('slug') or c.get('org') or c.get('token')}: {e}")
            return []

    out: list = []
    if not todo:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
        for items in ex.map(_safe, todo):
            out.extend(items)
    return out


# Города/страна для фильтра «вакансия в Дании» (площадки бывают международные).
_DK_WORDS = (
    "denmark", "danmark", "københavn", "kobenhavn", "copenhagen", "aarhus",
    "århus", "odense", "aalborg", "ålborg", "esbjerg", "randers", "kolding",
    "horsens", "vejle", "roskilde", "herning", "silkeborg", "taastrup",
    "hellerup", "glostrup", "ballerup", "lyngby", "frederiksberg",
)


def is_denmark(*texts: str) -> bool:
    """True, если в любом из переданных текстов есть датская страна/город.
    Используется коннекторами международных площадок, чтобы оставить только DK."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False
    if " dk" in f" {blob}" or blob.strip() == "dk":
        return True
    return any(w in blob for w in _DK_WORDS)
