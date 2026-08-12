"""Этап 4 — периодический ре-скрейп вакансий + уведомление о новых.

Запуск:  python scheduler.py            # каждые 30 минут
         python scheduler.py 10         # каждые 10 минут

Держи окно открытым (или поставь как задачу в Windows Task Scheduler на scraper.py).
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

import feed
import scraper
import source_health
from db import get_session, Job, select, utcnow


def notify(title: str, message: str):
    """Windows toast через PowerShell (без доп. зависимостей)."""
    try:
        text = title if not message else f"{title} - {message[:180]}"
        ps = (
            "$data = [Console]::In.ReadToEnd() | ConvertFrom-Json;"
            '[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,'
            'ContentType=WindowsRuntime] > $null;'
            '[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,'
            'ContentType=WindowsRuntime] > $null;'
            '$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;'
            '$doc.LoadXml("<toast><visual><binding template=""ToastGeneric""><text></text></binding></visual></toast>");'
            '[void]$doc.GetElementsByTagName("text").Item(0).AppendChild($doc.CreateTextNode([string]$data.text));'
            '$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);'
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("WexFlow").Show($toast);'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            input=json.dumps({"text": text}),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass
    print(f"[notify] {title} - {message}")


def _lookback_minutes(interval_minutes: int | float | None = None) -> int:
    """Окно уведомлений не должно быть короче реального интервала запуска."""
    try:
        interval = max(1, int(interval_minutes or 30))
    except (TypeError, ValueError):
        interval = 30
    return max(35, interval + 5)


def job_tick(interval_minutes: int | float | None = None):
    print(f"\n[{datetime.now():%H:%M:%S}] ре-скрейп…")
    try:
        result = scraper.sync() or {}
    except Exception as exc:
        source_health.report(
            "salling",
            hits=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        # scheduler.py может работать отдельно от веб-приложения. Поэтому он
        # сам обязан оживить сторожа после успешного ответа источника.
        source_health.report("salling", hits=result.get("hits"))

    # При запуске раз в 60/120 минут прежнее фиксированное окно 35 минут
    # навсегда теряло часть новых вакансий. Оставляем небольшой запас на дрейф.
    cutoff = utcnow() - timedelta(minutes=_lookback_minutes(interval_minutes))
    with get_session() as s:
        fresh = s.exec(
            select(Job).where(Job.first_seen >= cutoff, *feed.visible_clauses())
        ).all()
    if fresh:
        notify(f"Новых вакансий: {len(fresh)}", "; ".join(j.title for j in fresh[:5]))


if __name__ == "__main__":
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    if minutes <= 0:
        raise SystemExit("Интервал должен быть положительным числом минут.")
    job_tick(minutes)  # сразу один прогон
    sched = BlockingScheduler()
    sched.add_job(job_tick, "interval", minutes=minutes, args=[minutes])
    print(f"Расписание: каждые {minutes} мин. Ctrl+C для выхода.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
