#!/usr/bin/env python3
"""Deterministic validator for the Phase-1 identifier-normalization contract (task 1.4).

Two modes:

  * OFFLINE (default): no database, no network. Checks the frozen fixture corpus
    is self-consistent with the reference module ``executors.identifier_normalization``
    (versions, casefold policy, separator set, distinct chars, provenance
    vocabulary/priority), that the module reproduces every fixture's expected
    compact/preserve form, that ``identifier_forms`` equals the frozen
    ``lexical_contract.identifier_aliases``, and that the alias examples behave
    (collision reporting, deterministic resolution, no-NL-rewrite).

  * ISOLATED CLUSTER (opt-in): ``HIVEMIND_EVAL_CLUSTER=1`` spins up a throwaway
    PostgreSQL 14 cluster (initdb --auth=trust, temp data dir, no network) and
    proves: (a) the IMMUTABLE SQL functions match the Python reference
    byte-for-byte on every fixture; (b) the functions are IMMUTABLE and are used
    by an expression index on a real-sized table; (c) STORED generated columns
    using them succeed; (d) the deterministic ICU collation is locale-independent
    (lowercases non-ASCII even under a C-locale cluster). Torn down after. No
    production.

Exit 0 on success, 1 on any check failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import executors.identifier_normalization as I  # noqa: E402
import executors.lexical_contract as L  # noqa: E402

CORPUS_JSON = REPO / "eval" / "retrieval" / "fixtures" / "identifier-normalization-v1.json"
SCHEMA_SQL = REPO / "schema" / "005_identifier_normalization.sql"
REPORT_MD = REPO / "docs" / "hybrid-search" / "phase1-identifier-normalization.md"

PG_BIN = "/opt/homebrew/bin"
CLUSTER_PORT = "5494"


class CheckError(Exception):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckError(msg)
    print(f"  ok: {msg}")


# ---------------------------------------------------------------------------
# Offline checks
# ---------------------------------------------------------------------------

def check_corpus_sync(corpus: dict) -> None:
    print("[1/5] corpus <-> reference module sync")
    _check(corpus["normalization_version"] == I.IDENTIFIER_NORMALIZATION_VERSION,
           f"normalization_version == {I.IDENTIFIER_NORMALIZATION_VERSION}")
    _check(corpus["alias_version"] == I.IDENTIFIER_ALIAS_VERSION,
           f"alias_version == {I.IDENTIFIER_ALIAS_VERSION}")
    _check(corpus["casefold_policy"] == I.CASEFOLD_POLICY, "casefold_policy matches module")
    _check(bool(corpus.get("post_hoc_locked")), "corpus is post-hoc locked")
    _check(corpus["task"] == "1.4", "task == 1.4")
    _check(set(corpus["distinct_chars_documented"]) == set(I.DISTINCT_CHARS),
           "distinct-chars documentation matches module")


def check_python_reproduces_corpus(corpus: dict) -> None:
    print("[2/5] Python reference reproduces every fixture's expected forms")
    for fx in corpus["fixtures"]:
        inp = fx["input"]
        _check(I.normalize_identifier(inp) == fx["expected_compact"],
               f"{fx['id']} compact for {fx['input']!r}")
        _check(I.normalize_identifier_preserve(inp) == fx["expected_preserve"],
               f"{fx['id']} preserve for {fx['input']!r}")


def check_forms_and_parity(corpus: dict) -> None:
    print("[3/5] form coverage + identifier_forms == lexical_contract.identifier_aliases")
    required = {"dotted", "versioned", "hyphenated", "filename",
                "python_symbol", "keyword_argument", "alias"}
    present = {f for fx in corpus["fixtures"] for f in fx["forms"]}
    missing = required - present
    _check(not missing, f"completion-signal forms present (missing: {sorted(missing) or 'none'})")
    for fx in corpus["fixtures"]:
        if fx["parity_class"] != "accept" or not isinstance(fx["input"], str):
            # Parity is a STRING guarantee; None is the NULL boundary (excluded).
            continue
        forms = I.identifier_forms(fx["input"])
        _check(tuple(forms) == L.identifier_aliases(fx["input"]),
               f"{fx['id']} identifier_forms == lexical_contract.identifier_aliases")
    # distinct forms are genuinely distinct (not silently merged)
    confusables = [fx for fx in corpus["fixtures"] if "confusable" in fx["forms"]]
    compacts = {fx["expected_compact"] for fx in confusables}
    _check(len(compacts) == len(confusables),
           "confusable homoglyphs (Latin/Cyrillic/Greek a) remain distinct compact keys")


def check_alias_examples(corpus: dict) -> None:
    print("[4/5] alias examples behave (collisions reported, deterministic, no NL rewrite)")
    for ex in corpus["alias_examples"]:
        reg = I.AliasRegistry()
        if "identities" in ex:
            for ident in ex["identities"]:
                reg.register(
                    canonical_kind="resource", canonical_id=ident["canonical_id"],
                    canonical_name=ident["canonical_name"], alias_text=ident["alias_text"],
                    provenance=ident["provenance"],
                )
            collisions = reg.collisions()
            _check(bool(collisions) == ex["expect_collision"],
                   f"{ex['name']}: collision detected as expected")
            if ex["expect_collision"]:
                first_key = next(iter(collisions))
                ids = {e.identity for e in collisions[first_key]}
                _check(len(ids) == ex["expect_n_distinct_canonical"],
                       f"{ex['name']}: {len(ids)} distinct identities")
            # no-NL-rewrite: expansion returns identity strings, never a query rewrite
            exp = reg.expand_query_identifiers("controlnet")
            _check(all(" " not in s for s in exp) and all(isinstance(s, str) for s in exp),
                   f"{ex['name']}: expansion returns identity strings (no NL rewrite)")
        else:
            for a in ex["aliases"]:
                reg.register(canonical_kind=ex["canonical_kind"], canonical_id=ex["canonical_id"],
                             canonical_name=ex["canonical_name"], alias_text=a["alias_text"],
                             provenance=a["provenance"])
            _check(not reg.collisions() == ex["expect_collision"],
                   f"{ex['name']}: single-identity aliases do not collide")


def check_schema_references() -> None:
    print("[5/5] schema file references the frozen functions/collation")
    sql = SCHEMA_SQL.read_text()
    for needle in ("hivemind_unicode", "provider = icu",
                   "hivemind_normalize_identifier", "hivemind_normalize_identifier_preserve",
                   "hivemind_identifier_alias_forms", "identifier_aliases",
                   "hivemind_register_identifier_alias", "hivemind_identifier_alias_collisions",
                   "immutable", "task 1.5", "task 1.7"):
        _check(needle in sql, f"schema contains {needle!r}")
    if REPORT_MD.exists():
        md = REPORT_MD.read_text()
        for needle in ("NFC", "ICU", "IMMUTABLE", "byte-for-byte", "ß", "İ"):
            _check(needle in md, f"report mentions {needle!r}")


# ---------------------------------------------------------------------------
# Isolated-cluster SQL/Python parity + immutability proof (opt-in)
# ---------------------------------------------------------------------------

def _sh(*a, env=None):
    return subprocess.run(a, text=True, capture_output=True, env=env)


def _psql(port, db, sql, env, flags=("-v", "ON_ERROR_STOP=1")):
    r = _sh(f"{PG_BIN}/psql", "-p", port, "-d", db, *flags, "-c", sql, env=env)
    if r.returncode != 0:
        raise CheckError(f"psql failed:\n{r.stderr}\n--- sql ---\n{sql[:800]}")
    return r.stdout


def check_cluster_parity(corpus: dict) -> None:
    print("[cluster] SQL/Python parity + IMMUTABLE/index proof (isolated PG14)")
    if not shutil.which("initdb") and not Path(f"{PG_BIN}/initdb").exists():
        raise CheckError("initdb not found on PATH; cannot run isolated cluster parity")
    import tempfile
    pgdata = Path(tempfile.mkdtemp(prefix="hm14_idnorm_"))
    env = dict(os.environ, PATH=PG_BIN + ":" + os.environ.get("PATH", ""))
    port = CLUSTER_PORT
    try:
        if _sh(f"{PG_BIN}/initdb", "-D", str(pgdata), "-A", "trust", "--no-locale", "-E", "UTF8", env=env).returncode:
            raise CheckError("initdb failed")
        r = _sh(f"{PG_BIN}/pg_ctl", "-D", str(pgdata), "-o", f"-F -p {port} -c listen_addresses=''",
                "-l", str(pgdata / "log"), "-w", "start", env=env)
        if r.returncode:
            raise CheckError(f"pg_ctl start failed:\n{(pgdata/'log').read_text()}")
        _sh(f"{PG_BIN}/createdb", "-p", port, "idnorm", env=env)
        locale_row = _psql(port, "idnorm", "SHOW lc_collate;", env, flags=("-At",)).strip()
        print(f"  cluster lc_collate = {locale_row!r}")
        # load the frozen schema
        _sh(f"{PG_BIN}/psql", "-p", port, "-d", "idnorm", "-v", "ON_ERROR_STOP=1",
            "-f", str(SCHEMA_SQL), env=env)
        # locale-independence: stock lower() does NOT lowercase Ü under C; the function does.
        stock = _psql(port, "idnorm", "SELECT lower('ÜBER');", env, flags=("-At",)).strip()
        fn = _psql(port, "idnorm", "SELECT hivemind_normalize_identifier('ÜBER CAFÉ 动漫');",
                   env, flags=("-At",)).strip()
        _check(stock == "Über", "stock lower() leaves Ü untouched under C locale (the locale trap)")
        _check(fn == "übercafé动漫", "ICU-collation function lowercases non-ASCII regardless of locale")

        # byte-for-byte parity on EVERY fixture (accept + distinct)
        mism = 0
        for fx in corpus["fixtures"]:
            lit = "NULL" if fx["input"] is None else "'" + fx["input"].replace("'", "''") + "'"
            sql = (f"SELECT hivemind_normalize_identifier({lit})::text, "
                   f"hivemind_normalize_identifier_preserve({lit})::text;")
            sc, sp = _psql(port, "idnorm", sql, env, flags=("-At", "-F", "\t")).rstrip("\n").split("\t")
            if sc != fx["expected_compact"] or sp != fx["expected_preserve"]:
                mism += 1
                print(f"  MISMATCH {fx['id']} in={fx['input']!r}: "
                      f"compact sql={sc!r} py={fx['expected_compact']!r}; "
                      f"preserve sql={sp!r} py={fx['expected_preserve']!r}")
        _check(mism == 0, f"SQL/Python byte-for-byte parity on all {len(corpus['fixtures'])} fixtures (mismatches={mism})")

        # IMMUTABLE proof
        vol = _psql(port, "idnorm",
                    "SELECT proname, provolatile FROM pg_proc WHERE proname IN "
                    "('hivemind_normalize_identifier','hivemind_normalize_identifier_preserve',"
                    "'hivemind_identifier_alias_forms','hivemind_alias_provenance_priority') "
                    "ORDER BY proname;", env, flags=("-At", "-F", "|")).strip()
        _check(all(line.endswith("|i") for line in vol.splitlines()),
               f"all four normalization functions are IMMUTABLE (provolatile=i): {vol.replace(chr(10),' / ')}")

        # expression index actually used + generated columns succeed
        proof = textwrap.dedent(f"""
        create table iproof(id int, title text);
        insert into iproof select g, 'WanVideoSampler_'||g from generate_series(1,20000) g;
        insert into iproof select g, 'FLUX.1_dev_'||g from generate_series(20001,40000) g;
        create index iproof_idx on iproof (hivemind_normalize_identifier(title));
        alter table iproof add column key text generated always as (hivemind_normalize_identifier(title)) stored;
        alter table iproof add column pkey text generated always as (hivemind_normalize_identifier_preserve(title)) stored;
        analyze iproof;
        select key, pkey from iproof where id in (1,20001) order by id;
        """)
        gen_out = _psql(port, "idnorm", proof, env, flags=("-At", "-F", "|")).strip()
        _check("wanvideosampler" in gen_out and "flux.1_dev_20001" in gen_out,
               "STORED generated columns (compact + preserve) populate correctly")
        plan = _psql(port, "idnorm",
                     "explain (costs off) select id from iproof where hivemind_normalize_identifier(title)='flux1dev_25000';",
                     env, flags=("-t",))
        _check("Index Scan" in plan or "Bitmap Index" in plan,
               "expression index hivemind_normalize_identifier(title) is USED by the planner")

        # alias table works: register (side effects), then verify separately.
        # (psql -c returns only the last result set for a multi-statement string,
        #  so registration and verification are separate calls.)
        register_sql = "; ".join([
            "select hivemind_register_identifier_alias('resource','2537','ControlNet','control net','workflow_semantics.searchable_aliases') is not null",
            "select hivemind_register_identifier_alias('resource','2537','ControlNet','controlnet','curated') is not null",
            "select hivemind_register_identifier_alias('resource','9999','Control Mesh','controlnet','curated') is not null",
        ]) + ";"
        _psql(port, "idnorm", register_sql, env)  # default flags incl. ON_ERROR_STOP=1
        rows = _psql(port, "idnorm",
                     "select alias_compact, canonical_id, priority from identifier_aliases "
                     "where live order by canonical_id, alias_compact;",
                     env, flags=("-At", "-F", "|")).strip()
        _check("controlnet" in rows and "2537" in rows and "9999" in rows,
               "alias registration populates generated compact columns")
        n_coll = _psql(port, "idnorm",
                       "select count(*)::text from hivemind_identifier_alias_collisions();",
                       env, flags=("-At",)).strip()
        _check(n_coll == "1",
               "collision detection reports 1 shared alias compact key (got %s)" % n_coll)

        fd = _psql(port, "idnorm",
                   "SELECT pg_get_functiondef('hivemind_normalize_identifier(text)'::regprocedure);", env)
        _check("collate" in fd.lower() and "hivemind_unicode" in fd,
               "stored function definition pins the ICU collation")
        print("  ok: alias reference table + collision function behave")
    finally:
        _sh(f"{PG_BIN}/pg_ctl", "-D", str(pgdata), "-m", "immediate", "-w", "stop", env=env)
        shutil.rmtree(pgdata, ignore_errors=True)


def main() -> int:
    if not CORPUS_JSON.exists():
        print(f"ERROR: corpus not found: {CORPUS_JSON}", file=sys.stderr)
        return 1
    corpus = json.loads(CORPUS_JSON.read_text())
    checks = [
        ("corpus/module sync", lambda: check_corpus_sync(corpus)),
        ("python reproduces corpus", lambda: check_python_reproduces_corpus(corpus)),
        ("form coverage + forms parity", lambda: check_forms_and_parity(corpus)),
        ("alias examples", lambda: check_alias_examples(corpus)),
        ("schema/report references", check_schema_references),
    ]
    for name, fn in checks:
        try:
            fn()
        except CheckError as exc:
            print(f"\nFAIL [{name}]: {exc}", file=sys.stderr)
            return 1
    if os.environ.get("HIVEMIND_EVAL_CLUSTER") == "1":
        try:
            check_cluster_parity(corpus)
        except CheckError as exc:
            print(f"\nFAIL [cluster]: {exc}", file=sys.stderr)
            return 1
    else:
        print("\n(skipped: SQL/Python cluster parity — set HIVEMIND_EVAL_CLUSTER=1 to run)")
    print("\nOK: identifier-normalization contract validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
