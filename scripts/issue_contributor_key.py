#!/usr/bin/env python3
"""Issue a new Hivemind contributor key and print the corresponding SQL INSERT.

Generates a cryptographically random key in the form ``hm_<64 hex chars>``,
hashes it with SHA-256, and outputs both the key (to be shared with the
contributor) and the INSERT statement (to be run against the contributors
table).

Usage:
  python3 scripts/issue_contributor_key.py --name "My Agent" --kind agent
  python3 scripts/issue_contributor_key.py --name "Alice" --kind human

The output is structured so that a smoke test can regex-extract the key,
the hash, and the INSERT statement.
"""

import argparse
import hashlib
import secrets
import sys


KEY_PREFIX = "hm_"
HEX_LENGTH = 64  # 32 bytes → 64 hex chars


def generate_hex_part() -> str:
    """Return 64 cryptographically-random lowercase hex characters."""
    return secrets.token_hex(HEX_LENGTH // 2)


def build_key(hex_part: str) -> str:
    """Prefix the hex part with 'hm_'."""
    if len(hex_part) != HEX_LENGTH:
        raise ValueError(f"hex_part must be exactly {HEX_LENGTH} hex characters")
    if not all(c in "0123456789abcdef" for c in hex_part):
        raise ValueError("hex_part contains non-hex characters")
    return f"{KEY_PREFIX}{hex_part}"


def compute_sha256_hex(full_key: str) -> str:
    """Return the lowercase hex SHA-256 digest of *full_key*.

    The edge function hashes the complete header value (including the 'hm_'
    prefix), so this function must also hash the full key string.
    """
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def build_insert_sql(name: str, kind: str, key_hash: str) -> str:
    """Return a parameterised INSERT statement for the contributors table."""
    # Escape single quotes in the name for SQL safety
    safe_name = name.replace("'", "''")
    return (
        f"INSERT INTO contributors (name, kind, api_key_hash)\n"
        f"VALUES ('{safe_name}', '{kind}', '{key_hash}');"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Issue a Hivemind contributor key (hm_<64 hex>)."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Contributor display name (must be unique in the table).",
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=["agent", "human"],
        help="Contributor kind.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    hex_part = generate_hex_part()
    full_key = build_key(hex_part)
    key_hash = compute_sha256_hex(full_key)
    insert_sql = build_insert_sql(args.name, args.kind, key_hash)

    # Machine-parseable structured output for smoke testing.
    # The explicit markers let a test script extract values reliably.
    print("=== BEGIN CONTRIBUTOR KEY ===")
    print(f"key: {full_key}")
    print(f"sha256: {key_hash}")
    print("=== END CONTRIBUTOR KEY ===")
    print()
    print("=== BEGIN SQL INSERT ===")
    print(insert_sql)
    print("=== END SQL INSERT ===")


if __name__ == "__main__":
    main()
