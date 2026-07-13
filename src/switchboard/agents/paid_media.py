"""Paid-Media agent (PRD §6.7): spend + return for the marketplace campaigns.
Runs read-only adapters (Ads platforms, Sentinel events, lead feeds, mp-spend
RAW_DATA sheet); every entry is tagged ``domain:'paid_media'``. Never adjusts a
bid, budget, or campaign."""

from __future__ import annotations

from .base import BaseAgent


class PaidMediaAgent(BaseAgent):
    name = "paid_media"
