"""The frozen release must include Uvicorn's lazily imported protocols."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


ROOT = Path(__file__).resolve().parents[1]


def test_spec_collects_websockets_submodules():
    spec = (ROOT / "WexFlow_dist.spec").read_text(encoding="utf-8")
    assert 'collect_submodules("websockets")' in spec


def test_frozen_selftest_imports_server_protocols():
    desktop = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "import uvicorn.protocols.http.auto" in desktop
    assert "import uvicorn.protocols.websockets.auto" in desktop
    assert "import websockets.legacy" in desktop


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nALL {len(tests)} TESTS PASSED")
