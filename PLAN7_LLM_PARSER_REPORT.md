# Plan 7 — LLM Probability Parser Hardening Report

> Goal: LLM text parsing must NEVER convert malformed output, dates, years, prices,
> unrelated numbers, or out-of-range values into a confident trade probability.
>
> Paper-only. No real-money execution. Branch: `fix/llm-probability-parser-hardening`.
> Built on Plans 1-6 (8ea3920 … f0563d5).

Status: **COMPLETE** — 61/61 test modules pass (incl. the new 28-case Plan-7 module).
Committed as d6003da. A 3-skeptic adversarial workflow executed the parser on
~55 adversarial inputs: **0 holes found**, 1 low-severity quirk (fullwidth digits) hardened.

## Summary

The tradeable probability used to come from `core/agent._coerce_prob`, which took the
**first number** in prose, treated any **bare value > 1 as a percent**, and **clamped**
out-of-range values into [0,1]. So `2026` → 1.0, `"June 15"` → 0.15, `65` → 0.65,
`8` → 1.0 — a year/date/count could become a confident bet, and an out-of-range model
reply was silently clamped. Plan 7 adds ONE strict parser (`core/probability_parser.py`)
and routes both tradeable callers (`Agent.estimate`, `challenger.single_llm_forecast`)
through it: a probability is accepted ONLY when it is unambiguous, finite, strictly in
[0.01, 0.99], and (for percents) explicitly marked percent. Anything else is rejected
with a specific reason — never clamped, never fabricated. A parse failure raises in the
agent (so the swarm counts it as a failure → Plan 2 blocks) and returns None in the
challenger (a skipped vote, never 0.5).

## Old Dangerous Behavior

* **First-number prose fallback:** `_coerce_prob("I think 2026 will...")` → first number
  → a "probability."
* **Year/date parsing:** `2026` / `"June 15, 2026"` → a number → (after the >1 percent
  rule + clamp) a tradable probability.
* **Out-of-range clamp:** numeric `8` → `min(max(8,0),1)` → 1.0; `120%` → 1.2 → 1.0.
* **Bare number = percent:** any value > 1 was divided by 100 (`65` → 0.65) with no
  explicit percent marker.
* **Hidden inside a healthy forecast:** a fabricated/clamped value flowed into the swarm
  as if it were a real agent estimate (and the challenger clamped to (0.01,0.99)).

## New Behavior

* **Strict parser** `parse_probability_response(text, *, source, allow_percent=True,
  require_json=False)` returns `{ok, probability, confidence, reason, method, raw, warnings}`.
* **JSON-first contract:** accepts `{"probability": 0.62}`, `{"p_yes": 0.62}`,
  `{"yes_probability": 0.62}`, `{"probability_percent": 62}`, `{"probability": 62,
  "unit": "percent"}`. Rejects `{"probability": 62}` (no unit), `2026`, `1.4`, `-0.1`,
  `"likely"`, `{}`, malformed JSON.
* **Percent only when explicit:** a `%`, the word "percent", a `unit:"percent"`, or a
  `probability_percent` field. A bare value > 1 is REJECTED — never assumed percent.
* **No clamping:** an out-of-range value (`8`, `1.2`, `120%`, `-5%`, exact 0/1, sub-0.01)
  is REJECTED, never mapped into [0,1].
* **No arbitrary first-number fallback:** prose is accepted only when a number is clearly
  attached to probability wording (e.g. "probability 62%", "I assign 0.62 probability").
  Conflicting probability values → ambiguous → reject.
* **Parser-failure metadata:** `AgentEstimate` carries `parse_ok / parse_method /
  parse_reason`. A failure RAISES (no fake estimate).
* **Swarm/challenger failure behavior:** an agent parse failure → raise → counted in the
  swarm's `agent_failures` (with the parse reason) → if survivors < the Plan-2 minimum →
  degraded/no-bet. A challenger parse failure → None → excluded from the ensemble mean
  (never a 0.5 vote).
* **No-bet journaling:** parser-driven no-bets surface through the Plan-2 swarm-health
  skip (`_skip` / `_sd_skip`: print + obs + journal) carrying the parse reason.

## Parser Path Map (Phase 1)

