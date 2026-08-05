"""SQL + RPC tests for the message-metadata exposure change (schema/035).

Verifies, on an isolated throwaway PostgreSQL cluster, that the additive
override (``CREATE OR REPLACE VIEW unified_feed`` + ``CREATE OR REPLACE
FUNCTION hivemind_lexical_search``) does what it claims:

  1. Deleted messages (``is_deleted = true``) no longer surface on
     ``unified_feed`` (the public PostgREST surface behind ``get_item`` /
     ``search``) nor on the lexical RPC results.
  2. The message branch of ``unified_feed`` carries the full Discord envelope
     in ``metadata``: channel_id / reactions (original keys) plus guild_id,
     author_id, reference_id, thread_id, message_type, edited_at, is_pinned,
     reaction_count, embeds, channel_type, avatar_url.
  3. The lexical RPC returns the same enriched metadata, row-for-row.
  4. Resource and distillation branches are untouched.

The cluster is a throwaway ``initdb --auth=trust`` instance on an ephemeral
port with a temp data dir; torn down in ``tearDownClass``. No Docker, no
network, no production mutation. Skipped entirely when PostgreSQL binaries are
absent.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_lexical_candidate as R  # noqa: E402
from lexical_pg import find_pgbins  # noqa: E402

# A message planted with every newly-exposed field so the enrichment path is
# exercised end-to-end (not just nulls).
ENRICHED_ID = 5_000_000_000_000_000_001  # < bigint max (9.223e18); 19-digit snowflake-shaped
REFERENCED_ID = 4_000_000_000_000_000_001


def _seed_enriched(cluster: R.LP.LocalCluster) -> None:
    """Give member 1 an avatar, channel 100 a type, and insert one message
    whose embeds / reaction_count / reference_id / edited_at / is_pinned /
    thread_id / message_type / attachments are all populated."""
    cluster.psql("update public.members set avatar_url = 'https://cdn.example/avatar.png' where member_id = 1;")
    cluster.psql("update public.discord_channels set channel_type = 'forum' where channel_id = 100;")
    stmt = (
        "insert into public.discord_messages (message_id, channel_id, author_id, guild_id, content, "
        "created_at, is_deleted, thread_id, message_type, flags, embeds, reaction_count, reactors, "
        "reference_id, edited_at, is_pinned, edit_history) values "
        f"({ENRICHED_ID}, 100, 1, 9000, 'sigmaflux enriched message for exposure test', now(), false, "
        "123, 'DEFAULT', 0, '[{\"type\":\"link\",\"title\":\"Sigma Flux Guide\"}]', 7, '[]', "
        f"{REFERENCED_ID}, now() - interval '1 hour', true, '[]')"
    )
    rc, _ = cluster.psql(stmt)
    if rc != 0:
        raise RuntimeError(f"enriched seed failed rc={rc}")


def _query_json(cluster: R.LP.LocalCluster, sql: str) -> object:
    """Run *sql* (expected to yield one jsonb/json ::text value) and parse it."""
    rc, out = cluster.psql(sql)
    if rc != 0:
        raise RuntimeError(f"query failed rc={rc}:\n{sql}")
    return R._extract_json(out)


@unittest.skipUnless(find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestMessageMetadataExposureCluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = R.LP.LocalCluster.start()
        try:
            R.reset_schema(cls.cluster)
            R.bootstrap(cls.cluster)  # 001 + 003..009
            # Apply the exposure override on top (the unit under test).
            cls.cluster.psql_file(R.SCHEMA_DIR / "035_expose_message_metadata.sql")
            R.seed(cls.cluster, n_messages=2000)
            _seed_enriched(cls.cluster)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    # ------------------------------------------------------------------
    # Migration hygiene
    # ------------------------------------------------------------------
    def test_035_idempotent(self) -> None:
        """CREATE OR REPLACE VIEW + FUNCTION re-apply cleanly (the apply verb
        re-runs migrations safely)."""
        self.cluster.psql_file(R.SCHEMA_DIR / "035_expose_message_metadata.sql")

    # ------------------------------------------------------------------
    # Deletion is fully hidden from the public surface
    # ------------------------------------------------------------------
    def test_unified_feed_excludes_deleted(self) -> None:
        # Every non-deleted discord_message appears; no deleted one does.
        total = _one_int(self.cluster,
            "select count(*) from public.unified_feed where kind = 'message';")
        nondeleted = _one_int(self.cluster,
            "select count(*) from public.discord_messages where is_deleted = false;")
        self.assertEqual(total, nondeleted,
                         "unified_feed message count != non-deleted message count")

        deleted_msg = str(1_000_000_000_000_000_000)  # seed i=0 is deleted
        leaked = _one_int(self.cluster,
            "select count(*) from public.unified_feed "
            f"where kind = 'message' and item_id = '{deleted_msg}';")
        self.assertEqual(leaked, 0, f"deleted message {deleted_msg} leaked through unified_feed")

    def test_rpc_excludes_deleted(self) -> None:
        resp = R.call_rpc(self.cluster, "sampler video")
        ids = {r["item_id"] for r in resp["results"] if r.get("kind") == "message"}
        deleted_msg = str(1_000_000_000_000_000_000)
        self.assertNotIn(deleted_msg, ids)

    # ------------------------------------------------------------------
    # Enriched metadata on unified_feed
    # ------------------------------------------------------------------
    def test_unified_feed_metadata_shape(self) -> None:
        meta = _query_json(
            self.cluster,
            "select (select metadata::text from public.unified_feed "
            f"where kind='message' and item_id='{ENRICHED_ID}');",
        )
        # Original keys still present.
        self.assertIn("channel_id", meta)
        self.assertIn("reactions", meta)
        # Every newly-exposed key present.
        for key in ("guild_id", "author_id", "reference_id", "thread_id",
                    "message_type", "edited_at", "is_pinned", "reaction_count",
                    "embeds", "channel_type", "avatar_url"):
            self.assertIn(key, meta, f"metadata missing new key {key}")
        # Values round-trip from the source columns.
        self.assertEqual(meta["author_id"], 1)
        self.assertEqual(meta["reference_id"], REFERENCED_ID)
        self.assertEqual(meta["thread_id"], 123)
        self.assertEqual(meta["message_type"], "DEFAULT")
        self.assertEqual(meta["reaction_count"], 7)
        self.assertIs(meta["is_pinned"], True)
        self.assertEqual(meta["channel_type"], "forum")
        self.assertEqual(meta["avatar_url"], "https://cdn.example/avatar.png")
        self.assertEqual(meta["guild_id"], 9000)
        self.assertEqual(meta["embeds"][0]["title"], "Sigma Flux Guide")

    def test_unified_feed_metadata_for_plain_message(self) -> None:
        """A message with no enrichment still carries the keys (nulls)."""
        meta = _query_json(
            self.cluster,
            "select (select metadata::text from public.unified_feed "
            "where kind='message' and item_id='1000000000000000005');",
        )
        for key in ("channel_id", "reactions", "guild_id", "author_id", "reference_id",
                    "thread_id", "message_type", "edited_at", "is_pinned",
                    "reaction_count", "embeds", "channel_type", "avatar_url"):
            self.assertIn(key, meta)

    # ------------------------------------------------------------------
    # Enriched metadata on the lexical RPC (mirrors unified_feed)
    # ------------------------------------------------------------------
    def test_rpc_carries_enriched_metadata(self) -> None:
        resp = R.call_rpc(self.cluster, "sigmaflux")
        by_id = {r["item_id"]: r for r in resp["results"]}
        self.assertIn(str(ENRICHED_ID), by_id, "enriched message not retrieved by RPC")
        meta = by_id[str(ENRICHED_ID)]["metadata"]
        self.assertEqual(meta["reference_id"], REFERENCED_ID)
        self.assertEqual(meta["reaction_count"], 7)
        self.assertEqual(meta["channel_type"], "forum")
        self.assertEqual(meta["embeds"][0]["title"], "Sigma Flux Guide")
        self.assertEqual(meta["avatar_url"], "https://cdn.example/avatar.png")
        # Old keys preserved on the RPC path too.
        self.assertIn("channel_id", meta)
        self.assertIn("reactions", meta)

    # ------------------------------------------------------------------
    # Non-message branches untouched
    # ------------------------------------------------------------------
    def test_resource_and_distillation_branches_unchanged(self) -> None:
        res = _query_json(
            self.cluster,
            "select (select metadata::text from public.unified_feed "
            "where kind='workflow' and item_id='20');",
        )
        self.assertEqual(res, {})
        dist = _query_json(
            self.cluster,
            "select (select metadata::text from public.unified_feed "
            "where kind='distillation' and item_id='1');",
        )
        self.assertEqual(dist, {"status": "approved", "confidence": "high"})


def _one_int(cluster: R.LP.LocalCluster, sql: str) -> int:
    rc, out = cluster.psql(sql)
    if rc != 0:
        raise RuntimeError(f"query failed rc={rc}:\n{sql}")
    for tok in out.strip().split():
        if tok.isdigit():
            return int(tok)
    raise RuntimeError(f"no integer in output {out!r}")


if __name__ == "__main__":
    unittest.main()
