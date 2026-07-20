"""Стенд длительного теста стабильности (порт 8014).

Реальное приложение с настоящими фоновыми процессами: планировщик,
синк вакансий (Algolia), автопилот-сканы, сторожа. Выключено только то,
что шумит наружу в Telegram-чат:
  - опросчик облачных команд (_ensure_tg_poller);
  - отправка карточек/дайджеста (_tg_offer_tick).

Смысл: часами гонять приложение и следить за утечками процессов,
памятью и повторными запросами — не рассылая ничего в Telegram.
"""
import os
import sys

PROJ = r"C:\saling"
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import app  # noqa: E402

_noop = lambda *a, **k: None
app._ensure_tg_poller = _noop
app._tg_offer_tick = _noop

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.app, host="127.0.0.1", port=8014, log_level="warning")
