"""Dry-run REPLAY of the betting guards on the exact failures/passes from the transcript.
Pure classifier + guard evaluation — NO bets, NO LLM, NO live fetch.

    python -m harness.replay_guards
"""
import sys
sys.path.insert(0, r"C:\Users\OMEN\Pictures\Polymarket\polyswarm")
from harness import classifier
from harness import predict_today as pt


def label_of(title):
    return classifier.tag_market({"question": title}).label


def decide(title, swarm, chal, cons, price, group_legs=None, label=None):
    lab = label if label is not None else label_of(title)
    ok, reason = pt._betting_guards(lab, swarm, chal, cons, group_legs or [], price)
    side = "YES" if swarm > price else "NO"
    return lab, ok, reason, side


print("=" * 96)
print(f"GUARDS  SKIP_MECHANICAL={pt.SKIP_MECHANICAL}  MAX_DIVERGENCE={pt.MAX_SWARM_CHALLENGER_DIVERGENCE}  "
      f"MIN_CONSENSUS={pt.MIN_SWARM_CONSENSUS}  MAX_GROUP_SUM={pt.MAX_GROUP_PROB_SUM}  "
      f"ONE_YES_PER_EVENT={pt.ONE_YES_PER_EVENT}")
print("=" * 96)

# (title, swarm_p, challenger_p, consensus, market_price, expected)
CASES = [
    ("US x Iran permanent peace deal by June 15, 2026?",                0.139, 0.120, 0.959, 0.11, "BET"),
    ("Israel closes its airspace by June 15?",                          0.200, 0.200, 1.000, 0.14, "BET"),
    ("Will Sweden win on 2026-06-14?",                                  0.118, 0.515, 0.952, 0.52, "SKIP"),
    ("Will Elon Musk post 65-89 tweets from June 13 to June 15, 2026?", 0.534, 0.100, 0.526, 0.05, "SKIP"),
    ("GPT-5.6 released by June 15, 2026?",                              0.304, 0.025, 0.266, 0.02, "SKIP"),
]
allgood = True
for title, sw, ch, co, px, expect in CASES:
    lab, ok, reason, side = decide(title, sw, ch, co, px)
    got = "BET" if ok else "SKIP"
    allgood &= (got == expect)
    print(f"\n[{'PASS' if got == expect else 'FAIL'}] {title[:66]}")
    print(f"        class={lab:10} swarm={sw:.0%} challenger={ch:.0%} consensus={co:.2f} market={px:.0%} side={side}")
    print(f"        DECISION: {'BET ' + side if ok else 'NO BET — ' + reason}   (expected {expect})")

print("\n" + "=" * 96)
print("Guard D — NEW: bet MORE THAN ONE leg per mutually-exclusive event, but the WINNING side")
print("on each: at most one YES (one winner), unlimited NO (fade the losers). label forced 'unknown'.")
print("=" * 96)
# An Elon tweet-count event. Swarm thinks the 40-64 bucket WINS, the others LOSE.
# (name, swarm_p, market_price)
buckets = [("<40 tweets",   0.12, 0.30),   # swarm < market -> NO (fade)
           ("40-64 tweets", 0.68, 0.40),   # swarm > market -> YES (back the winner)
           ("65-89 tweets", 0.15, 0.25),   # swarm < market -> NO (fade)
           ("90+ tweets",   0.55, 0.20)]   # swarm > market -> YES, but we already hold a YES -> BLOCKED
group, n_yes, n_no = [], 0, 0
for name, sp, px in buckets:
    lab, ok, reason, side = decide("x", sp, None, 0.7, px, group_legs=list(group), label="unknown")
    if ok:
        if side == "YES": n_yes += 1
        else: n_no += 1
        group.append({"event_slug": "elon-tweets", "market_id": name, "side": side, "model_p": sp})
        print(f"   {name:14} swarm={sp:.0%} market={px:.0%} -> BET {side}")
    else:
        print(f"   {name:14} swarm={sp:.0%} market={px:.0%} -> NO BET — {reason}")
print(f"   => {n_yes} YES (the winner) + {n_no} NO (fades) bet; extra YES legs blocked. MULTIPLE bets, one winner.")

print("\n" + "=" * 96)
print("RESULT:", "all PASS — bad bets skipped, Iran + Israel still bet." if allgood else "MISMATCH — see FAIL above.")
print("=" * 96)
