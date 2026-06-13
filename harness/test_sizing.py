"""
P3 sizing verification — fractional Kelly. Pure math, no network/LLM.
Run:  ./.venv/Scripts/python.exe -m harness.test_sizing   (from polyswarm/)
"""
import sys
from harness.sizing import size_bet, kelly_fraction

ok = True
def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))

def approx(a, b, tol=1e-6): return abs(a - b) < tol

print("== Kelly formula direction + value ==")
side, f = kelly_fraction(0.60, 0.40)
check("p>c -> YES", side == "YES")
check("f* = (p-c)/(1-c) = 0.3333", approx(f, 0.2/0.6), f"got {f:.4f}")
side, f = kelly_fraction(0.40, 0.60)
check("p<c -> NO", side == "NO")
check("f* = (c-p)/c = 0.3333", approx(f, 0.2/0.6), f"got {f:.4f}")
side, f = kelly_fraction(0.50, 0.50)
check("p==c -> no side", side is None and f == 0.0)

print("\n== degenerate market prices ==")
check("c=1.0 -> no bet", kelly_fraction(0.9, 1.0)[0] is None)
check("c=0.0 -> no bet", kelly_fraction(0.1, 0.0)[0] is None)

print("\n== cap binds on a big edge (quarter-Kelly 0.083 > cap 0.02) ==")
s = size_bet(0.60, 0.40, bankroll=1000)
check("side YES", s.side == "YES")
check("capped at 2%", s.capped and approx(s.fraction, 0.02), f"frac={s.fraction}")
check("stake = 0.02*1000 = 20", approx(s.stake, 20.0), f"stake={s.stake}")

print("\n== lambda path WITHOUT cap (small edge) ==")
# p=0.53,c=0.50: f*=(0.03)/(0.50)=0.06; quarter=0.015 < cap 0.02 -> not capped
s = size_bet(0.53, 0.50, bankroll=1000)
check("not capped", not s.capped)
check("f* = 0.06", approx(s.f_star, 0.06), f"f*={s.f_star}")
check("fraction = 0.25*0.06 = 0.015", approx(s.fraction, 0.015), f"frac={s.fraction}")
check("stake = 15", approx(s.stake, 15.0), f"stake={s.stake}")

print("\n== min-edge cutoff ==")
s = size_bet(0.51, 0.50, bankroll=1000)   # edge 0.01 < min_edge 0.02
check("thin edge -> no bet (side None, stake 0)", s.side is None and s.stake == 0.0, s.reason)
s = size_bet(0.525, 0.50, bankroll=1000)  # edge 0.025 > min_edge -> bets
check("edge just above min -> bets", s.side == "YES" and s.stake > 0)

print("\n== bankroll compounding (capped bet scales linearly) ==")
s1 = size_bet(0.60, 0.40, bankroll=1000)
s2 = size_bet(0.60, 0.40, bankroll=2000)
check("2x bankroll -> 2x stake (20 vs 40)", approx(s2.stake, 2 * s1.stake), f"{s1.stake} vs {s2.stake}")
check("depleted bankroll -> no bet", size_bet(0.60, 0.40, bankroll=0).side is None)

print("\n== NO-side sizing sanity ==")
s = size_bet(0.20, 0.50, bankroll=1000)  # p<c -> NO; f*=(0.5-0.2)/0.5=0.6; quarter 0.15 -> cap 0.02
check("NO side, capped, stake 20", s.side == "NO" and approx(s.stake, 20.0), f"side={s.side} stake={s.stake}")

print("\n" + ("P3 SIZING: ALL PASSED" if ok else "P3 SIZING: FAILURES ABOVE"))
sys.exit(0 if ok else 1)
