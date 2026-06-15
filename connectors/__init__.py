"""WexFlow — слой коннекторов к платформам найма (ATS).

Идея (см. design_drafts/ROADMAP_connectors.md): расширяться не «по компаниям»,
а через коннекторы к платформам. Один коннектор Teamtailor = сразу сотни фирм.

Этот пакет НАМЕРЕННО изолирован от рабочего кода Salling и 7-Eleven: он ничего
из них не импортирует и ничего в их базах не трогает. Salling/7-Eleven работают
как раньше; коннекторы — новый слой рядом, который позже аккуратно объединит всё
в общую ленту.

Контракт коннектора и общий формат вакансии — в base.py.
"""
from .base import Connector, JobItem, register, all_connectors, get  # noqa: F401

# Регистрируем доступные коннекторы (импорт = регистрация в реестре).
from . import teamtailor   # noqa: F401,E402
from . import greenhouse   # noqa: F401,E402
from . import ashby        # noqa: F401,E402
