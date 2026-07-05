"""READ-ONLY preview launcher for the WexFlow UI audit.

Neutralises every outward / background side effect before the FastAPI app
starts, so the auditor can render and screenshot screens without:
  - scraping / writing jobs.db (scraper.sync)
  - sending Telegram messages (_tg_offer_tick, tg poller)
  - spawning Playwright apply workers (real submissions)

Nothing here changes project source files. It only monkeypatches the
in-memory module attributes for this throwaway process.
"""
import os
import sys

PROJ = r"C:\saling"
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import app          # noqa: E402  (imports run init_db() — safe, just table create)
import scraper      # noqa: E402

_noop = lambda *a, **k: None

# Background sync / scheduler work — make it do nothing.
app._sync_jobs = _noop
app._ensure_tg_poller = _noop
app._tg_offer_tick = _noop
scraper.sync = _noop

# Hard stop on any apply/submit path, even if a GET accidentally reaches it.
for _name in ("_launch_salling_apply", "_spawn_salling_apply",
              "_enqueue_auto_submit", "_apply_runner_loop"):
    if hasattr(app, _name):
        setattr(app, _name, _noop)


# Belt-and-suspenders: neutralise subprocess.Popen INSIDE the app process so that
# even the direct-spawn routes (/job/{id}/apply/start, /apply/batch) cannot launch
# a real Playwright worker during the audit. Returns a dummy process object.
class _FakePopen:
    def poll(self):  # already exited
        return 0

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0


app.subprocess.Popen = lambda *a, **k: _FakePopen()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app.app, host="127.0.0.1", port=8011, log_level="info")
