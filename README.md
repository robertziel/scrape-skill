# scrape — lean-context scraping fan-out skill

A portable agent skill that fans a scraping/extraction job out across **N parallel
subagents** while keeping the lead agent's context large and each worker's context
small. Workers run on the **same model** as the lead — the win is not a different
model, it's that each worker starts from an **isolated, near-empty context** and is
handed only what it needs: the scrape recipe + the target schema + its URLs. The lead
keeps its full window and the aggregate state.

Two optional arguments: `/scrape [max_agents] [limit]` — parallel workers (default 3) and a
cap on how many targets to scrape. Omit `limit` for **continuous mode**: the scrape runs round
after round until you type `finalise` (or the targets run dry).

## Why

A subagent inherits the parent model but **not** the parent transcript. Its input size
is exactly what you put in its task `context`. So the way to keep workers cheap and the
lead's context flat is simply: pass workers a lean brief, and have them return a short
status while writing their records to a file the lead reads back on demand. This skill
codifies that contract.

## Install

Drop the `scrape/` directory into a project's skills folder:

```sh
# Claude Code / Hermes project
cp -r scrape /path/to/project/.claude/skills/

# or symlink to keep it updated from this repo
ln -s "$(pwd)/scrape" /path/to/project/.claude/skills/scrape
```

Then invoke it as `/scrape <max_agents>` where the skill system registers `.claude/skills`,
or point the agent at `.claude/skills/scrape/SKILL.md` and tell it to follow it.

For Hermes, to resolve `/scrape` as a slash command add the dir to
`skills.external_skill_dirs` in `~/.hermes/config.yaml`:

```yaml
skills:
  external_skill_dirs:
    - /path/to/project/.claude/skills
```

## What it does

See [`scrape/SKILL.md`](scrape/SKILL.md). In short:

1. Build the target list, drop already-known targets.
2. Batch 2–3 targets per worker; dispatch `max_agents` at a time (clamped to the
   delegation concurrency cap).
3. Each worker gets a lean `context` (recipe + schema + URLs + output path + return
   contract) — never the parent conversation.
4. Workers write records to `raw/<label>.jsonl` and return only
   `{path, n_records, n_failed, failures, blocked_hosts}`.
5. The lead reads the `raw/` files in slices; records never ride back in a worker's
   final message.

The SKILL.md is written against a Hermes + chromium-`fetch` + findings-ledger setup
(the `deep_research` project it was extracted from); the pattern and the fan-out
contract are reusable anywhere subagents can be spawned — swap the fetch tool and the
schema source for your own.
