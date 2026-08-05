"""SQL tests for tasks 2.2–2.5 on an isolated throwaway local PostgreSQL cluster.

Mirrors the test_lexical_sql.py convention (discoverable; auto-skipped when PG
binaries are absent). Applies schema/020–024 in a throwaway cluster that has the
locally-compiled pgvector, and asserts the storage-layer guarantees that offline
tests cannot: dimension mixing is rejected at BOTH layers, the atomic
same-dimension active-contract switch respects coverage, one active contract per
dimension holds, a Discord snowflake item_id round-trips as exact text, and the
SQL canonical-text/hash functions match the Python canonicalizers (cross-
language parity). Skipped when pgvector is not installable in the local cluster.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg  # noqa: E402

from executors import canonical_representations as cr  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import entity_identity as ei  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402

SCHEMA_DIR = REPO / "schema"
MIGRATIONS = [
    "020_enable_pgvector.sql",
    "021_embedding_contracts.sql",
    "022_content_embeddings.sql",
    "023_embedding_contract_switch.sql",
    "024_identity_and_canonical_representations.sql",
]


def _vec(dim: int, value: str = "0.016") -> str:
    return "[" + ", ".join([value] * dim) + "]"


def _hash64(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


@unittest.skipUnless(lexical_pg.find_pgbins(), "PostgreSQL binaries not found")
class TestEmbeddingSchemaSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = lexical_pg.LocalCluster.start()
        try:
            # Skip the whole class if pgvector cannot be installed locally.
            rc, _ = cls.cluster.psql(
                "select 1 from pg_available_extensions where name='vector';"
            )
            probe = cls.cluster.psql(
                "select count(*) from pg_available_extensions where name='vector';"
            )[1].strip().splitlines()
            if not probe or probe[-1] != "1":
                raise unittest.SkipTest("pgvector not available in local cluster")
            for name in MIGRATIONS:
                cls.cluster.psql_file(SCHEMA_DIR / name)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    def _last(self, sql: str) -> str:
        rc, out = self.cluster.psql(sql)
        self.assertEqual(rc, 0, out)
        return out.strip().splitlines()[-1] if out.strip() else ""

    def _text(self, sql_expr: str) -> str:
        out = self.cluster.psql(
            f"select '<V>' || coalesce(({sql_expr}), '') || '</V>';"
        )[1]
        return out[out.index("<V>") + 3: out.rindex("</V>")]

    # -- 2.2 pgvector -----------------------------------------------------

    def test_vector_extension_and_cosine_operator(self) -> None:
        self.assertEqual(self._last("select extname from pg_extension where extname='vector';"), "vector")
        # PG14 prints booleans as t/f; PG17 prints true/false. Accept both.
        self.assertTrue(
            self._last("select ('[1,0]'::vector(2) <=> '[0,1]'::vector(2) >= 0)::text;").lower().startswith("t")
        )

    # -- 2.3 contract_id parity ------------------------------------------

    def test_contract_id_python_sql_parity(self) -> None:
        sql_id = int(self._last(
            "select hivemind_contract_id('openai','text-embedding-3-small',384,1,2);"
        ))
        py_id = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384).id
        self.assertEqual(sql_id, py_id)

    # -- 2.3 dimension mixing rejected -----------------------------------

    def test_dimension_mixing_physical_rejected(self) -> None:
        cid = ec.ContractSpec(provider="openai", model="dm", dimension=384).id
        self.cluster.psql(
            f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
            f"values ({cid},'openai','dm',384,1,1,'draft');"
        )
        h = _hash64("a")
        rc, _ = self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            "chunk_index,embedding,representation_hash,chunk_hash) "
            f"values ({cid},'resource','1','prose',0,'{_vec(1536)}'::vector,'{h}','{h}');"
        )
        self.assertNotEqual(rc, 0, "1536-d vector must be rejected by vector(384)")

    def test_dimension_mixing_data_trigger_rejected(self) -> None:
        c384 = ec.ContractSpec(provider="openai", model="dt", dimension=384).id
        c1536 = ec.ContractSpec(provider="openai", model="dt", dimension=1536).id
        self.cluster.psql(
            f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
            f"values ({c384},'openai','dt',384,1,1,'draft'),({c1536},'openai','dt',1536,1,1,'draft');"
        )
        h = _hash64("b")
        rc, _ = self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            "chunk_index,embedding,representation_hash,chunk_hash) "
            f"values ({c1536},'resource','1','prose',0,'{_vec(384)}'::vector,'{h}','{h}');"
        )
        self.assertNotEqual(rc, 0, "filing a 384 vector under a 1536 contract must be rejected")

    def test_prose_and_workflow_python_distinct_identity(self) -> None:
        cid = ec.ContractSpec(provider="openai", model="id", dimension=384).id
        self.cluster.psql(
            f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
            f"values ({cid},'openai','id',384,1,1,'draft');"
        )
        h = _hash64("c")
        self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            "chunk_index,embedding,representation_hash,chunk_hash) values "
            f"({cid},'resource','42','prose',0,'{_vec(384)}'::vector,'{h}','{h}'),"
            f"({cid},'resource','42','workflow_python',0,'{_vec(384)}'::vector,'{h}','{h}');"
        )
        n = int(self._last(
            "select count(*) from content_embeddings where entity_type='resource' and item_id='42';"
        ))
        self.assertEqual(n, 2)
        types = self._last(
            "select string_agg(distinct representation_type, ',' order by representation_type) "
            "from content_embeddings where entity_type='resource' and item_id='42';"
        )
        self.assertEqual(types, "prose,workflow_python")

    # -- 2.3 atomic contract switch + coverage ----------------------------

    def _seed_switch_contracts(self) -> tuple[int, int]:
        a = ec.ContractSpec(provider="openai", model="sw", dimension=384, canonicalization_version=1).id
        b = ec.ContractSpec(provider="openai", model="sw", dimension=384, canonicalization_version=2).id
        self.cluster.psql(
            f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
            f"values ({a},'openai','sw',384,1,1,'draft'),({b},'openai','sw',384,2,1,'draft');"
        )
        h = _hash64("sw")
        self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            "chunk_index,embedding,representation_hash,chunk_hash) values "
            f"({a},'resource','a1','prose',0,'{_vec(384)}'::vector,'{h}','{h}'),"
            f"({a},'resource','a2','prose',0,'{_vec(384)}'::vector,'{h}','{h}'),"
            f"({a},'resource','a3','prose',0,'{_vec(384)}'::vector,'{h}','{h}'),"
            f"({b},'resource','b1','prose',0,'{_vec(384)}'::vector,'{h}','{h}'),"
            f"({b},'resource','b2','prose',0,'{_vec(384)}'::vector,'{h}','{h}');"
        )
        return a, b

    def test_contract_switch_rejects_low_coverage(self) -> None:
        a, b = self._seed_switch_contracts()
        self.cluster.psql(f"select hivemind_set_active_embedding_contract({a}, true);")
        rc, _ = self.cluster.psql(
            f"select hivemind_set_active_embedding_contract({b}, true);"
        )
        self.assertNotEqual(rc, 0, "low-coverage replacement must be rejected")

    def test_one_active_contract_per_dimension_enforced(self) -> None:
        a, b = self._seed_switch_contracts()
        self.cluster.psql(f"select hivemind_set_active_embedding_contract({a}, true);")
        # Top up B to equal coverage, then switch.
        h = _hash64("sw2")
        self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            f"chunk_index,embedding,representation_hash,chunk_hash) values "
            f"({b},'resource','b3','prose',0,'{_vec(384)}'::vector,'{h}','{h}');"
        )
        rc, _ = self.cluster.psql(f"select hivemind_set_active_embedding_contract({b}, true);")
        self.assertEqual(rc, 0, "equal-coverage switch should succeed")
        n_active = int(self._last(
            "select count(*) from embedding_contracts where status='active' and dimension=384;"
        ))
        self.assertEqual(n_active, 1)
        self.assertEqual(self._last(f"select status from embedding_contracts where id={a};"), "superseded")
        self.assertEqual(self._last(f"select status from embedding_contracts where id={b};"), "active")

    # -- 2.4 snowflake + mapping parity ----------------------------------

    def test_snowflake_item_id_exact_text(self) -> None:
        cid = ec.ContractSpec(provider="openai", model="sn", dimension=384).id
        self.cluster.psql(
            f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
            f"values ({cid},'openai','sn',384,1,1,'draft');"
        )
        h = _hash64("snow")
        snow = "1234567890123456789"
        self.cluster.psql(
            "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
            "chunk_index,embedding,representation_hash,chunk_hash) "
            f"values ({cid},'message','{snow}','prose',0,'{_vec(384)}'::vector,'{h}','{h}');"
        )
        self.assertEqual(self._last("select item_id from content_embeddings where entity_type='message';"), snow)

    def test_result_kind_mapping_parity(self) -> None:
        for k in ("message", "resource", "workflow", "article", "transcript", "distillation"):
            sql_v = self._last(f"select hivemind_entity_type_for_result_kind('{k}');")
            self.assertEqual(sql_v, ei.entity_type_for_result_kind(k), k)

    # -- 2.5 canonical text + hash parity --------------------------------

    def test_canonical_message_parity(self) -> None:
        msg = "how do I lower the motion amplitude in Wan 2.2"
        self.assertEqual(self._text(f"hivemind_canonical_message_text($q${msg}$q$)"), cr.canonical_message_text(msg))
        self.assertEqual(
            self._last(f"select hivemind_representation_hash($q${msg}$q$);"),
            wr.representation_hash(msg),
        )

    def test_canonical_resource_parity(self) -> None:
        title, body, tags = "WanVideo I2V guide", "use WanVideoSampler with LoRA", "wan video lora"
        sql_text = self._text(
            "hivemind_canonical_resource_text($q${}$q$,$q${}$q$,$q${}$q$)".format(title, body, tags)
        )
        py_text = cr.canonical_resource_text(title, body, tags)
        self.assertEqual(sql_text, py_text)
        self.assertEqual(
            self._last(f"select hivemind_representation_hash($q${py_text}$q$);"),
            wr.representation_hash(py_text),
        )

    def test_canonical_distillation_parity(self) -> None:
        q, cond, ans = "best upscaler", "for anime video", "RealESRGAN x2"
        sql_text = self._text(
            "hivemind_canonical_distillation_text($q${}$q$,$q${}$q$,$q${}$q$)".format(q, cond, ans)
        )
        self.assertEqual(sql_text, cr.canonical_distillation_text(q, cond, ans))


if __name__ == "__main__":
    unittest.main()
