"""
Claude-powered exception triage.

The reconciliation engine produces mechanically-classified exceptions.
This module sends them to Claude in a single structured call and gets back,
per exception:

  - severity           P1 (money at risk) -> P3 (cosmetic)
  - probable_root_cause
  - recommended_action  (what an ops analyst should actually do)
  - auto_resolvable     (safe to close programmatically, true/false)

plus an executive summary for the ops lead. The prompt encodes real
reconciliation domain rules — e.g. a scheme-side duplicate is a P1 because it
means potential double-settlement, while reference format drift with matching
amount and date is a P3 write-off candidate.

Requires ANTHROPIC_API_KEY in the environment.
"""

import json
import os

import anthropic

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a senior payment-scheme reconciliation analyst.
You triage exceptions between a scheme settlement file and a participant's
internal ledger.

Domain rules you apply:
- DUPLICATE_SCHEME_ENTRY: potential double-settlement. Always P1. Action:
  raise with scheme operator before end of settlement window; block second
  posting.
- MISSING_IN_LEDGER: scheme moved money we never booked. P1 if amount is
  large or direction is DEBIT; otherwise P2. Action: verify against source
  system, book correcting entry, investigate booking-pipeline gap.
- MISSING_IN_SCHEME_FILE: we booked money the scheme never settled. P2.
  Could be a timing/cutoff difference — check the next settlement window
  before escalating to the scheme.
- AMOUNT_MISMATCH: small constant differences usually indicate scheme fees
  netted at settlement — P3, propose a fee-mapping fix. Large or irregular
  differences are P2.
- REFERENCE_FORMAT_DRIFT: amounts and dates match, reference truncated.
  P3, auto-resolvable; recommend a normalisation rule.

Respond ONLY with valid JSON, no markdown fences, in this shape:
{
  "triaged": [
    {
      "index": <int, position in the input list>,
      "severity": "P1" | "P2" | "P3",
      "probable_root_cause": "<one sentence>",
      "recommended_action": "<one imperative sentence>",
      "auto_resolvable": true | false
    }
  ],
  "executive_summary": "<3-4 sentences for the ops lead: overall risk level, money at risk, themes, what to do first>"
}"""


def triage(exceptions: list, batch_limit: int = 40) -> dict:
    """Send exceptions to Claude and return structured triage results."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    payload = [
        {
            "index": i,
            "bucket": e.bucket,
            "scheme_row": e.scheme_row,
            "ledger_rows": e.ledger_rows,
        }
        for i, e in enumerate(exceptions[:batch_limit])
    ]

    message = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                "Triage the following reconciliation exceptions:\n\n"
                + json.dumps(payload, indent=2, default=str)
            ),
        }],
    )

    raw = "".join(block.text for block in message.content if block.type == "text")
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)
