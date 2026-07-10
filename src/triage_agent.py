"""
Claude-powered exception triage — agentic, tiered, cost-metered (v4).

v2 made the triage agentic: Claude investigates with the same tools a
senior analyst uses before classifying. v3 grounded severity in the scheme
rulebook. v4 makes the economics production-shaped:

  * TIERED ROUTING — Haiku 4.5 (the cheap, fast model) runs the full
    investigative triage for every exception. Anything it rates P1, or
    marks low-confidence, gets a SECOND OPINION from Sonnet 4.6 on the
    already-gathered evidence. This mirrors how an ops team actually
    staffs a settlement window: juniors clear the queue, the senior
    analyst reviews only what's flagged.

  * PROMPT CACHING — the system prompt + tool definitions and the growing
    investigation transcript are cache-marked, so each tool turn re-reads
    the prefix at ~10% of the input price instead of paying full price
    every turn.

  * BUDGET GUARD — every API call is priced by cost_meter and checked
    against a hard dollar budget BEFORE it is made. Degradation is graceful
    and fail-safe: first the Sonnet escalation is skipped (annotated, not
    hidden), then the loop stops and untriaged items stay OPEN for a
    human. The agent never silently overspends and never silently closes
    an item to save money.

Every tool call is captured in an investigation log that goes into the
report — the agent must show its working, same as an analyst would.

Requires ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import json
import os

import anthropic

import history
from cost_meter import BudgetExceeded, CostMeter, PRICING
from investigation import TOOL_DEFINITIONS, TOOL_FUNCTIONS

TRIAGE_MODEL = "claude-haiku-4-5"      # clears the queue
ESCALATION_MODEL = "claude-sonnet-4-6"  # senior second opinion on P1 / low-confidence
MAX_TOOL_TURNS = 8
TRIAGE_MAX_TOKENS = 8000
ESCALATION_MAX_TOKENS = 2500
# Exceptions are triaged in chunks: small outputs never truncate, each
# chunk's tool-turn stays inside the prompt cache's 20-block lookback
# window, and a failure in one chunk cannot poison the rest of the batch.
CHUNK_SIZE = 25

# fields the agent actually needs — everything else is token noise
PAYLOAD_FIELDS = ("settlement_ref", "amount", "currency", "direction",
                  "value_date", "counterparty", "remit_info")

SYSTEM_PROMPT = """You are a senior payment-scheme reconciliation analyst.
You triage exceptions between a scheme settlement file and a participant's
internal ledger. The scheme in force runs the {profile} settlement profile.
You have investigation tools — use them BEFORE classifying, exactly as a
careful analyst would:

- FIRST call get_scheme_rules with profile "{profile}" to load the
  settlement semantics in force. Ground every severity and action in those
  semantics: e.g. on a BATCH_NET scheme a missing-in-scheme item is often
  a cutoff/timing difference and fees are commonly netted at settlement,
  whereas on an INSTANT_RAIL neither is true and a duplicate means funds
  moved twice with finality.
- For every MISSING_IN_SCHEME_FILE exception, call check_next_window to
  establish the facts, then apply the scheme semantics — the SAME facts
  mean OPPOSITE things on different rails:
  * On BATCH_NET: found in next window → cutoff/timing difference, P3,
    auto_resolvable true, note the timing difference. Not found → genuine
    gap, P2, escalate to the scheme operator.
  * On INSTANT_RAIL: there is NO batch cutoff, so a missing settlement can
    NEVER be a same-rail timing difference — even a next-window appearance
    is anomalous, not benign. The payment was booked but never settled:
    P1 if amount >10,000 or direction DEBIT, else P2; never
    auto_resolvable; escalate as a settlement failure immediately.
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

Batch efficiency: call each tool ONCE per distinct question (e.g. one
check_next_window call per missing ref, but get_scheme_rules and
get_fee_schedule only once for the whole batch). Keep every text field
TERSE — maximum ~10 words each; the batch is large and verbosity is cost.

Investigate first, then respond ONLY with valid JSON (no markdown fences):
{
  "triaged": [
    {
      "index": <int, the "index" field from the input item>,
      "severity": "P1" | "P2" | "P3",
      "probable_root_cause": "<one short sentence>",
      "recommended_action": "<one short imperative sentence>",
      "auto_resolvable": true | false,
      "confidence": "high" | "low",
      "evidence": "<one short sentence citing what your investigation found, or 'domain rule'>"
    }
  ]
}
Mark confidence "low" whenever the evidence is ambiguous, tools disagreed,
or you had to assume facts not in the data."""

ESCALATION_SYSTEM = """You are the senior reconciliation lead giving a second
opinion. A first-pass analyst triaged these settlement exceptions; every P1
and every low-confidence call comes to you. You see the exception data, the
analyst's proposed triage, and the investigation evidence already gathered.

