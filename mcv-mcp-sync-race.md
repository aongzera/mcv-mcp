# mcv MCP — two sync defects observed 2026-08-23

Written after a live session where `list_assignments` returned an incomplete
result set and nothing in the response indicated it was incomplete.

## TL;DR

1. **The sync-run record is never finalized.** Run #15 wrote ~5 assignments,
   11 materials and 22 events into the DB, yet `auth_status` still reports
   `finished_at: null, ok: 0, counts: null` for it long after it finished.
   There is no reliable way to tell "running" from "finished" from "crashed".
2. **Readers observe a partially-applied sync.** `list_assignments` called
   while run #15 was in flight returned only the rows committed so far. The
   response looked completely normal — no staleness flag, no warning.

Together these mean a caller can get a confidently-wrong answer and have no
way to detect it.

## Timeline (all UTC)

| Time | Event |
|---|---|
| 15:26:30 | Sync run #15 `started_at`. Row written to the sync-runs table. |
| ~15:26:3x | `auth_status` → `last_sync {id:15, finished_at:null, ok:0}`, `row_counts.assignments = 20` |
| ~15:26:3x | `list_assignments` (same batch) → **7 upcoming assignments**. No indication a sync was in progress. |
| 15:27:18 | 5 assignment rows land with this exact `first_seen` |
| ~15:4x | `list_assignments include_past=true` → **25 assignments** |
| ~15:5x | `auth_status` → `row_counts.assignments = 25`, `materials 112→123`, `events 1→23`, **but `last_sync` is still `{finished_at: null, ok: 0, counts: null}`** |

## The five rows that appeared mid-session

All share `first_seen: 2026-08-23T15:27:18+00:00`:

- `87118:2097088` — HW3 Fourier Series & Transform (CEM II) — **due that same night, 23:59**
- `87509:2150428` — A04: Pages, Props, Styles — due 27 Aug
- `86405:2104416` — Review Questions #3 — due 30 Aug
- `87396:2150845` — Lab 2 (Embedded) — due 20 Aug (past)
- `85489:2105790` — Assignment 5 (SE Lab) — due 21 Aug (past)

Three of those five were **future-dated**, so they were not hidden by the
`include_past: false` default — they simply did not exist in the DB yet when
the first read happened. The user asked "any assignment for CEM2?" and the
first listing had no CEM II row at all, despite one being due in ~90 minutes.

## Defect 1 — sync run never marked complete

`row_counts` prove the run did real work and did not die early (three separate
tables grew). But its sync-runs row is untouched since insert.

Likely causes, in rough order of probability:

- The completion `UPDATE` is on a path that a `return` / early exit skips.
- The run is committed per-table and the finalize step is outside the
  transaction that actually commits, so it's rolled back or never reached.
- The finalize writes to a different row id than the one inserted at start.
- The background poller (`poll_minutes: 30`) inserts the run row but the
  worker that finishes it has no handle on that id.

`error` is also `null`, so the failure path isn't being taken either — this
looks like the success path just never closing the record.

**Fix:** wrap the run in `try/finally`, and in the `finally` set
`finished_at`, `ok`, `counts`, and `error` on the same row id. Assert in a
test that after `sync_now()` returns, `last_sync.finished_at is not None`.

## Defect 2 — no read isolation, no staleness signal

A reader hitting the DB mid-sync sees whichever rows have been committed.
Nothing in the `list_*` response says so.

Pick one of these:

- **Atomic swap (best).** Sync writes into a staging table; one transaction
  swaps it in at the end. Readers see the old set or the new set, never half.
- **Single transaction per run.** Simpler if the sync is small enough — don't
  commit per course/page.
- **Staleness flag (cheapest).** Add `sync_in_progress: true` (and maybe
  `last_completed_sync`) to every `list_*` / `get_*` response so the caller
  can say "data may be incomplete" instead of asserting a wrong answer.

The flag is worth adding even alongside the atomic swap — it turns a silent
wrong answer into a visible caveat.

## Note on `ok: 0`

`ok` reads as an int-flavoured boolean. If it starts life as `0` and is only
raised to `1` on success, then "never started", "in progress", "crashed" and
"finished but finalize skipped" are all indistinguishable. Consider a real
`status` enum: `running | ok | error`.

## Reproduce

1. Trigger a sync (`sync_now`, or wait for the 30-minute poller).
2. Immediately call `list_assignments`.
3. Wait ~60s, call it again — compare counts.
4. Call `auth_status` — observe `last_sync.finished_at` is still null.

## Done when

- After `sync_now()` returns, `last_sync` has `finished_at`, a `status`, and `counts`.
- `list_assignments` during a sync either blocks, returns the previous complete
  snapshot, or is explicitly flagged as in-progress — never a silent partial.
- A test asserts assignment count does not shrink-then-grow across a sync boundary.

## Sidebar: DB state dir

`C:\Users\velys\AppData\Local\mcv_mcp`
