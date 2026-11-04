"""loadtest.py - simulate many users against the balancer (stdlib only).
   python loadtest.py --users 10000 --urls 2000 --concurrency 400"""
import argparse, asyncio, json, random, statistics, time

async def req(host, port, method, path, body=None, headers=None):
    r, w = await asyncio.open_connection(host, port)
    h = {"Host": "x", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else b""
    if data: h["Content-Length"] = str(len(data)); h["Content-Type"] = "application/json"
    w.write((f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + data)
    await w.drain()
    raw = await r.read(-1); w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), payload

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--users", type=int, default=10000); ap.add_argument("--urls", type=int, default=2000)
    ap.add_argument("--clicks", type=int, default=3, help="clicks per user")
    ap.add_argument("--concurrency", type=int, default=400)
    a = ap.parse_args()
    sem = asyncio.Semaphore(a.concurrency)
    run = str(int(time.time()))

    async def create(i):
        async with sem:
            s, p = await req(a.host, a.port, "POST", "/api/shorten",
                             {"url": f"https://example.com/{run}/page/{i}"}, {"X-Client-Id": f"u{i}"})
            return json.loads(p)["code"] if s in (200, 201) else None
    t = time.perf_counter()
    codes = [c for c in await asyncio.gather(*[create(i) for i in range(a.urls)]) if c]
    dt = time.perf_counter() - t
    print(f"[create] {len(codes)}/{a.urls} urls in {dt:.2f}s ({len(codes)/dt:.0f}/s)")

    lat, status = [], {}
    async def user(u):
        for _ in range(a.clicks):
            code = codes[int(len(codes) * random.random() ** 3)]   # skewed: hot links
            async with sem:
                t0 = time.perf_counter()
                try: s, _ = await req(a.host, a.port, "GET", f"/{code}")
                except OSError: s = -1
                lat.append((time.perf_counter() - t0) * 1000)
            status[s] = status.get(s, 0) + 1
    t = time.perf_counter()
    await asyncio.gather(*[user(u) for u in range(a.users)])
    dt = time.perf_counter() - t
    lat.sort(); n = len(lat)
    print(f"[redirect] {n} requests from {a.users} users in {dt:.2f}s -> {n/dt:.0f} req/s")
    print(f"  latency ms  p50={lat[n//2]:.1f}  p95={lat[int(n*.95)]:.1f}  p99={lat[int(n*.99)]:.1f}  max={lat[-1]:.1f}")
    print(f"  status codes: {status}")
    s, p = await req(a.host, a.port, "GET", "/healthz"); print("  worker metrics:", p.decode())

asyncio.run(main())
