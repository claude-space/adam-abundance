"""Competitor trend pipeline (docs/trend-pipeline.md).

Sourcing adapters (adapters/trend_sources.py) drop normalized signals into
shared memory; this package clusters + scores them into trends, builds
dossiers, and runs the human-gated content pipeline:

  detector.py    pure clustering/scoring (unit-testable, no I/O)
  repo.py        TrendRepo / PipelineRepo — lifecycle + approval invariants
  scout.py       the scan: sources → detect → upsert trends → trigger requests
  dossier.py     deep collection + LLM synthesis for one trend
  generators.py  content generation transports (llm / hc_viral_hits / social_api
                 / newsletter_api / shellagent_run)
  pipeline.py    job engine: generate → preview → approve/regenerate/publish
"""
