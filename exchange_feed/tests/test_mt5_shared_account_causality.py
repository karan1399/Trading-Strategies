import pytest

from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account


def trade(symbol, entry, exit_, *, sleeve="test", result=1.0, **fields):
    return dict(symbol=symbol, mt5_symbol=symbol, category="CRYPTO", side="BUY",
                sleeve=sleeve, bucket="TEST", ts_in=entry, ts_out=exit_, r=result, **fields)


def simulate(rows, **kwargs):
    return simulate_shared_account("causality", rows, max_global_conc=1, **kwargs)


@pytest.mark.parametrize("btc_exit,eth_exit", [(120, 180), (180, 120)])
def test_simultaneous_entries_ignore_their_exit_times_and_input_order(btc_exit, eth_exit):
    btc = trade("BTC", 60, btc_exit, result=1.0)
    eth = trade("ETH", 60, eth_exit, result=-1.0)
    for rows in ([btc, eth], [eth, btc]):
        result = simulate(rows)
        assert [row["symbol"] for row in result["by_asset"]] == ["BTC"]


def test_entry_priority_is_explicit_and_duplicate_identity_is_rejected():
    btc = trade("BTC", 60, 120)
    eth = trade("ETH", 60, 120, entry_priority=-1)
    assert simulate([btc, eth])["by_asset"][0]["symbol"] == "ETH"
    with pytest.raises(ValueError, match="Duplicate entry identity"):
        simulate([btc, dict(btc, ts_out=180)])


def test_minute_close_defers_cash_and_capacity():
    first = trade("BTC", 60, 120)
    during_exit_minute = trade("ETH", 120, 240)
    after_exit_minute = trade("SOL", 180, 240)
    result = simulate([during_exit_minute, after_exit_minute, first])
    assert set(row["symbol"] for row in result["by_asset"]) == {"BTC", "SOL"}
    assert result["curve"][1][0] == 180


def test_training_censors_unresolved_pnl_without_freeing_the_slot():
    first = trade("BTC", 60, 120, result=99)
    during_exit_minute = trade("ETH", 120, 120)
    result = simulate([during_exit_minute, first], max_ts=180)
    assert result["metrics"]["trades"] == 0
    assert result["metrics"]["end_equity"] == 100000


def test_native_ohlc_exit_is_available_at_native_bar_close():
    first = trade("MT5_XAUUSD", 60, 120, tf="H4")
    second = trade("ETH", 180, 240)
    result = simulate([second, first])
    assert result["curve"][1][0] == 3720
    assert [row["symbol"] for row in result["by_asset"]] == ["MT5_XAUUSD"]


def test_recorded_exit_availability_overrides_minute_bar_convention():
    first = trade("BTC", 60, 120, exit_available_ts=130)
    second = trade("ETH", 130, 240)
    assert simulate([second, first])["metrics"]["trades"] == 2
