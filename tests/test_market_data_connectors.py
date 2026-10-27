import unittest
from unittest.mock import Mock

import market_data_connectors as connectors


class MarketDataConnectorTests(unittest.TestCase):
    def test_missing_credentials_are_reported_not_silently_as_zero_activity(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"pairs": []}
        session.request.return_value = response
        report = connectors.collect(
            mints=["So11111111111111111111111111111111111111112"],
            session=session,
            environ={},
        )
        statuses = {item["provider"]: item["status"] for item in report["provider_status"]}
        self.assertEqual(statuses["dexscreener"], "ok")
        self.assertEqual(statuses["birdeye"], "not_configured")
        self.assertEqual(statuses["dune"], "not_configured")
        self.assertEqual(statuses["helius"], "not_configured")
        self.assertEqual(statuses["nansen"], "not_configured")
        self.assertTrue(report["research_only"])
        self.assertFalse(report["live_trading_authorized"])
        self.assertFalse(report["trade_signal"])

    def test_dexscreener_normalizes_only_solana_pairs(self):
        session = Mock()
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {
            "pairs": [
                {"chainId": "solana", "pairAddress": "pair-1", "url": "https://dexscreener.com/solana/pair-1", "dexId": "raydium",
                 "baseToken": {"address": "mint-a", "name": "Example", "symbol": "EX"},
                 "quoteToken": {"address": "mint-b", "name": "Wrapped SOL", "symbol": "SOL"},
                 "priceUsd": "0.001", "liquidity": {"usd": 10000}, "marketCap": 500000,
                 "volume": {"h1": 2500, "h6": 15000, "h24": 40000},
                 "priceChange": {"m5": 1.2, "h1": 2.0, "h6": 4.0, "h24": 10.0},
                 "txns": {"m5": {"buys": 10, "sells": 4}, "h6": {"buys": 80, "sells": 30}},
                 "boosts": {"active": 15}, "pairCreatedAt": 1793024451000},
                {"chainId": "ethereum", "pairAddress": "pair-2"},
            ]
        }
        records = connectors.fetch_dexscreener(session, "mint-a")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pair_address"], "pair-1")
        self.assertEqual(records[0]["liquidity_usd"], 10000)
        self.assertEqual(records[0]["market_cap_usd"], 500000)
        self.assertEqual(records[0]["volume_h24_usd"], 40000)
        self.assertEqual(records[0]["price_change_h24_pct"], 10.0)
        self.assertEqual(records[0]["txns_h6"], {"buys": 80, "sells": 30})
        self.assertEqual(records[0]["boosts_active"], 15)
        self.assertEqual(records[0]["pair_url"], "https://dexscreener.com/solana/pair-1")
        self.assertEqual(records[0]["token_symbol"], "EX")
        self.assertEqual(records[0]["source"], "dexscreener")

    def test_public_only_mode_never_queries_configured_optional_providers(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"pairs": [{"chainId": "solana", "pairAddress": "pair"}]}
        session.request.return_value = response
        env = {
            "BIRDEYE_API_KEY": "test-key",
            "DUNE_API_KEY": "test-key",
            "DUNE_SOLANA_TRADES_QUERY_ID": "123",
            "NANSEN_API_KEY": "test-key",
        }
        report = connectors.collect(
            mints=["M" * 44], session=session, environ=env,
            include_public=True, include_optional=False,
        )
        self.assertEqual(session.request.call_count, 2)
        statuses = {item["provider"]: item["status"] for item in report["provider_status"]}
        self.assertEqual(statuses["dexscreener"], "ok")
        self.assertEqual(statuses["geckoterminal"], "ok")
        self.assertEqual(statuses["birdeye"], "disabled_by_policy")
        self.assertEqual(statuses["dune"], "disabled_by_policy")
        self.assertEqual(statuses["nansen"], "disabled_by_policy")

    def test_optional_only_mode_does_not_duplicate_public_dexscreener_request(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"items": []}}
        session.request.return_value = response
        report = connectors.collect(
            mints=["M" * 44], session=session,
            environ={"BIRDEYE_API_KEY": "key"},
            include_public=False, include_optional=True,
        )
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertIn("/defi/v3/token/txs", session.request.call_args.args[1])
        statuses = {item["provider"]: item["status"] for item in report["provider_status"]}
        self.assertEqual(statuses["dexscreener"], "not_run_by_policy")
        self.assertEqual(statuses["birdeye"], "ok")

    def test_geckoterminal_normalizes_public_pool_metrics_and_skips_mint_as_quote(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        mint = "M" * 44
        response.json.return_value = {"data": [
            {"attributes": {"address": "pool-a", "name": "M / SOL", "base_token_price_usd": "0.01", "reserve_in_usd": "50000", "fdv_usd": "1000000", "volume_usd": {"h1": "2500", "h6": "10000", "h24": "40000"}, "price_change_percentage": {"m5": "1", "h1": "2", "h6": "3", "h24": "4"}, "transactions": {"m5": {"buys": 10, "sells": 4}, "h1": {"buys": 80, "sells": 30}, "h6": {"buys": 300, "sells": 200}, "h24": {"buys": 800, "sells": 600}}}, "relationships": {"base_token": {"data": {"id": "solana_" + mint}}, "quote_token": {"data": {"id": "solana_SOL"}}}},
            {"attributes": {"address": "pool-b", "name": "SOL / M", "base_token_price_usd": "99", "reserve_in_usd": "90000", "volume_usd": {}, "price_change_percentage": {}, "transactions": {}}, "relationships": {"base_token": {"data": {"id": "solana_SOL"}}, "quote_token": {"data": {"id": "solana_" + mint}}}}
        ]}
        session.request.return_value = response
        records = connectors.fetch_geckoterminal(session, mint)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pair_address"], "pool-a")
        self.assertEqual(records[0]["price_usd"], "0.01")
        self.assertEqual(records[0]["liquidity_usd"], "50000")
        self.assertEqual(records[0]["volume_h24_usd"], "40000")
        self.assertEqual(records[0]["source"], "geckoterminal")

    def test_provider_error_does_not_hide_other_provider_results(self):
        def bad():
            raise TimeoutError("provider timed out")
        result = connectors._provider_result("birdeye", True, bad)
        self.assertEqual(result["status"], "error")
        self.assertIn("TimeoutError", result["error"])
        self.assertEqual(result["records"], [])

    def test_mint_discovery_is_bounded_and_deduplicated(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            swaps = Path(temp) / "swaps.json"
            balances = Path(temp) / "balances.json"
            swaps.write_text(json.dumps({"tokens": ["A" * 32, "B" * 32, "A" * 32]}))
            balances.write_text(json.dumps({"mint": "C" * 32}))
            found = connectors._extract_mints(str(swaps), str(balances), limit=2)
        self.assertEqual(found, ["A" * 32, "B" * 32])

    def test_mint_discovery_reads_wallet_swap_mints_not_wallet_addresses(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            swaps = Path(temp) / "swaps.json"
            swaps.write_text(json.dumps({"wallets": {
                "W" * 44: {"normalized_swaps": [
                    {"mint": "M" * 44, "side": "buy"},
                    {"mint": "N" * 44, "side": "sell"},
                ]}
            }}))
            found = connectors._extract_mints(str(swaps), limit=5)
        self.assertEqual(found, ["M" * 44, "N" * 44])

    def test_nansen_dex_trades_uses_read_only_profiler_endpoint(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [{
            "transaction_hash": "sig-1",
            "block_timestamp": "2026-10-26T12:00:00Z",
            "trade_type": "buy",
            "token_bought_address": "mint-a",
            "token_sold_address": "mint-b",
            "trade_value_usd": 250.0,
        }]}
        session.request.return_value = response
        records = connectors.fetch_nansen_dex_trades(
            session, "W" * 44, "nansen-key"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["signature"], "sig-1")
        self.assertEqual(records[0]["mint"], "mint-a")
        self.assertEqual(records[0]["source"], "nansen")
        args = session.request.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertIn("/api/v1/profiler/dex-trades", args.args[1])
        self.assertEqual(args.kwargs["headers"]["apikey"], "nansen-key")
        self.assertEqual(args.kwargs["json"]["chain"], "solana")
        self.assertEqual(args.kwargs["json"]["pagination"]["per_page"], 100)

    def test_dune_saved_query_executes_and_fetches_results(self):
        session = Mock()
        def response(payload):
            item = Mock()
            item.raise_for_status.return_value = None
            item.json.return_value = payload
            return item
        session.request.side_effect = [
            response({"execution_id": "exec-1"}),
            response({"state": "QUERY_STATE_COMPLETED"}),
            response({"result": {"rows": [{"mint": "mint-a", "trades": 3}]}}),
        ]
        rows = connectors.fetch_dune_saved_query(session, "key", "123")
        self.assertEqual(rows, [{"mint": "mint-a", "trades": 3}])
        self.assertEqual(session.request.call_count, 3)
        self.assertIn("X-DUNE-API-KEY", session.request.call_args.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
