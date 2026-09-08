# Knowledge-model handover

Start with [START-HERE.md](./START-HERE.md). The plan and run state are
planning artifacts captured before implementation; the handover recipient is
instructed to activate `mode: delivery` and execute T1–T10.

The Megado skill is maintained in the public `poms-skills` repository:

```bash
git clone https://github.com/peteromallet/poms-skills.git ~/.local/share/poms-skills
git -C ~/.local/share/poms-skills pull --ff-only
mkdir -p ~/.codex/skills
test -e ~/.codex/skills/megado || ln -s ~/.local/share/poms-skills/megado ~/.codex/skills/megado
```

The second command is for an existing checkout. Do not overwrite an existing
user-managed `~/.codex/skills/megado` entry. Agents without Codex can read
`~/.local/share/poms-skills/megado/SKILL.md` directly.
