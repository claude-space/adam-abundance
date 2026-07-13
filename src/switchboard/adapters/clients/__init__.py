"""Shared low-level clients used by adapters. Each isolates one external
protocol/SDK, builds credentials from the credentials layer, and raises
:class:`~switchboard.adapters.base.AdapterUnavailable` when its SDK or credential
is absent (so the wrapping adapter degrades softly)."""
