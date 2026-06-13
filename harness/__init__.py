"""
Polymarket forecasting harness — net-new code built ON the (modified) PolySwarm repo.

Paper / read-only only. No real-money paths, no wallet, no execution endpoints.
Lives inside polyswarm/ so it reuses the .venv and imports core.swarm / core.calibration
directly, and shares the ./polyswarm.db calibration DB.

Modules:
  classifier  — tag_market(market) -> opinion | mechanical (+ liquidity floor)
"""
