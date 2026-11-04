#!/usr/bin/env bash
# Replaces run_cluster.py on Render (that script forces PUBLIC_BASE=localhost,
# which would make short_url wrong). Same pieces, started directly.
set -e
cd "$(dirname "$0")"
export PORT="${PORT:-10000}"
export PUBLIC_BASE="${PUBLIC_BASE:-${RENDER_EXTERNAL_URL:-http://localhost:$PORT}}"
WORKERS="${WORKERS:-2}"

python -c "from engine import _Repository, DB_PATH; _Repository(DB_PATH)"

BACKENDS=""
for i in $(seq 0 $((WORKERS-1))); do
  python server.py --port $((9001+i)) --workers 32 --cache 50000 &
  BACKENDS="$BACKENDS,127.0.0.1:$((9001+i))"
done
python balancer.py --port 8080 --backends "${BACKENDS#,}" &
streamlit run app.py --server.port 8501 --server.address 127.0.0.1 \
  --server.headless true --browser.gatherUsageStats false &
envsubst '${PORT}' < nginx.conf.template > /tmp/nginx.conf
nginx -c /tmp/nginx.conf -g 'daemon off;' &

wait -n          # any process dies -> exit so Render restarts the service
exit 1
