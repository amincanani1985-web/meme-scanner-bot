import unittest

from whale_monitor import detect_whale_balance_drops, confirmed_whale_dump


class WhaleMonitorTests(unittest.TestCase):
    def test_detects_large_drop_for_same_owner(self):
        previous = {"owner-a": 1000.0, "owner-b": 500.0}
        current = {"owner-a": 750.0, "owner-b": 490.0}
        self.assertEqual(
            detect_whale_balance_drops(previous, current, threshold=0.20),
            ["owner-a"],
        )

    def test_ignores_small_drop(self):
        self.assertEqual(
            detect_whale_balance_drops({"owner-a": 1000}, {"owner-a": 850}),
            [],
        )

    def test_does_not_treat_missing_owner_as_zero(self):
        self.assertEqual(
            detect_whale_balance_drops({"owner-a": 1000}, {"owner-b": 1}),
            [],
        )

    def test_invalid_or_empty_snapshots_are_not_a_dump(self):
        self.assertEqual(detect_whale_balance_drops({}, {"owner-a": 1}), [])
        self.assertEqual(
            detect_whale_balance_drops({"owner-a": "bad"}, {"owner-a": 0}),
            [],
        )

    def test_balance_drop_requires_market_sell_confirmation(self):
        previous = {"owner-a": 1000}
        current = {"owner-a": 700}
        quiet_market = {"txns": {"m5": {"buys": 20, "sells": 10}},
                        "priceChange": {"m5": 2.0}}
        sell_pressure = {"txns": {"m5": {"buys": 5, "sells": 15}},
                         "priceChange": {"m5": -3.0}}
        self.assertFalse(confirmed_whale_dump(previous, current, quiet_market))
        self.assertTrue(confirmed_whale_dump(previous, current, sell_pressure))

    def test_missing_market_data_fails_closed(self):
        self.assertFalse(
            confirmed_whale_dump({"owner-a": 1000}, {"owner-a": 700}, None)
        )


if __name__ == "__main__":
    unittest.main()
