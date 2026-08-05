#!/usr/bin/env python3
"""One-command retrieval comparison.

Runs one or more named systems over the same golden set + corpus and emits both
a machine-readable JSON report and a human-readable Markdown comparison, plus a
per-system JSON for parity with the Pumpernickel layout.

Usage (the completion signal for task 0.5)::

    python3 -m eval.retrieval.compare \\
        --systems legacy,stub,oracle \\
        --corpus eval/retrieval/fixtures/corpus.json \\
        --golden eval/retrieval/fixtures/golden.json \\
        --out-dir eval/retrieval/reports \\
        --name phase0-seed

Defaults point at the shipped seed fixtures and ``eval/retrieval/reports``. With
no ``--systems``, it compares the real legacy adapter against the stub and
oracle fixtures so the command is useful out of the box.

To compare a live system later (tasks 1.11 / 3.10 / 5.9), register or configure
an adapter and add its name to ``--systems``. The ``remote`` adapter is opt-in
(see :class:`eval.retrieval.adapters.RemoteSearchAdapter`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from eval.retrieval.adapters import build_adapter
from eval.retrieval.loader import load_corpus, load_golden_set
from eval.retrieval.runner import SystemReport, run_eval
from eval.retrieval.schema import Corpus, GoldenSet

_HERE = Path(__file__).resolve().parent
_DEFAULT_CORPUS = _HERE / "fixtures" / "corpus.json"
_DEFAULT_GOLDEN = _HERE / "fixtures" / "golden.json"
_DEFAULT_OUT = _HERE / "reports"
_DEFAULT_SYSTEMS = "legacy,stub,oracle"

# Metric rows shown in the overall comparison table, in display order.
_OVERALL_ROWS: list[tuple[str, str]] = [
    ("recall@1", "Recall@1"),
    ("recall@5", "Recall@5"),
    ("recall@10", "Recall@10"),
    ("mrr", "MRR"),
    ("ndcg@10", "nDCG@10"),
    ("map", "MAP"),
]

# Quality / operational rows (rates + counts), in display order.
_COUNT_ROWS: list[tuple[str, str]] = [
    ("zero_result_rate", "zero-result rate"),
    ("failure_rate", "failure rate"),
    ("timeout_rate", "timeout rate"),
    ("error_rate", "error rate"),
    ("no_hit_satisfied_rate", "no-hit satisfied"),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compare_systems(
    systems: Sequence[str],
    corpus: Corpus,
    golden: GoldenSet,
    *,
    ks: tuple[int, ...] = (1, 5, 10),
    timeout_s: float = 5.0,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run every system in *systems* and return a combined comparison dict.

    Each system gets a :class:`SystemReport`. The combined dict carries
    ``systems`` (ordered), per-system reports, and a stable ``generated_at``.
    """
    clock = now or _utc_now_iso
    reports: list[SystemReport] = []
    for name in systems:
        adapter = build_adapter(name, corpus)
        report = run_eval(adapter, golden, ks=ks, timeout_s=timeout_s, now=clock)
        reports.append(report)
    return {
        "generated_at": clock(),
        "ks": list(ks),
        "systems": list(systems),
        "reports": [r.to_dict() for r in reports],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _cell(x: Any, nd: int = 4) -> str:
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _by_system(reports: list[dict[str, Any]], key: str, default: Any = 0.0) -> list[Any]:
    return [r.get(key, default) for r in reports]


def render_markdown(comparison: dict[str, Any]) -> str:
    """Render a human-readable comparison report from a comparison dict."""
    reports = comparison["reports"]
    names = [r["system"] for r in reports]
    lines: list[str] = []
    A = lines.append

    A("# Hivemind Retrieval — System Comparison")
    A("")
    A(f"- Generated: `{comparison['generated_at']}`")
    A(f"- Systems: {', '.join(f'`{n}`' for n in names)}")
    A(f"- ks: {comparison['ks']}")
    A(
        "- Recall/MRR/nDCG are macro-averaged over judged cases (failures count "
        "as 0). No-hit cases feed `no-hit satisfied`, not Recall."
    )
    A("")

    # Overall relevance table.
    A("## Overall relevance")
    A("")
    header = "| Metric | " + " | ".join(names) + " | n |"
    A(header)
    A("|" + "---|" * (len(names) + 2))
    for key, label in _OVERALL_ROWS:
        vals = _by_system([r["overall"] for r in reports], key)
        ns = [r["overall"].get("n", 0) for r in reports]
        # n is the same across systems (judged cases); print the first.
        A(f"| {label} | " + " | ".join(_cell(v) for v in vals) + f" | {ns[0]} |")
    A("")

    # Operational rates + counts.
    A("## Operational metrics")
    A("")
    A("| Metric | " + " | ".join(names) + " |")
    A("|" + "---|" * (len(names) + 1))
    for key, label in _COUNT_ROWS:
        vals = _by_system([r["counts"] for r in reports], key)
        A(f"| {label} | " + " | ".join(_cell(v) for v in vals) + " |")
    counts_keys = [
        "n_total",
        "n_judged",
        "n_no_hit",
        "zero_result",
        "timeouts",
        "errors",
        "failures",
    ]
    A("| Count | " + " | ".join(names) + " |")
    A("|" + "---|" * (len(names) + 1))
    for ck in counts_keys:
        vals = [r["counts"].get(ck, 0) for r in reports]
        A(f"| {ck} | " + " | ".join(str(v) for v in vals) + " |")
    A("")

    # Latency.
    A("## Latency (ms, successful calls only)")
    A("")
    A("| Statistic | " + " | ".join(names) + " |")
    A("|" + "---|" * (len(names) + 1))
    for key in ("p50_ms", "p95_ms", "p99_ms", "mean_ms"):
        vals = _by_system([r["latency"] for r in reports], key)
        A(f"| {key} | " + " | ".join(_cell(v, 3) for v in vals) + " |")
    A("")

    # Per-category recall@10 / nDCG@10 (the plan's exact_name / workflow_code etc.).
    all_cats: set[str] = set()
    for r in reports:
        all_cats.update(r.get("by_category", {}).keys())
    if all_cats:
        A("## Per category — Recall@10 / nDCG@10 / MRR")
        A("")
        A("| Category | " + " | ".join(f"{n} R@10 / nDCG / MRR" for n in names) + " |")
        A("|" + "---|" * (len(names) + 1))
        for cat in sorted(all_cats):
            cells: list[str] = []
            for r in reports:
                bucket = r.get("by_category", {}).get(cat, {})
                cells.append(
                    f"{_cell(bucket.get('recall@10', 0.0))} / "
                    f"{_cell(bucket.get('ndcg@10', 0.0))} / "
                    f"{_cell(bucket.get('mrr', 0.0))}"
                )
            A(f"| {cat} | " + " | ".join(cells) + " |")
        A("")

    # Per-query diagnostics: show every judged case id with each system's outcome.
    A("## Per-query diagnostics")
    A("")
    # Use the first report's per_case ordering (all systems share case ids).
    case_ids = [c["case_id"] for c in reports[0]["per_case"]]
    A("| Case | Query | " + " | ".join(f"{n} top1" for n in names) + " | "
      + " | ".join(f"{n} R@10" for n in names) + " | outcome |")
    A("|" + "---|" * (1 + 1 + len(names) + len(names) + 1))
    by_sys_case = {
        r["system"]: {c["case_id"]: c for c in r["per_case"]} for r in reports
    }
    for cid in case_ids:
        query = by_sys_case[names[0]][cid]["query"]
        tops = []
        for n in names:
            ranked = by_sys_case[n][cid].get("ranked", [])
            tops.append(ranked[0][1] if ranked else "—")
        r10s = [
            _cell(by_sys_case[n][cid].get("recall_at_10", "—"))
            for n in names
        ]
        # outcome column: join per-system outcomes.
        outcomes = "/".join(by_sys_case[n][cid]["outcome"][:1].upper() for n in names)
        A(
            f"| {cid} | {query} | " + " | ".join(tops) + " | "
            + " | ".join(r10s) + f" | {outcomes} |"
        )
    A("")

    A("<!-- Generated by eval.retrieval.compare. Ported structure (Pumpernickel) "
      "with Hivemind-owned metrics/adapters; see NOTICE.md. -->")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_ks(raw: str) -> tuple[int, ...]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ks = tuple(int(p) for p in parts)
    if not ks:
        raise SystemExit("--ks must list at least one cutoff, e.g. 1,5,10")
    return ks


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval.retrieval.compare",
        description="Compare retrieval systems against a graded golden set.",
    )
    p.add_argument(
        "--systems",
        default=_DEFAULT_SYSTEMS,
        help=f"Comma-separated adapter names (default: {_DEFAULT_SYSTEMS}).",
    )
    p.add_argument("--corpus", type=Path, default=_DEFAULT_CORPUS, help="Corpus file.")
    p.add_argument("--golden", type=Path, default=_DEFAULT_GOLDEN, help="Golden-set file.")
    p.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT, help="Output directory.")
    p.add_argument("--ks", default="1,5,10", help="Recall cutoffs (default: 1,5,10).")
    p.add_argument("--name", default="compare", help="Comparison name (filename stem).")
    p.add_argument(
        "--generated-at",
        default=None,
        help="Override generated_at (deterministic mode for tests).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-call latency budget in seconds (default: 5.0).",
    )
    p.add_argument(
        "--list-adapters",
        action="store_true",
        help="Print registered adapter names and exit.",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_adapters:
        from eval.retrieval.adapters import available_adapters

        for name in available_adapters():
            print(name)
        return 0

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    ks = _parse_ks(args.ks)
    corpus = load_corpus(args.corpus)
    golden = load_golden_set(args.golden)

    clock: Callable[[], str] = (lambda: args.generated_at) if args.generated_at else _utc_now_iso
    comparison = compare_systems(
        systems, corpus, golden, ks=ks, timeout_s=args.timeout, now=clock
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-system JSON (Pumpernickel-compatible layout).
    for report in comparison["reports"]:
        (out_dir / f"{report['system']}_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # Combined JSON + Markdown.
    json_path = out_dir / f"comparison_{args.name}.json"
    md_path = out_dir / f"comparison_{args.name}.md"
    json_path.write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_text = render_markdown(comparison)
    md_path.write_text(md_text + "\n", encoding="utf-8")

    # Echo the headline sections to stdout for a quick terminal comparison. Stop
    # before the per-category / per-query detail, which lives in the MD file.
    md_lines = md_text.splitlines()
    cutoff = len(md_lines)
    for idx, line in enumerate(md_lines):
        if line.startswith("## Per category"):
            cutoff = idx
            break
    print("\n".join(md_lines[:cutoff]).rstrip())

    print(f"\nReports written to {out_dir.resolve()}", file=sys.stderr)
    print(f"  JSON: {json_path.name}", file=sys.stderr)
    print(f"  MD:   {md_path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
