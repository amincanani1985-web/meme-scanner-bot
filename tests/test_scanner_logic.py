import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "meme_scanner_cycle.py"
spec = importlib.util.spec_from_file_location("scanner", MODULE_PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def pair(**overrides):
    base = {
        "liquidity": {"usd": 20000},
        "volume": {"h1": 10000},
        "marketCap": 50000,
        "txns": {"m5": {"buys": 20, "sells": 10}},
        "priceChange": {"m5": 2.0, "h1": 8.0},
    }
    base.update(overrides)
    return base


def test_max_concurrent_positions_is_ten():
    assert scanner.MAX_CONCURRENT_POSITIONS == 10


def test_target_is_one_hundred_trades():
    assert scanner.TARGET_TRADES == 100


def test_positive_opportunity_passes():
    assert scanner.passes_market_filter(pair())[0] is True


def test_zero_momentum_gets_lower_score():
    good, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 2.0, "h1": 8.0}))
    weak, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 0, "h1": 8.0}))
    assert good > weak


def test_negative_momentum_gets_lower_score():
    good, _ = scanner.score_market_opportunity(pair(priceChange={"m5": 2.0, "h1": 8.0}))
    weak, _ = scanner.score_market_opportunity(pair(priceChange={"m5": -1.0, "h1": 8.0}))
    assert good > weak


def test_low_liquidity_is_rejected():
    assert scanner.passes_market_filter(pair(liquidity={"usd": 1999}))[0] is False


def test_low_volume_is_rejected():
    assert scanner.passes_market_filter(pair(volume={"h1": 499}))[0] is False


def test_weak_buy_pressure_lowers_score():
    weak, _ = scanner.score_market_opportunity(pair(txns={"m5": {"buys": 12, "sells": 10}}))
    strong, _ = scanner.score_market_opportunity(pair(txns={"m5": {"buys": 20, "sells": 10}}))
    assert strong > weak


def test_strong_opportunity_passes():
    assert scanner.passes_market_filter(pair())[0] is True


def test_no_recent_transactions_are_rejected():
    assert scanner.passes_market_filter(pair(txns={"m5": {"buys": 0, "sells": 0}}))[0] is False


def test_learning_changes_score_after_history():
    learning = {"trades": 10, "wins": 8, "buckets": {"80": {"n": 5, "wins": 4}}}
    score, _ = scanner.score_market_opportunity(pair(), learning)
    assert score > scanner.score_market_opportunity(pair())[0]
