"""The governor (PRD §8): the reason an always-on multi-agent loop is safe.

Hard spend caps, dry-run-by-default, the human-approval gate, provenance
enforcement, and the kill switch all live behind this component. It is invoked
by the orchestrator on dispatch and by every action adapter before a live write.
"""

from .governor import Governor

__all__ = ["Governor"]
