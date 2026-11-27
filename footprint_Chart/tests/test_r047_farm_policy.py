import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from desk.quant_audits.r047_review import farm_admit, policy


def intent(symbol, side="BUY", category="CRYPTO", state="reserved"):
    return {"symbol": symbol, "side": side, "category": category, "state": state}


@pytest.fixture
def proposed_policy():
    root = Path(__file__).resolve().parents[2]
    changes = json.loads((root / "desk/quant_audits/r047_farm_policy.patch.json").read_text("utf-8"))
    cfg = json.loads((root / changes["target"]).read_text("utf-8"))
    for change in changes["patch"]:
        key = change["path"].removeprefix("/governor/")
        if change["op"] == "test":
            assert cfg["governor"][key] == change["value"]
        else:
            cfg["governor"][key] = change["value"]
    return cfg


def test_replay_matches_config_proposal(proposed_policy):
    replay_config = policy(1, 1, 1, 1)
    replay_config["governor"]["max_global_positions"] = 2
    for key, expected in replay_config["governor"].items():
        assert proposed_policy["governor"][key] == expected


def test_same_side_cross_symbol_and_opposite_side_major_are_capped(proposed_policy):
    active = [intent("BTCUSDT", state="pending")]
    assert farm_admit(active, intent("ETHUSDT"), proposed_policy) == "same_side"
    assert farm_admit(active, intent("SOLUSDT", "SELL"), proposed_policy) == "major_slots"
    assert farm_admit(active, intent("YFIUSDT", "SELL"), proposed_policy) == "crypto_slots"


def test_same_canonical_symbol_across_venues_shares_one_slot(proposed_policy):
    # Gateway normalization maps MT5 BTCUSD.pi and Delta BTCUSD to the same base.
    mt5 = dict(intent("BTCUSDT"), venue="MT5", raw_symbol="BTCUSD.pi")
    delta = dict(intent("BTCUSDT", "SELL"), venue="DELTA", raw_symbol="BTCUSD")
    assert farm_admit([mt5], delta, proposed_policy) == "symbol"


def test_pending_and_reserved_slots_count_and_tradfi_can_use_slot_two(proposed_policy):
    crypto = intent("ETHUSDT", state="pending")
    index = intent("SP500", category="INDEX")
    assert farm_admit([crypto], index, proposed_policy) is None
    assert farm_admit([crypto, index], intent("XAUUSD", category="METALS"), proposed_policy) == "global_slots"


def test_two_venue_workers_cannot_reserve_the_same_crypto_slot(tmp_path, proposed_policy):
    db = tmp_path / "farm.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE active_intent (payload TEXT NOT NULL)")
    ready = Barrier(2)

    def reserve(trade):
        with sqlite3.connect(db, timeout=5) as conn:
            ready.wait(timeout=5)
            conn.execute("BEGIN IMMEDIATE")
            active = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM active_intent")]
            if farm_admit(active, trade, proposed_policy):
                return False
            conn.execute("INSERT INTO active_intent VALUES (?)", (json.dumps(trade),))
            return True

    with ThreadPoolExecutor(max_workers=2) as workers:
        rows = [dict(intent("BTCUSDT"), venue="MT5"), dict(intent("ETHUSDT"), venue="DELTA")]
        assert sorted(workers.map(reserve, rows)) == [False, True]


def test_asset_proposals_match_pinned_files_and_replay_arms():
    root = Path(__file__).resolve().parents[2]
    changes = json.loads((root / "desk/quant_audits/r047_asset_trials.patch.json").read_text("utf-8"))
    replay = json.loads((root / "desk/quant_audits/r047_review_results.json").read_text("utf-8"))
    for trial in changes["trials"]:
        cfg = json.loads((root / trial["target"]).read_text("utf-8"))
        pair = replay["pairs"][cfg["symbol"]][cfg["strategy"]]
        assert trial["replay_arm"] in pair["trials"]
        assert cfg == pair["recorded_config"]
        for change in trial["patch"]:
            *parents, key = change["path"].removeprefix("/").split("/")
            target = cfg
            for parent in parents:
                target = target[parent]
            if change["op"] == "test":
                assert target[key] == change["value"]
            else:
                assert key in target
                target[key] = change["value"]
        assert cfg["status"] == "SHADOW_TRIAL_ONLY"
