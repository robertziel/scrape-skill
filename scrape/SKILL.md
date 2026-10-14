---
name: scrape
description: Fan out a scraping/extraction job across N parallel Hermes `delegate_task` children that run on the SAME local model as the lead but with lean, isolated context — each child gets only how-to-scrape + the target schema + its URLs, writes records to the run's raw/ folder, and returns a short status. The lead keeps its full context window and the aggregate state. Use when a research run has many URLs/entities to scrape and you want controlled parallelism; the max number of parallel agents is the skill's argument.
version: 1.1.0
author: Robert Zieliński
license: MIT
metadata:
  hermes:
    tags: [scrape, fan-out, delegate, parallel, research]
    related_skills: []
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
  Clamp to `delegation.max_concurrent_children` in `$HERMES_HOME/config.yaml` (default 10); never
  exceed it — cost and load on your model endpoint scale linearly with children.
- **limit** (second) — how many targets to scrape before stopping on its own. **Omit it for
  unlimited**: the scrape runs continuously, worker after worker, and ends only when you type the
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
- The model's context window is a ceiling, not a per-call cost. A short child
  prompt uses little KV cache and runs fast even though the ceiling is large.

## Run it

1. **Build the target list** the lead already has: `[{id, url(s)}]`. Drop already-known entities
   first (`find_entities`) and anything `due_checks` will handle — scrape only what is new.
2. **One subject per child.** A subject = one entity (a company, a board, an event series) with
   its URLs. The child researches it, writes it to disk and ends; the next subject starts from a
   fresh context. At most `max_agents` in flight (rolling pool, step 4).
