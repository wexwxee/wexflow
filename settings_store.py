"""Простое хранилище пользовательских настроек (домашний адрес и т.п.)."""
import json
from pathlib import Path

import config

PATH = config.DATA_DIR / "settings.json"


def load() -> dict:
    if PATH.exists():
        return json.loads(PATH.read_text(encoding="utf-8"))
    return {}


def save(data: dict):
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_home(address: str, lat: float, lon: float, lookup_address: str | None = None):
    d = load()
    d["home"] = {"address": address, "lookup_address": lookup_address or address, "lat": lat, "lon": lon}
    save(d)


def get_home() -> dict | None:
    return load().get("home")


# --- сохранённые пресеты фильтров ---
def get_presets() -> list:
    return load().get("presets", [])


def add_preset(name: str, query: str):
    name = (name or "").strip()
    if not name:
        return
    d = load()
    presets = [p for p in d.get("presets", []) if p.get("name") != name]
    presets.append({"name": name, "query": query})
    d["presets"] = presets[:20]
    save(d)


def delete_preset(name: str):
    d = load()
    d["presets"] = [p for p in d.get("presets", []) if p.get("name") != name]
    save(d)
