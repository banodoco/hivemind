#!/usr/bin/env python3
"""Hivemind golden-set curation probe (hybrid-search plan, task 0.6).

Read-only, bounded discovery of **real** corpus identities so the production
golden set can be judged against the live public corpus rather than invented.

What it does (all network is read-only GET with the publishable anon key, exactly
what an installed client does on every search — no writes, no schema/function/
secret/corpus mutation):

  * lists every public distillation (id, status, confidence, question) and its
    cites, so distillation/cross-source/best-is-distillation cases reference real
    rows;
  * lists workflow resources (id, title, source, created_at, python-cohort flag
    via ``metadata.workflow_semantics.promotion_gates``) and validates that a set
    of candidate **exact names** and **Python symbols** actually appear in real
    workflow bodies (via bounded ``ilike`` that returns id+title only — never the
    matching code);
  * captures a bounded, rate-conscious sample of real messages (snowflake id,
    author, channel, created_at, redacted short snippet) for message/author/
    channel/time/settings cases;
  * captures real channel names and author display names for filter cases;
  * records an evidence timestamp and a bounded title/snippet/hash per item for
    future drift review.

Security (from the plan's security section and the task brief):

  * **No full bodies and no ``payload.python_source``** are ever fetched or stored.
    Workflow code evidence is validated by *existence* (``ilike`` → id only) and
    recorded as a benign symbol list; suspect Python is never turned into a
    snippet (quarantined by omission). Message/distillation text is truncated to a
    short, redacted snippet.
  * **Every** line of human-facing output is routed through
    :func:`verify_access.redact` (reused from task 0.1 — same safety boundary as
    the baseline tool).
  * The anon key is never written to outputs; only a masked prefix is recorded.

Outputs (under ``eval/retrieval/golden/`` by default):

  * ``evidence-v1.json``   — the bounded live-identity snapshot (the validator's
    drift anchor): distillation list, workflow id→meta map, symbol→workflow map,
    exact-name→workflow map, message/channel/author samples, validation summary.
  * ``curation-suggestions-v1.json`` — per candidate query, the matched real
    ``(kind, item_id, title)`` tuples, to help a human/curator assign grades.

Run::

    python3 scripts/golden_probe.py --dry-run                 # show the plan, no network
    python3 scripts/golden_probe.py                            # live discovery → evidence files
    python3 scripts/golden_probe.py --no-messages              # skip the slow message arm
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import verify_access as va  # noqa: E402
from executors._common import resolve_anon_key, resolve_endpoint  # noqa: E402

redact = va.redact

TOOL_VERSION = "golden-probe/0.6.0"
EVIDENCE_VERSION = "evidence/2026-07-28"
GOLDEN_DIR = REPO_ROOT / "eval" / "retrieval" / "golden"

SNIPPET_LEN = 160          # max chars of body text kept in evidence (short + redacted)
TITLE_LEN = 140            # max chars of title kept
MSG_SAMPLE_PER_TERM = 12   # messages captured per discovery term (bounded)
MSG_TERMS = ("upscale", "lora", "controlnet", "sampler", "VAE", "batch", "mask")
DEFAULT_TIMEOUT = 30.0     # matches executors/_common._http_get
MSG_TIMEOUT = 25.0         # message ilike can be slow; bounded
DEFAULT_SLEEP = 0.35       # seconds between network requests (rate-conscious)
MAX_RETRIES = 2

# Candidate exact names (model/family/filename/punctuation forms) to validate.
# Grounded in the live distillations + the corpus's ComfyUI/VibeComfy vocabulary.
EXACT_NAMES = [
    "FLUX.1", "FLUX.1 dev", "Flux 2", "Wan 2.2", "Wan 2.1", "Wan2.2",
    "Qwen", "Qwen Image", "Seedance", "Seedance 2", "LightX2V", "LTX2",
    "LTX-Video", "VACE", "Bernini", "Hunyuan", "CogVideoX", "Mochi",
    "SDXL", "Stable Diffusion", "Cascade", ".safetensors", ".gguf",
]

# Candidate Python symbols / node classes / imports to validate against workflow bodies.
# These are public ComfyUI API identifiers; matching validates that real workflow
# Python carries them. The matching *line* is never stored.
PY_SYMBOLS = [
    "WanVideoSampler", "KSampler", "VAEEncode", "VAEDecode", "LoadImage",
    "CheckpointLoaderSimple", "CLIPTextEncode", "LoraLoader", "EmptyLatentImage",
    "ModelSamplingDiscrete", "ControlNetApply", "from comfy", "import comfy",
    "from nodes", "def ", "class ",
]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested offline; no network)
# ---------------------------------------------------------------------------


def short_hash(text: str) -> str:
    """Stable, non-reversible 12-hex short hash of *text* (drift anchor)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def bound_snippet(text: Any, n: int = SNIPPET_LEN) -> str:
    """Collapse whitespace and truncate to *n* chars (no full bodies)."""
    if not isinstance(text, str) or not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= n:
        return collapsed
    return collapsed[:n].rstrip() + "…"


