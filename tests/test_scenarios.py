"""
Scenario loading, validation, level resolution and read-back.

Run: python -m tests.test_scenarios   (or pytest, if installed)
"""
import json

from desk.scenarios import load, from_dict, describe, resolve_level, normalize_level, ScenarioError

EXAMPLE = {
    "name": "Asia reclaim VAL to VAH",
    "type": "reclaim",
    "side": "long",
    "level": "pdVAL",
    "conditions": [{"price": "above", "level": "pdVAL"}],
    "session": "Asia",
    "until": "03:00",
    "target": "pdVAH",
    "stop": "default",
    "size": None,
    "rejection": "pattern",
    "once": True,
    "mode": "paper",
}
LEVELS = {"pdVAH": 31018.0, "pdPOC": 30944.0, "pdVAL": 30812.0, "ATR+50%": 31242.16, "VAH-09/22": 31033.0}


def expect_error(d, fragment):
    try:
        from_dict(d)
    except ScenarioError as e:
        assert fragment in str(e), str(e)
        return
    raise AssertionError(f"expected an error mentioning '{fragment}'")


def test_playbook_example_loads_and_reads_back():
    [sc] = load(json.dumps(EXAMPLE))
    text = describe(sc, LEVELS)
    assert "B. Reclaim and rotate, LONG" in text
    assert "Asia only, until 03:00" in text
    assert "trades below pdVAL 30812, then a 30m candle closes back above it" in text
    assert "Target: pdVAH 31018" in text and "PAPER" in text


def test_level_words_normalize():
    assert normalize_level("VAL") == "pdVAL"
    assert normalize_level("atr + 50%") == "ATR+50%"
    assert normalize_level("VAH 9/22") == "VAH-09/22"
    assert normalize_level("31,018.5") == 31018.5
    assert resolve_level("VAH 9/22", LEVELS) == 31033.0


def test_minimal_scenario_gets_defaults():
    sc = from_dict({"name": "fade VAH", "type": "fade", "side": "short", "level": "VAH",
                    "conditions": [{"day_range_pct": 50}]})
    assert sc.session == "any" and sc.target == "default" and sc.mode == "paper"
    text = describe(sc, LEVELS)
    assert "the day is up at least 50% of its ATR" in text    # fade short: day must be UP first
    assert "beyond the rejection wick" in text


def test_list_of_scenarios():
    two = [EXAMPLE, {**EXAMPLE, "name": "lose VAH", "type": "lose", "side": "short", "level": "VAH"}]
    assert [s.type for s in load(json.dumps(two))] == ["reclaim", "lose"]


def test_validation_errors_are_specific():
    expect_error({**EXAMPLE, "type": "scalp"}, "type 'scalp' must be one of")
    expect_error({**EXAMPLE, "side": "short"}, "'reclaim' is a long")
    expect_error({**EXAMPLE, "level": "VWAP"}, "unknown level 'VWAP'")
    expect_error({**EXAMPLE, "until": "3am"}, "until must be 'HH:MM'")
    expect_error({**EXAMPLE, "size": 5}, "size must be 1, 2, 3")
    expect_error({**EXAMPLE, "conditions": [{"rsi": 30}]}, "unknown condition")
    expect_error({**EXAMPLE, "leverage": 10}, "unknown field(s): leverage")
    expect_error({k: v for k, v in EXAMPLE.items() if k != "level"}, "needs a level")
    try:
        load("{not json")
    except ScenarioError as e:
        assert "not valid JSON" in str(e)
    else:
        raise AssertionError("bad JSON accepted")


def test_missing_level_today():
    try:
        resolve_level("ATR-75%", LEVELS)
    except ScenarioError as e:
        assert "isn't available today" in str(e)
    else:
        raise AssertionError("missing level resolved")


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
