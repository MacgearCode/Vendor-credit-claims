"""Read-only access to the reporting warehouse (macgear_reporting on the DO cluster).

REPORTING_DB_URL is the `reporting_read` role — SELECT on the `reporting` views
only. The app never writes to the warehouse; the ETL in `Sell Through/` owns it.

A connection per call rather than a pool: connecting costs ~70 ms against
queries of 0.3–3 s, reports are cached (see `cached`), and reporting_read has an
idle timeout that would kill pooled connections anyway.

Speed comes from the cache, not the connection:
  - a result is served from memory while fresh;
  - once stale it is STILL served instantly while one background thread
    rebuilds it ("stale-while-revalidate") — nobody waits on a rebuild of
    something they have seen before;
  - a refresher thread (start_refresher) rebuilds entries that were used in the
    last couple of hours just before they go stale, and `warm()` builds the
    common views at startup — so the first visit of the day is fast too.
"""
import logging
import os
import threading
import time
from datetime import date

import psycopg

log = logging.getLogger("warehouse")

CACHE_TTL_SEC = 10 * 60          # sell-in / stock: NetSuite lands every 15 min
SELL_THROUGH_TTL = 30 * 60       # retailer files land weekly; 30 min is plenty fresh
KEEP_WARM_SEC = 2 * 60 * 60      # refresh entries someone used within this window
MAX_STALE_SEC = 24 * 60 * 60     # older than this, rebuild while the user waits


class WarehouseUnavailable(RuntimeError):
    """No URL configured, or the cluster could not be reached."""


# The cluster allows ~22 usable connections and Power BI holds many of them, so
# the site never opens more than this many at once; extra requests queue briefly.
MAX_CONNECTIONS = int(os.environ.get("REPORTING_MAX_CONNECTIONS", "4"))
_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


class _Connection:
    """`with connect() as conn:` — takes a slot, opens a connection, and gives
    both back on exit (the connection is closed, never pooled)."""

    def __init__(self):
        self.conn = None

    def __enter__(self):
        url = os.environ.get("REPORTING_DB_URL", "")
        if not url:
            raise WarehouseUnavailable("REPORTING_DB_URL is not set")
        if not _slots.acquire(timeout=60):
            raise WarehouseUnavailable("too many warehouse queries at once — try again shortly")
        try:
            self.conn = psycopg.connect(url, connect_timeout=10)
        except psycopg.OperationalError as e:
            _slots.release()
            raise WarehouseUnavailable(str(e).splitlines()[0]) from e
        return self.conn

    def __exit__(self, *exc):
        try:
            self.conn.close()
        finally:
            _slots.release()
        return False


def connect():
    return _Connection()


def query(sql: str, params=None) -> list[tuple]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---- cache ---------------------------------------------------------------------
class _Entry:
    __slots__ = ("built", "value", "build", "ttl", "used", "refreshing")

    def __init__(self, value, build, ttl):
        self.built = self.used = time.time()
        self.value, self.build, self.ttl = value, build, ttl
        self.refreshing = False


_cache: dict = {}
_lock = threading.Lock()
_building: dict = {}             # key -> Lock, so one cold key is built once, not per request


def _refresh(key, entry):
    try:
        value = entry.build()
        with _lock:
            if _cache.get(key) is entry:
                entry.value, entry.built = value, time.time()
    except Exception as e:       # keep serving the old value; try again next time
        log.warning("cache refresh failed for %s: %s", key[0], e)
    finally:
        entry.refreshing = False


def cached(key, build, ttl: int = CACHE_TTL_SEC):
    """Memoise build() under key. The key must include every input that scopes
    the result (brands, regions, capabilities) — a key that drops one would hand
    one user's data to another.

    Fresh -> served. Stale (but under MAX_STALE_SEC) -> served, and rebuilt in
    the background. Missing -> built now, once even if several requests ask."""
    now = time.time()
    with _lock:
        e = _cache.get(key)
        if e is not None:
            e.used = now
            age = now - e.built
            if age < ttl:
                return e.value
            if age < MAX_STALE_SEC:
                if not e.refreshing:
                    e.refreshing = True
                    threading.Thread(target=_refresh, args=(key, e), daemon=True).start()
                return e.value
        klock = _building.setdefault(key, threading.Lock())
    with klock:
        with _lock:                      # someone else may have built it meanwhile
            e = _cache.get(key)
            if e is not None and time.time() - e.built < ttl:
                e.used = time.time()
                return e.value
        value = build()
        with _lock:
            _cache[key] = _Entry(value, build, ttl)
            _building.pop(key, None)
        return value


def clear_cache():
    with _lock:
        _cache.clear()


def refresh_due(now: float | None = None) -> int:
    """Rebuild, in this thread, entries used recently that are within a minute of
    going stale (or already stale). Returns how many were rebuilt."""
    now = now or time.time()
    with _lock:
        due = [(k, e) for k, e in _cache.items()
               if not e.refreshing and now - e.used < KEEP_WARM_SEC
               and now - e.built > e.ttl - 60]
        for _, e in due:
            e.refreshing = True
    for k, e in due:
        _refresh(k, e)
    return len(due)


def start_refresher(warm=None, every: int = 60):
    """Background thread: run `warm()` once, then keep used entries fresh."""
    def loop():
        if warm:
            try:
                t = time.time()
                warm()
                log.info("warehouse cache warmed in %.1fs", time.time() - t)
            except Exception as e:
                log.warning("cache warm-up failed: %s", e)
        while True:
            time.sleep(every)
            try:
                refresh_due()
            except Exception as e:
                log.warning("cache refresh loop: %s", e)
    threading.Thread(target=loop, daemon=True, name="warehouse-refresher").start()


# ---- small lookups -------------------------------------------------------------
def recent_weeks(n: int, upto=None) -> list:
    """The newest n sell-through week-ending dates (<= upto), oldest first.
    Read off the fact's date_key index (yyyymmdd): through the dimension joins
    the same question took ~10 s. The full list is cached; slicing is free."""
    keys = cached(("week_keys",), lambda: [k for (k,) in query(
        "select distinct date_key from reporting.fact_sell_through order by 1 desc limit 520")],
        ttl=SELL_THROUGH_TTL)
    hi = int(upto.strftime("%Y%m%d")) if upto else 99991231
    picked = [k for k in keys if k <= hi][:n]
    return sorted(date(k // 10000, k // 100 % 100, k % 100) for k in picked)


def region_key(code: str) -> int:
    keys = cached(("region_keys",), lambda: dict(query(
        "select country_code, region_key from reporting.dim_region")), ttl=24 * 60 * 60)
    return keys.get(code, -1)


def brand_names() -> list[str]:
    """Every NetSuite brand (class), for the user form's brand picker."""
    return cached(("brands",), lambda: [r[0] for r in query(
        "select distinct brand from reporting.dim_brand where brand is not null order by 1")])
