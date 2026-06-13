"""Локальное хранилище логина Salling Group для авто-входа.

Email хранится открыто, пароль — зашифрован через Windows DPAPI
(CryptProtectData): расшифровать его может только текущий пользователь
Windows на этой машине. Файл sf_credentials.json можно открыть, но пароль
в нём — нечитаемая base64-строка.

При первом чтении автоматически мигрирует старые данные:
- открытый "password" в sf_credentials.json -> "password_enc";
- блок "sf_login" из profile.json -> сюда (и удаляется из profile.json).
"""
import base64
import json

import config

PATH = config.DATA_DIR / "sf_credentials.json"


# --- Windows DPAPI (без внешних зависимостей) ---

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if protect
          else ctypes.windll.crypt32.CryptUnprotectData)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _encrypt(password: str) -> str:
    return base64.b64encode(_dpapi(password.encode("utf-8"), protect=True)).decode("ascii")


def _decrypt(token: str) -> str:
    return _dpapi(base64.b64decode(token), protect=False).decode("utf-8")


# --- файл ---

def _read() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _write(data: dict):
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate(data: dict) -> dict:
    """Однократно переводит старые открытые пароли в зашифрованный вид."""
    changed = False
    # 1) открытый пароль в самом файле
    plain = data.pop("password", None)
    if plain and not data.get("password_enc"):
        data["password_enc"] = _encrypt(plain)
        changed = True
    elif plain:
        changed = True  # просто удалить дубликат
    # 2) sf_login внутри profile.json (старое дублирование)
    try:
        if config.PROFILE_PATH.exists():
            profile = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
            login = profile.pop("sf_login", None)
            if login is not None:
                if not data.get("email") and login.get("username"):
                    data["email"] = login["username"]
                if not data.get("password_enc") and login.get("password"):
                    data["password_enc"] = _encrypt(login["password"])
                config.PROFILE_PATH.write_text(
                    json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
                changed = True
    except Exception:
        pass
    if changed:
        _write(data)
    return data


def save(email: str, password: str):
    data = _migrate(_read())
    if email is not None:
        data["email"] = email.strip()
    # пустой пароль из формы означает «не менять сохранённый»
    if password:
        data["password_enc"] = _encrypt(password)
    _write(data)


def get() -> dict:
    """Возвращает {"email": ..., "password": ...} с расшифрованным паролем."""
    data = _migrate(_read())
    password = ""
    if data.get("password_enc"):
        try:
            password = _decrypt(data["password_enc"])
        except Exception:
            password = ""
    return {"email": data.get("email", ""), "password": password}


def status() -> dict:
    data = _migrate(_read())
    return {"email": data.get("email", ""), "has_password": bool(data.get("password_enc"))}


def clear():
    try:
        PATH.unlink()
    except FileNotFoundError:
        pass
