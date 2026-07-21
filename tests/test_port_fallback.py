"""Тест авто-выбора свободного порта (фикс «серверы не запустились» при Docker/WSL).

Баг у Ивана: Docker Desktop / WSL держат порт 8080, который нужен Hub-серверу
WexFlow. Раньше код считал занятый порт «своим» и не поднимал Hub → окно
«WexFlow не смог запустить серверы». Теперь берём ближайший свободный порт.

Запуск:  python tests/test_port_fallback.py   (или pytest)
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import desktop_app as d


def _occupy(port):
    """Занять порт как «чужое приложение», вернуть слушающий сокет.
    Без SO_REUSEADDR — иначе Windows разрешает второй bind и проверка врёт."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def test_free_port_used_as_is():
    # берём заведомо свободный высокий порт
    s = _occupy(0)  # порт 0 → ОС даёт свободный; узнаем какой и освободим
    free = s.getsockname()[1]
    s.close()
    assert d._pick_port(free, set()) == free


def test_picks_next_when_occupied():
    busy = _occupy(0)
    port = busy.getsockname()[1]
    try:
        chosen = d._pick_port(port, set())
        assert chosen != port, "занятый порт брать нельзя"
        assert port < chosen <= port + 60
        assert d._bind_free(chosen), "выбранный порт должен быть реально свободен"
    finally:
        busy.close()


def test_used_set_prevents_collision():
    # два «сервера» не должны выбрать один и тот же альтернативный порт
    busy = _occupy(0)
    port = busy.getsockname()[1]
    try:
        used = set()
        a = d._pick_port(port, used); used.add(a)
        b = d._pick_port(port, used); used.add(b)
        assert a != b, "порты должны быть разными"
    finally:
        busy.close()


def test_resolve_sets_env_for_hub():
    # после resolve порты бэкендов должны уйти в окружение для Hub-проксёра
    os.environ.pop("WEXFLOW_SALLING_PORT", None)
    os.environ.pop("WEXFLOW_SEVEN_PORT", None)
    d._resolve_ports()
    assert os.environ.get("WEXFLOW_SALLING_PORT") == str(d.SALLING_PORT)
    assert os.environ.get("WEXFLOW_SEVEN_PORT") == str(d.SEVEN_PORT)
    assert d.HUB_PORT and d.SALLING_PORT and d.SEVEN_PORT


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
