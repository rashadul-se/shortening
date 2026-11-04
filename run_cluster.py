"""run_cluster.py - build C++ core, start N workers + load balancer.
   python run_cluster.py --workers 4 --port 8080"""
import argparse, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--base-port", type=int, default=9001)
    a = ap.parse_args()
    if not list((ROOT / "core").glob("libshortcore.*")):
        subprocess.check_call(["bash", str(ROOT / "build.sh")])
    from engine import _Repository, DB_PATH
    _Repository(DB_PATH)          # create schema + WAL once, before workers race
    env = {**os.environ, "PUBLIC_BASE": f"http://localhost:{a.port}"}
    procs = [subprocess.Popen([sys.executable, "server.py", "--port", str(a.base_port + i)],
                              cwd=ROOT, env=env) for i in range(a.workers)]
    backends = ",".join(f"127.0.0.1:{a.base_port + i}" for i in range(a.workers))
    procs.append(subprocess.Popen([sys.executable, "balancer.py", "--port", str(a.port),
                                   "--backends", backends], cwd=ROOT))
    def stop(*_):
        for p in procs: p.terminate()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    print(f"\nReady: http://localhost:{a.port}   (Ctrl+C to stop)\n", flush=True)
    while True:
        time.sleep(1)
        for p in procs:
            if p.poll() is not None:
                print("a process died, shutting down"); stop()

if __name__ == "__main__":
    main()
