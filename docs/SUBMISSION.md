# Settlement Sentinel — Submission for Build with Claude

**Repo:** <https://github.com/viggykrishna/settlement-sentinel>
**Author:** Vigneshwari K — payments product & operations. I launched multiple payment schemes across EU at Revolut (VIBER, GIRO, Qvik, Request to Pay, Secondary Identifier, Wero, RON in-house clearing).

## The problem, from someone who signs the attestation

Every settlement window, a bank participant receives a statement from the scheme and must prove it against its internal ledger. 98–99% of entries match mechanically. The remainder — the exceptions — are triaged by hand, by my team, under a closing window: *Is this "missing" payment just next cycle's timing? Does this €1.20 delta match a netted fee? Is this duplicate a real double-settlement with finality?* It is judgment work, it is scheme-specific, and at the end of it a human signs a regulatory attestation that the books balance.

Two constraints make naive "AI automation" unusable here, and they shaped everything: the attestation must be **reproducible** (a probabilistic model can never decide whether two entries are the same money), and every closure must be **attributable** ("the model closed it" is not an answer a regulator accepts).

## What I built

A reconciliation agent that splits the workflow exactly along that line:

- **Deterministic matcher** — parses real ISO 20022 camt.053 bank statements (tested against a bank-published Danske Bank sample) and matches with exact-then-conservative-fuzzy logic. No AI in the money path. The eval harness holds it to **1.00 precision/recall on all five exception classes** — anything less is defined as a bug.
- **Agentic triage** — Claude investigates each exception with the same tools my senior analysts use (`get_scheme_rules`, `check_next_window`, `get_fee_schedule`, `lookup_history`, `extract_reference`) and must ground severity in the **scheme rulebook**, not the pattern. The AI layer's judgment is *measured*, not asserted: **1.00 precision/recall on P1/P2/P3 severity** against rulebook-derived ground truth — for the cheap Haiku tier alone *and* for the full tiered pipeline. The rulebook claim is demonstrated live in [`docs/RAIL_CONTRAST.md`](RAIL_CONTRAST.md): the identical missing-settlement transaction, with identical tool-call facts, is auto-closed as routine timing under `BATCH_NET` and held open as a settlement failure under `INSTANT_RAIL`. Every tool call lands in the report — the agent shows its working ([committed example](sample_report.md)).
- **Human approval gate, enforced in code** — only P3 auto-resolvable items close automatically; everything else needs a named human, unattended runs leave risky items OPEN, and every decision hits an append-only audit log. Approved resolutions feed back as precedent — the learning loop.
- **MCP server** — the whole engine is drivable conversationally from Claude Desktop/Code. Deliberately no nested model calls: over MCP, Claude in the client *is* the analyst, and the server exposes only deterministic, auditable tools.

## What makes v4 the differentiator: cost as an enforced property

A recon tool that costs more than the analysts it assists never leaves the pilot. So the latest version makes the economics a first-class, *measured* claim: a **live 500-transaction settlement window ran end-to-end for $0.0934 against a hard $0.10 budget** — 450 matched at zero AI cost, all 50 exceptions investigated and triaged.

It holds that number the way a real ops team holds a headcount budget: **Haiku clears the whole queue** with the full investigation toolset (the junior analysts), **Sonnet second-opinions every P1** and writes the executive summary (the senior reviewer), prompt caching re-reads the growing investigation transcript at ~10% price, and a **cost meter prices every call and checks the budget before the next one is made**. Degradation is fail-safe and honest: the senior review trims to the highest-exposure items, then skips (annotated in the report, never hidden), and as a last resort triage stops and items stay OPEN for a human. The system never silently overspends and never closes an item to save money. Every run prints the per-model token and dollar breakdown into the report — the number is reproducible, not asserted.

## Commercial applicability — beyond payment schemes

The reference implementation reconciles payment-scheme settlement files, but the
architecture was deliberately built domain-agnostic: two independent records of the
same economic event, deterministic matching, rulebook-grounded exception triage,
human-gated resolution, and a learning loop. Domain knowledge lives only at the
edges — format adapters (camt.053 today) and rulebook profiles (batch-net vs
instant-rail today). Entering a new domain means one adapter plus one rulebook
profile; everything else is invariant.

The reconciliation software market crossed USD 2.5B in 2025 and is projected to
grow at ~13% CAGR toward USD 10B by 2036, with BFSI holding roughly 45% share —
and the industry direction is explicitly toward AI-assisted matching that learns
from historical resolutions with governed, auditable exception handling. That is
precisely the pattern this project implements: agentic investigation with a
severity-gated autonomy boundary and an append-only audit trail.

