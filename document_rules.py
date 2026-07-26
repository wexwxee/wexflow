"""Document sets for a brand or an individual store.

The candidate profile keeps the global CV and cover letter.  Rules stored in
settings.json override them per field with the following priority:

    exact store -> brand -> global profile

A rule may contain only one document.  The missing document then falls back to
the next level instead of silently disappearing from an application.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

import profile_store
import settings_store

SETTINGS_KEY = "document_rules"
MAX_RULES = 100
BRAND_ALIASES = {
    "lidl danmark": "lidl",
    "lidl danmark k/s": "lidl",
}


def _value(job: Any, name: str) -> str:
    if isinstance(job, dict):
        value = job.get(name)
    else:
        value = getattr(job, name, None)
    return str(value or "").strip()


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def brand_key(value_or_job: Any) -> str:
    """Return the stable key used to match a vacancy brand."""
    if isinstance(value_or_job, str):
        value = value_or_job
    else:
        value = _value(value_or_job, "brand")
    normalised = _normalise_text(value)
    return BRAND_ALIASES.get(normalised, normalised)[:80]


def store_key(job: Any) -> str:
    """Return a stable, privacy-light key for a physical store."""
    parts = [
        brand_key(job),
        _normalise_text(_value(job, "street")),
        _normalise_text(_value(job, "zip")),
        _normalise_text(_value(job, "city")),
    ]
    if not any(parts[1:]):
        return ""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"store-{digest}"


def _normalise_rule(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    rule_id = str(raw.get("id") or "").strip()[:80]
    scope = str(raw.get("scope") or "").strip().lower()
    brand = brand_key(str(raw.get("brand") or ""))
    selected_store = str(raw.get("store_key") or "").strip()[:80]
    if not rule_id or scope not in {"brand", "store"} or not brand:
        return None
    if scope == "store" and not selected_store:
        return None
    name = str(raw.get("name") or "").strip()[:80]
    return {
        "id": rule_id,
        "name": name or str(raw.get("brand_label") or brand).strip()[:80],
        "scope": scope,
        "brand": brand,
        "brand_label": str(raw.get("brand_label") or brand).strip()[:80],
        "store_key": selected_store if scope == "store" else "",
        "store_label": str(raw.get("store_label") or "").strip()[:160],
        "cv_path": str(raw.get("cv_path") or "").strip(),
        "cover_letter_path": str(raw.get("cover_letter_path") or "").strip(),
        "updated_at": int(raw.get("updated_at") or 0),
    }


def get_rules() -> list[dict]:
    rules = [
        rule
        for raw in settings_store.load().get(SETTINGS_KEY, [])
        if (rule := _normalise_rule(raw)) is not None
    ]
    return sorted(rules, key=lambda rule: rule["updated_at"], reverse=True)[:MAX_RULES]


def get_rule(rule_id: str) -> dict | None:
    wanted = str(rule_id or "").strip()
    return next((rule for rule in get_rules() if rule["id"] == wanted), None)


def save_rule(
    *,
    rule_id: str = "",
    name: str = "",
    scope: str,
    brand: str,
    brand_label: str = "",
    selected_store_key: str = "",
    store_label: str = "",
    cv_path: str = "",
    cover_letter_path: str = "",
) -> dict:
    scope = str(scope or "").strip().lower()
    brand = brand_key(str(brand or ""))
    selected_store_key = str(selected_store_key or "").strip()[:80]
    if scope not in {"brand", "store"}:
        raise ValueError("Выбери уровень: бренд или конкретный магазин.")
    if not brand:
        raise ValueError("Выбери бренд.")
    if scope == "store" and not selected_store_key:
        raise ValueError("Выбери магазин.")

    cv_path = profile_store.validate_document_path(cv_path)
    cover_letter_path = profile_store.validate_document_path(cover_letter_path)
    if not cv_path and not cover_letter_path:
        raise ValueError("Добавь хотя бы CV или мотивационное письмо.")

    wanted_id = str(rule_id or "").strip()[:80]
    saved: dict = {}

    def _mutate(data: dict) -> None:
        rules = [
            rule
            for raw in data.get(SETTINGS_KEY, [])
            if (rule := _normalise_rule(raw)) is not None
        ]
        existing = next(
            (
                rule for rule in rules
                if (wanted_id and rule["id"] == wanted_id)
                or (
                    not wanted_id
                    and rule["scope"] == scope
                    and rule["brand"] == brand
                    and (
                        scope == "brand"
                        or rule["store_key"] == selected_store_key
                    )
                )
            ),
            None,
        )
        merged_cv_path = cv_path or (existing or {}).get("cv_path", "")
        merged_cover_path = cover_letter_path or (existing or {}).get("cover_letter_path", "")
        final_name = str(name or "").strip()[:80]
        final_brand_label = str(brand_label or brand).strip()[:80]
        final_store_label = str(store_label or "").strip()[:160]
        if not final_name:
            final_name = final_store_label if scope == "store" else final_brand_label
        record = {
            "id": (existing or {}).get("id") or uuid.uuid4().hex,
            "name": final_name,
            "scope": scope,
            "brand": brand,
            "brand_label": final_brand_label,
            "store_key": selected_store_key if scope == "store" else "",
            "store_label": final_store_label if scope == "store" else "",
            "cv_path": merged_cv_path,
            "cover_letter_path": merged_cover_path,
            "updated_at": int(time.time()),
        }
        saved.update(record)
        data[SETTINGS_KEY] = [
            record,
            *[rule for rule in rules if rule["id"] != record["id"]],
        ][:MAX_RULES]

    settings_store.mutate(_mutate)
    return saved


def delete_rule(rule_id: str) -> bool:
    wanted = str(rule_id or "").strip()
    removed = False

    def _mutate(data: dict) -> None:
        nonlocal removed
        rules = []
        for raw in data.get(SETTINGS_KEY, []):
            rule = _normalise_rule(raw)
            if rule is None:
                continue
            if rule["id"] == wanted:
                removed = True
                continue
            rules.append(rule)
        data[SETTINGS_KEY] = rules

    settings_store.mutate(_mutate)
    return removed


def resolve_profile(profile: dict, job: Any) -> dict:
    """Return a profile copy with the best documents for this vacancy."""
    result = dict(profile or {})
    rules = get_rules()
    wanted_brand = brand_key(job)
    wanted_store = store_key(job)
    brand_rule = next(
        (
            rule for rule in rules
            if rule["scope"] == "brand" and rule["brand"] == wanted_brand
        ),
        None,
    )
    store_rule = next(
        (
            rule for rule in rules
            if rule["scope"] == "store"
            and rule["brand"] == wanted_brand
            and rule["store_key"] == wanted_store
        ),
        None,
    )

    sources: dict[str, dict] = {}
    for field in ("cv_path", "cover_letter_path"):
        selected_path = str(result.get(field) or "").strip()
        selected_rule = None
        if brand_rule and brand_rule.get(field):
            selected_path = brand_rule[field]
            selected_rule = brand_rule
        if store_rule and store_rule.get(field):
            selected_path = store_rule[field]
            selected_rule = store_rule
        result[field] = selected_path
        sources[field] = {
            "level": selected_rule["scope"] if selected_rule else "global",
            "rule_id": selected_rule["id"] if selected_rule else "",
            "label": selected_rule["name"] if selected_rule else "Общий комплект",
        }

    result["_document_selection"] = {
        "brand": wanted_brand,
        "store_key": wanted_store,
        "cv": sources["cv_path"],
        "cover": sources["cover_letter_path"],
    }
    return result
