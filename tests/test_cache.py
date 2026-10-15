"""The warehouse cache: stale results are served at once and rebuilt behind the
scenes, a cold key is built once however many requests arrive, and the
refresher only keeps recently used entries warm."""
import threading
import time

from app import warehouse


def setup_function():
    warehouse.clear_cache()


def test_fresh_hit_does_not_rebuild():
    calls = []
    build = lambda: calls.append(1) or len(calls)
    assert warehouse.cached(("k",), build, ttl=60) == 1
    assert warehouse.cached(("k",), build, ttl=60) == 1
    assert len(calls) == 1


def test_stale_is_served_immediately_then_refreshed_in_background():
    gate = threading.Event()
    calls = []

    def build():
        calls.append(1)
        if len(calls) > 1:
            gate.wait(2)               # the background rebuild is slow...
        return len(calls)

    assert warehouse.cached(("k",), build, ttl=60) == 1
    warehouse._cache[("k",)].built -= 120                  # make it stale
    t = time.time()
    assert warehouse.cached(("k",), build, ttl=60) == 1    # ...but the caller doesn't wait
    assert time.time() - t < 0.5
    gate.set()
    for _ in range(50):
        if warehouse._cache[("k",)].value == 2:
            break
        time.sleep(0.02)
    assert warehouse.cached(("k",), build, ttl=60) == 2


def test_cold_key_is_built_once_for_concurrent_requests():
    calls = []

    def build():
        calls.append(1)
        time.sleep(0.2)
        return "v"

    out = []
    threads = [threading.Thread(target=lambda: out.append(warehouse.cached(("cold",), build)))
               for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out == ["v"] * 5 and len(calls) == 1


def test_very_old_entries_are_rebuilt_while_waiting():
    n = []
    build = lambda: n.append(1) or len(n)
    warehouse.cached(("old",), build, ttl=60)
    warehouse._cache[("old",)].built -= warehouse.MAX_STALE_SEC + 1
    assert warehouse.cached(("old",), build, ttl=60) == 2


def test_refresher_keeps_only_recently_used_entries_warm():
    n = {"a": 0, "b": 0}
    warehouse.cached(("a",), lambda: n.__setitem__("a", n["a"] + 1), ttl=60)
    warehouse.cached(("b",), lambda: n.__setitem__("b", n["b"] + 1), ttl=60)
    now = time.time()
    for k in ("a", "b"):
        warehouse._cache[(k,)].built = now - 59             # about to go stale
    warehouse._cache[("b",)].used = now - warehouse.KEEP_WARM_SEC - 1   # nobody looked lately
    assert warehouse.refresh_due(now) == 1
    assert n == {"a": 2, "b": 1}


def test_a_failed_refresh_keeps_the_old_value():
    warehouse.cached(("f",), lambda: "good", ttl=60)
    e = warehouse._cache[("f",)]
    e.build = lambda: (_ for _ in ()).throw(RuntimeError("cluster down"))
    e.built -= 120
    assert warehouse.refresh_due() == 1
    assert warehouse.cached(("f",), e.build, ttl=60) == "good"


def test_connections_are_capped(monkeypatch):
    """No more than MAX_CONNECTIONS open at once; the rest wait their turn."""
    import os
    monkeypatch.setenv("REPORTING_DB_URL", "postgresql://x")
    live, peak = [0], [0]
    lock = threading.Lock()

    class FakeConn:
        def close(self):
            with lock:
                live[0] -= 1

    def fake_connect(url, connect_timeout):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        return FakeConn()

    monkeypatch.setattr(warehouse.psycopg, "connect", fake_connect)

    def use():
        with warehouse.connect():
            time.sleep(0.05)

    ts = [threading.Thread(target=use) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= warehouse.MAX_CONNECTIONS and live[0] == 0
