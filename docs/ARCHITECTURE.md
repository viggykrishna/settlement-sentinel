# Settlement Sentinel — Architecture Note

*An explanation for non-technical leaders: the problem, why it persists, and how this system solves it — including exactly where AI is used and, just as importantly, where it is deliberately not.*

---

## 1. The problem space

### What settlement reconciliation is

Every day, a bank that participates in a payment scheme (an instant-payments rail, a batch clearing house, a card network) receives a statement from that scheme saying: *"here is every payment we settled for you today."* The bank also has its own internal ledger saying: *"here is every payment we believe we sent or received."*

**Reconciliation is the act of proving those two records describe the same money.** It is not optional. It is how a bank knows that no payment was lost, duplicated, or mis-booked — and it is a regulatory obligation: at the end of each settlement window, someone signs an attestation that the books balance.

### Where it hurts

On a good day, 98–99% of entries match automatically. The pain is the remainder — the **exceptions**:

- A payment in the scheme statement that isn't in the ledger (or vice versa)
- The same payment appearing twice
- Amounts that almost match but differ by a few euros
- A reference number formatted differently on each side

Each exception must be investigated by a human analyst before the attestation can be signed. The analyst asks a familiar set of questions: *Is this just a timing difference — will it appear in the next settlement cycle? Does that €1.20 difference exactly match a scheme fee? Have we seen this pattern before, and how did we resolve it then? How urgent is this — is money actually at risk?*

### Why this is expensive and risky

1. **It is judgment work done under time pressure.** Settlement windows close on a schedule. The team triages every exception by hand, every window, every day.
2. **The judgment is scheme-specific.** The *same* exception means *opposite* things on different rails. A "missing" settlement on a batch clearing scheme is usually a harmless cutoff-timing difference — check the next cycle. The identical exception on an instant rail can **never** be a timing difference (instant rails have no cutoff): it means a payment was booked but never actually settled. Getting this wrong in either direction is costly — either you escalate noise, or you sit on a real loss.
3. **Duplicates on instant rails are time-critical.** Funds moved twice, with finality. Recovery odds drop sharply once the beneficiary account is drained. Every hour of triage delay has a price.
4. **Experience walks out the door.** The knowledge of "we've seen this pattern before, and here is how we resolved it" lives in analysts' heads and chat threads, not in a system.

### Why generic automation hasn't solved it

Rules engines catch the easy, repetitive cases but cannot reason about a novel combination. And a naive "let AI decide" approach fails the two tests that matter in this domain:

- **Reproducibility** — the attestation you sign must be reconstructible. A probabilistic model must never decide whether two ledger entries are the same money.
- **Accountability** — a regulator will ask *who* closed this exception and *why*. "The model closed it" is not an acceptable answer.

---

## 2. The solution space

Settlement Sentinel splits the workflow along exactly that line: **deterministic where the work is arithmetic, AI-assisted where the work is judgment, human-gated where the decision carries risk.**

```
scheme statement (camt.053) ──►┐
                               ├──► MATCHER ──► exceptions ──► TRIAGE AGENT ──► APPROVAL GATE ──► report
internal ledger (CSV) ─────────►┘   (deterministic)            (Claude + tools)   (human)          + audit log
```

### Stage 1 — Deterministic matching (no AI, by design)

The matcher pairs scheme entries with ledger entries: exact reference match first, then one conservative fuzzy pass (same amount, same value date, reference-prefix match). It is plain, auditable code. Run it twice on the same files and you get byte-identical results. **This is the reproducibility guarantee the attestation depends on.** Anything it cannot match becomes an exception, sorted into one of five buckets (missing-in-ledger, missing-in-scheme, amount mismatch, duplicate, reference-format drift).

### Stage 2 — Agentic triage (AI, with the analyst's toolbox)

Each exception goes to Claude — but not as a one-shot "guess the severity" prompt. The agent is given the same investigation tools a senior analyst uses, and must gather evidence before classifying:

| Tool | The analyst question it answers |
|---|---|
| `get_scheme_rules` | *What does the rulebook say happens on this rail?* (finality, cutoffs, fee treatment) |
| `check_next_window` | *Is this "missing" payment just in the next settlement cycle?* |
| `get_fee_schedule` | *Does this amount difference exactly match a scheme fee?* |
| `lookup_history` | *How did our team resolve this pattern last time?* |
| `extract_reference` | *Is the reference buried in the free-text remittance field?* |

The output per exception is a severity (P1/P2/P3), a recommended action, and — critically — **the full log of every tool call the agent made**, embedded in the report. The agent shows its working, the way an analyst would annotate a case file.

### Stage 3 — The approval gate (human, enforced in code)

Autonomy is severity-gated, and the boundary is code, not policy text:

- Only the lowest-severity, explicitly auto-resolvable items (P3) are closed automatically, logged under the actor `sentinel-auto`.
- Everything else requires a named human to approve the resolution. In non-interactive runs, risky items **stay open** rather than defaulting to closed.
- Every decision — automatic or human — is appended to an immutable audit log with actor attribution.

**The agent proposes; the human disposes.** That single sentence is the governance model, and it answers the regulator's question directly.

### Stage 4 — The learning loop

When a human approves a resolution, that outcome is stored and fed back to the agent as precedent for future triage (`lookup_history`). The institutional knowledge that used to live in analysts' heads accumulates in the system — with every entry traceable to the person who approved it.

