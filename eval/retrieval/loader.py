"""Load and validate corpus / golden-set data files.

Data files may be **JSON** (stdlib, always supported) or **YAML** (used when
PyYAML is importable). The shipped fixtures are JSON so the offline test suite
has zero third-party dependencies; human curators (task 0.6) may author the real
100-query golden set in YAML if they prefer comments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.retrieval.schema import Corpus, CorpusItem, GoldenCase, GoldenSet

#: Set to True (e.g. in tests) to also validate that every judged item and every
#: corpus cross-reference resolves against the loaded corpus. Off by default
#: because the golden set is allowed to reference live item ids that are not all
#: materialised in a small fixture corpus.
STRICT_REFERENCES: bool = False


def _read_data(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ImportError(
                f"reading {path} requires PyYAML; install it or use a .json file"
            ) from exc
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    # Default: JSON (stdlib).
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _records(data: Any, key: str | None) -> list[dict[str, Any]]:
    """Accept either a bare list or a ``{key: [...]}`` mapping."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and key and isinstance(data.get(key), list):
        return data[key]
    if isinstance(data, dict) and not key:
        # Allow a dict whose single value is a list.
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    hint = f'(or {{"{key}": [...]}})' if key else ""
    raise ValueError(
        f"expected a list of records {hint}, "
        f"got {type(data).__name__}"
    )


def load_corpus(path: str | Path) -> Corpus:
    """Load and validate a corpus file into a :class:`Corpus`.

    Accepts a JSON or YAML file containing either a list of ``unified_feed``-
    shaped rows or ``{"items": [...]}``.
    """
    records = _records(_read_data(Path(path)), "items")
    items = [CorpusItem.from_dict(r) for r in records]
    return Corpus(items=items)


def load_golden_set(path: str | Path, *, corpus: Corpus | None = None) -> GoldenSet:
    """Load and validate a golden-set file into a :class:`GoldenSet`.

    Accepts a JSON or YAML file containing either a list of case dicts or
    ``{"cases": [...]}``. When *corpus* is given and :data:`STRICT_REFERENCES`
    is set, judged items must resolve against the corpus.
    """
    records = _records(_read_data(Path(path)), "cases")
    golden = GoldenSet.from_records(records)

    if STRICT_REFERENCES and corpus is not None:
        corpus_keys = corpus.keys()
        for case in golden.cases:
            for j in case.expected:
                if j.key() not in corpus_keys:
                    raise ValueError(
                        f"GoldenCase {case.id!r} judges item {j.key()} not in corpus"
                    )
    return golden
