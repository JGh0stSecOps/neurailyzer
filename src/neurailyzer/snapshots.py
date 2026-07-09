"""Point-in-time snapshots: take / list / restore --to T.

Content-addressed store kept OUTSIDE the wipe targets, with its own retention so
the snapshots don't become the storage creep. See docs/DESIGN.md#6.
"""
