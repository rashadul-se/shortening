"""engine.py - business logic for the URL shortener.

Layers (each hides its internals behind a tiny public API):
    ShortenerService   public facade: shorten / resolve / stats / metrics
      |- _NativeCore   ctypes bridge to the C++ library (codec, cache, limiter)
      |- _Repository   SQLite (WAL, one connection per thread)
      '- _HitBuffer    batches click counters so reads never write to the DB

Callers only ever see ShortenerService. Name-mangled (__x) attributes keep
the cache, DB and limiter unreachable from outside the class.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple, Optional

ROOT = Path(__file__).resolve().parent
DB_PATH = os.environ.get("URLSHORT_DB", str(ROOT / "urls.db"))
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "http://localhost:8080")
_EXT = "dylib" if sys.platform == "darwin" else "so"
LIB_PATH = ROOT / "core" / f"libshortcore.{_EXT}"


class InvalidUrl(ValueError):
    pass


class RateLimited(Exception):
    pass


class ShortenResult(NamedTuple):
    code: str
    short_url: str
    created: bool


# ---------------------------------------------------------------- native bridge
class _NativeCore:
    """Only class that touches ctypes. ctypes drops the GIL during each call,
    so C++ cache/limiter work runs in parallel across threads."""

    _BUF = 4096

    def __init__(self, cache_capacity: int, ttl: int, rate: float, burst: float):
        if not LIB_PATH.exists():
            raise RuntimeError(f"{LIB_PATH} missing - run: bash build.sh")
        L = self._lib = ctypes.CDLL(str(LIB_PATH))
        c_u64p = ctypes.POINTER(ctypes.c_uint64)
        sig = {
            "sc_encode": (None, [ctypes.c_uint64, ctypes.c_char_p]),
            "sc_decode": (ctypes.c_int, [ctypes.c_char_p, c_u64p]),
            "sc_validate_url": (ctypes.c_int, [ctypes.c_char_p]),
            "sc_cache_new": (ctypes.c_void_p, [ctypes.c_size_t, ctypes.c_int]),
            "sc_cache_free": (None, [ctypes.c_void_p]),
            "sc_cache_get": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]),
            "sc_cache_put": (None, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]),
            "sc_cache_del": (None, [ctypes.c_void_p, ctypes.c_char_p]),
            "sc_cache_stats": (None, [ctypes.c_void_p, c_u64p]),
            "sc_rl_new": (ctypes.c_void_p, [ctypes.c_double, ctypes.c_double]),
            "sc_rl_free": (None, [ctypes.c_void_p]),
            "sc_rl_allow": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p]),
        }
        for name, (res, args) in sig.items():
            fn = getattr(L, name)
            fn.restype, fn.argtypes = res, args
        self._cache = L.sc_cache_new(cache_capacity, ttl)
        self._rl = L.sc_rl_new(rate, burst)
        self._tls = threading.local()

    def _buf(self):
        b = getattr(self._tls, "buf", None)
        if b is None:
            b = self._tls.buf = ctypes.create_string_buffer(self._BUF)
        return b

    def encode(self, i: int) -> str:
        out = ctypes.create_string_buffer(8)
        self._lib.sc_encode(i, out)
        return out.value.decode()

    def decode(self, code: str) -> Optional[int]:
        v = ctypes.c_uint64()
        ok = self._lib.sc_decode(code.encode("ascii", "ignore"), ctypes.byref(v))
        return v.value if ok else None

    def valid_url(self, u: str) -> bool:
        try:
            return bool(self._lib.sc_validate_url(u.encode("utf-8")))
        except UnicodeEncodeError:
            return False

    def cache_get(self, k: str) -> Optional[str]:
        b = self._buf()
        n = self._lib.sc_cache_get(self._cache, k.encode(), b, self._BUF)
        return b.value.decode() if n >= 0 else None

    def cache_put(self, k: str, v: str) -> None:
        self._lib.sc_cache_put(self._cache, k.encode(), v.encode())

    def cache_stats(self) -> dict:
        a = (ctypes.c_uint64 * 3)()
        self._lib.sc_cache_stats(self._cache, a)
        tot = a[0] + a[1]
        return {"hits": a[0], "misses": a[1], "entries": a[2],
                "hit_ratio": round(a[0] / tot, 4) if tot else 0.0}

    def allow(self, key: str) -> bool:
        return bool(self._lib.sc_rl_allow(self._rl, key.encode()))

    def close(self):
        self._lib.sc_cache_free(self._cache)
        self._lib.sc_rl_free(self._rl)


# ------------------------------------------------------------------- repository
class _Repository:
    """SQLite in WAL mode: many concurrent readers + one writer at a time.
    One connection per thread (sqlite3 connections are not shareable)."""

    def __init__(self, path: str):
        self._path = path
        self._tls = threading.local()
        c = self._conn()
        c.executescript(
            """CREATE TABLE IF NOT EXISTS urls(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 url_hash BLOB NOT NULL UNIQUE,
                 long_url TEXT NOT NULL,
                 created_at INTEGER NOT NULL,
                 hits INTEGER NOT NULL DEFAULT 0);"""
        )

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._tls, "c", None)
        if c is None:
            c = sqlite3.connect(self._path, timeout=10, isolation_level=None,
                                check_same_thread=False)
            for p in ("busy_timeout=10000", "journal_mode=WAL", "synchronous=NORMAL",
                      "temp_store=MEMORY", "cache_size=-65536", "mmap_size=268435456"):
                c.execute(f"PRAGMA {p}")
            self._tls.c = c
        return c

    def insert_or_get(self, url: str, url_hash: bytes) -> tuple[int, bool]:
        c = self._conn()
        row = c.execute("SELECT id FROM urls WHERE url_hash=?", (url_hash,)).fetchone()
        if row:                                   # fast path: no write lock
            return row[0], False
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT id FROM urls WHERE url_hash=?", (url_hash,)).fetchone()
            if row:
                c.execute("COMMIT")
                return row[0], False
            cur = c.execute("INSERT INTO urls(url_hash,long_url,created_at) VALUES(?,?,?)",
                            (url_hash, url, int(time.time())))
            c.execute("COMMIT")
            return cur.lastrowid, True
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def get_url(self, i: int) -> Optional[str]:
        row = self._conn().execute("SELECT long_url FROM urls WHERE id=?", (i,)).fetchone()
        return row[0] if row else None

    def get_row(self, i: int):
        return self._conn().execute(
            "SELECT long_url, hits, created_at FROM urls WHERE id=?", (i,)).fetchone()

    def add_hits(self, batch: dict) -> None:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany("UPDATE urls SET hits=hits+? WHERE id=?",
                          [(n, i) for i, n in batch.items()])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def recent(self, n: int):
        return self._conn().execute(
            "SELECT id, long_url, hits, created_at FROM urls ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()

    def count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM urls").fetchone()[0]


# -------------------------------------------------------------------- hit buffer
class _HitBuffer:
    """Click counting without a DB write per redirect: counts are merged in
    memory and flushed in one transaction every `interval` seconds."""

    def __init__(self, sink, interval: float = 2.0):
        self._sink, self._interval = sink, interval
        self._lock = threading.Lock()
        self._counts: dict[int, int] = {}
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="hit-flusher").start()

    def add(self, i: int) -> None:
        with self._lock:
            self._counts[i] = self._counts.get(i, 0) + 1

    def flush(self) -> None:
        with self._lock:
            batch, self._counts = self._counts, {}
        if batch:
            try:
                self._sink(batch)
            except sqlite3.Error:
                with self._lock:                  # put back, retry next tick
                    for i, n in batch.items():
                        self._counts[i] = self._counts.get(i, 0) + n

    def _loop(self):
        while not self._stop.wait(self._interval):
            self.flush()

    def close(self):
        self._stop.set()
        self.flush()


# ---------------------------------------------------------------------- facade
class ShortenerService:
    def __init__(self, db_path: str = DB_PATH, cache_capacity: int = 200_000,
                 cache_ttl: int = 3600, rate: float = 20.0, burst: float = 40.0,
                 public_base: str = PUBLIC_BASE):
        self.__base = public_base.rstrip("/")
        self.__core = _NativeCore(cache_capacity, cache_ttl, rate, burst)
        self.__repo = _Repository(db_path)
        self.__hits = _HitBuffer(self.__repo.add_hits)

    # ---- public API -------------------------------------------------------
    def shorten(self, url: str, client_id: str = "anon") -> ShortenResult:
        if not self.__core.allow(client_id):
            raise RateLimited(client_id)
        url = (url or "").strip()
        if not self.__core.valid_url(url):
            raise InvalidUrl("Only http(s) URLs up to 2048 chars are accepted")
        uid, created = self.__repo.insert_or_get(url, hashlib.sha256(url.encode()).digest())
        code = self.__core.encode(uid)
        self.__core.cache_put(code, url)          # warm the cache
        return ShortenResult(code, f"{self.__base}/{code}", created)

    def resolve(self, code: str, count_hit: bool = True) -> Optional[str]:
        uid = self.__core.decode(code)            # rejects junk without touching DB
        if uid is None:
            return None
        url = self.__core.cache_get(code)         # L1: C++ sharded LRU
        if url is None:
            url = self.__repo.get_url(uid)        # L2: SQLite primary-key lookup
            if url is None:
                return None
            self.__core.cache_put(code, url)
        if count_hit:
            self.__hits.add(uid)
        return url

    def stats(self, code: str) -> Optional[dict]:
        uid = self.__core.decode(code)
        row = self.__repo.get_row(uid) if uid else None
        if not row:
            return None
        return {"code": code, "url": row[0], "hits": row[1], "created_at": row[2]}

    def recent(self, n: int = 10) -> list[dict]:
        return [{"code": self.__core.encode(i), "url": u, "hits": h, "created_at": t}
                for i, u, h, t in self.__repo.recent(n)]

    def metrics(self) -> dict:
        return {**self.__core.cache_stats(), "total_urls": self.__repo.count()}

    def close(self) -> None:
        self.__hits.close()
        self.__core.close()
