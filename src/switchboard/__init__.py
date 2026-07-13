"""Switchboard — a thin orchestration + shared-state layer over the Valnet Auto
portfolio's existing systems (HotCars, CarBuzz, TopSpeed).

Architectural rule #1 (load-bearing): agents coordinate ONLY through shared
memory. They never call each other and never call each other's tools. See
PRD-switchboard.md §5 and §14.
"""

__version__ = "0.1.0"
