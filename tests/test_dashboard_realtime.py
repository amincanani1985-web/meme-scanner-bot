import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
MARKET_JS = (ROOT / "dashboard" / "market-live.js").read_text(encoding="utf-8")


class RealtimeDashboardTests(unittest.TestCase):
    def test_dashboard_loads_live_market_module_and_panel(self):
        self.assertIn('id="marketPanel"', DASHBOARD)
        self.assertIn('src="./market-live.js"', DASHBOARD)
        self.assertIn('window.refreshMarketData?.()', DASHBOARD)
        self.assertIn('id="marketوضعیت"', DASHBOARD)
        self.assertIn('if ($("marketوضعیت"))', MARKET_JS)

    def test_live_feed_uses_current_dexscreener_endpoints(self):
        self.assertIn("/token-profiles/latest/v1", MARKET_JS)
        self.assertIn("/token-boosts/latest/v1", MARKET_JS)
        self.assertIn("/latest/dex/tokens/", MARKET_JS)

    def test_market_feed_refreshes_and_labels_stale_data_honestly_in_persian(self):
        self.assertIn("setInterval(refreshMarketData, 30000)", MARKET_JS)
        self.assertIn("تازه‌سازی بازار ناموفق بود", MARKET_JS)
        self.assertIn("آخرین قیمت‌های موفق نمایش داده می‌شوند", MARKET_JS)
        self.assertIn("خود بات طبق زمان‌بندی اجرا می‌شود", MARKET_JS)

    def test_dashboard_never_claims_market_api_data_is_live_after_failure(self):
        self.assertIn("در حال حاضر قیمت زنده‌ای نمایش داده نمی‌شود", MARKET_JS)
        self.assertIn("داده‌های ذخیره‌شده معاملات آزمایشی بات", MARKET_JS)


if __name__ == "__main__":
    unittest.main()
