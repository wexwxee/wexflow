"""E2E-превью сквозной проверки подачи (порт 8013).

Отличие от run_preview_audit.py: subprocess.Popen НЕ подменяется — здесь
проверяется НАСТОЯЩАЯ цепочка подачи в режиме «прогон без отправки»
(воркер apply.py без флага --submit физически не нажимает отправку).

Отключены только фоновые побочные эффекты, чтобы прогон был чистым:
  - scraper.sync / _sync_jobs — база стабильна на время проверки;
  - Telegram-опросчик и отправка карточек — в чат ничего не уходит,
    команды с телефона не выполняются.

Ничего в исходниках не меняет — только атрибуты модулей этого процесса.
"""
import os
import sys

PROJ = r"C:\saling"
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import app          # noqa: E402
import scraper      # noqa: E402

_noop = lambda *a, **k: None

app._sync_jobs = _noop
app._ensure_tg_poller = _noop
app._tg_offer_tick = _noop
scraper.sync = _noop

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.app, host="127.0.0.1", port=8013, log_level="info")
