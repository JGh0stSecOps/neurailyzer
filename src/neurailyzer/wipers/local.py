"""Local adapters: runtime (unload/KV), chat-store, vector-store, sandbox, temp.

Each implements plan()/commit()/verify() per wipers/base.py and honors the
keep-list. See docs/WIPE-TAXONOMY.md for mechanisms + verification.
"""
