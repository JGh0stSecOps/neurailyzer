"""Remote wipers: provider-side stored state, where the API allows.

Least-privilege, per-provider tokens read from env/keyring only — never a broad
admin token, never logged. Dry-run enumerates remote IDs without deleting.
"""
