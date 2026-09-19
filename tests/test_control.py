import json
import urllib.error
import urllib.request

import pytest

from hueghost.control import ControlServer


@pytest.fixture
def server():
    calls = []

    def status():
        return {"enabled": True, "state": "idle"}

    def action(name, payload):
        calls.append((name, payload))
        return {"ok": True, "name": name}

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ControlServer("127.0.0.1", port, "secret", status, action)
    srv.start()
    yield srv, port, calls
    srv.stop()


def call(port, path, method="GET", body=None, token="secret"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read().decode())


def test_health_needs_no_token(server):
    _, port, _ = server
    code, body = call(port, "/health", token=None)
    assert code == 200 and body["ok"]


def test_status_and_auth(server):
    _, port, _ = server
    assert call(port, "/status")[1]["state"] == "idle"
    with pytest.raises(urllib.error.HTTPError) as e:
        call(port, "/status", token=None)
    assert e.value.code == 401
    assert call(port, "/status?token=secret", token=None)[0] == 200


def test_actions_get_and_post(server):
    _, port, calls = server
    assert call(port, "/on")[1] == {"ok": True, "name": "on"}
    call(port, "/set", "POST", {"offset_delta": 0.25})
    call(port, "/set?intensity=high&offset_s=1.5", "GET")
    assert calls == [("on", {}), ("set", {"offset_delta": 0.25}), ("set", {"intensity": "high", "offset_s": 1.5})]


def test_unknown_path_404(server):
    _, port, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(port, "/nope")
    assert e.value.code == 404
