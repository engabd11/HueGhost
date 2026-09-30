"""One Hue Ghost per session: a second copy is refused, and cannot share the
control port either (both used to "bind" 8787 on Windows)."""
import sys
import uuid

import pytest

from hueghost import instance
from hueghost.control import _ExclusiveServer
from http.server import BaseHTTPRequestHandler


@pytest.fixture
def lock_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HUEGHOST_INSTANCE", "HueGhostTest-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_second_copy_is_refused_until_the_first_releases(lock_name):
    first = instance.acquire()
    assert first is not None
    try:
        assert instance.acquire() is None
    finally:
        first.release()
    again = instance.acquire()
    assert again is not None
    again.release()


@pytest.mark.skipif(sys.platform != "win32", reason="the show event is Windows-only")
def test_a_second_launch_asks_the_running_copy_for_its_window(lock_name):
    first = instance.acquire()
    try:
        assert instance.ask_running_to_show()
        assert first.show_requested.wait(3.0)
    finally:
        first.release()
    assert not instance.ask_running_to_show()      # nobody left to ask


def test_control_port_cannot_be_bound_twice():
    a = _ExclusiveServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        port = a.server_address[1]
        with pytest.raises(OSError):
            _ExclusiveServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    finally:
        a.server_close()
