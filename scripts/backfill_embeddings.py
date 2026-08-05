"""Direct/session historical embedding backfill (plan task 2.12c).

Dry-run is the default and constructs no provider/key reader.  ``--apply`` is
the sole path that opens the existing OpenAI provider boundary and mutates only
derived run/vector state (after workflow remediation has enriched workflows).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from executors import embedding_backfill as eb
from executors import postgres_backfill as pg
from executors.selected_contract import SELECTED_CONTRACT_ID, SELECTED_DIMENSION

__all__ = ["build_parser", "parse_args", "run", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Defaults to a dry-run."""

    parser = argparse.ArgumentParser(
        prog="backfill_embeddings",
        description="Direct-session, resumable historical embedding backfill.",
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--contract-id", type=int, default=SELECTED_CONTRACT_ID,
                        help="Existing draft/approved contract id; this command never activates it.")
    parser.add_argument("--dimension", type=int, default=SELECTED_DIMENSION)
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply mode (default is a read-only dry-run).",
    )
    parser.add_argument("--resume-run-id", default=None)
    parser.add_argument(
        "--source", action="append", default=[],
        help="Source to backfill (repeatable).",
    )
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--batch-items", type=int, default=100)
    parser.add_argument("--input-items", type=int, default=64)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--rate-limit", type=int, default=None)
    parser.add_argument("--cost-cap", type=float, default=None)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--provider-env-file", default=None,
                        help="Existing approved env-file convention; only OPENAI_API_KEY is read.")
    parser.add_argument("--vibecomfy-root", default=None,
                        help="Explicit authorized root for local corpus_path recovery.")
    parser.add_argument("--remediation-batch-size", type=int, default=100)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse *argv* (defaults to ``sys.argv[1:]``)."""

    return build_parser().parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> eb.BackfillConfig:
    """Map the parsed namespace into a frozen :class:`BackfillConfig`."""

    sources = tuple(args.source) if args.source else ("messages",)
    return eb.BackfillConfig(
        sources=sources,
        apply=bool(args.apply),
        resume_run_id=args.resume_run_id,
        since=args.since,
        until=args.until,
        batch_items=args.batch_items,
        input_items=args.input_items,
        max_tokens_per_request=args.max_tokens,
        max_chars_per_request=args.max_chars,
        rate_limit_per_minute=args.rate_limit,
        cost_cap_usd=args.cost_cap,
        max_retries=args.retries,
        sample=args.sample,
        report_path=args.report_path,
        contract_id=int(args.contract_id),
        dimension=int(args.dimension),
    )


def run(
    args: argparse.Namespace,
    *,
    store: "eb.BackfillStore | None" = None,
    provider: "eb.Provider | None" = None,
    remediation_stage: "eb.RemediationStage | None" = None,
    rate_limiter: "eb.RateLimiter | None" = None,
) -> eb.BackfillReport:
    """Validate direct-session mode, then drive the engine.

    The direct-session URL is validated before any injected collaborator is
    touched. Without injected adapters (a real invocation) this raises a concise
    nonsecret operator error rather than fabricating a production adapter.
    """

    # Validate before touching any collaborator, provider key, or psql.
    eb.validate_direct_database_url(args.database_url)

    config = _config_from_args(args)
    # ``content_embeddings.embedding`` is physically vector(384).  A sibling
    # table migration is required for any other dimension; this command must
    # not accept a value that can only fail after provider work begins.
    if config.dimension != 384:
        raise ValueError("content_embeddings_fixed_dimension")
    if store is None or remediation_stage is None or rate_limiter is None:
        session = pg.PsqlSession(args.database_url)
        store = pg.PostgresBackfillStore(session)
        workflow_store = pg.PostgresWorkflowStore(session)
        root = Path(args.vibecomfy_root).resolve() if args.vibecomfy_root else None
        remediation_stage = pg.WorkflowRemediationStage(
            workflow_store, vibecomfy_root=root,
            batch_size=int(args.remediation_batch_size), sample=config.sample,
        )
        rate_limiter = pg.PacingRateLimiter(config.rate_limit_per_minute)
    if provider is None:
        if config.apply:
            # Reuse the established one-key env-file reader. It returns a value
            # only to this short-lived provider object and never logs it.
            from scripts.smoke_embedding_provider import load_approved_key
            key, _source = load_approved_key(args.provider_env_file)
            provider = pg.OpenAIProvider(dimension=config.dimension, api_key=key)
        else:
            provider = _DryRunProvider()

    report = eb.run_backfill(
        store,
        provider=provider,
        remediation_stage=remediation_stage,
        config=config,
        rate_limiter=rate_limiter,
    )
    if config.report_path:
        _write_report(Path(config.report_path), report.to_log_dict())
    return report


class _DryRunProvider:
    """No-key sentinel: the pure engine never calls it in dry-run mode."""
    model_name = "dry-run"
    dimension = 0

    def embed(self, texts):  # pragma: no cover - safety assertion
        raise AssertionError("dry-run must not invoke provider")


def _write_report(path: Path, report: dict) -> None:
    """Atomically write the engine's already secret-safe report envelope."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".backfill-report-", delete=False) as tmp:
        json.dump(report, tmp, sort_keys=True, indent=2)
        tmp.write("\n")
        temp = Path(tmp.name)
    os.replace(temp, path)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point."""

    args = parse_args(argv)
    report = run(args)
    sys.stdout.write(
        f"backfill status={report.status} mode={report.mode} "
        f"vectors_written={report.vectors_written}\n"
    )
    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
