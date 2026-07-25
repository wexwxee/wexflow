"""AI-assisted bulk import of CV and motivation-letter document sets."""
from __future__ import annotations

import json
import re
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

import ai_filters
import document_rules
import profile_store
import settings_store

PREVIEW_KEY = "document_import_preview"
MAX_FILES = 20
PREVIEW_TTL_SECONDS = 6 * 60 * 60
MAX_EXCERPT_CHARS = 1400

_CV_RE = re.compile(
    r"\b(?:cv|resume|résumé|curriculum\s+vitae|levnedsbeskrivelse)\b",
    re.I,
)
_COVER_RE = re.compile(
    r"\b(?:cover(?:\s+letter)?|motivation(?:al)?|motiveret|motivationsbrev|"
    r"ansøgning|ansoegning|application\s+letter)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _extract_pdf(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages[:8]:
            chunks.append(page.extract_text() or "")
            if sum(len(chunk) for chunk in chunks) >= 7000:
                break
        return "\n".join(chunks)
    except Exception:  # noqa: BLE001
        return ""


def _extract_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        chunks = [
            node.text or ""
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] in {"t", "tab", "br"}
        ]
        return " ".join(chunks)
    except Exception:  # noqa: BLE001
        return ""


def extract_text(path: str) -> str:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf(source)
    elif suffix == ".docx":
        text = _extract_docx(source)
    else:
        text = ""
    return re.sub(r"\s+", " ", text).strip()[:8000]


def _redacted_excerpt(text: str) -> str:
    value = _EMAIL_RE.sub("[email скрыт]", str(text or ""))
    value = _PHONE_RE.sub("[телефон скрыт]", value)
    return re.sub(r"\s+", " ", value).strip()[:MAX_EXCERPT_CHARS]


def _kind_hint(filename: str, text: str) -> str:
    name = Path(filename).stem.replace("_", " ").replace("-", " ")
    if _COVER_RE.search(name):
        return "cover"
    if _CV_RE.search(name):
        return "cv"
    sample = str(text or "")[:1800]
    if _COVER_RE.search(sample):
        return "cover"
    if _CV_RE.search(sample):
        return "cv"
    return "unknown"


def _candidate_stores(filename: str, text: str, stores: list[dict]) -> list[str]:
    haystack = _normalise(f"{filename} {text[:5000]}")
    candidates: list[tuple[int, str]] = []
    for store in stores:
        label = _normalise(store.get("label", ""))
        tokens = [
            token for token in re.findall(r"[\wæøåäöüé]{3,}|\d{4}", label)
            if token not in {"netto", "føtex", "foetex", "bilka", "salling", "group"}
        ]
        matched = sum(1 for token in set(tokens) if token in haystack)
        has_zip = any(token.isdigit() and token in haystack for token in tokens)
        if matched >= 3 and has_zip:
            candidates.append((matched, str(store.get("key") or "")))
    candidates.sort(reverse=True)
    return [key for _, key in candidates[:3] if key]


def _targets(brands: list[dict], stores: list[dict], files: list[dict]) -> list[dict]:
    result = [
        {
            "id": f"brand:{item['key']}",
            "scope": "brand",
            "brand": item["key"],
            "label": item["label"],
        }
        for item in brands
        if item.get("key")
    ]
    wanted_store_keys = {
        key for file in files for key in file.get("candidate_store_keys", [])
    }
    result.extend(
        {
            "id": f"store:{item['key']}",
            "scope": "store",
            "brand": item["brand"],
            "store_key": item["key"],
            "label": item["label"],
        }
        for item in stores
        if item.get("key") in wanted_store_keys
    )
    return result[:60]


def _analysis_prompt(files: list[dict], targets: list[dict]) -> str:
    safe_files = [
        {
            "id": item["id"],
            "filename": item["filename"],
            "type_hint": item["kind_hint"],
            "redacted_excerpt": item["excerpt"],
            "candidate_store_targets": [
                f"store:{key}" for key in item.get("candidate_store_keys", [])
            ],
        }
        for item in files
    ]
    safe_targets = [
        {"id": item["id"], "label": item["label"], "scope": item["scope"]}
        for item in targets
    ]
    return (
        "Ты сортируешь загруженные пользователем документы для откликов на вакансии "
        "Salling Group. Определи, какой файл является CV, какой мотивационным письмом, "
        "для какого бренда/магазина он подготовлен, и собери пары. Не анализируй личность "
        "кандидата и не возвращай личные данные.\n\n"
        "Правила:\n"
        "- Используй только target_id из ALLOWED_TARGETS. Не выдумывай бренды или магазины.\n"
        "- Один файл можно использовать только один раз.\n"
        "- В одной группе максимум один cv_id и один cover_id.\n"
        "- Своди CV и письмо в пару, только если они относятся к одной цели.\n"
        "- Если цель или тип неясны, оставь файл в unassigned_file_ids.\n"
        "- confidence — число 0..1. reason — одно короткое объяснение по-русски.\n"
        "- Ответ строго JSON: "
        '{"groups":[{"target_id":"brand:netto","cv_id":"f1","cover_id":"f5",'
        '"name":"Netto","confidence":0.95,"reason":"..."}],'
        '"unassigned_file_ids":["f3"]}.\n\n'
        f"ALLOWED_TARGETS:\n{json.dumps(safe_targets, ensure_ascii=False)}\n\n"
        f"FILES:\n{json.dumps(safe_files, ensure_ascii=False)}"
    )


