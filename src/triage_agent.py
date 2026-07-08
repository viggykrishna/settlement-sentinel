"""
Claude-powered exception triage — agentic version.

v1 of this module sent exceptions to Claude in a single call and got a
classification back. That was a classifier, not an agent.

v2 gives Claude the same investigation tools a senior analyst uses before
deciding, and lets it reason in a loop:

    exceptions ─► Claude ─► check_next_window / lookup_history /
                  ▲          get_fee_schedule / extract_reference
                  └──────────── tool results ◄─┘
                        ...
                  final structured triage JSON

Concretely, this changes outcomes:
  * MISSING_IN_SCHEME_FILE: instead of a blanket P2, the agent checks the
    next settlement window. Found there → timing difference, downgrade,
    auto-resolvable. Not found → genuine gap, escalate.
  * AMOUNT_MISMATCH: the agent pulls the fee schedule. Delta exactly matches
    a known fee → P3 fee-mapping fix. Otherwise → P2 investigation.
  * Recurring patterns: lookup_history calibrates severity and action
    against how THIS team resolved the same pattern before (few-shot
    learning loop — resolutions recorded via the approval gate feed back
    into future triage).

Every tool call is captured in an investigation log that goes into the
report — the agent must show its working, same as an analyst would.

Requires ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import json
import os

import anthropic

import history
from investigation import TOOL_DEFINITIONS, TOOL_FUNCTIONS

MODEL = "claude-sonnet-4-6"
MAX_TOOL_TURNS = 12

SYSTEM_PROMPT = """You are a senior payment-scheme reconciliation analyst.
You triage exceptions between a scheme settlement file and a participant's
internal ledger. You have investigation tools — use them BEFORE classifying,
exactly as a careful analyst would:

- For every MISSING_IN_SCHEME_FILE exception, call check_next_window first.
  If the transaction settled in the next window it is a cutoff/timing
  difference: severity P3, auto_resolvable true, action is to note the
  timing difference. If not found, it is a genuine gap: P2, escalate to the
  scheme operator.
- For AMOUNT_MISMATCH, call get_fee_schedule and check whether the delta
  matches a known fee exactly. Exact fee match: P3, auto_resolvable true,
  recommend a fee-mapping rule. No match: P2, investigate.
- Call lookup_history when a pattern looks familiar; align your severity and
  action with how the team resolved it before, unless the facts differ.
- If a reference is UNKNOWN but remit_info is present, call
  extract_reference to attempt recovery.

Fixed domain rules (do not deviate):
- DUPLICATE_SCHEME_ENTRY: potential double-settlement. Always P1, never
  auto-resolvable. Action: raise with scheme operator before end of the
  settlement window; block the second posting.
- MISSING_IN_LEDGER: scheme moved money we never booked. P1 if the amount is
  large (>10,000) or direction is DEBIT; otherwise P2. Never auto-resolvable.
- REFERENCE_FORMAT_DRIFT: amounts and dates match, reference truncated.
  P3, auto_resolvable true; recommend a normalisation rule.

Investigate first, then respond ONLY with valid JSON (no markdown fences):
{
  "triaged": [
    {
      "index": <int, position in the input list>,
      "severity": "P1" | "P2" | "P3",
      "probable_root_cause": "<one sentence>",
      "recommended_action": "<one imperative sentence>",
      "auto_resolvable": true | false,
      "evidence": "<one sentence citing what your investigation found, or 'domain rule' if no tool was needed>"
    }
  ],
  "executive_summary": "<3-4 sentences for the ops lead: overall risk level, money at risk, themes, what to do first>"
}"""


def _parse_json(raw: str) -> dict | None:
    """
    Robustly extract the triage JSON from a model reply.

    Handles: markdown fences, prose before/after the JSON, stray whitespace.
    Returns None if no parseable JSON object is present (e.g. empty or
    truncated reply) so the caller can retry.
    """
    if not raw:
        return None
    raw = raw.strip().removeprefix("```json").removeprefix("```")
    raw = raw.removesuffix("```").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def _few_shot_context() -> str:
    past = history.recent(8)
    if not past:
        return ""
    return (
        "\n\nRecent resolutions by this ops team (align your judgment with "
        "these unless the facts differ):\n" + json.dumps(past, indent=2, default=str)
    )


def triage(exceptions: list, batch_limit: int = 40,
           verbose: bool = True) -> dict:
    """
    Agentic triage: Claude investigates with tools, then returns structured
    results. Also returns the investigation log under "investigation_log".
    """
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

    messages = [{
        "role": "user",
        "content": (
            "Triage the following reconciliation exceptions. Investigate "
            "with your tools before classifying."
            + _few_shot_context()
            + "\n\nExceptions:\n"
            + json.dumps(payload, indent=2, default=str)
        ),
    }]

    investigation_log: list[dict] = []

    for _ in range(MAX_TOOL_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            raw = "".join(b.text for b in response.content if b.type == "text")
            result = _parse_json(raw)
            if result is None:
                # Final answer wasn't clean JSON (empty, truncated, or has
                # preamble). Nudge once and let the loop continue.
                if verbose:
                    print("  [agent] final answer not valid JSON "
                          f"(stop_reason={response.stop_reason}, "
                          f"{len(raw)} chars) — asking for re-emit")
                messages.append({"role": "assistant",
                                 "content": raw or "(empty)"})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous reply was not valid JSON. Respond "
                        "again with ONLY the complete triage JSON object — "
                        "no prose, no markdown fences, no tool calls."
                    ),
                })
                continue
            result["investigation_log"] = investigation_log
            return result

        # execute every tool call in this turn, feed results back
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            fn = TOOL_FUNCTIONS.get(block.name)
            output = fn(block.input) if fn else {"error": f"unknown tool {block.name}"}
            investigation_log.append({
                "tool": block.name,
                "input": block.input,
                "output": output,
            })
            if verbose:
                print(f"  [agent] {block.name}({json.dumps(block.input)}) "
                      f"-> {json.dumps(output, default=str)[:120]}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(
        f"triage did not converge within {MAX_TOOL_TURNS} tool turns")