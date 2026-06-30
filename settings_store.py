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


# --- сохранённые пресеты фильтров ---
def get_presets() -> list:
    return load().get("presets", [])


def add_preset(name: str, query: str):
    name = (name or "").strip()
    if not name:
        return

    def _m(d):
        presets = [p for p in d.get("presets", []) if p.get("name") != name]
        presets.append({"name": name, "query": query})
        d["presets"] = presets[:20]

    mutate(_m)


def delete_preset(name: str):
    mutate(lambda d: d.__setitem__(
        "presets", [p for p in d.get("presets", []) if p.get("name") != name]))