Confirm or revise each item. Revise ONLY when the evidence contradicts the
proposal — do not restyle correct calls. Keep every field to one short
sentence. You also write the executive summary for the whole batch (the
batch statistics are provided). Respond ONLY with valid JSON (no markdown
fences):
{
  "reviews": [
    {
      "index": <int, same index as the input>,
      "verdict": "confirm" | "revise",
      "severity": "P1" | "P2" | "P3",
      "recommended_action": "<one short imperative sentence>",
      "note": "<one short sentence: why confirmed or what changed>"
    }
  ],
  "executive_summary": "<3-4 sentences for the ops lead: overall risk level, money at risk, themes, what to do first>"
}"""

_CACHE = {"type": "ephemeral"}


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
    # human-approved precedent only — agent-made resolutions must never
    # feed back into the agent's own calibration (poisoning guard)
    past = history.recent(4, human_only=True)
    if not past:
        return ""
    return (
        "\n\nRecent resolutions by this ops team (align your judgment with "
        "these unless the facts differ):\n" + json.dumps(past, indent=2, default=str)
    )


def _compact(row: dict | None) -> dict | None:
    if not row:
        return row
    return {k: row[k] for k in PAYLOAD_FIELDS if k in row and row[k] not in ("", None)}


def _mark_cache_tail(messages: list) -> None:
    """
    Keep cache breakpoints on the last TWO user messages.

    Caching is a prefix match, and each breakpoint only looks back 20
    content blocks for an earlier cache entry. A tool turn with a dozen
    calls adds >20 blocks, so a single tail marker silently misses the
    previous entry and the whole prefix is re-written at 1.25x instead of
    re-read at 0.1x (measured, not theoretical — this happened in the first
    v4 live run). Two markers bound the gap. Only plain dict blocks are
    touched — assistant turns hold SDK objects and are left alone.
    """
    marked = 0
    for m in reversed(messages):
        content = m["content"]
        if isinstance(content, str) and marked < 2:
            m["content"] = [{"type": "text", "text": content,
                             "cache_control": dict(_CACHE)}]
            marked += 1
            continue
        if not isinstance(content, list):
            continue
        blocks = [b for b in content if isinstance(b, dict)]
        if not blocks:
            continue  # SDK objects (assistant turns) — leave alone
        for b in blocks:
            b.pop("cache_control", None)
        if marked < 2:
            blocks[-1]["cache_control"] = dict(_CACHE)
            marked += 1


def _estimate_usd(model: str, in_tokens: int, out_tokens: int) -> float:
    r = PRICING[model]
    return (in_tokens * r["input"] + out_tokens * r["output"]) / 1_000_000


# --------------------------------------------------------------- main pass --

def triage(exceptions: list, batch_limit: int = 64, verbose: bool = True,
           meter: CostMeter | None = None,
           scheme_profile: str = "BATCH_NET", escalate: bool = True) -> dict:
    """
    Tiered agentic triage. Haiku investigates and classifies every
    exception in chunks of CHUNK_SIZE; Sonnet reviews P1 / low-confidence
    calls and writes the executive summary if the budget allows
    (escalate=False turns the Sonnet tier off — used by the eval harness
    to measure what the review tier actually buys). scheme_profile selects
    the rulebook semantics in force (BATCH_NET or INSTANT_RAIL). Returns
    triage results plus "investigation_log" and "cost".
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    meter = meter or CostMeter()

    if len(exceptions) > batch_limit and verbose:
        print(f"  [warn] {len(exceptions) - batch_limit} exception(s) beyond "
              f"batch_limit={batch_limit} will NOT be triaged and stay OPEN")

    payload = [
        {
            "index": i,
            "bucket": e.bucket,
            "scheme_row": _compact(e.scheme_row),
            "ledger_rows": [_compact(r) for r in (e.ledger_rows or [])],
        }
        for i, e in enumerate(exceptions[:batch_limit])
    ]
    few_shot = _few_shot_context()
    system_text = SYSTEM_PROMPT.replace("{profile}", scheme_profile)

    triaged: list[dict] = []
    investigation_log: list[dict] = []
    for start in range(0, len(payload), CHUNK_SIZE):
        chunk = payload[start:start + CHUNK_SIZE]
        if verbose:
            print(f"  [chunk] triaging exceptions {start}..."
                  f"{start + len(chunk) - 1} ({len(chunk)} items)")
        chunk_triaged, chunk_log = _triage_chunk(
            client, meter, chunk, few_shot, system_text, verbose)
        triaged.extend(chunk_triaged)
        investigation_log.extend(chunk_log)

    result: dict = {"triaged": triaged,
                    "investigation_log": investigation_log,
                    "scheme_profile": scheme_profile}
    if escalate:
        _escalate(client, meter, payload, result, verbose)
    else:
        result["escalation"] = {"escalated": 0, "note": "disabled by caller"}
    if not result.get("executive_summary"):
        result["executive_summary"] = _fallback_summary(triaged)
    result["cost"] = meter.summary()
    return result


