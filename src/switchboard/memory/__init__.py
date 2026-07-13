"""Shared memory: a queryable store, not a firehose (PRD §7). Agents query for
what they need (by brand, type, freshness); they do not ingest everything."""

from .store import MemoryStore

__all__ = ["MemoryStore"]