3. **Give every child the same lean `context`**, filled with its own subject:
   - "Everything you need is in this brief. Do not open skills (`skill_view`), do not use
     `web_search`, crawl4ai, curl or WebFetch — fetch pages with the chromium `fetch(url, kind=…)`
     tool only." Pagination / dedupe / stop rule; `blocked` or 404 → report it, never invent absence;
   - the **budget**: "≤ 25 tool calls, then return the contract with what you have" (Hermes
     compacts a child at 40K tokens — `delegation.compression_threshold_tokens`);
   - the **target schema** (the project's `findings/schema.json` shape for the entities it scrapes);
   - its subject and target URLs; `<label>` = the subject's slug;
   - the **subject memory** — two files the child owns, re-read if the subject is resumed:
     - records: `<project>/findings/runs/<run>/raw/<label>.jsonl` — append each record the moment
       it is extracted, with the `target` URL it came from (lets the lead see progress and split
       leftovers). On resume: "continue from these files; don't refetch targets already there";
     - found links: `raw/links/<label>.jsonl` (see "Found-links cache");
   - the **return contract** (below). Do NOT paste prior results, other children's output, or the
     lead's conversation.
4. **Dispatch one `delegate_task` call per worker** — never one batch call for all of them. A
   batch is ONE async unit that joins on every child, so the slowest worker holds every other
   result back. Separate calls return separately:
   ```
   delegate_task(tasks=[{goal: "Scrape live roles from these 3 boards into JSONL per the schema",
                         context: "<scrape recipe + schema + urls + output path + return contract>"}])
   ```
   **Rolling pool:** keep `max_agents` workers in flight; each time one returns, dispatch the next
   batch from the frontier at once. No round barrier.
5. **Finalise the straggler, split its leftovers — in the same turn, no waiting.** When the
   frontier is empty and every other in-flight worker has returned:
   - `delegate_task(action="steer", subagent_id=<id>, message="FINALISE: finish the URL you are
     on, append it to <path>, start no new URL, return the status contract now.")`
   - Leftovers = the URLs you assigned it − the `target` URLs already in its `raw/` file (workers
     append each record as they go, step 3). Computed from disk, not from its self-report.
   - Split the leftovers across fresh workers at once (up to `max_agents − 1` — the straggler still
     counts against the cap — 1–2 URLs each) — they start from an empty context and finish faster
     than the straggler would. They resume the same subject: same records and links paths with a
     `-<n>` suffix on `<label>`. The URL it was on may be scraped twice; the ledger dedupes on keys.
   - If `delegate_task(action="list")` still shows it running when the fresh workers return,
     `delegate_task(action="stop", subagent_id=<id>)` — its partial result still comes back.
   - Leftover URLs go into `seen` only when a worker actually covered them.
6. **Return contract** — each child's final message is ONLY:
   `{"path": "...raw/<label>.jsonl", "n_records": N, "n_failed": M,
     "failures": [{"url","reason"}] (cap ~10), "blocked_hosts": [...], "unfinished": [url, ...],
     "n_links": K}`
   (`unfinished` is informational; the lead trusts the `raw/` file).
   Child records (page bodies, extracted rows) go to the `raw/` file, **never** into the final
   message — that is what would flood the lead's context.
7. **Lead collects**: read each `raw/*.jsonl` in slices as needed, not all at once. Then hand the
   verified records to the ledger the normal way (`research-run`: `open_run` → `add_findings`).
   Re-check every URL before it enters findings (subagents can hallucinate URLs — `fetch` is a
   cache hit if the child really fetched it).

## Found-links cache

A child stays on its subject. Links it meets fall in two groups — **the child decides** which:

- **Related** (the subject's own pages, its job board, its press or team page) → may follow,
  within the budget.
- **Not related but worth researching** (another company, a board listing many employers, an
  ecosystem/community page) → append one line to `raw/links/<label>.jsonl` and **never follow
  it**: `{"url", "found_on", "subject", "anchor", "hint", "ts"}` (`found_on` = the fetched page it
  was on, `hint` = one line on why it may matter, `subject` = the child's own subject). Only URLs
  that appear on a fetched page — never a guessed one. Cap 30 lines per subject.

The lead turns the cache into next subjects at replan (Continuous mode, step 3); a cached URL
enters findings only after its own worker fetches it.

## Caps and knobs (local vLLM, same model)

- `delegation.max_concurrent_children` (config, default 10) is the hard ceiling for `max_agents`.
  It counts children in flight across ALL `delegate_task` calls; a dispatch at capacity is
  rejected and runs synchronously, blocking the lead — never put more than `max_agents` in flight.
- Per-child budget (Hermes `delegation:`): `compression_threshold_tokens: 40000` (compaction
  trigger) and `child_timeout_seconds: 900` (inactivity cap). The 25-tool-call budget is in the
  brief: `delegate_task` exposes no per-call `max_iterations`, and the global one covers every
  delegation on the box.
- One served model handles lead + workers; no second server is needed.
- Only stand up a second vLLM instance with a smaller `--max-model-len` (→ more `--max-num-seqs`)
  if you genuinely need more than `max_concurrent_children` workers in flight — that is a
  throughput change, not required for this skill.

## When the scrape ends

The scrape runs as a **rolling pool** of up to `max_agents` workers; each returned worker's
`raw/*.jsonl` is counted into a one-line progress tally (targets done, records, failures,
blocked). It stops at the **first** of:

1. **Exhausted** — no new (unseen) targets left **and** no productive pivot remains (see
   "Continuous mode"). In unlimited mode, if more targets can still appear (a running discovery
   feed) or a pivot is open, say so and idle awaiting finalise or new URLs rather than ending.
2. **`limit` reached** — a `limit` was given and that many targets have been scraped.
3. **You finalise** — you type **`finalise`** (or "stop scrape"). The lead stops dispatching new
   workers and steers every in-flight child with the FINALISE message (step 5); URLs they did
   not cover stay out of `seen` for a later run.

So **unlimited mode (no `limit`) keeps going until you finalise** — it will not end on its own
while targets keep coming. Continuous/finalise mode needs an **interactive session**
(desk/gateway): a one-shot `hermes -z` run has no one to type finalise, so pass a `limit` there.

**Finalise** = stop dispatching, wait for in-flight workers, then consolidate every
`raw/*.jsonl` and hand the records to the ledger the normal way (`research-run`: `open_run` →
`add_findings`, re-checking each URL first). Report the totals and where they landed.

Done means: every scraped target written to a `raw/<label>.jsonl`, the lead holding only the
running tallies and condensed statuses (never the records), and the records consolidated into
a ledger run. If the lead's context grew with the scraped data, a child returned records in its
final message instead of writing them to `raw/`.

## Continuous mode: epochs, dedupe, replan

Unlimited mode (no `limit`) runs in **epochs** — one epoch is one pass over the current target
frontier (its rolling pool of workers). Between epochs the lead dedupes, concludes, and replans, all
from disk so compaction can't lose state.

1. **Dedupe across epochs (never re-loop the same data).** Keep a persistent **seen-set** of
   canonical target keys in `raw/seen.txt` — URLs in the form `scripts/links_queue.py canon <url>`
   prints (entity keys as they are). Before dispatching a
   worker, drop every target already in `seen` and every entity `find_entities` reports as known.
   After a worker returns, append the keys it covered to `seen`. A key in `seen` is never
   dispatched again.
2. **Conclusions after each epoch** — the lead reads only this epoch's `raw/*.jsonl` + tallies
   (never pull records back into context): yield per source/segment, score distribution, where
   the best/most data came from, which sources were dry or blocked.
3. **Replan from the conclusions** — the lead is the analyst with full context. Build the next
   epoch's plan: double down on the highest-yield sources/segments, drop the dry ones, and
   **pivot to an adjacent target** (new query / segment / geography / entity type) when the
   frontier is exhausted or low-yield. The found-links cache is the main input for the pivot:
   - run `python3 <skill dir>/scripts/links_queue.py tally <raw dir>` — ≤ 30 hosts ranked by
     votes (distinct subjects that cached them), never `raw/links/*` itself;
   - `find_entities` on the candidates; drop known ones;
   - pick ≤ `max_agents × 2` new subjects, ≤ 2 per host (the fetch layer serialises one host),
     each one judged against the run's question and scope;
   - write `## Epoch N dispatch` (subject · host · votes · why) and `## Rejected` (host · why) to
     `raw/plan.md`.
   Persist plan + seen-set to disk (`raw/plan.md`, `raw/seen.txt`) before starting the epoch.
4. **Exhaustion** (stop condition 1 above) now means: no new unseen targets **and** no
   productive pivot left. `limit` and user `finalise` are unchanged.
5. The lead's context stays **flat** across epochs — conclusions come from files and tallies,
   never from holding all records in the window.