def safe_symbols(matched: list[str]) -> list[str]:
    """Keep only benign, public identifier symbols for evidence.

    Anything that looks like a long opaque token (possible secret / huge literal)
    is dropped so the evidence never persists suspect material.
    """
    out: list[str] = []
    for s in matched:
        if not isinstance(s, str) or not s.strip():
            continue
        st = s.strip()
        # Only keep short, identifier-like symbols (letters/digits/_/./space).
        if len(st) > 64:
            continue
        if any(ch.isalpha() for ch in st):
            out.append(st)
    # de-dup, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def endpoint_ref(endpoint: str) -> str | None:
    import re

    m = re.search(r"https://([a-z0-9]{20})\.supabase\.co", endpoint)
    return m.group(1) if m else None


def mask_key(anon_key: str) -> str:
    if not anon_key:
        return "<unset>"
    if len(anon_key) <= 16:
        return f"{anon_key[:4]}…({len(anon_key)} chars)"
    return f"{anon_key[:14]}…({len(anon_key)} chars, last4=…{anon_key[-4:]})"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def identity_key(kind: str | None, item_id: Any) -> tuple[str, str]:
    return (str(kind or ""), str(item_id or ""))


# ---------------------------------------------------------------------------
# Network (read-only GET; retry + rate-conscious)
# ---------------------------------------------------------------------------


class ProbeError(Exception):
    """Raised when a discovery query fails terminally."""


