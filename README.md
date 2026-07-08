# Settlement Sentinel

**A Claude-powered agent that reconciles payment scheme settlement files and triages exceptions the way a senior ops analyst would — investigating before it decides, and never acting on risky items without human approval.**

Built by a payments operator, not a demo-builder: I run reconciliation operations for live European payment schemes (instant rails, batch clearing, in-house clearing) and manage the team that triages these exceptions manually every day. This project automates the part of that workflow where AI genuinely helps — and deliberately keeps it out of the part where it doesn't.

---

## The problem

Every settlement window, a payment scheme sends participants a settlement file. The participant's ops team must reconcile it against the internal ledger. In the real world this never matches cleanly:

| Exception | What it means | Risk |
|---|---|---|
| Duplicate scheme entry | Potential double-settlement | 🔴 Money at risk |
| Missing in ledger | Scheme moved money we never booked | 🔴 Money at risk |
| Missing in scheme file | We booked money the scheme never settled | 🟡 Timing or real gap |
| Amount mismatch | Often fees netted at settlement | 🟡 Fee-mapping fix |
| Reference format drift | Legacy truncation, same money | 🟢 Auto-resolvable |

Today, ops analysts triage these by hand — deciding severity, root cause, and action per exception. That judgment is exactly the kind of structured, rule-informed reasoning Claude does well.

## The design principle

> **Deterministic matching. Agentic triage. Human-gated resolution.**

```
camt.053 / CSV ─┐
                ├─► deterministic matcher ─► exceptions ─► Claude agent ──► approval gate ─► report + audit log
ledger ─────────┘        (auditable)                       │    ▲                │
                                                     tools ▼    │           resolutions feed
                                              check_next_window │           back into history
                                              lookup_history ───┘           (learning loop)
                                              get_fee_schedule
                                              extract_reference
```

1. **Deterministic matching** (`src/reconcile.py`) — pure, auditable code. A probabilistic model should never decide whether two ledger entries are the same money.
2. **Agentic triage** (`src/triage_agent.py`) — Claude investigates each exception with real tools *before* classifying, the same checks a senior analyst performs:
   - `check_next_window` — is a "missing in scheme file" item just a cutoff/timing difference? (Found next window → P3 timing note. Not found → genuine gap, escalate.)
   - `get_fee_schedule` — does an amount-mismatch delta exactly match a known scheme fee netted at settlement?
   - `lookup_history` — how did *this team* resolve the same pattern before?
   - `extract_reference` — recover a reference buried in free-text remittance info.

   Every tool call is captured in an **investigation log** in the report — the agent shows its working.
3. **Approval gate** (`src/approval.py`) — the consent boundary that agentic payments actually hinges on:
   - P3 + auto-resolvable → closed automatically, logged as `sentinel-auto`.
   - P1/P2 or anything not auto-resolvable → requires an explicit human approve/skip. In non-interactive mode risky items stay **OPEN** — the agent never closes them alone.
   - Every decision (automatic or human) is appended to an immutable audit log (`data/audit_log.jsonl`).
4. **Learning loop** (`src/history.py`) — approved resolutions are persisted and injected into the next window's triage as few-shot context. Today's human decisions become tomorrow's agent calibration.

## Real scheme file format: ISO 20022 camt.053

The pipeline natively parses **camt.053 (BankToCustomerStatement)** — the end-of-day statement standard used across SEPA and by most European banks (successor to SWIFT MT940). The adapter (`src/adapters/camt053.py`, pure standard library) handles the quirks that make reconciliation a job rather than a script:

- namespace differences between camt.053 versions
- **bulk entries** — one `<Ntry>` containing several `<TxDtls>` transactions, exploded into individual rows
- **reference priority ladder** — `EndToEndId` (ignoring `NOTPROVIDED`) → `TxId` → structured creditor reference → `AcctSvcrRef` → `NtryRef`
- references buried in unstructured remittance text (`<Ustrd>`), carried through for the agent to recover

