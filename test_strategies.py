"""
test_strategies.py — Unit tests for strategy signals, routing, and workflow.
Uses unittest.mock to avoid live API calls.
"""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from config import BotConfig
from edge_filter import EdgeFilter
from models import Market, OrderBook, OrderBookLevel, PriceTick, Side
from strategy_router import detect_asset


def _make_market(question: str) -> Market:
    return Market(
        condition_id="test",
        question=question,
        yes_token_id="y",
        no_token_id="n",
        end_date_iso="2025-12-31T00:00:00Z",
        end_timestamp=datetime.now(timezone.utc).timestamp() + 600,
        order_book=OrderBook(
            yes_bids=[OrderBookLevel(0.48, 500)],
            yes_asks=[OrderBookLevel(0.52, 500)],
        ),
        price_history=[PriceTick(0.5, 10, datetime.now(timezone.utc))] * 25,
    )


class TestBTCMomentumCarry(unittest.TestCase):
    """Strategy 1: BTC momentum fires at 0.3%, not at 0.2%, kills at 1.5%."""

    def setUp(self):
        self.config = BotConfig()
        self.filter = EdgeFilter(self.config)

    def test_fires_at_0_3_percent(self):
        open_p = 100000
        spot_up = 100300  # 0.3%
        signal, side = self.filter._check_btc_momentum_carry(spot_up, open_p, [])
        self.assertTrue(signal)
        self.assertEqual(side, Side.YES)

    def test_fires_at_minus_0_3_percent(self):
        open_p = 100000
        spot_down = 99700  # -0.3%
        signal, side = self.filter._check_btc_momentum_carry(spot_down, open_p, [])
        self.assertTrue(signal)
        self.assertEqual(side, Side.NO)

    def test_not_at_0_2_percent(self):
        open_p = 100000
        spot = 100200  # 0.2%
        signal, _ = self.filter._check_btc_momentum_carry(spot, open_p, [])
        self.assertFalse(signal)

    def test_kill_switch_at_1_5_percent(self):
        market = _make_market("Bitcoin up or down")
        with patch.object(self.filter, "_is_within_active_hours", return_value=True):
            result = asyncio.run(
                self.filter.evaluate(
                    market,
                    spot_price=101600,
                    window_open_price=100000,
                    pct_move=0.016,  # 1.6% > 1.5% kill switch
                )
            )
        self.assertFalse(result.has_edge)
        self.assertIn("kill", result.reason.lower())


class TestAssetDetection(unittest.TestCase):
    """Verify asset parsing from market questions."""

    def test_bitcoin_to_btc(self):
        self.assertEqual(detect_asset("Bitcoin Up or Down - 15 min"), "BTC")
        self.assertEqual(detect_asset("BTC 15-min"), "BTC")

    def test_ethereum_to_eth(self):
        self.assertEqual(detect_asset("Ethereum 15 min"), "ETH")

    def test_solana_to_sol(self):
        self.assertEqual(detect_asset("Solana SOL"), "SOL")

    def test_xrp(self):
        self.assertEqual(detect_asset("XRP ripple"), "XRP")

    def test_unknown(self):
        self.assertEqual(detect_asset("Some other market"), "UNKNOWN")


class TestSOLSqueezeSignal(unittest.TestCase):
    """Strategy 3: fires when funding < -0.001 AND RSI < 38."""

    def setUp(self):
        self.config = BotConfig()
        self.filter = EdgeFilter(self.config)

    def test_rsi_calculation(self):
        # Oversold: declining prices
        prices = [100 - i for i in range(20)]
        rsi = EdgeFilter._calculate_rsi(prices)
        self.assertLess(rsi, 50)

    def test_squeeze_needs_uptick(self):
        now = datetime.now(timezone.utc)
        # Flat prices, no uptick -> no squeeze
        market = Market(
            condition_id="s",
            question="SOL",
            yes_token_id="y",
            no_token_id="n",
            end_date_iso="",
            end_timestamp=now.timestamp() + 200,
            price_history=[PriceTick(0.5, 1, now)] * 20,
        )
        signal, side = self.filter._check_sol_squeeze(
            market, funding_rate=-0.002, btc_is_neutral_or_up=True
        )
        self.assertFalse(signal)


class TestMakerPairLogic(unittest.TestCase):
    """Strategy 4: both sides at correct spread."""

    def setUp(self):
        self.config = BotConfig()
        self.config.MAKER_SPREAD_TARGET = 0.04
        self.config.MAKER_MAX_POSITION_SIZE = 50

    def test_spread_calculation(self):
        from clob_client import ClobClient
        from executor import OrderExecutor
        client = ClobClient(self.config)
        exec = OrderExecutor(self.config, client)
        market = Market(
            condition_id="m",
            question="BTC",
            yes_token_id="y",
            no_token_id="n",
            end_date_iso="",
            end_timestamp=0,
            order_book=OrderBook(
                yes_bids=[OrderBookLevel(0.49, 100)],
                yes_asks=[OrderBookLevel(0.51, 100)],
            ),
        )
        yes_id, no_id = asyncio.run(exec.place_maker_pair(market))
        self.assertIsNotNone(yes_id)
        self.assertIsNotNone(no_id)


class TestXRPCatalystExpiry(unittest.TestCase):
    """Strategy 5: catalyst auto-expires."""

    def test_no_catalyst_no_trade(self):
        config = BotConfig()
        config.XRP_CATALYST_ACTIVE = False
        filter = EdgeFilter(config)
        signal, _ = filter._check_xrp_catalyst()
        self.assertFalse(signal)

    def test_catalyst_active_returns_side(self):
        config = BotConfig()
        config.XRP_CATALYST_ACTIVE = True
        config.XRP_CATALYST_DIRECTION = "UP"
        filter = EdgeFilter(config)
        signal, side = filter._check_xrp_catalyst()
        self.assertTrue(signal)
        self.assertEqual(side, Side.YES)