| Parser path | File/function | Input type | Old behavior | Failure risk | Fix |
| ----------- | ------------- | ---------- | ------------ | ------------ | --- |
| agent probability | `core/agent.Agent.estimate` | LLM reply | `_coerce_prob(data["probability"])` / first-number prose / clamp | year/date/bare/out-of-range → fake prob | **strict parser**; raise on fail |
| challenger | `harness/challenger.single_llm_forecast` | LLM reply | `_coerce_prob` + clamp (0.01,0.99) | same | **strict parser**; None on fail |
| ensemble | `harness/challenger.ensemble_forecast` | per-model | already skips None | (ok) | unchanged (None excluded) |
| legacy coercer | `core/agent._coerce_prob` | any | first-number + clamp | dangerous | retained but OFF every tradeable path |
| crowd signal | `harness/mirofish.py` parser | crowd report | own regex coercer | (not the trade prob) | OUT OF SCOPE (Plan-7 constraint) |
| scenario | `core/scenario.py` `_parse_json` | LLM reply | tolerant JSON | (not a betting path) | unchanged |
| swarm/aggregator | `core/swarm` / `core/aggregator` | AgentEstimate list | aggregates estimates | n/a (upstream parse now strict) | parse failures → agent_failures (Plan 2) |

## Accepted Formats

`{"probability": 0.62}` · `{"p_yes": 0.62}` · `{"yes_probability": 0.62}` ·
`{"probability_percent": 62}` · `{"probability": 62, "unit": "percent"}` ·
`{"probability": "60%"}` · `probability: 65%` · `I assign 0.62 probability` ·
`Market resolves in 2026, probability 62%` (the year is ignored, 62% accepted) ·
`6.5e-1` (== 0.65, a valid decimal).

## Rejected Formats

`{"probability": 65}` · `{"probability": 2026}` · `{"probability": 1.2/1.4}` ·
`{"probability": -0.1}` · `{"probability": 0.0/1.0}` · `{"probability": "likely"}` ·
`{}` · `{bad json` · `June 15, 2026` · `The market resolves in 2026` ·
`Price is 0.63, volume 10000` (no probability wording) · `There is 2026 and 65%` ·
`The odds are 0.62 and probability is 0.55` (conflicting) · `120%` / `-5%` ·
empty / whitespace · `NaN` / `Infinity` · bare `0.6` / `[0.6]` / `I estimate about 0.7`
(no probability wording).

## New No-Bet Reasons

Parser result reasons: `llm_probability_parse_error`, `llm_probability_out_of_range`,
`llm_probability_ambiguous`, `llm_probability_missing`.
No-bet codes (defined for callers/journaling): `llm_probability_parse_error_no_bet`,
`llm_probability_out_of_range_no_bet`, `llm_probability_ambiguous_no_bet`,
`llm_probability_missing_no_bet`, `llm_probability_fallback_display_only_no_bet`,
`challenger_parse_failed_no_bet`, `swarm_parse_failed_no_bet`. In practice a swarm parse
failure surfaces through the Plan-2 swarm-health no-bet (`swarm_*_no_bet`, journaled), with
the parse reason captured in `agent_failures`.

## Tests Added

`harness/tests/test_probability_parser.py` (27): valid JSON/percent/prose forms; rejection
of bare numbers / years / dates / over-1 / negative / non-numeric / malformed JSON /
no-wording prose / conflicting / empty / exact-0-1 / NaN-Inf / out-of-range-percent;
require_json; integration (1-valid+4-parse-fail → degraded no-bet; all-parse-fail → abort;
REAL parser+agent end-to-end abort on a date reply; challenger date → None; parse-failed
swarm blocked at `_p_swarm_health`; `_skip` never opens + journaled); static scans
(tradeable paths use the strict parser; no clamp in the parser). Plus `test_parser_robust.py`
updated to the strict contract (legacy `_coerce_prob` tests retained + flagged non-tradeable).

## Commands Run / Test Results

See `PLAN7_COMMAND_LOG.md`. `test_probability_parser` 28/28; `test_parser_robust` 5/5;
`python run_tests.py --no-llm` → **61/61 modules passed** (exit 0, no FAIL, no skips beyond
the pre-existing LLM-integration self-skip). pytest not used. No live-service / live-LLM /
live-DB / trading commands run.

## Static verification (Phase 11)

| Search | Finding | Safe? | Explanation |
| ------ | ------- | ----- | ----------- |
| first-number fallback (tradeable) | none | yes | agent/challenger use the strict parser; `_coerce_prob(probability)` removed from agent |
| clamp in parser path | none | yes | `min(max(`/`max(min(` absent from `probability_parser.py`; out-of-range rejected |
| `0.5` fallback to a bet | none | yes | parse failure → raise (agent) / None (challenger); no default-0.5 tradable forecast |
| `probability_percent` / `unit:"percent"` | handled | yes | explicit percent accepted; bare >1 rejected |
| `parse_probability_response` | used by agent + challenger | yes | the only tradeable parse path |
| parse failure → wallet | unreachable | yes | raise→swarm failure→Plan 2 block; challenger None→excluded; guard before wallet |