The adapter is unit-tested against a **real, bank-authored public sample file** (see Data sources below), and the generator also emits the synthetic settlement data in camt.053 shape, so the whole pipeline exercises the real format end-to-end.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python src/generate_data.py     # camt.053 statement + ledger + next-window file + fee schedule
python src/main.py              # reconcile → agentic triage → approval gate → report
python src/main.py --yes        # non-interactive: auto-resolve safe items, leave risky OPEN
python src/main.py --no-ai      # deterministic reconciliation only
pytest tests/                   # matcher + camt.053 adapter tests (runs against the real bank sample)

# run against the real public Danske Bank camt.053 sample:
python src/main.py --no-ai --scheme-file data/samples/camt053_danske_example.xml
```

Output: a markdown report in `reports/` with an executive summary, quantified impact metrics (money at risk, % auto-resolved, analyst time saved), exceptions sorted P1-first with status/evidence, and the full agent investigation log.

## Data sources (public domain / bank-published samples)

- **Danske Bank camt.053 example file** (included at `data/samples/camt053_danske_example.xml`, used by the test suite): <https://danskeci.com/-/media/pdf/danskeci-com/iso-20022-xml/camt053_dk_example.xml>
- **ISO 20022 official message definitions & schemas** (camt.053 XSD): <https://www.iso20022.org/iso-20022-message-definitions?search=camt.053>
- **Goldman Sachs Developer — camt.052/053 sample reports**: <https://developer.gs.com/docs/services/transaction-banking/camt53-sample>
- **Handelsbanken ISO 20022 XML examples**: <https://www.handelsbanken.com/en/our-services/digital-services/global-gateway/iso-20022-xml>

All sample and generated data in this repository is synthetic or bank-published example data. No production settlement data is used anywhere.

## Why this maps to real agentic payments infrastructure

- **Severity-gated autonomy with an enforced consent boundary** — the agent auto-closes only what is provably safe; risky items always route to a human, and everything lands in an audit trail. That control boundary — not raw intelligence — is the core trust problem in agentic payments.
- **Investigation before classification** — the agent uses tools to verify (next-window check, fee schedule, resolution history) rather than pattern-matching from a single prompt, and must show its evidence per exception.
- **A learning system, not a static one** — the human-approval loop feeds resolved exceptions back as few-shot context, so triage converges toward this team's actual operating decisions.
- **Scheme-agnostic core, real formats at the edge** — the matcher only needs `(ref, amount, value_date)`; the camt.053 adapter shows how real scheme/bank formats slot in front of it. UPI settlement reports or GIRO/SENT formats are the same pattern.
- **Obvious next steps** — batch across windows, wire triage output to a ticketing system, expose the whole pipeline as an MCP server so an agent can pull settlement files itself.

## Project structure

```
settlement-sentinel/
├── src/
│   ├── adapters/
│   │   └── camt053.py       # ISO 20022 camt.053 parser (stdlib only)
│   ├── generate_data.py     # synthetic data: camt.053 statement, ledger, next window, fee schedule
│   ├── reconcile.py         # deterministic matching engine (exact + safe fuzzy pass)
│   ├── investigation.py     # the agent's investigation tools + Anthropic tool specs
│   ├── triage_agent.py      # agentic Claude triage loop (tool use, investigation log)
│   ├── approval.py          # approval gate, audit log, resolution recording
│   ├── history.py           # resolution history — the learning loop
│   └── main.py              # CLI orchestration + markdown report
├── tests/
│   ├── test_reconcile.py    # unit tests for every exception bucket
│   └── test_camt053.py      # adapter tests against the real Danske Bank sample
├── data/
│   └── samples/             # public camt.053 sample (bank-published)
└── reports/                 # generated reconciliation reports
```

## Author

Vigneshwari K — payments product & operations. Launched 7 payment schemes across Hungary and Romania at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing); lead the reconciliation ops team this project is designed for.
