"""balancer.py - asyncio TCP load balancer (least-connections, round-robin
tie-break, passive health checks with 5s cool-down for failed backends)."""
import argparse
import asyncio
import time


class Backend:
    def __init__(self, idx, host, port):
        self.idx, self.addr = idx, (host, port)
        self.active = 0
        self.down_until = 0.0


class Balancer:
    def __init__(self, backends):
        self._b = [Backend(i, h, p) for i, (h, p) in enumerate(backends)]
        self._rr = 0

    def _order(self):
        now, n = time.monotonic(), len(self._b)
        self._rr = (self._rr + 1) % n
        up = [b for b in self._b if b.down_until <= now] or self._b
        return sorted(up, key=lambda b: (b.active, (b.idx - self._rr) % n))

    @staticmethod
    async def _pipe(r, w):
        try:
            while chunk := await r.read(65536):
                w.write(chunk)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def handle(self, cr, cw):
        try:
            for b in self._order():
                try:
                    br, bw = await asyncio.wait_for(asyncio.open_connection(*b.addr), 1.0)
                except (OSError, asyncio.TimeoutError):
                    b.down_until = time.monotonic() + 5
                    continue
                b.active += 1
                up = asyncio.create_task(self._pipe(cr, bw))
                try:
                    await self._pipe(br, cw)      # backend closes when response is done
                finally:
                    up.cancel()
                    b.active -= 1
                    bw.close()
                return
            cw.write(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            await cw.drain()
        except ConnectionError:
            pass
        finally:
            cw.close()


async def serve(port, backends):
    bal = Balancer(backends)
    srv = await asyncio.start_server(bal.handle, "0.0.0.0", port, backlog=4096)
    print(f"balancer on :{port} -> {[f'{h}:{p}' for h, p in backends]}", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--backends", default="127.0.0.1:9001,127.0.0.1:9002")
    a = ap.parse_args()
    be = [(h, int(p)) for h, p in (x.split(":") for x in a.backends.split(","))]
    try:
        asyncio.run(serve(a.port, be))
    except KeyboardInterrupt:
        pass
