"""Tool adapters — the integration plane. Each wraps one existing system or
external API behind a read adapter (``observe`` → typed memory entries) and/or an
action adapter (``act`` → a governor-gated side effect). Adapters are the only
place external I/O happens; agents never touch the network or filesystem directly
(PRD §3, §4)."""

from .base import AdapterUnavailable, BaseAdapter

__all__ = ["BaseAdapter", "AdapterUnavailable"]