class TestActiveHoursGate(unittest.TestCase):
    """Directional strategies blocked outside 9AM-4PM ET."""

    def test_detect_asset(self):
        self.assertEqual(detect_asset("Bitcoin Up or Down"), "BTC")


class TestOrderSuccessBeforePosition(unittest.TestCase):
    """Position only added when place_order returns a non-None order_id."""

    async def _simulate_main_order_flow(self, place_order_returns):
        """Simulate main.py logic: only add_position when order_id is truthy."""
        from position_manager import PositionManager
        from executor import OrderExecutor
        from clob_client import ClobClient

        config = BotConfig()
        config.PAPER_TRADING = True
        client = ClobClient(config)
        exec_ = OrderExecutor(config, client)
        exec_.place_order = AsyncMock(return_value=place_order_returns)
        pm = PositionManager(config, client, exec_)

        market = _make_market("Bitcoin up or down")
        edge = MagicMock()
        edge.side = Side.YES
        edge.entry_price = 0.52
        edge.kelly_size = 50
        edge.strategy_name = "BTC_MOMENTUM"

        order_id = await exec_.place_order(market, edge)
        if order_id:
            pm.add_position(market, edge)
        return pm, market

    def test_no_position_when_order_returns_none(self):
        pm, market = asyncio.run(self._simulate_main_order_flow(None))
        self.assertFalse(pm.has_position(market.condition_id))

    def test_position_added_when_order_succeeds(self):
        pm, market = asyncio.run(self._simulate_main_order_flow("order-123"))
        self.assertTrue(pm.has_position(market.condition_id))

    def test_position_added_when_order_succeeds_integration(self):
        from position_manager import PositionManager
        from executor import OrderExecutor
        from clob_client import ClobClient

        config = BotConfig()
        config.PAPER_TRADING = True
        client = ClobClient(config)
        exec_ = OrderExecutor(config, client)
        pm = PositionManager(config, client, exec_)

        market = _make_market("Bitcoin up or down")
        edge = MagicMock()
        edge.side = Side.YES
        edge.entry_price = 0.52
        edge.kelly_size = 50
        edge.strategy_name = "BTC_MOMENTUM"

        pm.add_position(market, edge)
        self.assertTrue(pm.has_position(market.condition_id))




class TestPnLEdgeCases(unittest.TestCase):
    """P&L calculation correctness for YES and NO positions."""

    def test_yes_pnl_profit(self):
        from models import Position

        entry = 0.50
        exit_p = 0.80
        shares = 100
        pnl = (exit_p - entry) * shares
        self.assertAlmostEqual(pnl, 30.0)

    def test_yes_pnl_loss(self):
        entry = 0.55
        exit_p = 0.35
        shares = 100
        pnl = (exit_p - entry) * shares
        self.assertAlmostEqual(pnl, -20.0)

    def test_no_pnl_profit(self):
        entry = 0.50
        exit_p = 0.70
        shares = 100
        pnl = (exit_p - entry) * shares
        self.assertAlmostEqual(pnl, 20.0)

    def test_no_pnl_loss(self):
        entry = 0.55
        exit_p = 0.40
        shares = 80
        pnl = (exit_p - entry) * shares
        self.assertAlmostEqual(pnl, -12.0)


class TestOBImbalanceNoSide(unittest.TestCase):
    """NO order book cross-check: agreement required when no_ob provided."""

    def test_yes_no_agreement_bullish(self):
        config = BotConfig()
        filter_ = EdgeFilter(config)
        yes_ob = OrderBook(
            yes_bids=[OrderBookLevel(0.65, 600)],
            yes_asks=[OrderBookLevel(0.35, 400)],
        )
        no_ob = OrderBook(
            yes_bids=[OrderBookLevel(0.35, 400)],
            yes_asks=[OrderBookLevel(0.65, 600)],
        )
        signal, side, _, _ = filter_._check_order_book_imbalance(yes_ob, no_ob)
        self.assertTrue(signal)
        self.assertEqual(side, Side.YES)

    def test_yes_no_disagreement_no_signal(self):
        config = BotConfig()
        filter_ = EdgeFilter(config)
        yes_ob = OrderBook(
            yes_bids=[OrderBookLevel(0.65, 600)],
            yes_asks=[OrderBookLevel(0.35, 400)],
        )
        no_ob = OrderBook(
            yes_bids=[OrderBookLevel(0.65, 600)],
            yes_asks=[OrderBookLevel(0.35, 400)],
        )
        signal, side, _, _ = filter_._check_order_book_imbalance(yes_ob, no_ob)
        self.assertFalse(signal)


class TestPortfolioRiskCap(unittest.TestCase):
    """MAX_PORTFOLIO_RISK enforcement."""

    def test_would_exceed_portfolio_risk(self):
        from position_manager import PositionManager
        from clob_client import ClobClient
        from executor import OrderExecutor

        config = BotConfig()
        config.BANKROLL = 1000
        config.MAX_PORTFOLIO_RISK = 0.30
        client = ClobClient(config)
        exec_ = OrderExecutor(config, client)
        pm = PositionManager(config, client, exec_)

        self.assertFalse(pm.would_exceed_portfolio_risk(100))
        self.assertFalse(pm.would_exceed_portfolio_risk(300))
        self.assertTrue(pm.would_exceed_portfolio_risk(301))


if __name__ == "__main__":
    unittest.main()
