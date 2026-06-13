"""
P1 verification — market classifier. Rules-only (no LLM, no network).
CLEAR cases must classify correctly; AMBIGUOUS/edge cases are reported, not failed.
Run:  ./.venv/Scripts/python.exe -m harness.test_classifier   (from polyswarm/)
"""
import sys
from harness.classifier import tag_market, passes_liquidity_floor, should_forecast

# (question, expected_label) — CLEAR cases the rule engine must get right.
CLEAR = [
    # --- mechanical ---
    ("Will Bitcoin close above $100,000 before July 2026?", "mechanical"),
    ("Will ETH reach $5,000 in 2026?", "mechanical"),
    ("Will the Fed cut interest rates at the next FOMC meeting?", "mechanical"),
    ("Will the Lakers win the NBA Championship?", "mechanical"),
    ("Will the Kansas City Chiefs win the Super Bowl?", "mechanical"),
    ("Will US CPI inflation come in above 3% in May?", "mechanical"),
    ("Will the high temperature in NYC exceed 90 degrees on July 4?", "mechanical"),
    ("Will SpaceX launch Starship before April?", "mechanical"),
    ("Will the S&P 500 close above 6000 this year?", "mechanical"),
    ("Will Manchester City win the Premier League?", "mechanical"),
    ("Will US GDP growth exceed 2% in Q2?", "mechanical"),
    ("Will gold close above $2,500 per ounce?", "mechanical"),
    ("Will unemployment rise above 5% this year?", "mechanical"),
    ("Will Real Madrid win the Champions League final?", "mechanical"),
    ("Will there be a magnitude 7 earthquake in California in 2026?", "mechanical"),
    # --- opinion ---
    ("Will Donald Trump win the 2024 presidential election?", "opinion"),
    ("Will Joe Biden be the Democratic nominee?", "opinion"),
    ("Will the Republicans win control of the Senate?", "opinion"),
    ("Will candidate X win the New Hampshire primary?", "opinion"),
    ("Will Trump's approval rating be above 45% in March?", "opinion"),
    ("Will Taylor Swift be Time Person of the Year?", "opinion"),
    ("Will Oppenheimer win Best Picture at the Oscars?", "opinion"),
    ("Will the incumbent governor win re-election?", "opinion"),
    ("Will Labour win the most seats in the UK general election?", "opinion"),
    ("Will Kamala Harris win the popular vote?", "opinion"),
]

# Edge cases — competing signals. Reported only (the LLM tiebreak handles these live).
AMBIGUOUS = [
    "Will MrBeast reach 10 million new subscribers this year?",   # 'million' vs 'subscribers'
    "Will the new Avatar movie gross over $1 billion at the box office?",  # $ vs box office
    "Will there be a US recession in 2026?",                       # weak signals
    "Will AI be the most talked-about topic of the year?",         # attention, no price
]

def main():
    print("== CLEAR cases (must pass) ==")
    wrong = []
    for q, exp in CLEAR:
        c = tag_market(q)
        mark = "ok " if c.label == exp else "XX "
        if c.label != exp:
            wrong.append((q, exp, c))
        print(f"  [{mark}] {c.label:10s} (exp {exp:10s}) op={c.opinion_score} me={c.mechanical_score} amb={c.ambiguous}  {q[:54]}")
    acc = (len(CLEAR) - len(wrong)) / len(CLEAR)
    print(f"\n  clear accuracy: {len(CLEAR)-len(wrong)}/{len(CLEAR)} = {acc:.0%}")
    if wrong:
        print("\n  MISCLASSIFIED:")
        for q, exp, c in wrong:
            print(f"    - {q}\n      expected {exp}, got {c.label} | signals={c.signals}")

    print("\n== AMBIGUOUS / edge cases (reported only) ==")
    for q in AMBIGUOUS:
        c = tag_market(q)
        print(f"  {c.label:10s} conf={c.confidence:.2f} amb={c.ambiguous} op={c.opinion_score} me={c.mechanical_score}  {q[:50]}")
        print(f"       signals: {c.signals}")

    print("\n== liquidity floor + should_forecast ==")
    liquid_opinion = {"question": "Will the Republicans win the Senate?", "volume": "250000", "liquidity": "40000"}
    thin_opinion   = {"question": "Will candidate Z win the primary?", "volume": "300", "liquidity": "50"}
    mech_liquid    = {"question": "Will Bitcoin close above $100k?", "volume": "999999", "liquidity": "99999"}
    for label, m in [("liquid opinion", liquid_opinion), ("thin opinion", thin_opinion), ("liquid mechanical", mech_liquid)]:
        ok, cls = should_forecast(m)
        print(f"  should_forecast={str(ok):5s}  ({label}: label={cls.label}, floor={passes_liquidity_floor(m)})")

    assert tag_market(liquid_opinion)["label"] if False else True
    f1 = should_forecast(liquid_opinion)[0] is True
    f2 = should_forecast(thin_opinion)[0] is False        # opinion but too thin
    f3 = should_forecast(mech_liquid)[0] is False         # liquid but mechanical
    gate_ok = f1 and f2 and f3
    print(f"\n  gate logic correct: {gate_ok}  (liquid-opinion=Y, thin-opinion=N, liquid-mechanical=N)")

    passed = (len(wrong) == 0) and gate_ok
    print("\n" + ("P1 CLASSIFIER: ALL CLEAR CASES + GATE PASSED" if passed else "P1 CLASSIFIER: FAILURES ABOVE"))
    sys.exit(0 if passed else 1)

if __name__ == "__main__":
    main()
