---
name: scrape
description: Fan out a scraping/extraction job across N parallel Hermes `delegate_task` children that run on the SAME local model as the lead but with lean, isolated context — each child gets only how-to-scrape + the target schema + its URLs, writes records to the run's raw/ folder, and returns a short status. The lead keeps its full context window and the aggregate state. Use when a research run has many URLs/entities to scrape and you want controlled parallelism; the max number of parallel agents is the skill's argument.
---

# Scrape fan-out (lean workers, same model, capped parallelism)

Spawn many scraper subagents without inflating the lead's context. Children run on the
**same** model as the lead (`delegate_task` inherits the parent model) — the win is not a
different model, it is that each child starts from an **isolated, near-empty context** and
is handed only what it needs to scrape. The lead keeps its big window and the running totals.

This skill is the fetch-and-extract stage. Validated findings still go through the ledger
(`research-run` skill / `CLAUDE.md`); this skill only produces the per-worker `raw/` files the
lead reads back.

## Arguments

Two optional positional numbers: `/scrape [max_agents] [limit]`.

- **max_agents** (first) — max workers running in parallel (the fan-out cap). Default **3**.
  Clamp to `delegation.max_concurrent_children` in `~/.hermes/config.yaml` (default 10); never
  exceed it — cost and load on the single local vLLM LB (`:8010`) scale linearly with children.
- **limit** (second) — how many targets to scrape before stopping on its own. **Omit it for
  unlimited**: the scrape runs continuously, round after round, and ends only when you type the
  finalise signal (see "When the scrape ends") or the target list runs dry.
- `/scrape 6` → 6 workers, unlimited (runs until you finalise). `/scrape 6 50` → 6 workers, stop
  after 50 targets.

## Why children stay small (don't fight this, it's the default)

- `delegate_task` children spawn with isolated context: `skip_context_files=True`,
  `skip_memory=True`, **no parent transcript**. A child sees only its `goal` + the `context`
  string you pass (`tools/delegate_tool.py`). So a child's input size is exactly what you put in
  `context` — keep it to the scrape recipe + schema + URLs. Nothing else.
- Children inherit the parent model unless `delegation.model`/`delegation.provider` is set in
  config. You want the same model here, so leave those unset.
- The model's window (131072 on the local Qwen) is a ceiling, not a per-call cost. A short child
  prompt uses little KV cache and runs fast even though the ceiling is large.

## Run it

1. **Build the target list** the lead already has: `[{id, url(s)}]`. Drop already-known entities
   first (`find_entities`) and anything `due_checks` will handle — scrape only what is new.
2. **Batch 2–3 targets per child** (4+ tends to hit the 600 s stall watchdog). Number of children
   = `ceil(targets / batch)`, dispatched `max_agents` at a time.
3. **Give every child the same lean `context`**, filled with its own URLs:
   - the scrape recipe: use the chromium `fetch(url, kind=…)` tool only (never curl/WebFetch);
     pagination / dedupe / stop rule; `blocked` or 404 → report it, never invent absence;
   - the **target schema** (the project's `findings/schema.json` shape for the entities it scrapes);
   - its 2–3 target URLs (or a path to a URL-list file);
   - the **output path**: `<project>/findings/runs/<run>/raw/<label>.jsonl`;
   - the **return contract** (below). Do NOT paste prior results, other children's output, or the
     lead's conversation.
4. **Dispatch** with `delegate_task` batch mode, `max_concurrent_children` = `max_agents`:
   ```
   delegate_task(tasks=[
     {goal: "Scrape live roles from these 3 boards into JSONL per the schema",
      context: "<scrape recipe + schema + urls + output path + return contract>"},
     ...
   ])
   ```
5. **Return contract** — each child's final message is ONLY:
   `{"path": "...raw/<label>.jsonl", "n_records": N, "n_failed": M,
     "failures": [{"url","reason"}] (cap ~10), "blocked_hosts": [...]}`.
   Child records (page bodies, extracted rows) go to the `raw/` file, **never** into the final
   message — that is what would flood the lead's context.
6. **Lead collects**: read each `raw/*.jsonl` in slices as needed, not all at once. Then hand the
   verified records to the ledger the normal way (`research-run`: `open_run` → `add_findings`).
   Re-check every URL before it enters findings (subagents can hallucinate URLs — `fetch` is a
   cache hit if the child really fetched it).

## Caps and knobs (local vLLM, same model)

- `delegation.max_concurrent_children` (config, default 10) is the hard ceiling for `max_agents`.
- One served model on `:8010` handles lead + workers; no second server is needed.
- Only stand up a second vLLM instance with a smaller `--max-model-len` (→ more `--max-num-seqs`)
  if you genuinely need more than `max_concurrent_children` workers in flight — that is a
  throughput change, not required for this skill.

## When the scrape ends

The scrape runs in **rounds**: each round takes the next batch of targets, fans out up to
`max_agents` workers, writes their `raw/*.jsonl`, and prints a one-line progress tally (targets
done, records, failures, blocked). It stops at the **first** of:

1. **Targets exhausted** — nothing new left to scrape. In unlimited mode, if more targets can
   still appear (a running discovery feed), say so and idle awaiting finalise or new URLs rather
   than ending.
2. **`limit` reached** — a `limit` was given and that many targets have been scraped.
3. **You finalise** — you type **`finalise`** (or "stop scrape"). The lead stops dispatching new
   workers and lets in-flight children finish.

So **unlimited mode (no `limit`) keeps going until you finalise** — it will not end on its own
while targets keep coming. Continuous/finalise mode needs an **interactive session**
(desk/gateway): a one-shot `hermes -z` run has no one to type finalise, so pass a `limit` there.

**Finalise** = stop dispatching, wait for in-flight workers, then consolidate every
`raw/*.jsonl` and hand the records to the ledger the normal way (`research-run`: `open_run` →
`add_findings`, re-checking each URL first). Report the totals and where they landed.

Done means: every scraped target written to a `raw/<label>.jsonl`, the lead holding only the
per-round tallies and condensed statuses (never the records), and the records consolidated into
a ledger run. If the lead's context grew with the scraped data, a child returned records in its
final message instead of writing them to `raw/`.
