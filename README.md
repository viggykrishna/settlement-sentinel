# Settlement Sentinel

**A payment-scheme reconciliation agent, built by a payments operator. Deterministic matching, rulebook-grounded agentic triage, human-gated resolution — available as a CLI pipeline and as an MCP server that Claude can drive directly.**

I run reconciliation operations for live European payment schemes (instant rails, batch clearing, in-house clearing) and manage the team that triages these exceptions by hand every settlement window. This project automates the part of that workflow where AI genuinely helps — and deliberately keeps it out of the part where it doesn't.

---

## Architecture

```
                 ┌──────────────────────── DOMAIN CORE (deterministic) ───────────────────────┐
camt.053 ──►     │                                                                            │
 adapter  ──►  matcher ──► exceptions ──► triage agent ──► approval gate ──► report + audit log
ledger CSV ──►   │             │          (Claude + tools)      │                              │
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

3. **Autonomy is severity-gated, and the boundary is enforced in code.** The agent auto-closes only P3 + auto-resolvable items (logged as `sentinel-auto`). Everything else requires an explicit human approval, and in non-interactive mode risky items stay OPEN rather than being closed by default. Every decision — automatic or human — lands in an immutable audit log with actor attribution. The agent proposes; the human disposes. The same boundary applies over MCP: `record_resolution` is the only write tool on the server and requires a named human approver.

## The agent investigates before it classifies

For each exception, Claude uses the same checks a senior analyst runs, and must cite its evidence per exception (the full tool-call log goes into the report):

- `get_scheme_rules` — load the settlement semantics in force (finality, duplicate handling, fee treatment, recon window)
- `check_next_window` — is this "missing" settlement just the next cycle? (verified in eval data: timing differences vs genuine gaps are distinguished correctly)
- `get_fee_schedule` — does an amount-mismatch delta exactly match a fee netted at settlement?
- `lookup_history` — how did *this team* resolve the same pattern before? (approved resolutions feed back as few-shot context — the learning loop)
- `extract_reference` — recover a reference buried in free-text remittance info

## Evaluation

`evals/run_eval.py` builds a labeled synthetic set (32 exception cases across 8 scenario types, each with ground-truth bucket and rulebook-derived expected severity) and reports precision/recall per class:

- **Layer 1 — deterministic matcher** (offline): bucket classification. The matcher promises exactness; current result is 1.00 precision/recall across all buckets — anything less is a bug, not a tuning problem.
- **Layer 2 — agentic triage** (`--with-ai`): severity classification against rulebook-derived ground truth, including the cases that *require* investigation to get right (fee vs non-fee mismatches, timing vs genuine gaps).

```bash
python evals/run_eval.py            # layer 1, offline
python evals/run_eval.py --with-ai  # layers 1 + 2 (needs ANTHROPIC_API_KEY)
```

## Real scheme file format: ISO 20022 camt.053

The pipeline natively parses **camt.053 (BankToCustomerStatement)** — the end-of-day statement standard used across SEPA (successor to SWIFT MT940). The adapter (`src/adapters/camt053.py`, pure standard library) handles what makes reconciliation a job rather than a script: version/namespace differences, **bulk entries** exploded into individual transactions, the **reference priority ladder** (`EndToEndId` skipping `NOTPROVIDED` → `TxId` → structured creditor ref → `AcctSvcrRef` → `NtryRef`), and references buried in unstructured remittance text. It is unit-tested against a **real, bank-authored public sample file** (see Data sources).

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python src/generate_data.py     # camt.053 statement + ledger + next-window file + fee schedule
python src/main.py              # reconcile → agentic triage → approval gate → report
python src/main.py --yes        # non-interactive: auto-resolve safe items, leave risky OPEN
python src/main.py --no-ai      # deterministic reconciliation only
pytest tests/                   # matcher + camt.053 adapter tests (against the real bank sample)

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
│   ├── triage_agent.py      # agentic Claude triage loop (tool use, investigation log)
│   ├── approval.py          # approval gate, immutable audit log, resolution recording
│   ├── history.py           # resolution history — the learning loop
│   ├── mcp_server.py        # MCP server exposing the whole engine to Claude
│   └── main.py              # CLI orchestration + markdown report
├── evals/run_eval.py        # labeled eval set + precision/recall harness
├── tests/                   # matcher tests + adapter tests against the real bank sample
├── data/samples/            # public camt.053 sample (bank-published)
└── reports/                 # generated reconciliation reports
```

## Author

Vigneshwari K — payments product & operations. Launched 7 payment schemes across Hungary and Romania at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing); lead the reconciliation ops team this project is designed for.