## Remaining Risks (Plan 7 only)

* The legacy `core/agent._coerce_prob` (first-number + clamp) still EXISTS for backward
  compatibility but is OFF every tradeable path (a static test enforces this). It could be
  deleted in a future cleanup once no non-tradeable caller needs it.
* The prose parser is intentionally conservative: a number not adjacent to probability
  wording is rejected, so a sloppy model reply ("0.62" with no "probability" word) becomes
  an agent parse-failure rather than a bet. This raises the agent-failure rate for very
  sloppy models, which Plan 2 then handles (degraded/no-bet) — the safe direction.
* MiroFish's own crowd-signal parser is unchanged (out of Plan-7 scope); it does not produce
  the trade probability.

## Proof

* **Years/dates rejected** — `{"probability": 2026}`, `June 15, 2026`, `The market resolves
  in 2026` → ok=False (`year_rejected`).
* **Out-of-range rejected, not clamped** — `8`, `1.2`, `120%`, `-5%`, exact 0/1 → ok=False
  (`over_one_rejected_not_clamped`, `percent_out_of_range_not_clamped`, `exact_zero_one_rejected`).
* **Fallback 0.5 cannot bet** — agent parse failure raises (no 0.5); challenger returns None
  (`challenger_date_reply_returns_none_not_half`); no default-0.5 tradable path exists.
* **Parser failure reduces swarm survivors** — `one_valid_four_parse_fail_is_degraded_no_bet`
  (allow_bet False, 4 agent_failures) + `all_parse_fail_aborts_swarm` +
  `real_parser_failure_aborts_swarm_end_to_end`.
* **Parser failure cannot reach wallet** — `parse_failed_swarm_blocks_at_guard`
  (`_p_swarm_health` blocks) + `parser_failure_skip_never_opens_position`.
* **All parser no-bets visible** — surfaced via `_skip`/`_sd_skip` (print + obs + journal).
* **Adversarial verification** — 3 skeptics *executed* the parser on ~55 adversarial
  strings (years, dates, times, money, counts, scientific notation, nested JSON, arrays,
  conflicting fields, out-of-range, exact 0/1, bool, NaN/Inf, `65/100`, `1,234`, `+0.65`)
  and traced every wallet path. **0 holes found** — every dangerous input rejects (never
  clamped, never fabricated) and a parse failure can never reach `wallet.open_position`.
  They found ONE low-severity quirk — now HARDENED.

## Hardening from adversarial review

* **Fullwidth/Unicode digits** — the number regexes used Unicode-aware `\d` with an
  ASCII-only filler class, so `"probability ６５%"` could mis-scale to an in-range (but
  wrong) value. (Requires non-ASCII digits an English local model never emits, and the
  result was always bounded — never clamped/0/1 — so not a guarantee breach.) Fixed by
  compiling the parser regexes with `re.ASCII`: `\d` now matches only ASCII 0-9, so
  fullwidth digits are not captured and the input REJECTS (fail-safe).
  (`test_fullwidth_unicode_digits_rejected`)
* (Noted, not changed: `"probability: 0.65 or maybe 0.70"` returns 0.65 — the value
  attached to the probability wording; the hedge "0.70" carries no wording within reach.
  0.65 is a genuine stated probability, so this is correct, not a violation.)

## Phase 13 — acceptance criteria

1. out-of-range rejected, not clamped — YES.
2. years/dates/counts/money cannot be parsed as probability — YES.
3. first-number fallback removed from tradeable paths — YES (static test).
4. malformed JSON cannot produce a tradable probability — YES.
5. `65` without percent/unit rejected — YES.
6. `65%` / `probability_percent` accepted — YES.
7. parser failures count as swarm/challenger failures — YES (raise→agent_failures; None→excluded).
8. parser failure cannot reach sizing/wallet — YES (Plan-2 guard + challenger None).
9. fallback 0.5 is display-only or no-bet — YES (no tradable 0.5 fallback exists).
10. parser no-bets journaled — YES.
11. tests prove the above — YES (27 cases).
12. existing tests still pass — YES (61/61).
13. report written — YES (this file).
