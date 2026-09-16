"""Standalone worker process package.

The worker runs the row-claimable loops on **every** instance, serialized by
``FOR UPDATE SKIP LOCKED`` (dispatch, ingest, watchdog), and a single-leader radio
coordinator elected via a Postgres advisory lock (spec #75). This scaffold ships the
process skeleton, heartbeat, and health probe; the loops themselves arrive in later
tickets at the seams marked in :mod:`songforge.worker.main`.
"""
