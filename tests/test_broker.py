"""
Live broker safety rules, against a fake Topstep client. No network, no orders.

Run: python -m tests.test_broker   (or pytest, if installed)
"""
import os
import tempfile

import desk.broker as broker_mod
from desk.broker import LiveBroker, BrokerError

ACCOUNT = 123456
MNQ = {"id": "CON.F.US.MNQ.Z26", "name": "MNQZ6", "tickSize": 0.25, "tickValue": 0.5, "activeContract": True}


class FakeResponse:
    def __init__(self, data):
        self._data, self.status_code = data, 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def post(self, url, headers=None, json=None, timeout=None):
        path = url.split("topstepx.com")[-1] if "topstepx.com" in url else url
        self.calls.append((path, json))
        r = self.routes.get(path, {"success": True})
        return FakeResponse(r(json) if callable(r) else r)


class FakeClient:
    _base_url = "https://api.topstepx.com"
    _token = "t"

    def __init__(self, routes):
        self._session = FakeSession(routes)

    def _ensure_token(self):
        pass

    def search_contracts(self, text, live=False):
        return [MNQ]


class FakeSlack:
    def __init__(self):
        self.sent = []

    def send_message(self, text):
        self.sent.append(text)
        return True


def routes(positions=(), orders=(), trades=(), accounts=None):
    return {
        "/api/Account/search": {"success": True, "accounts": accounts if accounts is not None else
                                [{"id": ACCOUNT, "name": "50KTC", "canTrade": True}]},
        "/api/Position/searchOpen": {"success": True, "positions": list(positions)},
        "/api/Order/searchOpen": {"success": True, "orders": list(orders)},
        "/api/Trade/search": {"success": True, "trades": list(trades)},
        "/api/Order/place": {"success": True, "orderId": 9056},
    }


def make(mode, **r):
    os.environ["TOPSTEP_ACCOUNT_ID"] = str(ACCOUNT)
    client, slack = FakeClient(routes(**r)), FakeSlack()
    return LiveBroker(client, slack, mode=mode), client, slack


def paths(client):
    return [p for p, _ in client._session.calls]


def test_off_mode_does_nothing():
    b = LiveBroker(FakeClient(routes()), FakeSlack(), mode="off")
    assert not b.active and "OFF" in b.describe()


def test_dryrun_builds_the_bracket_order_but_never_sends_it():
    b, client, slack = make("dryrun")
    order = b.enter("long", 100.0, 90.0, 120.0, "test reclaim")
    p = order["payload"]
    assert order["dry"] and "/api/Order/place" not in paths(client)
    assert p["contractId"] == "CON.F.US.MNQ.Z26" and p["size"] == 1 and p["side"] == 0 and p["type"] == 2
    # long: stop below the fill (negative ticks), target above (positive); the stop carries the 4-tick pad
    assert p["stopLossBracket"] == {"ticks": -44, "type": 4} and p["takeProfitBracket"] == {"ticks": 80, "type": 1}
    assert "DRY RUN" in slack.sent[-1]


def test_short_bracket_ticks_are_signed_the_other_way():
    # The live failure on 10/12: "Invalid take profit ticks (675). Ticks should be less than zero when going short."
    b, _, _ = make("dryrun")
    p = b.enter("short", 30960.75, 30987.0, 30792.0, "Support breaks VAL 10/8")["payload"]
    assert p["side"] == 1
    assert p["takeProfitBracket"]["ticks"] == -675 and p["stopLossBracket"]["ticks"] == 109


def test_live_places_one_bracketed_order():
    b, client, slack = make("true")
    order = b.enter("short", 100.0, 110.0, 80.0, "fade")
    assert order["orderId"] == 9056 and b.trades_today == 1
    placed = [j for p, j in client._session.calls if p == "/api/Order/place"][0]
    assert placed["side"] == 1 and placed["size"] == 1
    assert placed["stopLossBracket"] == {"ticks": 44, "type": 4}         # short: stop above (+), padded
    assert placed["takeProfitBracket"] == {"ticks": -80, "type": 1}      # short: target below (-)


def test_size_is_hard_capped_at_one():
    os.environ["LIVE_MAX_SIZE"] = "3"
    try:
        b, _, _ = make("dryrun")
        assert b.size == 1
    finally:
        del os.environ["LIVE_MAX_SIZE"]


def test_refuses_with_an_existing_position_or_working_orders():
    b, client, slack = make("true", positions=[{"contractId": "CON.F.US.ENQ.Z26", "size": 1}])
    assert b.enter("long", 100.0, 90.0, 120.0, "x") is None and "/api/Order/place" not in paths(client)
    assert "already has an open position" in slack.sent[-1]
    b, client, slack = make("true", orders=[{"id": 1, "contractId": "CON.F.US.MNQ.Z26"}])
    assert b.enter("long", 100.0, 90.0, 120.0, "x") is None and "working" in slack.sent[-1]


