#!/usr/bin/env python3
"""Operator CLI for direct/session workflow representation remediation.

The command defaults to an audit.  Its real path uses the same direct ``psql``
session adapter as embedding backfill and the existing VibeComfy converter;
stored Python is never executed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors import workflow_remediation as remediation  # noqa: E402
from executors import postgres_backfill as pg  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Patch rows; default is a read-only dry-run audit.",
    )
    parser.add_argument("--database-url", default=None,
                        help="Required for a real direct/session PostgreSQL invocation.")
    parser.add_argument("--report-path", default=None)
    parser.add_argument(
        "--resume-run-id",
        default=None,
        help="Resume a prior interrupted run by its recorded run id.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=remediation.DEFAULT_BATCH_SIZE,
        help="Rows fetched per page (bounded stable paging).",
    )
    parser.add_argument(
        "--vibecomfy-root",
        type=Path,
        default=None,
        help="Authorized VibeComfy checkout root for corpus_path recovery.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Stop after N rows (audit sampling).",
    )
    return parser


def run(
    ns: argparse.Namespace,
    *,
    store: Any = None,
    recoverer: Any = None,
) -> dict[str, Any]:
    """Run the reconciliation, wiring the injected store/recoverer.

    Fails safely until the real Supabase store / VibeComfy adapter is supplied
    by the integration slice — no invented generator or unsafe local reader.
    """
    if store is None:
        if not ns.database_url:
            raise RuntimeError("database_url_required")
        session = pg.PsqlSession(ns.database_url)
        store = pg.PostgresWorkflowStore(session)
    if recoverer is None:
        root = ns.vibecomfy_root.resolve() if ns.vibecomfy_root else None
        recoverer = pg.vibecomfy_recoverer(root)
    report = remediation.reconcile(
        store,
        recoverer=recoverer,
        vibecomfy_root=ns.vibecomfy_root,
        apply=ns.apply,
        batch_size=ns.batch_size,
        sample=ns.sample,
        run_id=ns.resume_run_id,
    )
    if ns.report_path:
        _write_report(Path(ns.report_path), report)
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Atomic report write; the pure engine report has no source bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".workflow-remediation-", delete=False) as tmp:
        json.dump(report, tmp, sort_keys=True, indent=2)
        tmp.write("\n")
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        run(ns)
    except Exception as exc:  # noqa: BLE001
        print(
            "backfill_workflow_representations: unavailable "
            f"({type(exc).__name__}); supply the real store/adapter."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
