import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meme_scanner_cycle as scanner
import paper_position_monitor


class PaperPositionMonitorTests(unittest.TestCase):
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
