"""Desktop child processes cannot outlive an application shutdown race."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop_app


class _Process:
    def __init__(self):
        self.running = True
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated += 1
        self.running = False

    def kill(self):
        self.killed += 1
        self.running = False

    def wait(self, timeout=None):
        self.waited += 1
        return 0


def test_shutdown_drains_tracked_and_rejects_late_processes():
    first = _Process()
    late = _Process()
    with desktop_app._started_lock:
        desktop_app._started.clear()
        desktop_app._stopping = False
    desktop_app._remember_started("server", first)
    desktop_app.stop_started()
    desktop_app._remember_started("late-browser-install", late)
    try:
        assert first.terminated == 1 and first.waited == 1
        assert late.terminated == 1 and late.waited == 1
        assert desktop_app._started == []
    finally:
        with desktop_app._started_lock:
            desktop_app._stopping = False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
