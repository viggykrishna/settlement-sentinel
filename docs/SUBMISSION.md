# Settlement Sentinel — Submission for Build with Claude

**Repo:** <https://github.com/viggykrishna/settlement-sentinel>
**Author:** Vigneshwari K — payments product & operations. I launched seven payment schemes across Hungary and Romania at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing) and I lead the reconciliation operations team this project is designed for.

## The problem, from someone who signs the attestation

Every settlement window, a bank participant receives a statement from the scheme and must prove it against its internal ledger. 98–99% of entries match mechanically. The remainder — the exceptions — are triaged by hand, by my team, under a closing window: *Is this "missing" payment just next cycle's timing? Does this €1.20 delta match a netted fee? Is this duplicate a real double-settlement with finality?* It is judgment work, it is scheme-specific, and at the end of it a human signs a regulatory attestation that the books balance.

Two constraints make naive "AI automation" unusable here, and they shaped everything: the attestation must be **reproducible** (a probabilistic model can never decide whether two entries are the same money), and every closure must be **attributable** ("the model closed it" is not an answer a regulator accepts).

## What I built

A reconciliation agent that splits the workflow exactly along that line:

- **Deterministic matcher** — parses real ISO 20022 camt.053 bank statements (tested against a bank-published Danske Bank sample) and matches with exact-then-conservative-fuzzy logic. No AI in the money path. The eval harness holds it to **1.00 precision/recall on all five exception classes** — anything less is defined as a bug.
- **Agentic triage** — Claude investigates each exception with the same tools my senior analysts use (`get_scheme_rules`, `check_next_window`, `get_fee_schedule`, `lookup_history`, `extract_reference`) and must ground severity in the **scheme rulebook**, not the pattern: the same missing-settlement exception is routine timing on a batch-net rail and a P1 incident on an instant rail. Every tool call lands in the report — the agent shows its working.
- **Human approval gate, enforced in code** — only P3 auto-resolvable items close automatically; everything else needs a named human, unattended runs leave risky items OPEN, and every decision hits an immutable audit log. Approved resolutions feed back as precedent — the learning loop.
- **MCP server** — the whole engine is drivable conversationally from Claude Desktop/Code. Deliberately no nested model calls: over MCP, Claude in the client *is* the analyst, and the server exposes only deterministic, auditable tools.

## What makes v4 the differentiator: cost as an enforced property

A recon tool that costs more than the analysts it assists never leaves the pilot. So the latest version makes the economics a first-class, *measured* claim: a **live 500-transaction settlement window ran end-to-end for $0.0934 against a hard $0.10 budget** — 450 matched at zero AI cost, all 50 exceptions investigated and triaged.

It holds that number the way a real ops team holds a headcount budget: **Haiku clears the whole queue** with the full investigation toolset (the junior analysts), **Sonnet second-opinions every P1** and writes the executive summary (the senior reviewer), prompt caching re-reads the growing investigation transcript at ~10% price, and a **cost meter prices every call and checks the budget before the next one is made**. Degradation is fail-safe and honest: the senior review trims to the highest-exposure items, then skips (annotated in the report, never hidden), and as a last resort triage stops and items stay OPEN for a human. The system never silently overspends and never closes an item to save money. Every run prints the per-model token and dollar breakdown into the report — the number is reproducible, not asserted.

## How to verify in five minutes

```bash
pip install -r requirements.txt
python evals/run_eval.py                      # matcher: 1.00 P/R, offline, no key
python src/generate_data.py --entries 500     # the 500-item demo window
export ANTHROPIC_API_KEY=sk-ant-...
python src/main.py --yes --budget 0.10        # full run, live cost meter, < $0.10
```

## The trail

Every version is one auditable commit on `main`: **v1** deterministic core → **v2** camt.053 + agentic triage + approval gate + learning loop → **v3** rulebook grounding + MCP server + eval harness → **v4** tiered routing + caching + hard budget guard. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) explains the whole arc for non-technical readers, problem space to solution space.

The through-line: AI where the work is judgment, deterministic code where the work is arithmetic, a named human wherever the decision carries risk — and now, a dollar cost that is enforced, not estimated. That is what it takes for this class of tool to move from demo to a settlement desk.

*A note on data: the camt.053 format and structure come from the openly published Danske Bank example; the 500-item volume is synthetic, because no bank publishes real settlement data. The scheme rulebook module encodes publicly known settlement-model semantics in generic form — no proprietary rulebook content is reproduced.*
