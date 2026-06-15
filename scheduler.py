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

import scraper
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


def job_tick():
    print(f"\n[{datetime.now():%H:%M:%S}] ре-скрейп…")
    scraper.sync()
    # вакансии, впервые увиденные за последние 35 минут
    cutoff = utcnow() - timedelta(minutes=35)
    with get_session() as s:
        fresh = s.exec(
            select(Job).where(Job.first_seen >= cutoff, Job.status != "closed")
        ).all()
    if fresh:
        notify(f"Новых вакансий: {len(fresh)}", "; ".join(j.title for j in fresh[:5]))


if __name__ == "__main__":
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    job_tick()  # сразу один прогон
    sched = BlockingScheduler()
    sched.add_job(job_tick, "interval", minutes=minutes)
    print(f"Расписание: каждые {minutes} мин. Ctrl+C для выхода.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
