"""
RSI and MACD.

Run: python -m tests.test_indicators   (or pytest, if installed)
"""
from desk.indicators import rsi, macd


def test_rsi_extremes_and_midpoint():
    up = [float(i) for i in range(30)]
    assert rsi(up)[-1] == 100.0 and rsi(up)[13] is None
    down = [float(30 - i) for i in range(30)]
    assert rsi(down)[-1] == 0.0
    zigzag = [100.0 + (1 if i % 2 else -1) for i in range(40)]
    assert 40 < rsi(zigzag)[-1] < 60


def test_macd_flat_and_trending():
    line, sig, hist = macd([100.0] * 60)
    assert line[-1] == 0 and sig[-1] == 0 and hist[-1] == 0
    assert line[24] is None and line[25] is not None and sig[33] is not None
    line, sig, hist = macd([100.0 + i for i in range(60)])
    assert line[-1] > 0                      # fast EMA above slow in an uptrend


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
