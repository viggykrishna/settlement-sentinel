# Settlement Sentinel

[![CI](https://github.com/viggykrishna/settlement-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/viggykrishna/settlement-sentinel/actions/workflows/ci.yml)

**A payment-scheme reconciliation agent, built by a payments operator. Deterministic matching, rulebook-grounded agentic triage, human-gated resolution — available as a CLI pipeline and as an MCP server that Claude can drive directly. A full 500-item settlement window is triaged for under $0.10, measured and enforced by the run itself.**

I run reconciliation operations for live European payment schemes (instant rails, batch clearing, in-house clearing) and manage the team that triages these exceptions by hand every settlement window. This project automates the part of that workflow where AI genuinely helps — and deliberately keeps it out of the part where it doesn't.

---

## Architecture

```
                 ┌──────────────────────── DOMAIN CORE (deterministic) ───────────────────────┐
camt.053 ──►     │                                                                            │
 adapter  ──►  matcher ──► exceptions ──► triage agent ──► approval gate ──► report + audit log
ledger CSV ──►   │             │       Haiku 4.5 (queue) ──►    │                              │
                 │             │       Sonnet 4.6 (P1 review)   │        cost meter +          │
                 │             │       [budget-guarded, cached] │        hard $ budget         │
                 └─────────────┼────────────────┬───────────────┼──────────────────────────────┘
                               │                │               │
                               │        investigation tools     └── approved resolutions ──┐
                               │        ├ get_scheme_rules  ◄── scheme rulebook semantics  │
                               │        ├ check_next_window                                │
                               │        ├ get_fee_schedule                                 │
                               │        ├ lookup_history  ◄────── learning loop ◄──────────┘
                               │        └ extract_reference
                               │
                     MCP server (src/mcp_server.py)
                     exposes ALL of the above as MCP tools
                     → Claude Desktop / Claude Code drives the
                       whole workflow conversationally
```

Two runtimes, one set of tools:

| Mode | Who is the runtime | Who reasons |
|---|---|---|
| **CLI** (`src/main.py`) | This codebase | Claude via API (agentic tool-use loop) |
| **MCP** (`src/mcp_server.py`) | Claude Desktop / Code | Claude in the client, calling the same domain tools over MCP |

The MCP server deliberately contains **no nested model calls** — in MCP mode, Claude in the client *is* the triage agent, and the server exposes only deterministic, auditable domain tools. Model reasoning happens in one place, visible to the user, never hidden inside a tool result.

## Why a payments operator built this differently

Three design decisions that come from running scheme operations, not from generic engineering judgment:

1. **The matcher is not allowed to be intelligent.** Matching is exact-reference first, then one conservative fuzzy pass (reference-prefix + identical amount + identical value date). A probabilistic model must never decide whether two ledger entries are the same money — because the reconciliation attestation you sign at end of day has to be reproducible. AI enters only *after* matching, where the work is judgment, not arithmetic.

2. **Severity comes from the rulebook, not the pattern.** The same exception means different things on different rails, and the triage agent is forced to load the scheme's settlement semantics (`get_scheme_rules`) before classifying. A missing-in-scheme booking on a **batch-net** scheme is often a cutoff timing difference — check the next cycle before escalating. The identical exception on an **instant rail** can *never* be a same-rail timing difference, because there is no cutoff — it means the payment was booked but never settled. Same pattern, opposite action. A pattern-matching classifier gets this wrong; an analyst who knows the rulebook doesn't.

3. **Autonomy is severity-gated, and the boundary is enforced in code.** The agent auto-closes only P3 + auto-resolvable items (logged as `sentinel-auto`). Everything else requires an explicit human approval, and in non-interactive mode risky items stay OPEN rather than being closed by default. Every decision — automatic or human — lands in an append-only audit log with actor attribution. The agent proposes; the human disposes. The same boundary applies over MCP: `record_resolution` is the only write tool on the server and requires a named human approver.

## The agent investigates before it classifies

For each exception, Claude uses the same checks a senior analyst runs, and must cite its evidence per exception (the full tool-call log goes into the report):

- `get_scheme_rules` — load the settlement semantics in force (finality, duplicate handling, fee treatment, recon window)
- `check_next_window` — is this "missing" settlement just the next cycle? (verified in eval data: timing differences vs genuine gaps are distinguished correctly)
- `get_fee_schedule` — does an amount-mismatch delta exactly match a fee netted at settlement?
- `lookup_history` — how did *this team* resolve the same pattern before? (approved resolutions feed back as few-shot context — the learning loop)
- `extract_reference` — recover a reference buried in free-text remittance info

## v4: The economics of a settlement window (measured, not asserted)

A reconciliation tool that costs more to run than the analysts it assists is a demo, not a product. v4 makes the cost per window a **first-class, enforced property**, the same way v3's eval harness made matcher exactness one.

**Live measured run** — 500 transactions (450 matched deterministically at zero AI cost, 50 exceptions triaged agentically):

| Stage | Model | What it did | Cost |
|---|---|---|---|
| Matching | — (deterministic) | 450/500 matched, 50 exceptions raised | $0.0000 |
| Queue triage | Haiku 4.5 | investigated + classified all 50 exceptions (2 chunks, tools, cached prefix) | $0.0584 |
| Senior review | Sonnet 4.6 | second opinion on every flagged P1, wrote the executive summary | $0.0350 |
| **Total** | | **full 500-item window** | **$0.0934 of a $0.10 budget** |

How it stays under budget:

1. **Tiered routing** — Haiku 4.5 (cheap, fast) clears the whole queue with the full investigation toolset. Anything it rates P1 or marks low-confidence gets a second opinion from Sonnet 4.6 on the already-gathered evidence. This is how a real ops team staffs a window: juniors clear the queue, the senior reviews what's flagged.
2. **Prompt caching** — the system prompt, tool definitions, and the growing investigation transcript are cache-marked, so each tool turn re-reads the prefix at ~10% of the input price. (The first live run exposed a real subtlety: a tool turn with 13 parallel calls exceeded the cache's 20-block lookback window and silently paid full price — fixed by chunking and double breakpoints. The telemetry made the miss visible; that's the point of metering.)
3. **A hard budget guard** — every API call is priced from its actual token usage and checked against the budget *before* the next call is made. Degradation is graceful and fail-safe: first the Sonnet review trims to the highest-exposure items that still fit (observed live: 13 of 17 reviewed, $0.0988 total), then it skips entirely (annotated in the report, never hidden), and as a last resort triage stops and items stay OPEN for a human. The agent never silently overspends and never silently closes an item to save money.

Every run prints a per-model cost breakdown (tokens in/out, cache reads/writes, dollars) and writes it into the report — the moderator can reproduce the number, not take it on faith.

## Evaluation

`evals/run_eval.py` builds a labeled synthetic set (32 exception cases across 8 scenario types, each with ground-truth bucket and rulebook-derived expected severity) and reports precision/recall per class. **Both layers are measured, including the cheap tier alone** — cost optimisation is only a result if quality is proven retained:

| Layer | What's measured | Result (live) | Cost |
|---|---|---|---|
| 1 — deterministic matcher | bucket classification, offline | **1.00 P/R, all 5 classes** (enforced in CI) | $0 |
| 2 — Haiku tier alone (`--no-escalation`) | severity vs rulebook ground truth | **1.00 P/R on P1/P2/P3** | $0.0460 |
| 2 — full tiered (Haiku + Sonnet review) | severity vs rulebook ground truth | **1.00 P/R on P1/P2/P3**; Sonnet reviewed 12 flagged items, 0 revisions needed | $0.0786 |

The eval set includes every case that *requires* investigation to get right (fee vs non-fee mismatches, timing vs genuine gaps). That the Sonnet reviewer found nothing to revise on this set is the measured justification for routing the queue to Haiku; the senior tier exists for the long tail an enumerable eval can't contain — and for the governance requirement that every P1 gets a second pair of eyes.

```bash
python evals/run_eval.py                            # layer 1, offline (runs in CI)
python evals/run_eval.py --with-ai                  # layer 2, tiered (needs ANTHROPIC_API_KEY)
python evals/run_eval.py --with-ai --no-escalation  # layer 2, Haiku tier alone
```

## Same exception, opposite action — the rulebook, demonstrated

The project's central domain claim is demonstrated live in [`docs/RAIL_CONTRAST.md`](docs/RAIL_CONTRAST.md): the same transaction (`STL8B143C4B574B`, booked in the ledger, missing from the scheme file, present in the next window) is **auto-closed as a P3 cutoff-timing difference under `--scheme-profile BATCH_NET`** and **held OPEN as a P2 booked-but-never-settled failure under `--scheme-profile INSTANT_RAIL`** — identical facts from the same tool call, opposite disposition, because the rulebook is what changed. A full generated report is committed at [`docs/sample_report.md`](docs/sample_report.md).

## Real scheme file format: ISO 20022 camt.053

The pipeline natively parses **camt.053 (BankToCustomerStatement)** — the end-of-day statement standard used across SEPA (successor to SWIFT MT940). The adapter (`src/adapters/camt053.py`, pure standard library) handles what makes reconciliation a job rather than a script: version/namespace differences, **bulk entries** exploded into individual transactions, the **reference priority ladder** (`EndToEndId` skipping `NOTPROVIDED` → `TxId` → structured creditor ref → `AcctSvcrRef` → `NtryRef`), and references buried in unstructured remittance text. It is unit-tested against a **real, bank-authored public sample file** (see Data sources).

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python src/generate_data.py     # camt.053 statement + ledger + next-window file + fee schedule
python src/main.py              # reconcile → tiered agentic triage → approval gate → report
python src/main.py --yes        # non-interactive: auto-resolve safe items, leave risky OPEN
python src/main.py --no-ai      # deterministic reconciliation only
pytest tests/                   # matcher + adapter + cost-meter tests

# the v4 cost demo: a 500-item settlement window, triaged for < $0.10
python src/generate_data.py --entries 500
python src/main.py --yes --budget 0.10

# the rulebook contrast: same data, opposite triage (see docs/RAIL_CONTRAST.md)
python src/main.py --yes --scheme-profile INSTANT_RAIL

# against the real public Danske Bank camt.053 sample:
python src/main.py --no-ai --scheme-file data/samples/camt053_danske_example.xml
```

### Run as an MCP server (Claude Desktop / Claude Code)

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "settlement-sentinel": {
      "command": "python",
      "args": ["/absolute/path/to/settlement-sentinel/src/mcp_server.py"]
    }
  }
}
```

Then, in Claude: *"Reconcile today's settlement file against the ledger, investigate the exceptions using the scheme rules, and walk me through what needs my approval."* Claude calls `reconcile_settlement`, investigates with the same tools the CLI agent uses, and records resolutions only via the human-attributed `record_resolution` tool.

## Data sources (public / bank-published samples)

- **Danske Bank camt.053 example file** (included at `data/samples/camt053_danske_example.xml`, used by the test suite): <https://danskeci.com/-/media/pdf/danskeci-com/iso-20022-xml/camt053_dk_example.xml>
- **ISO 20022 official message definitions & schemas** (camt.053 XSD): <https://www.iso20022.org/iso-20022-message-definitions?search=camt.053>
- **Goldman Sachs Developer — camt.052/053 sample reports**: <https://developer.gs.com/docs/services/transaction-banking/camt53-sample>
- **Handelsbanken ISO 20022 XML examples**: <https://www.handelsbanken.com/en/our-services/digital-services/global-gateway/iso-20022-xml>

All sample and generated data in this repository is synthetic or bank-published example data. The scheme rulebook module encodes publicly known settlement-model semantics in generic form — **no proprietary scheme rulebook content is reproduced**. No production settlement data is used anywhere.

## Project structure

```
settlement-sentinel/
├── src/
│   ├── adapters/camt053.py  # ISO 20022 camt.053 parser (stdlib only)
│   ├── generate_data.py     # synthetic data: camt.053 statement, ledger, next window, fees
│   ├── reconcile.py         # deterministic matching engine (exact + safe fuzzy pass)
│   ├── rulebook.py          # scheme settlement semantics (instant vs batch-net)
│   ├── investigation.py     # the agent's investigation tools + Anthropic tool specs
│   ├── triage_agent.py      # tiered agentic triage: Haiku queue + Sonnet review, cached
│   ├── cost_meter.py        # per-call token pricing, budget guard, degradation ladder
│   ├── approval.py          # approval gate, append-only audit log, resolution recording
│   ├── history.py           # resolution history — the learning loop
│   ├── mcp_server.py        # MCP server exposing the whole engine to Claude
│   └── main.py              # CLI orchestration + markdown report
├── evals/run_eval.py        # labeled eval set + precision/recall harness
├── tests/                   # matcher tests + adapter tests against the real bank sample
├── data/samples/            # public camt.053 sample (bank-published)
└── reports/                 # generated reconciliation reports
```

## Trail of updates

Each version is a single commit on `main`, so the evolution is auditable end to end:

| Version | What it added | Why |
|---|---|---|
| **v1** — `bfb80ab` | Deterministic matcher, exception buckets, basic reports | The reproducible core: prove the books balance with no AI in the money path |
| **v2** — `0641ac0` | camt.053 adapter, agentic triage with investigation tools, approval gate, learning loop | Real bank-statement format; AI takes the judgment work; humans keep control; approved resolutions feed back as precedent |
| **v3** — `0718d18` | Scheme rulebook grounding, MCP server, evaluation harness | Severity from settlement semantics, not patterns; Claude Desktop/Code can drive the engine directly; matcher exactness proven at 1.00 precision/recall |
| **v3 docs** — `d9f3555` | `docs/ARCHITECTURE.md` | Problem-space → solution-space explanation for non-technical leaders |
| **v4** | Tiered model routing (Haiku → Sonnet), prompt caching, cost meter with hard budget guard, 500-item open-format demo batch | A full settlement window triaged for a **measured** $0.0934 — cost as an enforced property, with fail-safe degradation |
| **v5** | Layer 2 eval published (1.00 P/R both tiers), `--scheme-profile` rail-contrast demo, CI, approval-gate tests, precedent-poisoning guard | The AI layer's *correctness* measured, not asserted; the signature domain insight demonstrated live; every claim now enforced by CI or an eval |

## Known limits and roadmap

Stated plainly, because a tool for attestations should not oversell itself:

- **`batch_limit` caps AI triage at 64 exceptions per run** (the run warns and leaves the remainder OPEN — fail-safe, but a 1,000-item window with >64 exceptions needs multiple runs today). The natural v6 is the **Message Batches API**: 50% token discount, ideal for non-interactive overnight windows where the answer is needed by morning, not by minute.
- **The learning loop has a precedent-poisoning guard** (only human-approved resolutions become few-shot precedent — agent output never calibrates the agent), but no human review workflow for *correcting* bad precedent yet; in production this would be a supervised table, not a JSON file.
- **The scheme profiles are generic public-knowledge summaries.** A live deployment loads the participant's actual rulebook extracts into `src/rulebook.py` — the module is shaped for that swap.
- **The audit log is append-only JSONL with actor attribution** — the semantics a regulator needs, but production would put it in WORM storage.

## Author

Vigneshwari K — payments product & operations. Launched 7 payment schemes across Hungary and Romania at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing); lead the reconciliation ops team this project is designed for.
