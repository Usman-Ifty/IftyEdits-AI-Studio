# app/

Streamlit dashboard. Added in Phase 1 together with the `streamlit` dependency.

Rules: the UI is a thin layer. It calls `iftyedits.services` only and holds no business logic.
Run it bound to localhost: `uv run streamlit run app/Home.py --server.address 127.0.0.1`.
