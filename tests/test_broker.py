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
        return FakeResponse(self.routes.get(path, {"success": True}))


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
    assert p["stopLossBracket"] == {"ticks": 40, "type": 4} and p["takeProfitBracket"] == {"ticks": 80, "type": 1}
    assert "DRY RUN" in slack.sent[-1]


def test_live_places_one_bracketed_order():
    b, client, slack = make("true")
    order = b.enter("short", 100.0, 110.0, 80.0, "fade")
    assert order["orderId"] == 9056 and b.trades_today == 1
    placed = [j for p, j in client._session.calls if p == "/api/Order/place"][0]
    assert placed["side"] == 1 and placed["size"] == 1
    assert placed["stopLossBracket"]["type"] == 4 and placed["takeProfitBracket"]["type"] == 1


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


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
