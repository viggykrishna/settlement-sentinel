# Same exception, opposite action — the rulebook demo

The claim at the heart of this project: **severity comes from the scheme's
settlement semantics, not from the exception's pattern.** A pattern
classifier sees "booked in the ledger, missing from the scheme file" and
gives one answer. An analyst who knows the rulebook gives two different
answers depending on the rail. This document shows the agent doing exactly
that, on the same 500-item dataset, in two live runs:

```bash
python src/main.py --yes                                # BATCH_NET (default)
python src/main.py --yes --scheme-profile INSTANT_RAIL  # same data, instant-rail rulebook
```

## The exception

Transaction `STL8B143C4B574B`, amount **86,761.16** — booked in the internal
ledger, absent from today's scheme settlement file, and **present in the
next settlement window** (the agent's `check_next_window` tool confirms
this in both runs — the *facts* are identical).

## Run 1 — BATCH_NET (deferred net settlement, fixed daily cycles)

> ### [P3] MISSING_IN_SCHEME_FILE — ref `STL8B143C4B574B` — amount 86761.16 ✅
> - **Status:** AUTO_RESOLVED
> - **Root cause:** Ledger entry found in next settlement window; cutoff timing difference.
> - **Auto-resolvable:** yes

On a batch scheme, a booking that misses today's cutoff settles in
tomorrow's cycle. Routine. The agent verifies it in the next window,
classifies P3, and the approval gate closes it automatically (actor
`sentinel-auto`, in the audit log).

## Run 2 — INSTANT_RAIL (continuous gross settlement, no cutoffs)

> ### [P2] MISSING_IN_SCHEME_FILE — ref `STL8B143C4B574B` — amount 86761.16 ⏳
> - **Status:** OPEN
> - **Root cause:** Payment booked but never settled; appears in next window (anomalous retry).
> - **Auto-resolvable:** no

On an instant rail there is no batch cutoff, so "it'll settle next window"
does not exist as an explanation. The identical next-window appearance is
now *anomalous* — a payment the participant booked but the rail never
settled, resurfacing where nothing should resurface. The agent escalates,
refuses auto-resolution, and the item stays OPEN for a named human.

## Why this matters

Same exception bucket. Same amounts. Same tool call returning the same
fact. **Opposite classification, opposite disposition** — because the agent
is required to load the rulebook (`get_scheme_rules`) before judging, and
the rulebook is what changed. This is the difference between pattern
matching and reconciliation: the pattern is the question, the settlement
semantics are the answer.

Both runs completed inside the same $0.10 budget ($0.0949 and $0.0961),
with the cost meter and degradation ladder active. Full reports are
generated under `reports/`; a committed example is at
[`sample_report.md`](sample_report.md).