def test_refuses_bad_stops():
    b, client, slack = make("true")
    assert b.enter("long", 100.0, 99.0, 120.0, "x") is None and "ticks away" in slack.sent[-1]      # 4 ticks
    assert b.enter("long", 200.0, 70.0, 500.0, "x") is None and "risk $260" in slack.sent[-1]     # 130 pts
    assert "/api/Order/place" not in paths(client)


def test_kill_switch_and_daily_loss_and_max_trades():
    b, client, slack = make("true", trades=[{"contractId": "CON.F.US.MNQ.Z26", "profitAndLoss": -310,
                                             "fees": 2, "voided": False}])
    with tempfile.NamedTemporaryFile() as f:
        old = broker_mod.KILL_FILE
        broker_mod.KILL_FILE = f.name
        try:
            assert "kill switch" in b.blocked()
        finally:
            broker_mod.KILL_FILE = old
    from datetime import datetime, timezone
    b.session_start = datetime.now(timezone.utc)
    assert "daily loss stop" in b.blocked()
    b2, _, _ = make("true")
    b2.trades_today = b2.max_trades
    assert "max" in b2.blocked()


def test_wrong_account_is_rejected_at_startup():
    os.environ["TOPSTEP_ACCOUNT_ID"] = "999"
    try:
        LiveBroker(FakeClient(routes()), FakeSlack(), mode="true")
    except BrokerError as e:
        assert "not an active account" in str(e)
    else:
        raise AssertionError("wrong account accepted")


def test_flatten_cancels_orders_and_closes_the_position():
    b, client, slack = make("true", positions=[{"contractId": "CON.F.US.MNQ.Z26", "size": 1}],
                            orders=[{"id": 7, "contractId": "CON.F.US.MNQ.Z26"}])
    b.flatten("test")
    calls = paths(client)
    assert "/api/Order/cancel" in calls and "/api/Position/closeContract" in calls
    assert "Flattened" in slack.sent[-1]


def bracket_broker(fill, stop_price, modify_ok=True):
    """A filled position with its bracket working; /api/Order/modify really changes the orders."""
    orders = [{"id": 11, "contractId": MNQ["id"], "type": 4, "stopPrice": stop_price},
              {"id": 12, "contractId": MNQ["id"], "type": 1, "limitPrice": 30655.0}]

    def modify(j):
        if not modify_ok:
            return {"success": False, "errorMessage": "rejected"}
        o = next(o for o in orders if o["id"] == j["orderId"])
        o.update({k: j[k] for k in ("stopPrice", "limitPrice") if k in j})
        return {"success": True}
    b, client, slack = make("true")
    client._session.routes["/api/Order/searchOpen"] = lambda j: {"success": True, "orders": orders}
    client._session.routes["/api/Order/modify"] = modify
    return b, client, slack, {"averagePrice": fill, "size": 1, "contractId": MNQ["id"]}, orders


# 10/13 weekly-low short: planned entry 30782.25, stop 30794 (level 30792 + 2), target 30719.
TEN_13 = {"planned": {"side": "short", "entry": 30782.25, "stop": 30794.0, "target": 30719.0}}


def test_bracket_is_moved_to_the_planned_prices_after_slippage():
    # Filled 2 pts low: the padded stop (47 + 4 ticks from the fill) sits at 30793 - move it to 30794.
    b, client, slack, pos, orders = bracket_broker(30780.25, 30793.0)
    info = b.correct_bracket(TEN_13, pos)
    assert info["status"] == "corrected" and info["slippage"] == 2.0
    assert "2 pts worse than planned" in slack.sent[-1]
    assert orders[0]["stopPrice"] == 30794.0 and orders[1]["limitPrice"] == 30719.0
    assert "Bracket set to the plan" in slack.sent[-1]


def test_bracket_waits_until_the_orders_show_up():
    b, client, slack, pos, orders = bracket_broker(30780.25, 30793.0)
    orders.clear()
    assert b.correct_bracket(TEN_13, pos) is None and "/api/Order/modify" not in paths(client)


def test_bracket_kept_when_slippage_breaks_the_risk_rules():
    # Filled 20 pts low: from 30762.25 the planned stop is 31.75 pts away -> target only 1.36R.
    b, client, slack, pos, orders = bracket_broker(30762.25, 30775.0)
    info = b.correct_bracket(TEN_13, pos)
    assert info["status"] == "kept" and "R <" in info["note"]
    assert orders[0]["stopPrice"] == 30775.0 and "/api/Order/modify" not in paths(client)


def test_failed_modify_leaves_the_padded_stop_working():
    b, client, slack, pos, orders = bracket_broker(30780.25, 30793.0, modify_ok=False)
    info = b.correct_bracket(TEN_13, pos)
    assert info["status"] == "failed" and orders[0]["stopPrice"] == 30793.0
    assert "padded stop" in slack.sent[-1]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