---

## 3. What each version added

| Version | What it added | Why it matters |
|---|---|---|
| **v1** | Deterministic matcher, exception buckets, basic reports | The reproducible core: proves the books balance without AI |
| **v2** | camt.053 adapter (the ISO 20022 bank-statement standard), agentic triage with investigation tools, approval gate, learning loop | Real input format; AI does the judgment work; humans keep control; the system starts learning |
| **v3** | Scheme rulebook module, MCP server, evaluation harness | See below — grounding, a second runtime, and proof of correctness |
| **v4** | Tiered model routing, prompt caching, cost meter with a hard budget guard | See below — the economics of a settlement window, measured and enforced |

### v3 in detail

**Scheme rulebook (`src/rulebook.py`).** Severity now comes from *settlement semantics*, not pattern-matching. The module encodes two scheme profiles — `INSTANT_RAIL` (continuous gross settlement, immediate finality, no cutoffs) and `BATCH_NET` (deferred net settlement, fixed cycles, recall windows) — and the triage agent is instructed to load the applicable rulebook *first* and ground every severity decision in it. This is the difference between a pattern classifier and an analyst who knows the rules. (The profiles describe publicly known settlement models in generic form; no proprietary rulebook content is reproduced. In a live deployment this is where the participant's actual rulebook extracts load.)

**MCP server (`src/mcp_server.py`).** The whole engine is now also exposed over the Model Context Protocol, so Claude Desktop or Claude Code can drive the workflow conversationally: *"Reconcile today's statement, investigate the exceptions, and tell me which ones I need to look at."* One deliberate design decision: in MCP mode the server contains **no hidden model calls** — Claude in the client *is* the triage agent, and the server exposes only deterministic, auditable domain tools. Model reasoning happens in one place, visible to the user, never buried inside a tool result. The approval boundary carries over: `record_resolution` is the only tool that writes anything, and it requires a named human approver.

**Evaluation harness (`evals/run_eval.py`).** The two layers make different promises, so they are measured separately:

- *Layer 1 — the matcher* promises exactness. The harness injects a labeled synthetic exception set and verifies the matcher classifies every bucket at 1.00 precision and recall. Anything less is a bug, not a tuning problem. (Current result: 1.00 across all five classes.)
- *Layer 2 — the triage agent* promises sound judgment. The harness derives ground-truth severities from the rulebook semantics (e.g. *duplicate on the scheme side → P1*; *amount mismatch whose delta equals a known fee → P3*) and scores the agent's severity calls against them.

### v4 in detail: cost as an enforced property

A tool that costs more to run than the work it saves never leaves the pilot phase. v4 makes the cost of a settlement window a measured, enforced number rather than a hope. In a live run, a **500-transaction window** (450 matched deterministically at zero AI cost, 50 exceptions investigated and triaged) completed for **$0.0934 against a hard $0.10 budget**.

Three mechanisms, each with a direct operations analogy:

**Tiered model routing (`src/triage_agent.py`).** The fast, inexpensive model (Haiku) clears the entire exception queue with the full investigation toolset — exactly like the junior analysts who work the queue. Everything it rates P1, and everything it marks low-confidence, goes to the stronger model (Sonnet) for a second opinion on the evidence already gathered — exactly like the senior analyst who reviews only what is flagged. The senior model also writes the executive summary. Roughly 60% of the AI spend goes to the junior tier, 40% to the senior review.

**Prompt caching.** Each investigation turn re-sends the conversation so far; caching means the repeated prefix is re-read at about a tenth of the normal input price instead of being re-processed in full. Getting this right required chunking the batch so each turn stays inside the cache's technical limits — the first live run showed a silent cache miss in the telemetry, which is precisely why the metering exists.

**A hard budget guard (`src/cost_meter.py`).** Every API call is priced from its actual token usage, and the projected cost of the *next* call is checked against the budget before it is made. When money runs short the system degrades gracefully, in order: the senior review trims to the highest-exposure items that still fit; then it is skipped entirely (recorded in the report, never hidden); as a last resort triage stops and the remaining exceptions stay OPEN for a human. The failure mode is always "a human looks at it," never "the system overspent" and never "the system closed something to save money."

---

## 4. Design principles, in one place

1. **The matcher is not allowed to be intelligent.** Matching money is arithmetic; it must be reproducible. AI enters only after matching, where the work is judgment.
2. **Severity comes from the rulebook, not the pattern.** The same exception means opposite things on different rails; the agent must reason from settlement semantics.
3. **Autonomy is severity-gated and the boundary is enforced in code.** Auto-close only for the lowest-risk class; everything else requires a named human; unattended runs fail safe (items stay open).
4. **Every decision is attributable.** Immutable audit log, actor on every entry — human or `sentinel-auto`.
5. **The agent shows its working.** Full investigation logs in every report; no conclusion without cited evidence.
6. **Model reasoning happens in one visible place.** Never nested inside tools where the user can't see it.

---

## 5. What this is and isn't

**It is** a working demonstration of how a payment-operations team can use an AI agent for exception triage without surrendering reproducibility, accountability, or control — built by someone who runs this workflow in production for live European payment schemes.

**It isn't** a production deployment: the data is synthetic, the scheme profiles are generic public-knowledge summaries, and a live rollout would add real rulebook extracts, entitlement controls, and integration with the bank's case-management system. The architecture is deliberately shaped so those are additive steps, not redesigns.
