# -*- mode: python ; coding: utf-8 -*-
"""Сборка раздаваемого дистрибутива WexFlow (onedir).

Особенности:
- Код 7-Eleven кладётся отдельной папкой seven11/ внутри сборки (как данные),
  его сторонние зависимости объявлены в hiddenimports, потому что статический
  анализатор PyInstaller их «не видит» (модули грузятся по пути в рантайме).
- Браузер Playwright НЕ кладём — он скачивается при первом запуске в %AppData%.
- Результат: dist\\WexFlow\\WexFlow.exe + папка _internal. Эту папку и раздаём.
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.building.datastruct import Tree

SALING = os.path.abspath(".")
SEVEN = r"C:\seven11-apply"

datas, binaries, hiddenimports = [], [], []

# Полный сбор пакетов, которые тянет код 7-Eleven (анализатор их не видит).
for pkg in ("playwright", "pydantic", "pydantic_core", "email_validator", "typer", "rich"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("uvicorn")
# Uvicorn loads its WebSocket protocol dynamically. The standard hook can keep
# uvicorn itself while dropping websockets.legacy, which makes a frozen server
# exit immediately after an otherwise successful update.
hiddenimports += collect_submodules("websockets")
# Все кодеки текста: getaddrinfo требует "idna", а разные локали — cp1252/idna и т.п.
# Без явного сбора собранный exe падает «unknown encoding: …» на сетевых операциях.
hiddenimports += collect_submodules("encodings")
hiddenimports += ["encodings.idna", "stringprep"]
# Пакет коннекторов (БЕТА-модуль подачи) — грузится лениво в воркерах,
# поэтому объявляем его подмодули явно, иначе анализатор их не положит.
hiddenimports += collect_submodules("connectors")
# Провайдеры ИИ грузятся через общий шлюз (в т.ч. лениво из воркеров) —
# объявляем пакет явно, иначе анализатор не положит gemini/groq в сборку.
hiddenimports += collect_submodules("ai_providers")
hiddenimports += [
    "dns", "dns.resolver", "email_validator",
    # модули Salling (на всякий случай — большинство анализируется автоматически)
    "app", "hub", "apply", "config", "paths", "db", "geo", "transit",
    "scraper", "connector_sync", "labels", "translator", "translator_setup",
    "profile_store", "settings_store", "credentials_store",
    "version", "update_check", "autopilot", "ai_filters", "autostart",
    "ai_gateway", "ai_secrets", "ai_usage",
    # трей-иконка: бэкенд pystray для Windows грузится динамически
    "pystray._win32",
]

# Одиночные ресурсы Salling.
datas += [("app.ico", ".")]
if os.path.exists("profile.example.json"):
    datas += [("profile.example.json", ".")]

# JSON-каталоги коннекторов (читаются из _MEIPASS/connectors/ в сборке).
for _c in ("teamtailor_companies.json", "greenhouse_companies.json", "ashby_companies.json"):
    _p = os.path.join(SALING, "connectors", _c)
    if os.path.exists(_p):
        datas += [(_p, "connectors")]

# Код 7-Eleven (отдельные файлы) + посев публичной базы магазинов.
datas += [
    (os.path.join(SEVEN, "web_app.py"), "seven11"),
    (os.path.join(SEVEN, "apply.py"), "seven11"),
    (os.path.join(SEVEN, "runtime_paths.py"), "seven11"),
    (os.path.join(SEVEN, "data", "addresses.json"), "seven11/data"),
    (os.path.join(SEVEN, "data", "store_links.json"), "seven11/data"),
]

a = Analysis(
    ["desktop_app.py"],
    pathex=[SALING],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

# Рекурсивные папки-ресурсы.
a.datas += Tree("templates", prefix="templates",
                excludes=["__pycache__", "*.pyc", "*_wexflow.html"])
a.datas += Tree("static", prefix="static",
                excludes=["*.bak", "theme_wexflow.css"])
a.datas += Tree(os.path.join(SEVEN, "core"), prefix="seven11/core",
                excludes=["__pycache__", "*.pyc"])
a.datas += Tree(os.path.join(SEVEN, "web_static"), prefix="seven11/web_static",
                excludes=["__pycache__", "*.pyc", "*.before_wexflow_*"])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # UTF-8 mode (PEP 540): print/open по умолчанию в UTF-8 на ЛЮБОЙ локали
    # пользователя — иначе вывод датских/русских символов падает в windowed-сборке.
    [("X utf8=1", None, "OPTION")],
    exclude_binaries=True,
    name="WexFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="WexFlow",
)
