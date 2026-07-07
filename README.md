# Settlement Sentinel

**A Claude-powered agent that reconciles payment scheme settlement files and triages exceptions the way a senior ops analyst would.**

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

> **Deterministic matching. AI triage.**

The matching engine (`src/reconcile.py`) is pure, auditable code — a probabilistic model should never decide whether two ledger entries are the same money. Claude (`src/triage_agent.py`) is applied *after* matching, to the exceptions only: classifying severity (P1–P3), inferring probable root cause, recommending the analyst action, and flagging what's safe to auto-resolve. The system prompt encodes real reconciliation domain rules.

```
scheme file ─┐
             ├─► deterministic matcher ─► exceptions ─► Claude triage ─► ops report
ledger ──────┘        (auditable)                        (judgment)      (P1s first)
```

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python src/generate_data.py     # create sample scheme + ledger files (~200 txns, 20 injected exceptions)
python src/main.py              # reconcile, triage with Claude, write markdown report
python src/main.py --no-ai      # deterministic reconciliation only
pytest tests/                   # matcher unit tests
```

Output: a markdown report in `reports/` with an executive summary and exceptions sorted P1-first, each with root cause, recommended action, and auto-resolve flag.

## Why this maps to real agentic payments infrastructure

This is a single settlement window run as a CLI, but the shape is production-real:

- **Severity-gated autonomy** — `auto_resolvable: true` items (e.g. reference drift) could be closed programmatically; P1s always route to a human. That consent/control boundary is the core trust problem in agentic payments.
- **Scheme-agnostic core** — the matcher only needs `(ref, amount, value_date)`; adapters for real scheme file formats (ISO 20022 camt.053, UPI settlement reports, GIRO/SENT formats) slot in front of it.
- **Obvious next steps** — batch across windows, feed resolved exceptions back as few-shot examples, wire the triage output to a ticketing system, expose it as an MCP tool so the agent can pull settlement files itself.

## Project structure

```
settlement-sentinel/
├── src/
│   ├── generate_data.py    # realistic sample scheme file + ledger with injected exceptions
│   ├── reconcile.py        # deterministic matching engine (exact + safe fuzzy pass)
│   ├── triage_agent.py     # Claude exception triage (structured JSON output)
│   └── main.py             # CLI orchestration + markdown report
├── tests/
│   └── test_reconcile.py   # unit tests for every exception bucket
├── data/                   # generated sample files (gitignored in real use)
└── reports/                # generated reconciliation reports
```

## Author

Vigneshwari K — payments product & operations. Launched 7 payment schemes across Hungary and Romania at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing); lead the reconciliation ops team this project is designed for.
