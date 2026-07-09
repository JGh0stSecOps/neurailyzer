"""Core orchestration: snapshot -> wipe -> verify -> report.

Enforces the invariants individual wipers don't: snapshot-before-commit and the
keep-list.
"""
