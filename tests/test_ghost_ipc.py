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


def test_a_swapped_track_opens_where_it_is_asked_to_not_at_the_launch_start():
    """mpv's --start from the launch applies to every later loadfile too: each
    new song opened seconds in and was then seeked back (audible)."""
    from hueghost.ghost import GhostPlayer

    fh = LivePipe()
    g = GhostPlayer(proc=None, ipc=ipc_with(fh), item_id="track1")
    assert g.swap("http://jf/Audio/track2/stream", start=0.4) is True
    sent = [json.loads(line)["command"] for line in fh.written.splitlines()]
    assert sent.index(["set_property", "start", "0.400"]) < sent.index(
        ["loadfile", "http://jf/Audio/track2/stream", "replace"]), sent


def test_the_idle_music_ghost_ends_on_eof_but_not_on_a_loadfile_replace():
    from hueghost.ghost import GhostPlayer

    g = GhostPlayer(proc=None, ipc=ipc_with(LivePipe()), item_id="track1")
    g._on_event({"event": "end-file", "reason": "eof"})
    assert g.ended
    g._on_event({"event": "start-file"})
    assert not g.ended
    g._on_event({"event": "end-file", "reason": "stop"})     # replaced by the next song
    assert not g.ended


def _playing_ghost(monkeypatch, clock):
    from hueghost import ghost as ghost_mod
    from hueghost.ghost import GhostPlayer

    monkeypatch.setattr(ghost_mod.time, "monotonic", lambda: clock[0])
    g = GhostPlayer(proc=None, ipc=ipc_with(LivePipe()), item_id="film")
    g._on_event({"event": "property-change", "name": "core-idle", "data": False})
    return g


def test_the_ghost_position_is_the_median_of_its_reports_not_the_last_one(monkeypatch):
    """Live 2026-11-13: one time-pos event scatters +/-10 ms (frame steps,
    vsync, delivery), which made a locked drift flicker 0.00 / +/-0.01 / -0.02."""
    import random

    rnd = random.Random(1)
    clock = [100.0]
    g = _playing_ghost(monkeypatch, clock)
    errs = []
    for i in range(300):                                  # 10 s of 30 fps events
        clock[0] = 100.0 + i / 30.0
        g._on_event({"event": "property-change", "name": "time-pos",
                     "data": 50.0 + i / 30.0 + rnd.uniform(-0.012, 0.012)})
        clock[0] += 0.01                                  # the tick reads between events
        if i > 30:
            errs.append(abs(g.pos - (50.0 + i / 30.0 + 0.01)))
    errs.sort()
    assert errs[int(0.95 * len(errs))] < 0.004 and errs[-1] < 0.008, (errs[int(0.95 * len(errs))], errs[-1])


def test_a_jump_starts_the_position_window_over(monkeypatch):
    clock = [100.0]
    g = _playing_ghost(monkeypatch, clock)
    for i in range(30):
        clock[0] = 100.0 + i / 30.0
        g._on_event({"event": "property-change", "name": "time-pos", "data": 50.0 + i / 30.0})
    clock[0] += 1 / 30.0
    g._on_event({"event": "property-change", "name": "time-pos", "data": 300.0})   # mpv jumped
    assert abs(g.pos - 300.0) < 1e-6, "the old line must not outvote the new position"


def test_reports_from_before_a_small_seek_do_not_mix_into_the_position(monkeypatch):
    """Live 2026-11-14, music: a 0.05 s seek is under the jump threshold, and
    the old line's last reports mixed into the median - every correction
    overshot and the next one went the other way, every 2 s."""
    from hueghost.lockstep import Seek

    clock = [100.0]
    g = _playing_ghost(monkeypatch, clock)
    for i in range(30):
        clock[0] = 100.0 + i / 30.0
        g._on_event({"event": "property-change", "name": "time-pos", "data": 50.0 + i / 30.0})
    g.apply([Seek(51.0 + 0.05)], clock[0])          # 0.05 s ahead of where it plays
    for i in range(30, 33):                         # the old line, still coming in
        clock[0] = 100.0 + i / 30.0
        g._on_event({"event": "property-change", "name": "time-pos", "data": 50.0 + i / 30.0})
    g._on_event({"event": "playback-restart"})
    for i in range(33, 45):                         # the new line
        clock[0] = 100.0 + i / 30.0
        g._on_event({"event": "property-change", "name": "time-pos", "data": 50.05 + i / 30.0})
    assert abs(g.pos - (50.05 + 44 / 30.0)) < 1e-6
