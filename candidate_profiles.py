"""Local candidate profiles for using WexFlow for more than one person."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import json_store


APP_NAME = "WexFlow"
PRIMARY_ID = "primary"
REGISTRY_FILENAME = "candidate_profiles.json"
REMOTE_SWITCH_FILENAME = "candidate_profile_switch.json"
_LOCK = threading.RLock()


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def storage_root() -> Path:
    if _is_frozen():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    return Path(__file__).resolve().parent


def registry_path(root: Path | None = None) -> Path:
    return Path(root or storage_root()) / REGISTRY_FILENAME


def remote_switch_path(root: Path | None = None) -> Path:
    return Path(root or storage_root()) / REMOTE_SWITCH_FILENAME


def data_dir(profile_id: str, root: Path | None = None) -> Path:
    """Return one candidate's data directory, preserving the legacy primary path."""
    base = Path(root or storage_root())
    if profile_id == PRIMARY_ID:
        return base / "salling" if _is_frozen() and root is None else base
    return base / "profiles" / profile_id / "salling"


def _clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:40]


def _primary_name(root: Path) -> str:
    candidates = [data_dir(PRIMARY_ID, root) / "profile.json", root / "profile.json"]
    for path in candidates:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            first = _clean_name(saved.get("first_name", ""))
            last = _clean_name(saved.get("last_name", ""))
            full = _clean_name(f"{first} {last}")
            if full:
                return full
        except (OSError, ValueError, AttributeError):
            pass
    return "Мой профиль"


def _default_state(root: Path) -> dict:
    return {
        "version": 1,
        "active_id": PRIMARY_ID,
        "profiles": [{
            "id": PRIMARY_ID,
            "name": _primary_name(root),
            "created_at": "",
        }],
    }


def _normalise(raw: dict, root: Path) -> dict:
    profiles = []
    seen = set()
    for item in raw.get("profiles", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", profile_id) or profile_id in seen:
            continue
        name = _clean_name(item.get("name", ""))
        if not name:
            continue
        seen.add(profile_id)
        profiles.append({
            "id": profile_id,
            "name": name,
            "created_at": str(item.get("created_at") or "")[:40],
        })

    if PRIMARY_ID not in seen:
        profiles.insert(0, {
            "id": PRIMARY_ID,
            "name": _primary_name(root),
            "created_at": "",
        })
    else:
        profiles.sort(key=lambda item: item["id"] != PRIMARY_ID)

    active_id = str(raw.get("active_id") or PRIMARY_ID) if isinstance(raw, dict) else PRIMARY_ID
    if active_id not in {item["id"] for item in profiles}:
        active_id = PRIMARY_ID
    return {"version": 1, "active_id": active_id, "profiles": profiles}


def load(root: Path | None = None) -> dict:
    base = Path(root or storage_root())
    try:
        raw = json.loads(registry_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = _default_state(base)
    return _normalise(raw, base)


def _save(state: dict, root: Path) -> dict:
    state = _normalise(state, root)
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    json_store.replace_file(temp, path)
    return state


def active_profile(root: Path | None = None) -> dict:
    state = load(root)
    return next(item for item in state["profiles"] if item["id"] == state["active_id"])


def active_profile_id(root: Path | None = None) -> str:
    return active_profile(root)["id"]


def is_primary(root: Path | None = None) -> bool:
    return active_profile_id(root) == PRIMARY_ID


def create_profile(name: str, root: Path | None = None, *, activate: bool = True) -> dict:
    base = Path(root or storage_root())
    clean_name = _clean_name(name)
    if not clean_name:
        raise ValueError("Введи имя профиля — например, «Сестра».")
    with _LOCK:
        state = load(base)
        if any(item["name"].casefold() == clean_name.casefold() for item in state["profiles"]):
            raise ValueError("Профиль с таким именем уже есть.")
        profile_id = f"person_{uuid.uuid4().hex[:12]}"
        profile = {
            "id": profile_id,
            "name": clean_name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        state["profiles"].append(profile)
        if activate:
            state["active_id"] = profile_id
        data_dir(profile_id, base).mkdir(parents=True, exist_ok=True)
        _save(state, base)
        return profile


def set_active(profile_id: str, root: Path | None = None) -> dict:
    base = Path(root or storage_root())
    profile_id = str(profile_id or "").strip()
    with _LOCK:
        state = load(base)
        profile = next((item for item in state["profiles"] if item["id"] == profile_id), None)
        if profile is None:
            raise ValueError("Профиль не найден.")
        state["active_id"] = profile_id
        data_dir(profile_id, base).mkdir(parents=True, exist_ok=True)
        _save(state, base)
        return profile


def get_profile(profile_id: str, root: Path | None = None) -> dict | None:
    profile_id = str(profile_id or "").strip()
    return next((p for p in load(root)["profiles"] if p["id"] == profile_id), None)


def request_remote_switch(profile_id: str, root: Path | None = None) -> dict:
    """Ask the native desktop shell to restart into an existing candidate.

    The web worker cannot safely hot-swap SQLAlchemy/browser/profile module
    globals, so it writes a tiny authenticated local hand-off. The native shell
    consumes it and performs the same clean restart as a manual profile switch.
    """
    base = Path(root or storage_root())
    profile = get_profile(profile_id, base)
    if profile is None:
        raise ValueError("Профиль не найден.")
    path = remote_switch_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({
        "profile_id": profile["id"],
        "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    json_store.replace_file(temp, path)
    return profile


def take_remote_switch(root: Path | None = None) -> dict | None:
    """Atomically consume a pending remote switch request, if valid."""
    base = Path(root or storage_root())
    path = remote_switch_path(base)
    with _LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return None
        try:
            path.unlink()
        except OSError:
            return None
        return get_profile(str(raw.get("profile_id") or ""), base)


def ui_state(root: Path | None = None) -> dict:
    state = load(root)
    profiles = []
    for item in state["profiles"]:
        words = [part for part in item["name"].split() if part]
        profiles.append({
            **item,
            "initials": "".join(part[0].upper() for part in words[:2]) or "П",
            "active": item["id"] == state["active_id"],
            "primary": item["id"] == PRIMARY_ID,
        })
    return {
        "active": next(item for item in profiles if item["active"]),
        "profiles": profiles,
    }
