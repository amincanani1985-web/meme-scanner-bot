import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meme_scanner_cycle as scanner
import paper_position_monitor


class PaperPositionMonitorTests(unittest.TestCase):
    def test_monitor_batches_quotes_for_open_and_shadow_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper_dir = Path(tmp) / "paper_account"
            paper_dir.mkdir()
            state = {
                "equity": 1000.0,
                "initial_equity": 1000.0,
                "positions": {"mint-a": {}},
                "shadow_positions": {"mint-b": {}},
                "trade_count": 0,
                "strategy": {"active_threshold": 80, "status": "baseline"},
            }
            (paper_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with patch.object(scanner, "fetch_dex_pairs") as batch, \
                 patch.object(scanner, "manage_open_positions"), \
                 patch.object(scanner, "manage_shadow_positions"), \
                 patch.object(scanner, "fetch_candidate_mints", side_effect=AssertionError("monitor must not discover candidates")), \
                 patch.object(scanner, "open_position", side_effect=AssertionError("monitor must not open positions")):
                paper_position_monitor.run(tmp)
            batch.assert_called_once_with(["mint-a", "mint-b"])

    def test_monitor_only_persists_state_without_discovery_or_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "paper_account").mkdir()
            with patch.object(scanner, "fetch_candidate_mints", side_effect=AssertionError("monitor must not discover candidates")), \
                 patch.object(scanner, "open_position", side_effect=AssertionError("monitor must not open positions")):
                result = paper_position_monitor.run(tmp)

            self.assertEqual(result["mode"], "monitor_only")
            self.assertEqual(result["closed_trades"], 0)
            saved = json.loads((Path(tmp) / "paper_account" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["last_position_monitor"], result["timestamp"])
            self.assertEqual(result["open_positions"], 0)


if __name__ == "__main__":
    unittest.main()
