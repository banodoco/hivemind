#!/usr/bin/env python3
"""Rehearse contributor authentication on a disposable local PostgreSQL cluster.

This never connects to Supabase or a configured project. It uses the repository's
throwaway cluster harness, loads the smallest schema needed by migration 040,
exercises direct Auth binding, legacy-key blocking, approval/redeem replay
protection, and two concurrent redemption attempts, then tears the cluster down.
"""

from __future__ import annotations

import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lexical_pg import LocalCluster  # noqa: E402

MIGRATION = ROOT / "schema" / "040_contributor_auth.sql"


def main() -> int:
    cluster = LocalCluster.start()
    try:
        fixture = """
create extension if not exists pgcrypto;
create schema auth;
create table auth.users (id uuid primary key);
create table public.contributors (
  id bigint generated always as identity primary key,
  name text not null unique,
  kind text not null check (kind in ('agent','human')),
  api_key_hash text,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  is_editor boolean not null default false
);
create role anon;
create role authenticated;
create role service_role;
insert into auth.users values ('11111111-1111-4111-8111-111111111111'), ('22222222-2222-4222-8222-222222222222');
insert into public.contributors (name, kind, api_key_hash) values
  ('legacy', 'human', encode(digest(convert_to('hm_' || repeat('a', 64), 'utf8'), 'sha256'), 'hex'));
"""
        cluster.psql(fixture, capture=False)
        cluster.psql_file(MIGRATION)
        blocked_rc, blocked = cluster.psql("select count(*) from hivemind_resolve_contributor_key('hm_' || repeat('a', 64));")
        assert blocked_rc == 0 and blocked.strip() == "0", blocked
        cluster.psql("select hivemind_claim_contributor('postgres', 1, '11111111-1111-4111-8111-111111111111');", capture=False)
        resolved_rc, resolved = cluster.psql("select contributor_id from hivemind_resolve_contributor_key('hm_' || repeat('a', 64));")
        assert resolved_rc == 0 and resolved.strip() == "1", resolved

        request = secrets.token_urlsafe(32)
        poll = secrets.token_urlsafe(32)
        approval = secrets.token_hex(8).upper()
        create_rc, created = cluster.psql(
            f"select hivemind_auth_create_request('{request}', '{poll}', '{approval}', 'rehearsal', 600)->>'request_id';"
        )
        assert create_rc == 0 and created.strip(), created
        approve_rc, approved = cluster.psql(
            f"select hivemind_auth_approve_request('{request}', '{approval}', '22222222-2222-4222-8222-222222222222')->>'status';"
        )
        assert approve_rc == 0 and approved.strip() == "approved", approved

        outputs: list[str] = []
        errors: list[str] = []

        def redeem() -> None:
            rc, output = cluster.psql(
                f"select hivemind_auth_redeem_request('{request}', '{poll}')->>'status';"
            )
            if rc == 0:
                outputs.append(output.strip())
            else:
                errors.append(output)

        threads = [threading.Thread(target=redeem) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert outputs.count("redeemed") == 1, (outputs, errors)
        assert len(errors) == 1, (outputs, errors)

        replay_rc, _ = cluster.psql(
            f"select hivemind_auth_redeem_request('{request}', '{poll}')->>'status';"
        )
        assert replay_rc != 0
        wrong_poll_rc, _ = cluster.psql(
            f"select hivemind_auth_poll_request('{request}', 'wrong-secret');"
        )
        assert wrong_poll_rc != 0
        consumed_rc, consumed = cluster.psql(
            f"select hivemind_auth_poll_request('{request}', '{poll}')->>'status';"
        )
        assert consumed_rc == 0 and consumed.strip() == "consumed", consumed

        rate_request = secrets.token_urlsafe(32)
        rate_poll = secrets.token_urlsafe(32)
        rate_approval = secrets.token_hex(8).upper()
        cluster.psql(
            f"select hivemind_auth_create_request('{rate_request}', '{rate_poll}', '{rate_approval}', 'rate', 600);",
            capture=False,
        )
        cluster.psql(
            f"select hivemind_auth_approve_request('{rate_request}', '{rate_approval}', '22222222-2222-4222-8222-222222222222');",
            capture=False,
        )
        first_poll_rc, first_poll = cluster.psql(
            f"select hivemind_auth_poll_request('{rate_request}', '{rate_poll}')->>'status';"
        )
        assert first_poll_rc == 0 and first_poll.strip() == "approved", first_poll
        rate_limited_rc, _ = cluster.psql(
            f"select hivemind_auth_poll_request('{rate_request}', '{rate_poll}');"
        )
        assert rate_limited_rc != 0

        expiry_request = secrets.token_urlsafe(32)
        expiry_poll = secrets.token_urlsafe(32)
        expiry_approval = secrets.token_hex(8).upper()
        cluster.psql(
            f"select hivemind_auth_create_request('{expiry_request}', '{expiry_poll}', '{expiry_approval}', 'expiry', 600);",
            capture=False,
        )
        cluster.psql(
            f"update contributor_auth_requests set expires_at = now() - interval '1 second' where request_token_hash = hivemind_secret_hash('{expiry_request}');",
            capture=False,
        )
        expired_rc, expired = cluster.psql(
            f"select hivemind_auth_poll_request('{expiry_request}', '{expiry_poll}')->>'status';"
        )
        assert expired_rc == 0 and expired.strip() == "expired", expired

        revoke_request = secrets.token_urlsafe(32)
        revoke_poll = secrets.token_urlsafe(32)
        revoke_approval = secrets.token_hex(8).upper()
        cluster.psql(
            f"select hivemind_auth_create_request('{revoke_request}', '{revoke_poll}', '{revoke_approval}', 'revoke', 600);",
            capture=False,
        )
        cluster.psql(
            f"select hivemind_auth_approve_request('{revoke_request}', '{revoke_approval}', '22222222-2222-4222-8222-222222222222');",
            capture=False,
        )
        key_rc, issued_key = cluster.psql(
            f"select hivemind_auth_redeem_request('{revoke_request}', '{revoke_poll}')->>'key';"
        )
        issued_key = issued_key.strip()
        assert key_rc == 0 and issued_key.startswith("hm_"), issued_key
        revoked_rc, revoked = cluster.psql(
            f"select hivemind_auth_revoke_key('{issued_key}')->>'revoked';"
        )
        assert revoked_rc == 0 and revoked.strip() == "true", revoked
        status_rc, status = cluster.psql(
            f"select hivemind_auth_key_status('{issued_key}')->>'status';"
        )
        assert status_rc == 0 and status.strip() == "revoked", status
        resolved_revoked_rc, resolved_revoked = cluster.psql(
            f"select count(*) from hivemind_resolve_contributor_key('{issued_key}');"
        )
        assert resolved_revoked_rc == 0 and resolved_revoked.strip() == "0", resolved_revoked
        time.sleep(0.01)
        print("contributor auth disposable migration/redeem rehearsal passed")
        return 0
    finally:
        cluster.tear_down()


if __name__ == "__main__":
    raise SystemExit(main())
