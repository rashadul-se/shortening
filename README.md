# URL shortener (C++ core + Python + SQLite + Streamlit)

    pip install -r requirements.txt
    bash build.sh                       # compiles core/core.cpp -> libshortcore.so
    python run_cluster.py --workers 4   # 4 workers + balancer on :8080
    streamlit run app.py                # UI (separate terminal)
    python loadtest.py --users 10000    # simulate 10k users

    curl -X POST localhost:8080/api/shorten -d '{"url":"https://python.org"}'
    curl -i localhost:8080/<code>       # -> 302
