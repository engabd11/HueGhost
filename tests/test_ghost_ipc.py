"""mpv's IPC connection, and what happens when mpv is no longer on the end of it.

mpv exits on its own - the file ends, the user closes the window - and the
daemon only finds out on its next tick. A command already on its way at that
moment used to raise out of ``apply()``, through the tick, and take the whole
daemon down with it, which left the ghost mpv orphaned and playing to nobody.
"""
import json

import pytest

from hueghost.ghost import MpvIpc


class DeadPipe:
    """A handle that behaves like a named pipe whose other end has gone."""

    def __init__(self, exc=OSError(22, "Invalid argument")):
        self.exc = exc
        self.writes = 0

    def write(self, data):
        self.writes += 1
        raise self.exc


class LivePipe:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data


def ipc_with(fh):
    ipc = MpvIpc(r"\\.\pipe\test")
    ipc._fh = fh
    return ipc


def test_a_command_to_a_live_mpv_is_written_as_one_json_line():
    fh = LivePipe()
    ipc = ipc_with(fh)
    assert ipc.send("set_property", "pause", True) is True
    assert fh.written.endswith(b"\n")
    assert json.loads(fh.written) == {"command": ["set_property", "pause", True]}


def test_a_command_to_an_mpv_that_has_gone_is_not_fatal():
    fh = DeadPipe()
    ipc = ipc_with(fh)
    assert ipc.send("set_property", "speed", 1.02) is False
    assert ipc.closed, "the connection is marked dead, not retried for ever"


def test_nothing_is_written_once_the_connection_is_known_dead():
    fh = DeadPipe()
    ipc = ipc_with(fh)
    ipc.send("quit")
    ipc.send("quit")
    assert fh.writes == 1


@pytest.mark.parametrize("exc", [OSError(22, "Invalid argument"), ValueError("closed file")])
def test_a_request_gives_up_at_once_instead_of_waiting_out_its_timeout(exc):
    ipc = ipc_with(DeadPipe(exc))
    assert ipc.request("get_property", "time-pos", timeout=30.0) is None
    assert not ipc._pending, "the pending slot must not leak"


def test_a_track_swapped_into_a_paused_ghost_plays():
    """Live 2026-10-06: CAMusic paused (the ghost paused with it), then the
    next song started. The swap reset the ghost's own 'paused' to False, but
    mpv keeps pause=yes across loadfile and reports no change - so the
    daemon thought it played, never sent Resume, and seeked a frozen ghost
    3 s forward every 3 s for whole songs while the lights sat still."""
    from hueghost.ghost import GhostPlayer
    from hueghost.lockstep import Pause

    fh = LivePipe()
    g = GhostPlayer(proc=None, ipc=ipc_with(fh), item_id="track1")
    g.apply([Pause()], 0.0)
    fh.written = b""
    assert g.swap("http://jf/Audio/track2/stream") is True
    sent = [json.loads(line)["command"] for line in fh.written.splitlines()]
    assert ["set_property", "pause", False] in sent, sent
    assert g.paused is False, "what the ghost believes now matches what mpv does"
