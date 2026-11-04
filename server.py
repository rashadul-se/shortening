"""server.py - one worker process. Run several behind balancer.py.

  GET  /<code>            302 redirect (Cache-Control: private, max-age=60)
  POST /api/shorten       {"url": "..."} -> {"code", "short_url", "created"}
  GET  /api/stats/<code>  click stats (eventually consistent, flushed every 2s)
  GET  /healthz           liveness + cache metrics
Bounded thread pool (not thread-per-request) keeps memory flat under load.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

from engine import InvalidUrl, RateLimited, ShortenerService

SERVICE: ShortenerService = None  # set in main()


class PooledHTTPServer(HTTPServer):
    request_queue_size = 2048

    def __init__(self, addr, handler, workers):
        super().__init__(addr, handler)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="req")

    def process_request(self, request, client_address):
        self._pool.submit(self._run, request, client_address)

    def _run(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class Handler(BaseHTTPRequestHandler):
    server_version = "Shortener/1.0"

    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", ctype="application/json", **headers):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._send(200, {"ok": True, **SERVICE.metrics()}, Cache_Control="no-store")
        if path.startswith("/api/stats/"):
            s = SERVICE.stats(path[len("/api/stats/"):])
            return self._send(200 if s else 404, s or {"error": "not found"}, Cache_Control="no-store")
        code = path.lstrip("/")
        if not code:
            return self._send(200, "URL shortener. POST /api/shorten {\"url\": ...}\n",
                              "text/plain", Cache_Control="public, max-age=300")
        url = SERVICE.resolve(code)
        if url:
            # 302 (not 301) so clicks can still be counted; private cache for 60s
            # lets the browser skip repeat trips while limiting staleness.
            return self._send(302, b"", Location=url, Cache_Control="private, max-age=60")
        self._send(404, {"error": "not found"}, Cache_Control="no-store")

    do_HEAD = do_GET

    def do_POST(self):
        if self.path != "/api/shorten":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 8192:
                raise ValueError
            url = json.loads(self.rfile.read(n))["url"]
        except Exception:
            return self._send(400, {"error": "body must be JSON {\"url\": \"...\"}"})
        client = self.headers.get("X-Client-Id") or self.client_address[0]
        try:
            r = SERVICE.shorten(url, client)
        except InvalidUrl as e:
            return self._send(400, {"error": str(e)})
        except RateLimited:
            return self._send(429, {"error": "slow down"}, Retry_After="1", Cache_Control="no-store")
        self._send(201 if r.created else 200, r._asdict(), Cache_Control="no-store")


def main():
    global SERVICE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--workers", type=int, default=64, help="request threads")
    ap.add_argument("--cache", type=int, default=200_000, help="cache entries")
    ap.add_argument("--ttl", type=int, default=3600, help="cache TTL seconds")
    a = ap.parse_args()
    SERVICE = ShortenerService(cache_capacity=a.cache, cache_ttl=a.ttl)
    srv = PooledHTTPServer(("0.0.0.0", a.port), Handler, a.workers)
    print(f"worker listening on :{a.port} ({a.workers} threads)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE.close()


if __name__ == "__main__":
    main()
