"""
P3 paper-wallet verification. No network/LLM; throwaway DB.
Run:  ./.venv/Scripts/python.exe -m harness.test_wallet   (from polyswarm/)
"""
import os, tempfile, sys
TEST_DB = os.path.join(tempfile.gettempdir(), "polyswarm_wallet_test.db")
if os.path.exists(TEST_DB): os.remove(TEST_DB)
os.environ["DATABASE_URL"] = TEST_DB

from harness import wallet as W           # noqa: E402
from harness.wallet import WalletConfig   # noqa: E402
from harness.sizing import size_bet       # noqa: E402

ok = True
def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
def approx(a, b, tol=1e-4): return abs(a - b) < tol

print("== init ==")
W.init_wallet(1000.0)
s = W.get_state()
check("starts cash=1000 equity=1000 realized=0", s["cash"] == 1000 and s["equity"] == 1000 and s["realized_pnl"] == 0, str(s))

print("\n== size + open a YES position (fill worse than mid) ==")
sz = size_bet(0.60, 0.40, bankroll=W.bankroll_for_sizing())   # stake 20 (capped)
check("sizer -> YES stake 20", sz.side == "YES" and approx(sz.stake, 20.0), f"stake={sz.stake}")
fr = W.open_position("MKT-1", "Will X win?", sz.side, 0.60, 0.40, sz.edge, sz.stake)
check("filled", fr.opened, fr.reason)
check("fill = market_p + slippage = 0.41", approx(fr.fill_price, 0.41), f"fill={fr.fill_price}")
check("shares = 20/0.41 = 48.7805", approx(fr.shares, 20/0.41), f"shares={fr.shares}")
check("cash now 980", approx(W.get_state()["cash"], 980.0), str(W.get_state()))
check("open exposure 20", approx(W.get_open_exposure(), 20.0))

print("\n== settle a WIN (YES, outcome=1) ==")
res = W.settle_market("MKT-1", outcome=1.0)
pnl = res[0]["realized_pnl"]
check("payout = shares*1 = 48.7805", approx(res[0]["payout"], 20/0.41), str(res[0]))
check("realized_pnl = payout - stake = 28.7805", approx(pnl, 20/0.41 - 20), f"pnl={pnl}")
st = W.get_state()
check("cash = 980 + payout = 1028.78", approx(st["cash"], 980 + 20/0.41), str(st))
check("realized_pnl tracked on wallet", approx(st["realized_pnl"], 20/0.41 - 20))
check("no open positions", st["n_open"] == 0)

print("\n== settle a LOSS (YES bet, outcome=0) ==")
cash_before = W.get_state()["cash"]
sz2 = size_bet(0.60, 0.40, bankroll=W.bankroll_for_sizing())
W.open_position("MKT-2", "Will Y win?", "YES", 0.60, 0.40, sz2.edge, sz2.stake)
W.settle_market("MKT-2", outcome=0.0)
st = W.get_state()
check("loss costs exactly the stake", approx(st["cash"], cash_before - sz2.stake), f"cash {st['cash']} vs {cash_before - sz2.stake}")

print("\n== NO-side round trip (buy NO, NO wins) ==")
# market_p 0.70 -> NO base = 0.30, fill 0.31; model_p 0.50 -> NO side; cap stake
cash0 = W.bankroll_for_sizing()
szn = size_bet(0.50, 0.70, bankroll=cash0)
check("sizer -> NO", szn.side == "NO", f"side={szn.side}")
frn = W.open_position("MKT-3", "NO market", "NO", 0.50, 0.70, szn.edge, szn.stake)
check("NO fill = (1-0.70)+0.01 = 0.31", approx(frn.fill_price, 0.31), f"fill={frn.fill_price}")
resn = W.settle_market("MKT-3", outcome=0.0)   # NO wins
check("NO win pays out shares", resn[0]["won"] and approx(resn[0]["payout"], szn.stake/0.31), str(resn[0]))

print("\n== guardrails ==")
big = W.open_position("MKT-X", "too big", "YES", 0.6, 0.4, 0.2, stake=500.0)   # > per-bet cap
check("per-bet cap rejects oversized stake", (not big.opened) and "cap" in big.reason, big.reason)
tight = WalletConfig(max_exposure_frac=0.001)
exp = W.open_position("MKT-Y", "exposure", "YES", 0.6, 0.4, 0.2, stake=W.bankroll_for_sizing()*0.02, cfg=tight)
check("max-exposure cap rejects", not exp.opened, exp.reason)

print("\n" + ("P3 WALLET: ALL PASSED" if ok else "P3 WALLET: FAILURES ABOVE"))
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
