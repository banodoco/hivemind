#!/usr/bin/env bash
# Install banodoco-search as a Claude Code skill.
# Usage: bash install.sh   (run from a clone of this repo)
#        OR pipe via curl:
#          curl -fsSL https://raw.githubusercontent.com/banodoco/discord-search/main/install.sh | bash

set -euo pipefail

SKILL_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "→ Cloning banodoco/discord-search…"
git clone --depth 1 https://github.com/banodoco/discord-search "$TMP_DIR/repo" >/dev/null 2>&1

mkdir -p "$SKILL_DIR"
rm -rf "$SKILL_DIR/banodoco-search"
cp -r "$TMP_DIR/repo/banodoco-search" "$SKILL_DIR/banodoco-search"

echo "✓ Installed to $SKILL_DIR/banodoco-search"
echo
echo "Verify with:"
echo "  curl -s 'https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed?select=content,channel_name&limit=2' \\"
echo "    -H 'apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx'"
echo
echo "Then restart Claude Code and try: /banodoco-search"
