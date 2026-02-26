"""
edge_filter.py — The core profit gate.

This is the most important module in the bot. A trade is ONLY approved
when at least MIN_EDGE_SIGNALS of the 4 signals fire AND Kelly edge > minimum.

Signals:
  1. Order Book Imbalance — heavy bid/ask pressure on one side
  2. Momentum / Price Velocity — sustained directional price movement
  3. Volume Spike — abnormal volume vs. rolling baseline
  4. Kelly Criterion — positive expected value with meaningful edge size

Strategy-specific signals (5): BTC Momentum, ETH Lag, SOL Squeeze, XRP Catalyst

Philosophy: if the edge isn't clear, the answer is no trade.
"""

import logging
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import BotConfig
from models import Market, EdgeResult, Side, OrderBook

logger = logging.getLogger("edge_filter")


class EdgeFilter:
    def __init__(self, config: BotConfig):
        self.config = config

    def _detect_asset(self, question: str) -> str:
        """Parse market question for asset: BTC, ETH, SOL, XRP, or UNKNOWN."""
        q = question.lower()
        if "bitcoin" in q or "btc" in q:
            return "BTC"
        if "ethereum" in q or "eth" in q:
            return "ETH"
        if "solana" in q or "sol" in q:
            return "SOL"
        if "xrp" in q or "ripple" in q:
            return "XRP"
        return "UNKNOWN"

    def _is_within_active_hours(self) -> bool:
        """True if current UTC time is within ACTIVE_HOURS (9 AM - 4 PM ET)."""
        if not self.config.ACTIVE_HOURS_ENABLED:
            return True
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
        except ImportError:
            et = timezone.utc  # fallback if zoneinfo unavailable
        now_et = datetime.now(et)
        hour = now_et.hour
        start, end = self.config.ACTIVE_HOURS_START, self.config.ACTIVE_HOURS_END
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    @staticmethod
    def _calculate_rsi(prices: List[float], period: int = 14) -> float:
        """Wilder's RSI. Returns 0-100."""
        if len(prices) < period + 1:
            return 50.0
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    async def evaluate(
        self,
        market: Market,
        btc_signal_state: Optional[Dict] = None,
        spot_price: Optional[float] = None,
        window_open_price: Optional[float] = None,
        pct_move: Optional[float] = None,
        funding_rate: Optional[float] = None,
        btc_is_neutral_or_up: bool = True,
        btc_price_history: Optional[list] = None,
    ) -> EdgeResult:
        """
        Run all signal checks and return an EdgeResult.
        Only sets has_edge=True if minimum signals fire + Kelly confirms.
        Supports strategy-specific context via optional params.
        """
        asset = self._detect_asset(market.question)

        # GATE: Order book required; price_history optional (CLOB often returns empty for 15-min markets)
        if not market.order_book:
            logger.info(f"[{asset}] GATE BLOCK: No order book — SKIP")
            return EdgeResult(has_edge=False, side=None, signal_count=0,
                              reason="Missing order book")
        if not market.price_history:
            market.price_history = []  # Allow eval with OB+Kelly only; momentum/vol will fail
            logger.info(f"[{asset}] ORDERBOOK: price_history empty — using OB+Kelly only (no momentum/vol)")

        mid = market.order_book.mid_price
        if mid is None:
            logger.info(f"[{asset}] GATE BLOCK: No mid price — SKIP")
            return EdgeResult(has_edge=False, side=None, signal_count=0,
                              reason="No mid price available")

        # Log orderbook summary for every market
        bid_depth = sum(l.price * l.size for l in market.order_book.yes_bids[:5])
        ask_depth = sum(l.price * l.size for l in market.order_book.yes_asks[:5])
        total_ob = bid_depth + ask_depth
        n_bids = len(market.order_book.yes_bids)
        n_asks = len(market.order_book.yes_asks)
        if n_bids < 3 or n_asks < 3:
            logger.info(f"[{asset}] ORDERBOOK: Thin book | bids={n_bids} asks={n_asks} depth=${total_ob:.0f} — may block signals")
        else:
            logger.info(f"[{asset}] ORDERBOOK: bids={n_bids} asks={n_asks} depth=${total_ob:.0f} mid={mid:.3f}")
        market.asset = asset

        # Active hours gate: skip directional strategies 1, 2, 3 outside 9AM-4PM ET
        if self.config.ACTIVE_HOURS_ENABLED and not self._is_within_active_hours():
            if asset in ("BTC", "ETH", "SOL"):
                logger.info(f"[{asset}] GATE BLOCK: Outside active hours (9AM-4PM ET) — SKIP")
                return EdgeResult(
                    has_edge=False, side=None, signal_count=0,
                    reason="Outside active hours (9AM-4PM ET) — directional strategies disabled",
                )
            # XRP catalyst and maker can run 24/7 per doc

        # ── Strategy-specific signals ────────────────────────────────────────
        btc_mom_signal, btc_mom_side = False, None
        eth_lag_signal, eth_lag_side = False, None
        sol_squeeze_signal, sol_squeeze_side = False, None
        xrp_catalyst_signal, xrp_catalyst_side = False, None

        if asset == "BTC" and spot_price is not None and window_open_price is not None and pct_move is not None:
            # Kill switch: pct_move > MAX_ENTRY
            if abs(pct_move) > self.config.BTC_MOMENTUM_MAX_ENTRY:
                logger.info(
                    f"BTC move already {pct_move:.2%} — too late, edge priced in"
                )
                return EdgeResult(has_edge=False, side=None, signal_count=0,
                                  reason="BTC_MOMENTUM_MAX_ENTRY kill switch")
            btc_price_ticks = btc_price_history or []
            btc_mom_signal, btc_mom_side = self._check_btc_momentum_carry(
                spot_price, window_open_price, btc_price_ticks
            )

        if asset == "ETH" and btc_signal_state:
            eth_lag_signal, eth_lag_side = self._check_eth_lag_trade(
                btc_signal_state, mid
            )

        if asset == "SOL" and funding_rate is not None:
            sol_squeeze_signal, sol_squeeze_side = self._check_sol_squeeze(
                market, funding_rate, btc_is_neutral_or_up
            )

        if asset == "XRP":
            xrp_catalyst_signal, xrp_catalyst_side = self._check_xrp_catalyst()
            # XRP: require catalyst only when XRP_REQUIRE_CATALYST=true
            if getattr(self.config, "XRP_REQUIRE_CATALYST", True) and not self.config.XRP_CATALYST_ACTIVE and not xrp_catalyst_signal:
                logger.info(f"[{asset}] GATE BLOCK: XRP no catalyst active — SKIP")
                return EdgeResult(
                    has_edge=False, side=None, signal_count=0,
                    reason="XRP — no catalyst active, no trade",
                )

        # ── Base signals 1–4 ──────────────────────────────────────────────────
        ob_signal, ob_side = self._check_order_book_imbalance(
            market.order_book, market.no_order_book, asset=asset
        )
        mom_signal, mom_side = self._check_momentum(market, asset=asset)
        vol_signal, vol_ratio = self._check_volume_spike(market, asset=asset)

        # Resolve directional side: base + strategy-specific
        consensus_side = self._resolve_directional_side(
            ob_side, mom_side,
            extra_sides=[btc_mom_side, eth_lag_side, sol_squeeze_side, xrp_catalyst_side],
        )
        kelly_boost = getattr(self.config, "BASE_KELLY_BOOST", 0.08)
        if eth_lag_signal:
            kelly_boost = self.config.ETH_LAG_SIGNAL_BOOST
        elif sol_squeeze_signal:
            kelly_boost = self.config.SOL_SQUEEZE_SIGNAL_BOOST
        elif xrp_catalyst_signal:
            kelly_boost = self.config.XRP_CATALYST_SIGNAL_BOOST
        # Binance funding alignment: negative funding + YES = shorts paying, potential squeeze
        elif funding_rate is not None and funding_rate < -0.0005 and consensus_side == Side.YES:
            kelly_boost = getattr(self.config, "BASE_KELLY_BOOST", 0.08) + 0.02
        est_prob, implied_prob, kelly_edge, kelly_size, kelly_signal = \
            self._check_kelly(mid, consensus_side, edge_boost=kelly_boost)

        # ── Per-asset signal count ─────────────────────────────────────────────
        base_signals = [ob_signal, mom_signal, vol_signal, kelly_signal]
        base_count = sum(base_signals)
        strategy_credits = 0
        if eth_lag_signal:
            strategy_credits = 2  # counts as 2
        elif xrp_catalyst_signal:
            strategy_credits = 3  # counts as 3
        elif btc_mom_signal or sol_squeeze_signal:
            strategy_credits = 1
        effective_count = base_count + strategy_credits

        min_signals_map = {
            "BTC": self.config.MIN_EDGE_SIGNALS,
            "ETH": self.config.MIN_EDGE_SIGNALS,
            "SOL": self.config.SOL_MIN_EDGE_SIGNALS,
            "XRP": self.config.XRP_NO_CATALYST_MIN_SIGNALS,
        }
        min_signals = min_signals_map.get(asset, self.config.MIN_EDGE_SIGNALS)
        if eth_lag_signal:
            min_signals = 1  # ETH only needs 1 more when lag fires
        if xrp_catalyst_signal:
            min_signals = 1  # XRP only needs 1 when catalyst fires
        if sol_squeeze_signal:
            min_signals = self.config.SOL_MIN_EDGE_SIGNALS  # 2

        directions_agree = self._directions_agree(
            ob_side, mom_side,
            extra_sides=[btc_mom_side, eth_lag_side, sol_squeeze_side, xrp_catalyst_side],
        )

        min_edge = max(
            self.config.MIN_KELLY_EDGE,
            getattr(self.config, "MIN_EDGE_PCT", 0.03),
        )

        # SIGNAL SUMMARY — logged for every market every scan (INFO = always in bot.log)
        ob_str = "PASS" if ob_signal else "FAIL"
        mom_str = "PASS" if mom_signal else "FAIL"
        vol_str = f"PASS ({vol_ratio:.2f}x)" if vol_signal else f"FAIL ({vol_ratio:.2f}x < {self.config.VOLUME_SPIKE_MULTIPLIER})"
        kelly_str = f"PASS ({kelly_edge:.2%})" if kelly_signal else f"FAIL ({kelly_edge:.2%} < {min_edge:.2%})"
        logger.info(
            f"[{asset}] SIGNALS | OB:{ob_str} MOM:{mom_str} VOL:{vol_str} KELLY:{kelly_str} | "
            f"side={consensus_side} dir_ok={directions_agree} size=${kelly_size:.2f} need={min_signals}"
        )

        has_edge = (
            effective_count >= min_signals
            and kelly_edge >= min_edge
            and directions_agree
            and consensus_side is not None
            and kelly_size >= self.config.MIN_BET_SIZE
        )

        # Final decision log
        if has_edge:
            logger.info(f"[{asset}] EDGE DECISION: TRADE | signals={effective_count}/{min_signals} edge={kelly_edge:.2%}")
        else:
            fail_reasons = []
            if effective_count < min_signals:
                fail_reasons.append(f"signals {effective_count}<{min_signals}")
            if kelly_edge < min_edge:
                fail_reasons.append(f"kelly {kelly_edge:.2%}<{min_edge:.2%}")
            if not directions_agree:
                fail_reasons.append("dir_mismatch")
            if consensus_side is None:
                fail_reasons.append("no_side")
            if kelly_size < self.config.MIN_BET_SIZE:
                fail_reasons.append(f"size ${kelly_size:.2f}<${self.config.MIN_BET_SIZE}")
            logger.info(f"[{asset}] EDGE DECISION: NO TRADE | {', '.join(fail_reasons)}")

        strategy_name = ""
        if btc_mom_signal:
            strategy_name = "BTC_MOMENTUM"
        elif eth_lag_signal:
            strategy_name = "ETH_LAG"
        elif sol_squeeze_signal:
            strategy_name = "SOL_SQUEEZE"
        elif xrp_catalyst_signal:
            strategy_name = "XRP_CATALYST"

        rsi_val = 0.0
        if asset == "SOL" and market.price_history:
            prices = [t.price for t in market.price_history]
            rsi_val = self._calculate_rsi(prices)

        result = EdgeResult(
            has_edge=has_edge,
            side=consensus_side,
            signal_count=effective_count,
            ob_imbalance_signal=ob_signal,
            momentum_signal=mom_signal,
            volume_signal=vol_signal,
            kelly_signal=kelly_signal,
            strategy_name=strategy_name,
            asset=asset,
            eth_lag_signal=eth_lag_signal,
            sol_squeeze_signal=sol_squeeze_signal,
            xrp_catalyst_signal=xrp_catalyst_signal,
            spot_price=spot_price or 0.0,
            pct_move_from_open=pct_move or 0.0,
            funding_rate=funding_rate or 0.0,
            rsi_value=rsi_val,
            estimated_prob=est_prob,
            implied_prob=implied_prob,
            kelly_edge=kelly_edge,
            kelly_size=kelly_size,
            entry_price=mid,
            reason=self._build_reason(
                ob_signal, mom_signal, vol_signal, kelly_signal,
                directions_agree, kelly_edge,
                btc_mom=btc_mom_signal, eth_lag=eth_lag_signal,
                sol_squeeze=sol_squeeze_signal, xrp_catalyst=xrp_catalyst_signal,
            ),
        )
        return result

    def _check_btc_momentum_carry(
        self,
        spot_price: float,
        window_open_price: float,
        price_ticks: Optional[List[float]] = None,
    ) -> Tuple[bool, Optional[Side]]:
        """Strategy 1: BTC momentum carry. 0.3%+ move with 70% tick consistency."""
        if window_open_price <= 0:
            return False, None
        pct_move = (spot_price - window_open_price) / window_open_price
        thresh = self.config.BTC_MOMENTUM_THRESHOLD
        direction = None
        if pct_move >= thresh:
            direction = Side.YES
        elif pct_move <= -thresh:
            direction = Side.NO
        if direction is None:
            return False, None
        # Directional consistency: 70% of last 5 ticks must align
        if price_ticks and len(price_ticks) >= 5:
            ticks = price_ticks[-5:]
            deltas = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
            if direction == Side.YES:
                aligned = sum(1 for d in deltas if d > 0)
            else:
                aligned = sum(1 for d in deltas if d < 0)
            if aligned / len(deltas) < self.config.MOMENTUM_DIRECTION_CONSISTENCY:
                return False, None
        return True, direction

    def _check_eth_lag_trade(
        self, btc_signal_state: Dict, eth_mid_price: float
    ) -> Tuple[bool, Optional[Side]]:
        """Strategy 2: ETH lag — BTC fired, ETH odds not yet repriced."""
        if not btc_signal_state.get("fired"):
            return False, None
        from datetime import datetime, timezone
        ts = btc_signal_state.get("timestamp")
        if not ts:
            return False, None
        elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
        if elapsed > self.config.ETH_LAG_EXPIRY_SECONDS:
            return False, None
        btc_side = btc_signal_state.get("side")
        if not btc_side:
            return False, None
        # ETH odds within MAX_REPRICING of 0.50 in BTC direction
        dist_from_50 = abs(eth_mid_price - 0.50)
        if dist_from_50 > self.config.ETH_LAG_MAX_REPRICING:
            return False, None
        pct_move = btc_signal_state.get("pct_move", 0)
        logger.info(
            f"ETH LAG SIGNAL: BTC moved {pct_move:.2%} {btc_side} — "
            f"ETH odds at {eth_mid_price:.3f}, lag window open"
        )
        return True, btc_side

    def _check_sol_squeeze(
        self,
        market: Market,
        funding_rate: float,
        btc_is_neutral_or_up: bool,
    ) -> Tuple[bool, Optional[Side]]:
        """Strategy 3: SOL short-squeeze detection."""
        if funding_rate > self.config.SOL_FUNDING_RATE_THRESHOLD:
            return False, None
        if not btc_is_neutral_or_up:
            return False, None
        # Only enter in first 3 min of window
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).timestamp()
        window_duration = 15 * 60  # seconds
        window_start = market.end_timestamp - window_duration
        minutes_into_window = (now_ts - window_start) / 60
        if minutes_into_window > self.config.SOL_SQUEEZE_MAX_ENTRY_MINUTES:
            return False, None
        prices = [t.price for t in market.price_history] if market.price_history else []
        if len(prices) < 15:
            return False, None
        rsi = self._calculate_rsi(prices)
        if rsi >= self.config.SOL_RSI_OVERSOLD_THRESHOLD:
            return False, None
        # Last 3 ticks show uptick 0.2%+ from local low
        if len(prices) < 3:
            return False, None
        recent = prices[-3:]
        local_low = min(recent)
        latest = recent[-1]
        if local_low <= 0:
            return False, None
        uptick_pct = (latest - local_low) / local_low
        if uptick_pct < 0.002:
            return False, None
        logger.info(
            f"SOL SQUEEZE: funding={funding_rate:.6f}, RSI={rsi:.1f}, uptick confirmed"
        )
        return True, Side.YES

    def _check_xrp_catalyst(
        self, _market_side: Optional[Side] = None
    ) -> Tuple[bool, Optional[Side]]:
        """Strategy 5: XRP catalyst — only trade when catalyst flag active."""
        if not self.config.XRP_CATALYST_ACTIVE:
            return False, None
        set_time = self.config.XRP_CATALYST_SET_TIME
        if set_time:
            from datetime import datetime, timezone
            try:
                set_dt = datetime.fromisoformat(set_time.replace("Z", "+00:00"))
                expiry_mins = self.config.XRP_CATALYST_EXPIRY_MINUTES
                if (datetime.now(timezone.utc) - set_dt).total_seconds() > expiry_mins * 60:
                    self.config.XRP_CATALYST_ACTIVE = False
                    logger.warning("XRP catalyst expired — flag cleared")
                    return False, None
            except Exception:
                pass
        direction = self.config.XRP_CATALYST_DIRECTION.upper()
        side = Side.YES if direction == "UP" else Side.NO
        return True, side

    # ── Signal 1: Order Book Imbalance ────────────────────────────────────────

    def _check_order_book_imbalance(
        self, ob: OrderBook, no_ob: Optional[OrderBook] = None, asset: str = ""
    ) -> Tuple[bool, Optional[Side]]:
        """
        Compare bid depth vs ask depth across top N levels.
        If bids dominate → YES (price likely to rise).
        If asks dominate → NO (price likely to fall, buy NO = bet against YES).
        When no_ob provided: NO bids heavy = bearish, NO asks heavy = bullish; require agreement.
        """
        bid_depth = sum(
            l.price * l.size
            for l in ob.yes_bids[:self.config.OB_DEPTH_LEVELS]
        )
        ask_depth = sum(
            l.price * l.size
            for l in ob.yes_asks[:self.config.OB_DEPTH_LEVELS]
        )
        total = bid_depth + ask_depth
        if total == 0:
            logger.info(f"[{asset}] OB: bid_ratio=N/A ask_ratio=N/A (total=0) — FAIL")
            return False, None

        bid_ratio = bid_depth / total
        ask_ratio = ask_depth / total
        threshold = self.config.OB_IMBALANCE_THRESHOLD

        yes_side = None
        if bid_ratio >= threshold:
            yes_side = Side.YES
        elif ask_ratio >= threshold:
            yes_side = Side.NO

        if no_ob and yes_side is not None:
            no_bid = sum(l.price * l.size for l in no_ob.yes_bids[:self.config.OB_DEPTH_LEVELS])
            no_ask = sum(l.price * l.size for l in no_ob.yes_asks[:self.config.OB_DEPTH_LEVELS])
            no_total = no_bid + no_ask
            if no_total > 0:
                no_bid_ratio = no_bid / no_total
                no_ask_ratio = no_ask / no_total
                no_side = None
                if no_bid_ratio >= threshold:
                    no_side = Side.NO
                elif no_ask_ratio >= threshold:
                    no_side = Side.YES
                if no_side is not None and no_side != yes_side:
                    logger.info(f"[{asset}] OB: YES/NO sides disagree — FAIL")
                    return False, None

        if yes_side == Side.YES:
            logger.info(f"[{asset}] OB: bid_ratio={bid_ratio:.2%} (thresh {threshold}) — PASS -> YES")
            return True, Side.YES
        elif yes_side == Side.NO:
            logger.info(f"[{asset}] OB: ask_ratio={ask_ratio:.2%} (thresh {threshold}) — PASS -> NO")
            return True, Side.NO

        # Fallback: when book is balanced but mid is extreme (like trades.csv YES@0.185)
        mid = ob.mid_price
        if mid is not None:
            if mid < 0.42:
                logger.info(f"[{asset}] OB: mid={mid:.3f} (<0.42) — PASS mid-extreme -> YES")
                return True, Side.YES
            if mid > 0.58:
                logger.info(f"[{asset}] OB: mid={mid:.3f} (>0.58) — PASS mid-extreme -> NO")
                return True, Side.NO

        logger.info(f"[{asset}] OB: bid_ratio={bid_ratio:.2%} ask_ratio={ask_ratio:.2%} (thresh {threshold}) — FAIL")
        return False, None

    # ── Signal 2: Momentum / Price Velocity ───────────────────────────────────

    def _check_momentum(self, market: Market, asset: str = "") -> Tuple[bool, Optional[Side]]:
        """
        Look at the last N price ticks.
        Signal fires if price has moved MIN_MOVE% in a consistent direction.
        """
        history = market.price_history
        min_move = self.config.MOMENTUM_MIN_MOVE
        min_consistency = self.config.MOMENTUM_DIRECTION_CONSISTENCY

        if len(history) < self.config.MOMENTUM_WINDOW + 1:
            logger.info(f"[{asset}] MOM: need {self.config.MOMENTUM_WINDOW+1} ticks, have {len(history)} — FAIL")
            return False, None

        window = history[-self.config.MOMENTUM_WINDOW:]
        prices = [t.price for t in window]

        start_price = prices[0]
        end_price = prices[-1]
        total_move = (end_price - start_price) / start_price if start_price > 0 else 0

        # Check directional consistency (% of ticks moving in same direction)
        tick_deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        if not tick_deltas:
            logger.info(f"[{asset}] MOM: move={total_move:.2%} (no deltas) — FAIL")
            return False, None

        up_ticks = sum(1 for d in tick_deltas if d > 0)
        down_ticks = sum(1 for d in tick_deltas if d < 0)
        consistency = max(up_ticks, down_ticks) / len(tick_deltas)

        if abs(total_move) >= min_move and consistency >= min_consistency:
            side = Side.YES if total_move > 0 else Side.NO
            logger.info(f"[{asset}] MOM: move={total_move:.2%} (min {min_move:.2%}) cons={consistency:.2%} — PASS -> {side}")
            return True, side

        logger.info(f"[{asset}] MOM: move={total_move:.2%} (min {min_move:.2%}) cons={consistency:.2%} (min {min_consistency:.2%}) — FAIL")
        return False, None

    # ── Signal 3: Volume Spike ────────────────────────────────────────────────

    def _check_volume_spike(self, market: Market, asset: str = "") -> Tuple[bool, float]:
        """
        Compare recent volume to rolling baseline.
        A spike = VOLUME_SPIKE_MULTIPLIER × rolling average.
        Returns (signal_fired, ratio).
        """
        history = market.price_history
        window = self.config.VOLUME_ROLLING_WINDOW
        ratio = 0.0

        if len(history) < window + 1:
            logger.info(f"[{asset}] VOL: need {window+1} ticks, have {len(history)} — FAIL (ratio=N/A)")
            return False, 0.0

        baseline_vols = [t.volume for t in history[-(window+1):-1]]
        recent_vol = history[-1].volume

        if not baseline_vols or all(v == 0 for v in baseline_vols):
            logger.info(f"[{asset}] VOL: baseline all zero (recent={recent_vol:.0f}) — FAIL")
            return False, 0.0

        avg_vol = statistics.mean(baseline_vols)
        if avg_vol == 0:
            return False, 0.0

        ratio = recent_vol / avg_vol
        fires = ratio >= self.config.VOLUME_SPIKE_MULTIPLIER

        if fires:
            logger.info(f"[{asset}] VOL: ratio={ratio:.2f}x (min {self.config.VOLUME_SPIKE_MULTIPLIER}) — PASS")
        else:
            logger.info(f"[{asset}] VOL: ratio={ratio:.2f}x (min {self.config.VOLUME_SPIKE_MULTIPLIER}) — FAIL")

        return fires, ratio

    # ── Signal 4: Kelly Criterion ─────────────────────────────────────────────

    def _check_kelly(
        self,
        mid_price: float,
        side: Optional[Side],
        edge_boost: float = 0.08,
    ) -> Tuple[float, float, float, float, bool]:
        """
        Estimate true probability vs. implied market probability.
        Use Kelly formula to compute bet size.

        Returns: (estimated_prob, implied_prob, kelly_edge, kelly_size, signal_fired)

        Kelly formula:  f* = (p*(b+1) - 1) / b
        Where:
          p = estimated win probability
          b = net odds (payout / cost - 1)
          f* = fraction of bankroll to bet

        On a binary market at price X:
          Cost to buy 1 share of YES = X USDC
          Payout if YES resolves = 1 USDC
          Net odds b = (1 - X) / X
        """
        if side is None or mid_price <= 0 or mid_price >= 1:
            return 0.0, 0.0, 0.0, 0.0, False

        # Implied probability from market price
        if side == Side.YES:
            implied_prob = mid_price
            price = mid_price  # cost to buy 1 YES share
        else:
            # For NO: market price of NO = 1 - YES mid
            implied_prob = 1.0 - mid_price
            price = 1.0 - mid_price

        # Our edge estimate: we assume our signals give us a probability boost.
        # The boost is configurable per strategy (lag/squeeze/catalyst get higher boost).
        estimated_prob = min(implied_prob + edge_boost, 0.95)

        # Kelly: f* = (p * (b+1) - 1) / b
        # b = net odds = (1 - price) / price
        b = (1.0 - price) / price
        kelly_fraction = (estimated_prob * (b + 1) - 1) / b

        if kelly_fraction <= 0:
            return estimated_prob, implied_prob, 0.0, 0.0, False

        kelly_edge = estimated_prob - implied_prob

        # Position sizing: kelly (full), fractional_kelly, or bankroll_pct
        mode = getattr(self.config, "POSITION_SIZING_MODE", "fractional_kelly")
        kelly_frac = getattr(self.config, "KELLY_FRACTION", 0.5)
        if mode == "kelly":
            frac = kelly_fraction
        elif mode == "fractional_kelly":
            frac = kelly_fraction * kelly_frac  # e.g. 0.5 = half-Kelly
        else:
            # bankroll_pct: dynamic % based on edge, capped
            base_pct = min(0.08, max(0.02, kelly_edge + 0.02))
            frac = base_pct
        raw_size = frac * self.config.BANKROLL

        # Clamp to configured limits
        kelly_size = max(
            self.config.MIN_BET_SIZE,
            min(raw_size, self.config.MAX_BET_SIZE)
        )

        signal_fired = kelly_edge >= self.config.MIN_KELLY_EDGE
        # Note: no asset param in _check_kelly — caller logs summary
        return estimated_prob, implied_prob, kelly_edge, kelly_size, signal_fired

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_directional_side(
        self,
        ob_side: Optional[Side],
        mom_side: Optional[Side],
        extra_sides: Optional[List[Optional[Side]]] = None,
    ) -> Optional[Side]:
        """Return the consensus directional side from all signals."""
        sides = [s for s in [ob_side, mom_side] + (extra_sides or []) if s is not None]
        if not sides:
            return None
        first = sides[0]
        if any(s != first for s in sides):
            return None  # Contradiction
        return first

    def _directions_agree(
        self,
        ob_side: Optional[Side],
        mom_side: Optional[Side],
        extra_sides: Optional[List[Optional[Side]]] = None,
    ) -> bool:
        """True if no directional contradiction between all signals."""
        sides = [s for s in [ob_side, mom_side] + (extra_sides or []) if s is not None]
        if not sides:
            return True
        first = sides[0]
        return all(s == first for s in sides)

    def _build_reason(
        self,
        ob: bool, mom: bool, vol: bool, kelly: bool,
        directions_agree: bool, kelly_edge: float,
        btc_mom: bool = False, eth_lag: bool = False,
        sol_squeeze: bool = False, xrp_catalyst: bool = False,
    ) -> str:
        fired = []
        if ob: fired.append("OB_IMBALANCE")
        if mom: fired.append("MOMENTUM")
        if vol: fired.append("VOLUME_SPIKE")
        if kelly: fired.append("KELLY")
        if btc_mom: fired.append("BTC_MOMENTUM")
        if eth_lag: fired.append("ETH_LAG")
        if sol_squeeze: fired.append("SOL_SQUEEZE")
        if xrp_catalyst: fired.append("XRP_CATALYST")
        if not directions_agree:
            fired.append("⚠ DIRECTION_CONFLICT")
        return f"Signals: {', '.join(fired)} | Kelly edge: {kelly_edge:.2%}"
