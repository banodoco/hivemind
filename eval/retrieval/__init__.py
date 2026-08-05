"""Retrieval evaluation harness — Hivemind port of Pumpernickel's eval structure.

Public API::

    from eval.retrieval.schema import CorpusItem, GoldenCase, GoldenSet, JudgedItem, Query, Result
    from eval.retrieval.adapters import build_adapter, ADAPTERS, LegacyIlikeAdapter
    from eval.retrieval.runner import run_eval
    from eval.retrieval.compare import compare_systems

See ``README.md`` for the one-command usage and ``NOTICE.md`` for attribution.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.5.0"
