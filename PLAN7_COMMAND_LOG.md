# Plan 7 — Command Log

Every command for Plan 7 (LLM probability parser hardening). Paper-only.
Branch: `fix/llm-probability-parser-hardening`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/event-portfolio-safety)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -7            -> f0563d5 Plan6 … 8ea3920 Plan1 (ALL committed)
git checkout -b fix/llm-probability-parser-hardening  -> Switched to a new branch
```
Plans 1-6 committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — locate every LLM probability parser (Grep/Read)

Tradeable LLM-probability parsers (IN SCOPE):
  core/agent._coerce_prob (line 174) — first-number + treats bare >1 as percent + CLAMPS min(max(v,0),1);
    used by Agent.estimate (probability) AND challenger.single_llm_forecast.
  -> _coerce_prob("2026")=1.0, _coerce_prob("65")=0.65, _coerce_prob(8)=1.0  (the bugs)
Out of scope: harness/mirofish.py has its OWN crowd-signal parser (not the trade probability; Plan-7
constraint excludes MiroFish). core/scenario.py uses _parse_json (not a betting path). All other
min(max(...)) are PRICE/FILL clamps (wallet/profitability/event_portfolio/bankroll) — legitimate, not LLM-prob.

## Phases 2-9 — implementation (no shell)

NEW core/probability_parser.py — strict parse_probability_response (JSON-first; percent only when
explicit; bare >1 / years / dates / out-of-range REJECTED never clamped; conflicting -> ambiguous;
ok=False on any failure). Edited core/agent.py (Agent.estimate uses the strict parser for the TRADABLE
probability -> raises on failure so the swarm counts it as a parse failure; confidence/reasoning parsed
leniently; AgentEstimate gains parse_ok/method/reason; prompt tightened). Edited harness/challenger.py
(single_llm_forecast uses the strict parser -> None on failure, never 0.5/clamp; ensemble already
excludes None). Legacy core/agent._coerce_prob left UNCHANGED but now OFF every tradeable path.
NEW harness/tests/test_probability_parser.py (27 cases); updated harness/tests/test_parser_robust.py
to the strict contract (the old lenient asserts encoded the bug).

## Phases 10-11 — test + static verification

```
# interpreter: .\.venv\Scripts\python.exe
python -m harness.tests.test_probability_parser   -> 27/27 passed (exit 0)
python -m harness.tests.test_parser_robust         -> 5/5 passed (exit 0)
test_challenger_ensemble / test_swarm_sizes / test_swarm_degradation -> pass
python run_tests.py --no-llm                       -> SUMMARY: 61/61 modules passed (exit 0, no FAIL)
```
A 3-skeptic ADVERSARIAL workflow (run wf_f74779a3) EXECUTED the parser on ~55 adversarial inputs
+ traced the wallet paths: **0 holes**, 1 low-severity quirk (fullwidth Unicode digits) HARDENED
via re.ASCII on the parser regexes. Re-ran: test_probability_parser 28/28, full suite 61/61 (exit 0).
pytest not used. NOT run (per constraints): supervisor start/stop, live daemons, real betting loop,
live LLM calls, DB repair, live polyswarm.db.

Static (Grep/scan, read-only):
```
core/probability_parser.py             -> NO min(max( / max(min( (test_parser_has_no_clamp)
core/agent.py Agent.estimate           -> parse_probability_response(raw, source="agent"); no _coerce_prob(probability)
harness/challenger.single_llm_forecast -> parse_probability_response(raw, source="challenger"); returns None on fail
```