def _get(path: str, params: dict[str, str], *, endpoint: str, anon_key: str,
         timeout: float, sleep: float) -> tuple[list[dict[str, Any]], float]:
    """Bounded GET returning (rows, latency_ms). Retries transient failures."""
    url = f"{endpoint.rstrip('/')}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"apikey": anon_key, "Accept": "application/json"})
    last_exc: BaseException | None = None
    t0 = time.perf_counter()
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            parsed = json.loads(body)
            rows = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
            return rows, (time.perf_counter() - t0) * 1000.0
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 4xx won't fix on retry; stop.
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise ProbeError(f"HTTP {exc.code} on {path}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_exc = exc
        if attempt < MAX_RETRIES and sleep > 0:
            time.sleep(sleep * (attempt + 1))
    raise ProbeError(f"{type(last_exc).__name__ if last_exc else 'error'} on {path}")


def _paged(path: str, select: str, *, endpoint: str, anon_key: str, order: str,
           page_size: int, max_pages: int, extra: dict[str, str] | None = None,
           timeout: float, sleep: float) -> tuple[list[dict[str, Any]], float]:
    """Page a table by id.asc until empty or max_pages; returns (rows, total_ms)."""
    rows: list[dict[str, Any]] = []
    total_ms = 0.0
    offset = 0
    extra = extra or {}
    for _ in range(max_pages):
        params = {"select": select, "order": order, "limit": str(page_size),
                  "offset": str(offset), **extra}
        page, ms = _get(path, params, endpoint=endpoint, anon_key=anon_key,
                        timeout=timeout, sleep=sleep)
        total_ms += ms
        if not page:
            break
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        if sleep > 0:
            time.sleep(sleep)
    return rows, total_ms


# ---------------------------------------------------------------------------
# Discovery arms (each bounded; redacted on the way out)
# ---------------------------------------------------------------------------


def discover_distillations(*, endpoint: str, anon_key: str, timeout: float, sleep: float) -> dict[str, Any]:
    rows, ms = _get(
        "distillations",
        {"select": "id,question,conditions,answer,status,confidence,created_at,author_id,approved_by",
         "order": "id.asc", "limit": "100"},
        endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep,
    )
    cites, _ = _get(
        "distillation_cites",
        {"select": "distillation_id,item_kind,item_id", "order": "distillation_id.asc", "limit": "500"},
        endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep,
    )
    by_dist: dict[str, list[dict[str, Any]]] = {}
    for c in cites:
        by_dist.setdefault(str(c.get("distillation_id")), []).append(
            {"item_kind": c.get("item_kind"), "item_id": str(c.get("item_id"))}
        )
    items = []
    for d in rows:
        items.append({
            "kind": "distillation",
            "source": "hivemind",
            "item_id": str(d["id"]),
            "question": redact(bound_snippet(d.get("question"), 200)),
            "answer_snippet": redact(bound_snippet(d.get("answer"), SNIPPET_LEN)),
            "conditions_snippet": redact(bound_snippet(d.get("conditions"), SNIPPET_LEN)),
            "status": d.get("status"),
            "confidence": d.get("confidence"),
            "created_at": d.get("created_at"),
            "cites": by_dist.get(str(d["id"]), []),
        })
    return {"items": items, "n": len(items), "latency_ms": round(ms, 1)}


def discover_workflows(*, endpoint: str, anon_key: str, timeout: float, sleep: float) -> dict[str, Any]:
    """All kind=workflow rows: id, title, source, created_at, python-cohort flag.

    ``metadata.workflow_semantics.promotion_gates.has_python_source`` is the
    observable python-cohort signal (no payload fetched). Light projection only.
    """
    rows, ms = _paged(
        "external_resources",
        "id,title,source,created_at,metadata",
        endpoint=endpoint, anon_key=anon_key, order="id.asc",
        page_size=1000, max_pages=6,
        extra={"kind": "eq.workflow"},
        timeout=timeout, sleep=sleep,
    )
    items = []
    longest: list[dict[str, Any]] = []
    for r in rows:
        md = r.get("metadata") or {}
        sem = md.get("workflow_semantics") if isinstance(md, dict) else None
        gates = sem.get("promotion_gates", {}) if isinstance(sem, dict) else {}
        ev = sem.get("evidence", {}) if isinstance(sem, dict) else {}
        models = sem.get("models", []) if isinstance(sem, dict) else []
        node_types = sem.get("node_types", []) if isinstance(sem, dict) else []
        aliases = sem.get("searchable_aliases", []) if isinstance(sem, dict) else []
        title = redact(bound_snippet(r.get("title"), TITLE_LEN))
        rec = {
            "kind": "workflow",
            "source": r.get("source") or "vibecomfy-external",
            "item_id": str(r["id"]),
            "title": title,
            "title_hash": short_hash(r.get("title") or ""),
            "created_at": r.get("created_at"),
            "has_python_source": bool(gates.get("has_python_source")),
            "has_workflow_json": bool(gates.get("has_workflow_json")),
            "has_compiled_api": bool(gates.get("has_compiled_api")),
            "models": safe_symbols(models)[:8],
            "node_types": safe_symbols(node_types)[:8],
            "aliases": safe_symbols(aliases)[:8],
            "confidence": ev.get("confidence") if isinstance(ev, dict) else None,
        }
        items.append(rec)
    python_cohort = [i for i in items if i["has_python_source"]]
    return {
        "items": items,
        "n": len(items),
        "n_python_cohort": len(python_cohort),
        "python_cohort_ids": [i["item_id"] for i in python_cohort],
        "latency_ms": round(ms, 1),
    }


def discover_symbol_map(workflows: list[dict[str, Any]], *, endpoint: str, anon_key: str,
                        timeout: float, sleep: float, symbols: list[str]) -> dict[str, Any]:
    """For each symbol, the real workflow ids whose body contains it (ilike→id only)."""
    out: dict[str, list[dict[str, Any]]] = {}
    meta = {"queries": 0, "latency_ms": 0.0, "terms": symbols}
    by_id = {w["item_id"]: w for w in workflows}
    for sym in symbols:
        params = {"kind": "eq.workflow", "select": "id,title",
                  "or": f"(body.ilike.*{urllib.parse.quote(sym)}*)", "limit": "30", "order": "id.asc"}
        try:
            rows, ms = _get("external_resources", params, endpoint=endpoint, anon_key=anon_key,
                            timeout=timeout, sleep=sleep)
        except ProbeError as exc:
            out[sym] = [{"error": redact(str(exc))}]
            continue
        meta["queries"] += 1
        meta["latency_ms"] += ms
        hits = []
        for r in rows:
            wid = str(r["id"])
            w = by_id.get(wid, {})
            hits.append({"item_id": wid, "title": redact(bound_snippet(r.get("title"), TITLE_LEN)),
                         "has_python_source": w.get("has_python_source")})
        out[sym] = hits
        if sleep > 0:
            time.sleep(sleep)
    meta["latency_ms"] = round(meta["latency_ms"], 1)
    return {"map": out, "meta": meta}


def discover_exact_names(workflows: list[dict[str, Any]], *, endpoint: str, anon_key: str,
                         timeout: float, sleep: float, names: list[str]) -> dict[str, Any]:
    """For each exact name, real workflow ids whose title/body contains it (ilike→id)."""
    out: dict[str, list[dict[str, Any]]] = {}
    meta = {"queries": 0, "latency_ms": 0.0, "terms": names}
    by_id = {w["item_id"]: w for w in workflows}
    for name in names:
        enc = name.replace("*", "\\*")  # escape literal asterisks (PostgREST ilike)
        params = {"kind": "eq.workflow", "select": "id,title",
                  "or": f"(title.ilike.*{enc}*,body.ilike.*{enc}*)", "limit": "20", "order": "id.asc"}
        try:
            rows, ms = _get("external_resources", params, endpoint=endpoint, anon_key=anon_key,
                            timeout=timeout, sleep=sleep)
        except ProbeError as exc:
            out[name] = [{"error": redact(str(exc))}]
            continue
        meta["queries"] += 1
        meta["latency_ms"] += ms
        hits = []
        for r in rows:
            wid = str(r["id"])
            w = by_id.get(wid, {})
            hits.append({"item_id": wid, "title": redact(bound_snippet(r.get("title"), TITLE_LEN)),
                         "has_python_source": w.get("has_python_source")})
        out[name] = hits
        if sleep > 0:
            time.sleep(sleep)
    meta["latency_ms"] = round(meta["latency_ms"], 1)
    return {"map": out, "meta": meta}


def discover_messages(terms: list[str], *, endpoint: str, anon_key: str, timeout: float,
                      sleep: float) -> dict[str, Any]:
    """Bounded message sample per term (snowflake id, author, channel, snippet)."""
    out: dict[str, list[dict[str, Any]]] = {}
    meta = {"queries": 0, "latency_ms": 0.0, "timeouts": 0, "errors": 0, "terms": terms}
    for term in terms:
        enc = term.replace("*", "\\*")
        params = {"kind": "eq.message", "select": "item_id,author,context,created_at,body",
                  "body": f"ilike.*{enc}*", "limit": str(MSG_SAMPLE_PER_TERM), "order": "created_at.desc"}
        try:
            rows, ms = _get("unified_feed", params, endpoint=endpoint, anon_key=anon_key,
                            timeout=MSG_TIMEOUT, sleep=sleep)
        except ProbeError as exc:
            msg = str(exc)
            if "TimeoutError" in msg or "timeout" in msg.lower():
                meta["timeouts"] += 1
            else:
                meta["errors"] += 1
            out[term] = [{"error": redact(msg)}]
            continue
        meta["queries"] += 1
        meta["latency_ms"] += ms
        hits = []
        for r in rows:
            iid = r.get("item_id")
            hits.append({
                "item_id": str(iid) if iid is not None else "",
                "item_id_is_string": isinstance(iid, str),
                "author": redact(bound_snippet(r.get("author"), 60)),
                "context": redact(bound_snippet(r.get("context"), 60)),
                "body_snippet": redact(bound_snippet(r.get("body"), SNIPPET_LEN)),
                "created_at": r.get("created_at"),
            })
        out[term] = hits
        if sleep > 0:
            time.sleep(sleep)
    meta["latency_ms"] = round(meta["latency_ms"], 1)
    return {"map": out, "meta": meta}


def discover_channels(*, endpoint: str, anon_key: str, timeout: float, sleep: float) -> dict[str, Any]:
    rows, ms = _get(
        "discord_channels",
        {"select": "channel_name,channel_type", "order": "channel_name.asc", "limit": "200"},
        endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep,
    )
    names = sorted({redact(str(r.get("channel_name"))) for r in rows if r.get("channel_name")})
    return {"channels": names[:120], "n": len(names), "latency_ms": round(ms, 1)}


# ---------------------------------------------------------------------------
# Curation suggestions (derived offline from evidence; helps grade)
# ---------------------------------------------------------------------------


def build_suggestions(evidence: dict[str, Any]) -> dict[str, Any]:
    """Flatten evidence into per-candidate-query matched identities for grading."""
    sugg: list[dict[str, Any]] = []

    def add(case_id: str, query: str, categories: list[str], matches: list[dict[str, Any]],
            notes: str = "", filters: dict[str, Any] | None = None) -> None:
        sugg.append({"id": case_id, "query": query, "categories": categories,
                     "filters": filters or {}, "matches": matches[:8], "notes": notes})

    sym_map = evidence.get("symbols", {}).get("map", {})
    for sym, hits in sym_map.items():
        clean = [h for h in hits if "error" not in h]
        if clean:
            add(f"SUGG_SYM_{abs(hash(sym)) % 100000:05d}", sym, ["workflow_code", "exact_name"],
                [{"kind": "workflow", "item_id": h["item_id"], "title": h.get("title")} for h in clean],
                notes=f"symbol '{sym}' present in {len(clean)} workflow body/bodies (python cohort: "
                      f"{sum(1 for h in clean if h.get('has_python_source'))})")

    name_map = evidence.get("exact_names", {}).get("map", {})
    for name, hits in name_map.items():
        clean = [h for h in hits if "error" not in h]
        if clean:
            add(f"SUGG_NAME_{abs(hash(name)) % 100000:05d}", name, ["exact_name"],
                [{"kind": "workflow", "item_id": h["item_id"], "title": h.get("title")} for h in clean],
                notes=f"exact name '{name}' present in {len(clean)} workflow resource(s)")

    msg_map = evidence.get("messages", {}).get("map", {})
    for term, hits in msg_map.items():
        clean = [h for h in hits if "error" not in h]
        if clean:
            add(f"SUGG_MSG_{abs(hash(term)) % 100000:05d}", term, ["best_is_message"],
                [{"kind": "message", "item_id": h["item_id"], "author": h.get("author"),
                  "context": h.get("context")} for h in clean],
                notes=f"term '{term}' in {len(clean)} sampled message(s)")

    for d in evidence.get("distillations", {}).get("items", []):
        add(f"SUGG_DIST_{d['item_id']}", d["question"], ["best_is_distillation", "paraphrase"],
            [{"kind": "distillation", "item_id": d["item_id"], "title": d["question"]}],
            notes=f"distillation id={d['item_id']} status={d['status']} conf={d['confidence']} "
                  f"cites={len(d.get('cites', []))}")
    return {"suggestions": sugg, "n": len(sugg)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_discovery(*, endpoint: str, anon_key: str, timeout: float, sleep: float,
                  include_messages: bool, symbols: list[str], names: list[str],
                  msg_terms: list[str]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "tool_version": TOOL_VERSION,
        "captured_at_utc": utc_now_iso(),
        "endpoint_ref": endpoint_ref(endpoint),
        "anon_key_masked": mask_key(anon_key),
        "secret_policy": (
            "Read-only GETs with the publishable key. No full bodies, no payload.python_source, "
            "no anon key is persisted; all free text is routed through verify_access.redact. "
            "Workflow code evidence is existence-only; suspect Python is never turned into a snippet."
        ),
        "notes": [],
    }

    dist = discover_distillations(endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep)
    evidence["distillations"] = dist
    evidence["notes"].append(f"distillations: {dist['n']} rows ({dist['latency_ms']} ms)")

    wfs = discover_workflows(endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep)
    evidence["workflows"] = wfs
    evidence["notes"].append(
        f"workflows: {wfs['n']} rows, python_cohort={wfs['n_python_cohort']} ({wfs['latency_ms']} ms)"
    )

    sym = discover_symbol_map(wfs["items"], endpoint=endpoint, anon_key=anon_key, timeout=timeout,
                              sleep=sleep, symbols=symbols)
    evidence["symbols"] = sym
    evidence["notes"].append(f"symbols: {sym['meta']['queries']} ok queries ({sym['meta']['latency_ms']} ms)")

    names_res = discover_exact_names(wfs["items"], endpoint=endpoint, anon_key=anon_key, timeout=timeout,
                                     sleep=sleep, names=names)
    evidence["exact_names"] = names_res
    evidence["notes"].append(
        f"exact_names: {names_res['meta']['queries']} ok queries ({names_res['meta']['latency_ms']} ms)"
    )

    chans = discover_channels(endpoint=endpoint, anon_key=anon_key, timeout=timeout, sleep=sleep)
    evidence["channels"] = chans
    evidence["notes"].append(f"channels: {chans['n']} ({chans['latency_ms']} ms)")

    if include_messages:
        msgs = discover_messages(msg_terms, endpoint=endpoint, anon_key=anon_key,
                                 timeout=timeout, sleep=sleep)
        evidence["messages"] = msgs
        evidence["notes"].append(
            f"messages: {msgs['meta']['queries']} ok, {msgs['meta']['timeouts']} timeouts, "
            f"{msgs['meta']['errors']} errors ({msgs['meta']['latency_ms']} ms)"
        )
    else:
        evidence["messages"] = {"map": {}, "meta": {"skipped": True}}
        evidence["notes"].append("messages: skipped (--no-messages)")

    return evidence


def render_dry_run(endpoint: str, anon_key: str, *, include_messages: bool,
                   symbols: list[str], names: list[str], msg_terms: list[str]) -> str:
    lines = [
        "golden_probe.py — DRY RUN (no network requests will be made)",
        f"endpoint_ref={endpoint_ref(endpoint)}  anon_key={mask_key(anon_key)}",
        f"evidence_version={EVIDENCE_VERSION}  tool_version={TOOL_VERSION}",
        "",
        f"distillations: 1 GET (limit 100) + cites GET",
        f"workflows: paged GET kind=eq.workflow select=id,title,source,created_at,metadata (<=6 pages)",
        f"symbols: {len(symbols)} bounded ilike GETs (kind=eq.workflow, id+title only)",
        f"exact_names: {len(names)} bounded ilike GETs",
        "channels: 1 GET (limit 200)",
    ]
    if include_messages:
        lines.append(f"messages: {len(msg_terms)} bounded ilike GETs on unified_feed kind=message "
                     f"(limit {MSG_SAMPLE_PER_TERM}, timeout {MSG_TIMEOUT}s)")
    else:
        lines.append("messages: skipped (--no-messages)")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only discovery of real Hivemind corpus identities for the golden set.")
    p.add_argument("--dry-run", action="store_true", help="print the request plan and exit; no network")
    p.add_argument("--no-messages", action="store_true", help="skip the slow message discovery arm")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"per-request timeout s (default {DEFAULT_TIMEOUT})")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help=f"seconds between requests (default {DEFAULT_SLEEP})")
    p.add_argument("--out-dir", type=Path, default=GOLDEN_DIR, help="output directory")
    p.add_argument("--evidence-name", default="evidence-v1.json", help="evidence snapshot filename")
    p.add_argument("--suggestions-name", default="curation-suggestions-v1.json", help="suggestions filename")
    args = p.parse_args(argv)

    endpoint = resolve_endpoint()
    anon_key = resolve_anon_key()

    if args.dry_run:
        print(redact(render_dry_run(endpoint, anon_key, include_messages=not args.no_messages,
                                    symbols=PY_SYMBOLS, names=EXACT_NAMES, msg_terms=MSG_TERMS)))
        return 0

    evidence = run_discovery(
        endpoint=endpoint, anon_key=anon_key, timeout=args.timeout, sleep=args.sleep,
        include_messages=not args.no_messages, symbols=PY_SYMBOLS, names=EXACT_NAMES,
        msg_terms=MSG_TERMS,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ev_path = args.out_dir / args.evidence_name
    ev_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    suggestions = build_suggestions(evidence)
    sug_path = args.out_dir / args.suggestions_name
    sug_path.write_text(json.dumps(suggestions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Redacted stdout summary.
    print(redact(f"golden_probe: wrote {ev_path} and {sug_path}"))
    for n in evidence["notes"]:
        print(redact(f"  - {n}"))
    # symbol coverage quick-look
    sym_map = evidence.get("symbols", {}).get("map", {})
    found = {s: len([h for h in v if "error" not in h]) for s, v in sym_map.items()}
    print(redact("  symbol hits: " + ", ".join(f"{s}={c}" for s, c in found.items())))
    nm = evidence.get("exact_names", {}).get("map", {})
    nfound = {s: len([h for h in v if "error" not in h]) for s, v in nm.items()}
    print(redact("  exact-name hits: " + ", ".join(f"{s}={c}" for s, c in nfound.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
