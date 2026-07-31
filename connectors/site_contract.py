"""Защита от изменений на стороне работодателя («контракт формы»).

Зачем. Заполнитель ищет поля по подписям и селекторам конкретного сайта. Если
Lidl или Salling Group переделают анкету, старый код в лучшем случае заполнит
половину, а в худшем — нажмёт не то. Слепая подача хуже неподачи: у человека
одна попытка на вакансию.

Как. Перед заполнением проверяем, на месте ли ОПОРЫ формы — те элементы, без
которых подача заведомо неправильная (поля имени, загрузка CV, финальная
кнопка). Не нашли — не заполняем и не жмём ничего, а честно говорим человеку:
сайт изменился, нужно обновление WexFlow, следи за новостями.

Проверка только читает страницу: никаких кликов и ввода.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import support


@dataclass(frozen=True)
class Anchor:
    """Одна опора формы.

    key    — короткое имя для лога и отчёта об изменении;
    human  — как объяснить человеку, чего не хватает;
    kind   — 'label' (подпись поля), 'selector' (CSS), 'button' (кнопка по тексту),
             'caption' (голая подпись sap.m.Label без for=);
    query  — регэксп для label/button/caption либо CSS-селектор;
    vital  — без неё подача невозможна (иначе — предупреждение, не блокировка).
    """

    key: str
    human: str
    kind: str
    query: str
    vital: bool = True


@dataclass(frozen=True)
class Contract:
    platform: str
    anchors: tuple[Anchor, ...] = field(default_factory=tuple)


LIDL_EASY_APPLY = Contract(
    platform="Lidl EasyApply",
    anchors=(
        Anchor("first_name", "поле имени (Fornavn)", "label", r"Fornavn"),
        Anchor("last_name", "поле фамилии (Efternavn)", "label", r"Efternavn"),
        Anchor("email", "поле почты (E-mail-adresse)", "label", r"E-mail-adresse"),
        Anchor("phone", "поле телефона (Mobilnummer)", "label", r"Mobilnummer"),
        Anchor("street", "адрес: улица (Gade)", "caption", r"Gade", vital=False),
        Anchor("zip", "адрес: индекс (Postnummer)", "caption", r"Postnummer", vital=False),
        Anchor("cv_upload", "загрузка CV", "selector", 'input[type="file"][name="EACVUploader"]'),
        Anchor("cover_upload", "загрузка мотивационного письма", "selector",
               'input[type="file"][name="EACoverLetterUploader"]', vital=False),
        Anchor("submit", "финальная кнопка (Ansøg)", "button", r"^\s*(Ansøg|Send ansøgning)\s*$"),
    ),
)

SALLING = Contract(
    platform="Salling Group",
    anchors=(
        Anchor("file_upload", "загрузка документов", "selector", 'input[type="file"]'),
        Anchor("submit", "кнопка отправки (Send/Submit/Ansøg)", "button",
               r"^\s*(send|send ansøgning|submit|ansøg|apply)\s*$"),
    ),
)

CONTRACTS: dict[str, Contract] = {
    "lidl_easy_apply": LIDL_EASY_APPLY,
    "salling": SALLING,
}


class SiteChanged(RuntimeError):
    """Форма работодателя больше не совпадает с контрактом."""

    def __init__(self, report: dict):
        super().__init__(report.get("short") or "форма изменилась")
        self.report = report


def _has_label(page, pattern: str) -> bool:
    try:
        return page.get_by_label(re.compile(pattern, re.I)).count() > 0
    except Exception:  # noqa: BLE001 — страница может быть чем угодно
        return False


def _frames(page):
    """Страница и все её фреймы: у Salling форма живёт внутри iframe."""
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001
        frames = []
    return [page] + [f for f in frames if f is not getattr(page, "main_frame", None)]


def _has_selector(page, selector: str) -> bool:
    for scope in _frames(page):
        try:
            if scope.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _has_button(page, pattern: str) -> bool:
    rx = re.compile(pattern, re.I)
    for scope in _frames(page):
        try:
            if scope.get_by_role("button", name=rx).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _has_caption(page, pattern: str) -> bool:
    """Подпись без for= — ищем текстом среди меток формы."""
    try:
        return page.evaluate(
            """(pattern) => {
                const rx = new RegExp(pattern, 'i');
                return [...document.querySelectorAll('label, .sapMLabel, span')]
                    .some(el => rx.test((el.innerText || '').trim()));
            }""",
            pattern,
        )
    except Exception:  # noqa: BLE001
        return False


_CHECKS = {
    "label": _has_label,
    "selector": _has_selector,
    "button": _has_button,
    "caption": _has_caption,
}


def check(page, key: str) -> dict:
    """Сверить открытую страницу с контрактом платформы.

    Неизвестная платформа — не ошибка: проверять нечего, работаем как раньше.
    """
    contract = CONTRACTS.get(str(key or ""))
    if contract is None:
        return {"ok": True, "platform": "", "known": False,
                "missing": [], "warnings": [], "checked": 0}

    missing: list[str] = []
    warnings: list[str] = []
    for anchor in contract.anchors:
        probe = _CHECKS.get(anchor.kind)
        if probe is None or probe(page, anchor.query):
            continue
        (missing if anchor.vital else warnings).append(anchor.human)

    ok = not missing
    short = ("" if ok else
             f"{contract.platform}: не найдено — " + ", ".join(missing))
    return {
        "ok": ok,
        "platform": contract.platform,
        "known": True,
        "missing": missing,
        "warnings": warnings,
        "checked": len(contract.anchors),
        "short": short,
    }


def human_message(report: dict) -> str:
    """Текст для человека: что случилось, что делать, куда смотреть.

    Без обвинений и без «попробуйте ещё раз» — повтор здесь не поможет.
    """
    platform = str(report.get("platform") or "сайт работодателя")
    missing = list(report.get("missing") or [])
    lines = [
        f"{platform} изменил анкету — WexFlow не станет подавать вслепую.",
        "Что именно пропало: " + (", ".join(missing) if missing else "опорные поля формы") + ".",
        "Заявка НЕ отправлена и ничего не испорчено. Подать можно вручную по ссылке —"
        " или дождаться обновления WexFlow: как только форму разберут заново,"
        " подача снова заработает сама.",
    ]
    contacts = support.contact_line()
    if contacts:
        lines.append("Следить за обновлением: " + contacts + ".")
    return "\n".join(lines)


def short_message(report: dict) -> str:
    """Одна строка — для статуса подачи и уведомления на телефон."""
    platform = str(report.get("platform") or "Сайт")
    return (f"{platform} изменил анкету — подача остановлена до обновления WexFlow. "
            "Заявка не отправлена.")
