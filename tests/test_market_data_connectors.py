import unittest
from unittest.mock import Mock

import market_data_connectors as connectors


class MarketDataConnectorTests(unittest.TestCase):
    def test_missing_credentials_are_reported_not_silently_as_zero_activity(self):
        report = connectors.collect(
            mints=["So11111111111111111111111111111111111111112"],
            session=Mock(),
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
                {"chainId": "solana", "pairAddress": "pair-1", "dexId": "raydium",
                 "baseToken": {"address": "mint-a"}, "quoteToken": {"address": "mint-b"},
                 "priceUsd": "0.001", "liquidity": {"usd": 10000},
                 "volume": {"h1": 2500}, "priceChange": {"m5": 1.2},
                 "txns": {"m5": {"buys": 10, "sells": 4}}},
                {"chainId": "ethereum", "pairAddress": "pair-2"},
            ]
        }
        records = connectors.fetch_dexscreener(session, "mint-a")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pair_address"], "pair-1")
        self.assertEqual(records[0]["liquidity_usd"], 10000)
        self.assertEqual(records[0]["source"], "dexscreener")

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