def _sanitise_groups(data: dict, files: list[dict], targets: list[dict]) -> tuple[list[dict], list[str]]:
    file_map = {item["id"]: item for item in files}
    target_map = {item["id"]: item for item in targets}
    used: set[str] = set()
    groups_by_target: dict[str, dict] = {}
    rejected: list[str] = []

    raw_groups = data.get("groups", []) if isinstance(data, dict) else []
    for raw in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(raw, dict):
            continue
        target_id = str(raw.get("target_id") or "")
        if target_id not in target_map:
            continue
        cv_id = str(raw.get("cv_id") or "")
        cover_id = str(raw.get("cover_id") or "")
        group = groups_by_target.setdefault(
            target_id,
            {
                "id": uuid.uuid4().hex[:12],
                "target": target_id,
                "target_label": target_map[target_id]["label"],
                "name": str(raw.get("name") or target_map[target_id]["label"]).strip()[:80],
                "cv_id": "",
                "cover_id": "",
                "confidence": 0.0,
                "reason": "",
            },
        )
        for role, file_id in (("cv_id", cv_id), ("cover_id", cover_id)):
            if not file_id or file_id not in file_map or file_id in used:
                continue
            if group[role]:
                rejected.append(file_id)
                continue
            group[role] = file_id
            used.add(file_id)
        try:
            confidence = float(raw.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        group["confidence"] = max(group["confidence"], max(0.0, min(confidence, 1.0)))
        reason = str(raw.get("reason") or "").strip()[:220]
        if reason:
            group["reason"] = reason

    groups = [
        group for group in groups_by_target.values()
        if group["cv_id"] or group["cover_id"]
    ]
    raw_unassigned = data.get("unassigned_file_ids", []) if isinstance(data, dict) else []
    explicit = raw_unassigned if isinstance(raw_unassigned, list) else []
    unassigned = [
        file_id for file_id in dict.fromkeys([
            *[str(item) for item in explicit],
            *rejected,
            *[item["id"] for item in files if item["id"] not in used],
        ])
        if file_id in file_map and file_id not in used
    ]
    return groups, unassigned


def _safe_preview_file(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "")
    try:
        source = Path(path).resolve()
        upload_dir = profile_store.UPLOAD_DIR.resolve()
        if source.parent != upload_dir or not source.name.startswith("bulk_doc_"):
            return None
    except OSError:
        return None
    return {
        "id": str(raw.get("id") or "")[:20],
        "filename": str(raw.get("filename") or source.name)[:180],
        "path": str(source),
        "kind_hint": str(raw.get("kind_hint") or "unknown"),
    }


def save_preview(preview: dict) -> None:
    settings_store.mutate(lambda data: data.__setitem__(PREVIEW_KEY, preview))


def get_preview() -> dict | None:
    raw = settings_store.load().get(PREVIEW_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        created_at = float(raw.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    if time.time() - created_at > PREVIEW_TTL_SECONDS:
        clear_preview(delete_files=True)
        return None
    files = [
        item for raw_file in raw.get("files", [])
        if (item := _safe_preview_file(raw_file)) is not None
    ]
    file_ids = {item["id"] for item in files}
    groups = []
    for group in raw.get("groups", []):
        if not isinstance(group, dict):
            continue
        try:
            confidence = float(group.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        groups.append({
            "id": str(group.get("id") or "")[:20],
            "target": str(group.get("target") or "")[:100],
            "target_label": str(group.get("target_label") or "")[:180],
            "name": str(group.get("name") or "")[:80],
            "cv_id": str(group.get("cv_id") or "") if str(group.get("cv_id") or "") in file_ids else "",
            "cover_id": str(group.get("cover_id") or "") if str(group.get("cover_id") or "") in file_ids else "",
            "confidence": max(0.0, min(confidence, 1.0)),
            "reason": str(group.get("reason") or "")[:220],
        })
    groups = [group for group in groups if group["id"] and (group["cv_id"] or group["cover_id"])]
    unassigned = [
        str(file_id) for file_id in raw.get("unassigned", [])
        if str(file_id) in file_ids
    ]
    return {
        "id": str(raw.get("id") or "")[:40],
        "created_at": created_at,
        "files": files,
        "groups": groups,
        "unassigned": unassigned,
        "model": str(raw.get("model") or "")[:80],
    }


def clear_preview(delete_files: bool = False) -> None:
    raw = settings_store.load().get(PREVIEW_KEY)
    if delete_files and isinstance(raw, dict):
        for raw_file in raw.get("files", []):
            safe_file = _safe_preview_file(raw_file)
            if safe_file is None:
                continue
            try:
                Path(safe_file["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    settings_store.mutate(lambda data: data.pop(PREVIEW_KEY, None))


def _delete_uploaded(files: list[dict]) -> None:
    for raw in files:
        safe_file = _safe_preview_file(raw)
        if safe_file is None:
            continue
        try:
            Path(safe_file["path"]).unlink(missing_ok=True)
        except OSError:
            pass


def analyse_uploads(uploads, brands: list[dict], stores: list[dict]) -> dict:
    uploads = [upload for upload in uploads if upload and getattr(upload, "filename", "")]
    if len(uploads) < 2:
        return {"ok": False, "error": "Выбери минимум два файла для массового разбора."}
    if len(uploads) > MAX_FILES:
        return {"ok": False, "error": f"За один раз можно разобрать максимум {MAX_FILES} файлов."}
    if not ai_filters.available():
        return {"ok": False, "error": "ИИ не подключён: сначала добавь ключ Gemini в настройках анкет."}

    clear_preview(delete_files=True)
    files: list[dict] = []
    try:
        for index, upload in enumerate(uploads, start=1):
            path = profile_store.save_upload(upload, "bulk_doc")
            text = extract_text(path)
            files.append({
                "id": f"f{index}",
                "filename": Path(upload.filename).name[:180],
                "path": path,
                "kind_hint": _kind_hint(upload.filename, text),
                "excerpt": _redacted_excerpt(text),
                "candidate_store_keys": _candidate_stores(upload.filename, text, stores),
            })
    except ValueError as exc:
        _delete_uploaded(files)
        return {"ok": False, "error": str(exc)}

    targets = _targets(brands, stores, files)
    response = ai_filters.generate_json(_analysis_prompt(files, targets), timeout=55)
    if not response.get("ok"):
        _delete_uploaded(files)
        return {"ok": False, "error": response.get("error") or "ИИ не смог разобрать документы."}
    groups, unassigned = _sanitise_groups(response.get("data") or {}, files, targets)
    if not groups:
        _delete_uploaded(files)
        return {
            "ok": False,
            "error": "ИИ не смог уверенно связать файлы с брендами. Переименуй их понятнее и попробуй ещё раз.",
        }
    preview = {
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        "files": [
            {
                "id": item["id"],
                "filename": item["filename"],
                "path": item["path"],
                "kind_hint": item["kind_hint"],
            }
            for item in files
        ],
        "groups": groups,
        "unassigned": unassigned,
        "model": response.get("model") or "",
    }
    save_preview(preview)
    return {"ok": True, "preview": get_preview()}


def apply_preview(
    preview: dict,
    selections: dict[str, str],
    brands: list[dict],
    stores: list[dict],
) -> list[dict]:
    brand_map = {item["key"]: item for item in brands}
    store_map = {item["key"]: item for item in stores}
    file_map = {item["id"]: item for item in preview.get("files", [])}
    created: list[dict] = []
    for group in preview.get("groups", []):
        target = str(selections.get(group["id"]) or group.get("target") or "")
        if target == "skip":
            continue
        scope, separator, key = target.partition(":")
        if not separator:
            continue
        if scope == "brand":
            option = brand_map.get(key)
            if option is None:
                continue
            brand = key
            brand_label = option["label"]
            selected_store_key = ""
            store_label = ""
        elif scope == "store":
            option = store_map.get(key)
            if option is None:
                continue
            brand = option["brand"]
            brand_label = option["brand_label"]
            selected_store_key = key
            store_label = option["label"]
        else:
            continue
        cv = file_map.get(group.get("cv_id")) or {}
        cover = file_map.get(group.get("cover_id")) or {}
        if not cv.get("path") and not cover.get("path"):
            continue
        created.append(document_rules.save_rule(
            name=group.get("name") or (store_label or brand_label),
            scope=scope,
            brand=brand,
            brand_label=brand_label,
            selected_store_key=selected_store_key,
            store_label=store_label,
            cv_path=cv.get("path", ""),
            cover_letter_path=cover.get("path", ""),
        ))
    return created
