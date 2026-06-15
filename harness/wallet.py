"""
P3 — simulated PAPER wallet. Single source of truth for sizing.

NO real money, NO keys, NO execution. Everything here is a simulation that
records what a bet WOULD have done, with realistic frictions:
  - fill at price + slippage (never the mid) — you pay a worse price than quoted
  - a configurable per-trade fee
  - a hard per-bet cap and a max total concurrent-exposure cap (guardrails)
  - open positions are marked at cost for equity; ONLY realized P&L counts toward
    the profitability gate (Gate 2)

Accounting (binary $1-payout shares):
  open YES at fill f (= market_p + slippage): shares = stake / f; cash -= stake+fee
  settle: won if (side==YES and outcome==1) or (side==NO and outcome==0)
          payout = shares*$1 if won else 0;  cash += payout
          realized_pnl = payout - stake - fee
Sizing bankroll = current LIQUID cash (conservative: locked stakes brake new bets
and returning settlements compound it). Equity = cash + open stakes (at cost).

Shares the ./polyswarm.db calibration DB (same DATABASE_URL convention).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime

try:
    from harness import obs
except Exception:
    obs = None

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")

DEFAULT_STARTING_BANKROLL = 1000.0


@dataclass
class WalletConfig:
    slippage: float = 0.01          # absolute price worsening on the share you buy
    fee_frac: float = 0.0           # per-trade fee as a fraction of stake (Polymarket ~0)
    max_bet_frac: float = 0.02      # hard cap: reject any single stake > this * cash
    max_exposure_frac: float = 0.50 # reject opens that push total open stake past this * equity


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_wallet(starting: float = DEFAULT_STARTING_BANKROLL):
    """Create paper tables and seed the single wallet row if absent. Idempotent."""
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_wallet (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_bankroll REAL NOT NULL,
            cash REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            question TEXT,
            side TEXT,                -- YES | NO
            model_p REAL, market_p REAL, edge REAL,
            stake REAL, fill_price REAL, shares REAL, fee REAL,
            status TEXT DEFAULT 'open',   -- open | settled
            outcome REAL, payout REAL, realized_pnl REAL,
            end_date TEXT,                -- market resolution time (for the countdown)
            opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
            settled_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_market ON paper_positions(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_status ON paper_positions(status)")
    # migrate older DBs: add end_date (countdown) + event_slug (clickable link)
    _pcols = [r[1] for r in conn.execute("PRAGMA table_info(paper_positions)").fetchall()]
    if "end_date" not in _pcols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN end_date TEXT")
    if "event_slug" not in _pcols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN event_slug TEXT")
    if conn.execute("SELECT COUNT(*) FROM paper_wallet").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO paper_wallet (id, starting_bankroll, cash, realized_pnl, updated_at) VALUES (1, ?, ?, 0, ?)",
            (starting, starting, datetime.utcnow().isoformat()),
        )
    conn.commit()
    conn.close()


def _cash() -> float:
    conn = _conn(); row = conn.execute("SELECT cash FROM paper_wallet WHERE id=1").fetchone(); conn.close()
    return row["cash"] if row else 0.0


def get_open_exposure() -> float:
    conn = _conn()
    row = conn.execute("SELECT COALESCE(SUM(stake),0) AS x FROM paper_positions WHERE status='open'").fetchone()
    conn.close()
    return row["x"]


def get_state() -> dict:
    conn = _conn()
    w = conn.execute("SELECT * FROM paper_wallet WHERE id=1").fetchone()
    nopen = conn.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0]
    conn.close()
    cash = w["cash"] if w else 0.0
    exposure = get_open_exposure()
    return {
        "starting_bankroll": w["starting_bankroll"] if w else 0.0,
        "cash": round(cash, 4),
        "open_exposure": round(exposure, 4),
        "equity": round(cash + exposure, 4),        # open positions marked at cost
        "realized_pnl": round(w["realized_pnl"] if w else 0.0, 4),
        "n_open": nopen,
    }


def bankroll_for_sizing() -> float:
    """The bankroll passed to the Kelly sizer = liquid cash (conservative)."""
    return _cash()


@dataclass
class FillResult:
    opened: bool
    reason: str
    position_id: int | None = None
    side: str | None = None
    fill_price: float | None = None
    shares: float | None = None
    stake: float | None = None
    fee: float | None = None
    def to_dict(self): return asdict(self)


def open_position(market_id: str, question: str, side: str, model_p: float, market_p: float,
                  edge: float, stake: float, cfg: WalletConfig | None = None,
                  end_date: str | None = None, event_slug: str | None = None) -> FillResult:
    """Open a simulated paper position with a realistic (worse-than-mid) fill.
    Enforces the per-bet cap and the max-exposure guardrail; returns FillResult."""
    cfg = cfg or WalletConfig()

    def _skip(fr):
        # obs (leaf emit): record a guardrail rejection. Returns fr UNCHANGED so the
        # caller's return value is byte-identical to before instrumentation.
        if obs:
            try:
                obs.hooks.on_trade_skip(
                    forecast_id=obs.current().get("forecast_id"),
                    reason=fr.reason,
                    inputs={"market_id": market_id, "side": side, "stake": stake,
                            "model_p": model_p, "market_p": market_p, "edge": edge},
                )
            except Exception:
                pass
        return fr

    if side not in ("YES", "NO"):
        return _skip(FillResult(False, f"bad side {side!r}"))
    if stake <= 0:
        return _skip(FillResult(False, "non-positive stake"))

    cash = _cash()
    if stake > cash + 1e-6:
        return _skip(FillResult(False, f"stake {stake:.2f} exceeds cash {cash:.2f}"))
    # Defensive guardrails (the sizer is the primary cap). Use a small relative
    # tolerance so a stake sized EXACTLY at the cap isn't rejected by float rounding
    # — otherwise every high-edge (cap-binding) bet would be refused.
    if stake > cfg.max_bet_frac * cash * (1 + 1e-6) + 1e-9:
        return _skip(FillResult(False, f"stake {stake:.2f} exceeds per-bet cap {cfg.max_bet_frac:.0%} of cash"))
    exposure = get_open_exposure()
    equity = cash + exposure
    if exposure + stake > cfg.max_exposure_frac * equity * (1 + 1e-6) + 1e-9:
        return _skip(FillResult(False, f"would exceed max exposure {cfg.max_exposure_frac:.0%} of equity"))

    # fill at a WORSE price than quoted (slippage on the share you actually buy)
    base = market_p if side == "YES" else (1.0 - market_p)
    fill_price = min(max(base + cfg.slippage, 0.01), 0.99)
    fee = round(cfg.fee_frac * stake, 6)
    shares = round(stake / fill_price, 6)

    conn = _conn()
    cur = conn.execute(
        """INSERT INTO paper_positions
           (market_id, question, side, model_p, market_p, edge, stake, fill_price, shares, fee, status, end_date, event_slug)
           VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
        (market_id, question, side, model_p, market_p, edge, round(stake, 6), fill_price, shares, fee, end_date, event_slug),
    )
    pid = cur.lastrowid
    conn.execute("UPDATE paper_wallet SET cash = cash - ?, updated_at=? WHERE id=1",
                 (round(stake + fee, 6), datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    if obs:
        try:
            obs.hooks.on_trade_open(
                trade_id=str(pid),
                market_id=market_id,
                forecast_id=obs.current().get("forecast_id"),
                side=side,
                stake=round(stake, 6),
                fill_price=fill_price,
                slippage=cfg.slippage,
                fee=fee,
            )
        except Exception:
            pass
    return FillResult(True, "filled", pid, side, fill_price, shares, round(stake, 6), fee)


def settle_market(market_id: str, outcome: float) -> list[dict]:
    """Settle every OPEN position on a market. outcome: 1.0=YES won, 0.0=NO won.
    Returns the list of settled positions with realized P&L."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)
    ).fetchall()
    settled = []
    for r in rows:
        won = (r["side"] == "YES" and outcome == 1.0) or (r["side"] == "NO" and outcome == 0.0)
        payout = round(r["shares"] * 1.0, 6) if won else 0.0
        realized = round(payout - r["stake"] - r["fee"], 6)
        conn.execute(
            "UPDATE paper_positions SET status='settled', outcome=?, payout=?, realized_pnl=?, settled_at=? WHERE id=?",
            (outcome, payout, realized, datetime.utcnow().isoformat(), r["id"]),
        )
        conn.execute("UPDATE paper_wallet SET cash = cash + ?, realized_pnl = realized_pnl + ?, updated_at=? WHERE id=1",
                     (payout, realized, datetime.utcnow().isoformat()))
        settled.append({"market_id": market_id, "side": r["side"], "stake": r["stake"],
                        "won": won, "payout": payout, "realized_pnl": realized})
        if obs:
            try:
                _row = conn.execute("SELECT cash FROM paper_wallet WHERE id=1").fetchone()
                _cash_after = _row["cash"] if _row else None
                _cash_before = (_cash_after - payout) if _cash_after is not None else None
                obs.hooks.on_trade_settle(
                    trade_id=str(r["id"]),
                    market_id=market_id,
                    outcome=outcome,
                    payout=payout,
                    realized_pnl=realized,
                    bankroll_before=_cash_before,
                    bankroll_after=_cash_after,
                )
            except Exception:
                pass
    conn.commit(); conn.close()
    return settled


def close_at_price(market_id: str, current_yes_price: float) -> list[dict]:
    """Cash OUT every open position on a market at the current market price (sell the
    shares now instead of waiting for resolution). Used to dump bets that resolve too
    far out. Realized P&L = sell value - stake."""
    conn = _conn()
    rows = conn.execute("SELECT * FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)).fetchall()
    out = []
    for r in rows:
        side_price = current_yes_price if r["side"] == "YES" else (1.0 - current_yes_price)
        sell_value = round(r["shares"] * max(0.0, min(1.0, side_price)), 6)
        realized = round(sell_value - r["stake"], 6)
        conn.execute("UPDATE paper_positions SET status='closed', payout=?, realized_pnl=?, settled_at=? WHERE id=?",
                     (sell_value, realized, datetime.utcnow().isoformat(), r["id"]))
        conn.execute("UPDATE paper_wallet SET cash = cash + ?, realized_pnl = realized_pnl + ?, updated_at=? WHERE id=1",
                     (sell_value, realized, datetime.utcnow().isoformat()))
        out.append({"market_id": market_id, "side": r["side"], "stake": r["stake"],
                    "sell_value": sell_value, "realized_pnl": realized})
    conn.commit(); conn.close()
    return out


def get_open_positions() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_closed_positions(limit: int = 80) -> list[dict]:
    """Settled (market resolved) and closed (cashed-out early) positions, newest first.
    Each carries realized_pnl — the win/loss in dollars on that single bet."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM paper_positions WHERE status IN ('settled','closed') "
        "ORDER BY settled_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
