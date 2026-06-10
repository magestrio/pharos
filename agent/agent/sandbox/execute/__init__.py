"""Execution layer — decision diff -> actions -> dispatch (ah.25 split).

`execute.py` (~7k LOC) was split into focused submodules along the import DAG:

    types -> common -> positions/budget -> swaps -> builders/sweep
          -> planner -> dispatch -> cli

This package is the stable public facade: `from agent.sandbox.execute import X`
keeps working for every symbol loop.py and the tests historically imported,
regardless of which submodule now owns it. Internal code imports from the
specific submodule (e.g. `from .budget import _usdt_supply`), never from this
package root.
"""

from __future__ import annotations

from agent.sandbox.execute.budget import (  # noqa: F401
    _buy_usd_for_coin,
    _buy_usdt_demand,
    _carry_open_usdc_reserve,
    _enforce_usdc_budget,
    _enforce_usdt_budget,
    _funding_carry_targets,
    _hedged_pick_underfunded_coins,
    _preflight_spend_reserve,
    _unfunded_nonstable_subscribe_coins,
    _usdt_supply,
)
from agent.sandbox.execute.builders import (  # noqa: F401
    _advance_earn_positions_held,
    _advance_earn_subscribe_action,
    _advance_product_coin,
    _advance_product_pair,
    _alpha_action_for_target,
    _auto_hedge_targets,
    _build_advance_extra,
    _decode_offer_from_reason,
    _funding_carry_diff,
    _hedge_diff_actions,
    _invalidate_for_coin,
    _lm_action_for_target,
    _lm_hedge_targets,
    _offer_expired,
    _pick_advance_offer,
    _pick_offer_for_execute,
    apply_carry_results_to_state,
)
from agent.sandbox.execute.cli import (  # noqa: F401
    _load_paired_snapshot,
    _main,
    _render_plan_summary,
    request_approval,
)
from agent.sandbox.execute.common import (  # noqa: F401
    _ACCOUNT_TYPE,
    _ADVANCE_EARN_AMOUNT_FIELDS,
    _ADVANCE_EARN_CATEGORIES,
    _ALPHA_CATEGORY,
    _ALPHA_DEFAULT_SLIPPAGE,
    _ALPHA_PAY_TOKEN_CODE,
    _AUTO_HEDGE_CATEGORIES,
    _BASIC_EARN_CATEGORIES,
    _CARRY_OPEN_USDT_FACTOR,
    _CARRY_PAIRED_NOTIONAL_TOLERANCE,
    _CARRY_SPOT_FILL_POLL_INTERVAL,
    _CARRY_SPOT_FILL_POLL_SECONDS,
    _FUNDING_SWAP_FEE_FACTOR,
    _LM_CATEGORY,
    _LM_QUOTE_ACCOUNT_TYPE,
    _OFFER_PREFIX,
    _ORDER_HISTORY_CATEGORY,
    _PERP_SL_RETRY_BACKOFF,
    _STABLE_CONSOLIDATE_PAIRS,
    _STABLE_LOT,
    _STABLE_SWAP_HEADROOM,
    _STABLES,
    _UNIFIED_SPEND_RESERVE_FACTOR,
    _UNIFIED_SPEND_RESERVE_FLOOR,
    _USDC_PAIR_COINS,
    ALPHA_EXEC_ENABLED,
    DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
    EXECUTIONS_DIR,
    HEDGE_NOTIONAL_REBALANCE_THRESHOLD,
    MAX_CARRY_CLOSE_ATTEMPTS,
    MIN_ACTION_USDC,
    MIN_SWAP_USDC,
    REDEEM_SETTLE_TIMEOUT_SECONDS,
    _amount_to_usd,
    _coin_equity_from_wallet,
    _coin_from_perp_symbol,
    _coin_mark,
    _coin_to_long_exposure,
    _coin_to_perp_short_size,
    _coin_wallet_native,
    _current_lm_position,
    _earn_product_lookup,
    _is_fully_processing,
    _liquid_for_coin,
    _lm_base_leg_native,
    _lm_principal_usd,
    _lm_product_from_snapshot,
    _notional_drifts,
    _order_link_id,
    _orphan_sell_quote,
    _position_notional_usd,
    _redeem_settles_in_cycle,
    _round_to_qty_step,
    _safe_decimal,
    _swap_base_coin,
    _transfer_quantum,
)
from agent.sandbox.execute.dispatch import (  # noqa: F401
    _confirmable_order_links,
    _dry_run_payload,
    _ensure_fund_balance,
    _ensure_unified_balance,
    _execute_one,
    _fund_carry_open_usdt,
    _live_earn_native_qty,
    _redeem_onchain_by_position,
    _transfer_satisfies_swap,
    _unwind_carry_spot,
    execute_actions,
    reconcile_executions,
    verify_executions_against_bybit,
    verify_order_links,
)
from agent.sandbox.execute.planner import (  # noqa: F401
    _defer_subscribes_awaiting_slow_redeem,
    _reindex_order_link_ids,
    diff_to_actions,
)
from agent.sandbox.execute.positions import (  # noqa: F401
    _alpha_current_positions,
    _current_positions_by_pid,
    _target_usd_by_pid,
)
from agent.sandbox.execute.swaps import (  # noqa: F401
    _swap_actions_for_earn_picks,
    _swap_actions_for_hedges,
    _swap_actions_for_usdt_excess,
)
from agent.sandbox.execute.sweep import (  # noqa: F401
    _carry_liq_close_actions,
    _close_naked_perp_actions,
    _lm_residual_redeem_actions,
    _orphan_perp_close_actions,
    _orphan_spot_sell_actions,
    _reconcile_hedge_to_earn_actions,
    _stable_consolidate_actions,
    build_redeem_exit_intents,
    exit_actions_from_intent,
)
from agent.sandbox.execute.types import (  # noqa: F401
    Action,
    ActionKind,
    ActionResult,
    _CurrentPos,
    _TargetPos,
)
