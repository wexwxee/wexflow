"""Хранилище пользовательских настроек (домашний адрес, пресеты, правило автопилота).

Надёжность (F33):
- запись атомарная (tmp + os.replace) — читатель никогда не видит полу-записанный
  файл, поэтому settings.json не «бьётся» при обрыве/одновременной записи;
- чтение терпимо к битому JSON — не роняет приложение, а откладывает копию и
  стартует с пустыми настройками;
- общий замок + mutate() для чтения-изменения-записи — параллельные изменения из
  разных потоков (скан автопилота, Telegram-поллер, веб-запросы) не теряют друг
  друга. Файл пишет только этот процесс, поэтому достаточно потокового замка.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import config

PATH = config.DATA_DIR / "settings.json"

# Один замок на весь read-modify-write. RLock — потому что mutate() вызывает
# save(), который тоже берёт замок (повторный вход из того же потока — ок).
_LOCK = threading.RLock()


def _backup_corrupt(err) -> None:
    """Отложить повреждённый settings.json в копию, чтобы его можно было разобрать,
    а приложение продолжило работу с чистыми настройками."""
    try:
        if PATH.exists():
            bad = PATH.parent / f"{PATH.stem}.corrupt-{int(time.time())}.json"
            PATH.replace(bad)
            print(f"settings.json повреждён ({err}); отложил копию: {bad.name}")
    except OSError:
        pass


def load() -> dict:
    if not PATH.exists():
        return {}
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except ValueError as e:           # содержимое — не JSON: откладываем и стартуем чисто
        _backup_corrupt(e)
        return {}
    except OSError:                   # временная проблема чтения — не роняем, без перемещения
        return {}


def save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_name(PATH.name + ".tmp")
    with _LOCK:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, PATH)         # атомарная подмена


def mutate(mutator) -> dict:
    """Атомарное чтение-изменение-запись под общим замком. mutator(data) меняет
    словарь на месте. Возвращает итоговый словарь. Так параллельные правки
    (submitting_ids vs seen_ids из разных потоков) не теряются."""
    with _LOCK:
        data = load()
        mutator(data)
        save(data)
        return data


def set_home(address: str, lat: float, lon: float, lookup_address: str | None = None):
    mutate(lambda d: d.update({
        "home": {"address": address, "lookup_address": lookup_address or address,
                 "lat": lat, "lon": lon}
    }))


def get_home() -> dict | None:
    return load().get("home")


# --- сохранённые профили поиска вакансий ---
def _normalise_preset(preset: dict) -> dict | None:
    """Привести старый пресет к новому формату профиля поиска.

    До 1.3.15 пресеты состояли только из name/query. Детерминированный legacy-id
    позволяет показать и удалить их без отдельной миграции settings.json.
    """
    if not isinstance(preset, dict):
        return None
    name = str(preset.get("name") or "").strip()[:50]
    if not name:
        return None
    query = str(preset.get("query") or "")[:4000]
    preset_id = str(preset.get("id") or "").strip()
    if not preset_id:
        import hashlib
        preset_id = "legacy-" + hashlib.sha256(
            f"{name}\0{query}".encode("utf-8")
        ).hexdigest()[:12]
    return {
        "id": preset_id[:80],
        "name": name,
        "query": query,
        "updated_at": int(preset.get("updated_at") or 0),
    }


def get_presets() -> list:
    return [
        normalised
        for preset in load().get("presets", [])
        if (normalised := _normalise_preset(preset)) is not None
    ][:20]


def add_preset(name: str, query: str, preset_id: str = "") -> dict | None:
    """Создать профиль или обновить существующий и вернуть сохранённую запись."""
    name = (name or "").strip()[:50]
    if not name:
        return None
    query = str(query or "")[:4000]
    wanted_id = str(preset_id or "").strip()[:80]
    result: dict = {}

    def _m(d):
        presets = [
            normalised
            for preset in d.get("presets", [])
            if (normalised := _normalise_preset(preset)) is not None
        ]
        existing = next(
            (
                p for p in presets
                if (wanted_id and p["id"] == wanted_id)
                or (not wanted_id and p["name"].casefold() == name.casefold())
            ),
            None,
        )
        saved = {
            "id": (existing or {}).get("id") or uuid.uuid4().hex,
            "name": name,
            "query": query,
            "updated_at": int(time.time()),
        }
        result.update(saved)
        remaining = [p for p in presets if p["id"] != saved["id"]]
        # Последний созданный/обновлённый профиль показываем первым.
        d["presets"] = [saved, *remaining][:20]

    mutate(_m)
    return result or None


def delete_preset(name: str = "", preset_id: str = ""):
    name = str(name or "").strip()
    preset_id = str(preset_id or "").strip()

    def _m(data):
        kept = []
        for raw in data.get("presets", []):
            preset = _normalise_preset(raw)
            if preset is None:
                continue
            matches = (
                (preset_id and preset["id"] == preset_id)
                or (not preset_id and name and preset["name"] == name)
            )
            if not matches:
                kept.append(preset)
        data["presets"] = kept

    mutate(_m)


# --- БЕТА: ИИ-дозаполнение форм (по умолчанию ВЫКЛ) ---
def get_ai_fill() -> bool:
    """Включена ли бета ИИ-дозаполнения. По умолчанию False."""
    return bool(load().get("ai_fill"))


def set_ai_fill(enabled: bool) -> None:
    enabled = bool(enabled)

    def _m(data):
        data["ai_fill"] = enabled
        if not enabled:
            data["ai_fill_motivation"] = False

    mutate(_m)


def get_apply_mode() -> str:
    """Что делать с анкетой внешнего магазина: «auto» или «fill».

    auto — заполнить и отправить самому (смысл приложения: подача без человека).
    fill — заполнить и остановиться перед финальной кнопкой; отправляет человек.
    По умолчанию auto: отправка всё равно не произойдёт, пока есть хоть один
    неотвеченный вопрос анкеты — за это отвечает проверка в коннекторе.
    """
    value = str(load().get("apply_mode") or "").strip().lower()
    return value if value in {"auto", "fill"} else "auto"


def set_apply_mode(mode: str) -> str:
    mode = str(mode or "").strip().lower()
    mode = mode if mode in {"auto", "fill"} else "auto"
    mutate(lambda d: d.__setitem__("apply_mode", mode))
    return mode


def get_ai_fill_motivation() -> bool:
    """Разрешён ли ИИ-черновик мотивации (свободные вопросы «почему к нам»).
    По умолчанию False — единственное место, где ИИ сочиняет текст."""
    return bool(load().get("ai_fill_motivation"))


def set_ai_fill_motivation(enabled: bool) -> None:
    mutate(lambda d: d.__setitem__("ai_fill_motivation", bool(enabled)))