Concrete adjacent deployments:

**1. Intraday order / trade-break reconciliation (brokerage).** Executed orders vs
exchange confirmations vs OMS records. India's move to T+1 (2023) and active T+0
pilots compress the window in which breaks must be found, triaged, and resolved
from hours to minutes — batch recon surfaces exceptions after the settlement window
has closed. An agent that investigates breaks at occurrence and prioritises by
settlement risk (margin-call exposure first) is what T+0 operationally demands.

**2. Positions reconciliation (wealth & trading / custody).** Internal position
book vs custodian and depository statements. Same pattern, different join keys
(ISIN, quantity, account). Most position breaks are corporate-action or
settlement-lag artifacts — the "check next window" investigation tool becomes
"check corporate-action calendar," distinguishing timing artifacts from genuine
breaks before escalation.

**3. Options / derivatives lifecycle reconciliation.** Exercise and assignment
records vs clearing-house reports; margin calls vs collateral postings. Severity
here depends on contract semantics — an unmatched assignment near expiry is
critical, the same mismatch mid-cycle is routine — which is exactly the judgment
the rulebook-grounding layer encodes and a pattern-matching classifier gets wrong.

**4. Merchant settlement reconciliation as a B2B SaaS product.** Merchant order
records vs payment-gateway settlement files vs bank credits — the core
Razorpay-ecosystem problem. India is the hardest version of this market: sellers
reconcile across multiple marketplaces and gateways, TDS/TCS deduction chains,
GSTR-2B input-credit matching, NACH returns, and COD remittance — a single
transaction can create four parallel reconciliation obligations. The productisation
insight: the human-approval gate becomes the user interface. A business owner sees
a plain-language explanation ("₹4,200 short on this payout: gateway fee + TDS,
both verified against your rate card") with approve/dispute actions — not a raw
reconciliation report only an ops analyst can read.

**5. Treasury / nostro-vostro reconciliation (banks, fintechs).** Expected vs
actual movements across correspondent accounts. The camt.053 adapter is already the
native input format — nostro statements arrive as exactly these ISO 20022 messages.
Investigation tools map to FX cutoffs and correspondent fee schedules.

**6. Corporate financial close.** Ledger vs bank vs intercompany entries at
period-end, under SOX/IFRS audit requirements. In this domain the append-only audit
log with actor attribution is not a feature — it is the compliance requirement, and
it is already built.

**7. Insurance claims and payout reconciliation.** Claims approved vs payouts
executed vs reinsurance recoveries — the same triple-match structure with a
policy-rules rulebook profile.

The common thread: every domain above currently resolves exceptions with analyst
headcount that scales linearly with volume. This architecture converts that into a
fixed engineering investment plus a controllable API cost — with the trust
boundary (investigate freely, act only within a provably safe zone, audit
everything) that regulated financial operations actually require.

## How to verify in five minutes

```bash
pip install -r requirements.txt
python evals/run_eval.py                      # matcher: 1.00 P/R, offline, no key (also runs in CI)
python src/generate_data.py --entries 500     # the 500-item demo window
export ANTHROPIC_API_KEY=sk-ant-...
python src/main.py --yes --budget 0.10        # full run, live cost meter, < $0.10
python src/main.py --yes --scheme-profile INSTANT_RAIL   # same data, opposite triage
python evals/run_eval.py --with-ai            # AI severity triage: 1.00 P/R measured
```

## The trail

Every version is one auditable commit on `main`: **v1** deterministic core → **v2** camt.053 + agentic triage + approval gate + learning loop → **v3** rulebook grounding + MCP server + eval harness → **v4** tiered routing + caching + hard budget guard → **v5** the AI layer's accuracy measured and published, the rail-contrast demonstrated live, CI enforcing the matcher's 1.00 on every push, and a precedent-poisoning guard on the learning loop. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) explains the whole arc for non-technical readers, problem space to solution space.

The through-line: AI where the work is judgment, deterministic code where the work is arithmetic, a named human wherever the decision carries risk — and now, a dollar cost that is enforced, not estimated. That is what it takes for this class of tool to move from demo to a settlement desk.

*A note on data: the camt.053 format and structure come from the openly published Danske Bank example; the 500-item volume is synthetic, because no bank publishes real settlement data. The scheme rulebook module encodes publicly known settlement-model semantics in generic form — no proprietary rulebook content is reproduced.*
