"""app.py - Streamlit UI.  Run:  streamlit run app.py
Talks to the same SQLite DB as the cluster (WAL mode allows concurrent access).
Real HTTP redirects are served by the cluster (http://localhost:8080/<code>);
here a ?c=<code> link resolves and shows a button to open the target."""
import time
import streamlit as st
from engine import InvalidUrl, RateLimited, ShortenerService

st.set_page_config(page_title="Short links", page_icon="🔗")

@st.cache_resource
def service() -> ShortenerService:      # one engine per Streamlit process
    return ShortenerService()

svc = service()
client_id = st.session_state.setdefault("cid", f"st-{id(st.session_state)}")
st.title("🔗 URL Shortener")

if code := st.query_params.get("c"):
    url = svc.resolve(code)
    if url:
        st.success("Link found")
        st.link_button("Open destination", url)
        st.caption(url)
    else:
        st.error("Unknown code")

t1, t2, t3 = st.tabs(["Shorten", "Look up", "Dashboard"])

with t1:
    long_url = st.text_input("Long URL", placeholder="https://example.com/very/long/path")
    if st.button("Shorten", type="primary") and long_url:
        try:
            r = svc.shorten(long_url, client_id)
            st.success("Created" if r.created else "Already existed - same code returned")
            st.code(r.short_url)
            st.caption(f"Code: {r.code}")
        except InvalidUrl as e:
            st.error(str(e))
        except RateLimited:
            st.warning("Too many requests - try again in a moment.")

with t2:
    c = st.text_input("Short code", max_chars=7)
    if c:
        s = svc.stats(c)
        if s:
            st.write(s["url"])
            st.metric("Clicks", s["hits"])
            st.link_button("Open", s["url"])
        else:
            st.error("Not found")

with t3:
    m = svc.metrics()
    a, b, d = st.columns(3)
    a.metric("Total URLs", m["total_urls"])
    b.metric("Cache hit ratio", f'{m["hit_ratio"]:.0%}')
    d.metric("Cache entries", m["entries"])
    st.subheader("Recent links")
    st.dataframe(svc.recent(20), use_container_width=True, hide_index=True)
    st.button("Refresh")