def _triage_chunk(client, meter: CostMeter, chunk: list, few_shot: str,
                  system_text: str, verbose: bool) -> tuple[list, list]:
    """One agentic Haiku loop over a chunk of exceptions."""
    user_text = (
        "Triage the following reconciliation exceptions. Investigate "
        "with your tools before classifying."
        + few_shot
        + "\n\nExceptions:\n"
        + json.dumps(chunk, default=str)
    )
    messages = [{"role": "user", "content": user_text}]
    system = [{"type": "text", "text": system_text,
               "cache_control": dict(_CACHE)}]

    log: list[dict] = []
    # conservative per-turn estimate: uncached prompt on the first turn,
    # mostly cache reads after; output dominated by the chunk's final JSON
    first_in_est = len(user_text) // 3 + 3000  # payload + system + tools
    out_est = 60 * len(chunk) + 500

    for turn in range(MAX_TOOL_TURNS):
        in_est = first_in_est if turn == 0 else 2000  # later turns mostly cached
        meter.guard(_estimate_usd(TRIAGE_MODEL, in_est, out_est),
                    f"triage turn {turn + 1}")

        _mark_cache_tail(messages)
        response = client.messages.create(
            model=TRIAGE_MODEL,
            max_tokens=TRIAGE_MAX_TOKENS,
            system=system,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        cost = meter.record(TRIAGE_MODEL, response.usage)
        if verbose:
            u = response.usage
            print(f"  [cost] {TRIAGE_MODEL} turn {turn + 1}: "
                  f"in={u.input_tokens} out={u.output_tokens} "
                  f"cache_read={u.cache_read_input_tokens or 0} "
                  f"cache_write={u.cache_creation_input_tokens or 0} "
                  f"-> ${cost:.4f} (total ${meter.total_usd:.4f})")

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
            return result.get("triaged", []), log

        # execute every tool call in this turn, feed results back
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            fn = TOOL_FUNCTIONS.get(block.name)
            output = fn(block.input) if fn else {"error": f"unknown tool {block.name}"}
            log.append({
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
        f"triage chunk did not converge within {MAX_TOOL_TURNS} tool turns")


def _fallback_summary(triaged: list) -> str:
    """Programmatic executive summary when the Sonnet pass didn't run."""
    counts = {"P1": 0, "P2": 0, "P3": 0}
    for t in triaged:
        counts[t.get("severity", "P2")] = counts.get(t.get("severity", "P2"), 0) + 1
    auto = sum(1 for t in triaged if t.get("auto_resolvable"))
    return (
        f"{len(triaged)} exceptions triaged: {counts['P1']} P1, "
        f"{counts['P2']} P2, {counts['P3']} P3; {auto} auto-resolvable. "
        "Work P1 duplicates and missing-in-ledger items first. "
        "(Summary generated locally — Sonnet review pass did not run.)")


# ---------------------------------------------------------- escalation pass --

def _escalate(client, meter: CostMeter, payload: list, result: dict,
              verbose: bool) -> None:
    """
    Sonnet second opinion on P1 and low-confidence items, on the evidence
    Haiku already gathered. Skipped (and annotated) if the budget doesn't
    cover it — the cheap triage stands rather than the run failing.
    """
    triaged = result.get("triaged", [])
    flagged = [t for t in triaged
               if t.get("severity") == "P1" or t.get("confidence") == "low"]
    if not flagged:
        result["escalation"] = {"escalated": 0, "note": "nothing flagged"}
        return

    by_index = {p["index"]: p for p in payload}

    # only ship evidence relevant to the flagged items, with tool outputs
    # truncated — the reviewer needs found/not-found and deltas, not full
    # rows; every extra char here is Sonnet input cost
    flagged_refs = set()
    for t in flagged:
        p = by_index.get(t["index"], {})
        for row in [p.get("scheme_row") or {}] + list(p.get("ledger_rows") or []):
            ref = (row or {}).get("settlement_ref")
            if ref:
                flagged_refs.add(str(ref))
    evidence = [
        {"tool": s["tool"], "input": s["input"],
         "output": json.dumps(s["output"], default=str)[:160]}
        for s in result.get("investigation_log", [])
        if s["tool"] != "get_scheme_rules"
        and (s["tool"] == "lookup_history"
             or any(r in json.dumps(s, default=str) for r in flagged_refs))
    ][:25]
    evidence_ser = json.dumps(evidence, default=str)[:6000]

    counts = {"P1": 0, "P2": 0, "P3": 0}
    for t in triaged:
        counts[t.get("severity", "P2")] = counts.get(t.get("severity", "P2"), 0) + 1
    stats = {"total_exceptions": len(triaged), "by_severity": counts,
             "auto_resolvable": sum(1 for t in triaged if t.get("auto_resolvable")),
             "flagged_for_review": len(flagged)}

    def _item(t):
        return {
            "index": t["index"],
            "exception": by_index.get(t["index"], {}),
            "first_pass_triage": {k: t.get(k) for k in
                                  ("severity", "probable_root_cause",
                                   "recommended_action", "auto_resolvable",
                                   "confidence", "evidence")},
        }

    def _amount(t):
        p = by_index.get(t["index"], {})
        row = p.get("scheme_row") or (p.get("ledger_rows") or [{}])[0] or {}
        try:
            return float(row.get("amount", 0))
        except (TypeError, ValueError):
            return 0.0

    # estimate from the ACTUAL serialized sizes (chars/3 ≈ tokens, then a
    # 30% margin) — the first live run proved hand-waved token guesses
    # under-count and let the guard approve a call that broke the budget
    per_item_chars = max(len(json.dumps(_item(t), default=str))
                         for t in flagged)

    def _est(n_items):
        in_tokens = (len(evidence_ser) + n_items * per_item_chars) // 3 + 700
        return 1.3 * _estimate_usd(ESCALATION_MODEL, in_tokens,
                                   60 * n_items + 250)

    # degradation rung: if the full review doesn't fit the remaining
    # budget, review the highest-exposure items that do fit
    if not meter.can_afford(_est(len(flagged))):
        flagged.sort(key=_amount, reverse=True)
        keep = 0
        for n in range(len(flagged), 0, -1):
            if meter.can_afford(_est(n)):
                keep = n
                break
        skipped = flagged[keep:]
        flagged = flagged[:keep]
        for t in skipped:
            t["escalated"] = False
            t["review"] = "escalation_skipped_budget"
        if verbose and skipped:
            print(f"  [cost] escalation TRIMMED (budget): reviewing top "
                  f"{keep} of {keep + len(skipped)} flagged items by exposure")
        if not flagged:
            result["escalation"] = {
                "escalated": 0, "flagged": len(skipped),
                "note": "skipped entirely: remaining budget "
                        f"(${meter.remaining_usd:.4f}) below minimum estimate",
            }
            return

    items = [_item(t) for t in flagged]
    user_text = (
        "Second-opinion review. Batch statistics:\n"
        + json.dumps(stats)
        + "\n\nFlagged items:\n"
        + json.dumps(items, default=str)
        + "\n\nInvestigation evidence gathered by the first pass:\n"
        + evidence_ser
    )

    response = client.messages.create(
        model=ESCALATION_MODEL,
        max_tokens=ESCALATION_MAX_TOKENS,
        system=ESCALATION_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    cost = meter.record(ESCALATION_MODEL, response.usage)
    if verbose:
        print(f"  [cost] {ESCALATION_MODEL} second opinion on "
              f"{len(items)} item(s) -> ${cost:.4f} "
              f"(total ${meter.total_usd:.4f})")

    raw = "".join(b.text for b in response.content if b.type == "text")
    parsed = _parse_json(raw) or {}
    if parsed.get("executive_summary"):
        result["executive_summary"] = parsed["executive_summary"]
    reviews = {r["index"]: r for r in parsed.get("reviews", [])
               if isinstance(r, dict) and "index" in r}

    revised = 0
    for t in flagged:
        t["escalated"] = True
        r = reviews.get(t["index"])
        if not r:
            t["review"] = "sonnet-no-verdict"
            continue
        if r.get("verdict") == "revise":
            t["severity"] = r.get("severity", t["severity"])
            t["recommended_action"] = r.get("recommended_action",
                                            t["recommended_action"])
            t["review"] = f"sonnet-revised: {r.get('note', '')}"
            revised += 1
        else:
            t["review"] = f"sonnet-confirmed: {r.get('note', '')}"
    result["escalation"] = {"escalated": len(flagged), "revised": revised}
