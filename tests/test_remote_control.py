"""2.13: what a remote control (Hue Synco in Home Assistant) can now do over
/set - pick the source to sync now, send a source to another entertainment
area or give it its own mode and intensity, and set every timing - and the
/status block it reads them back from."""
import pytest

from tests.test_daemon_flow import _music, playing, session, world  # noqa: F401  (fixture)
from tests.test_watcher import LOCAL0

AREAS = [{"id": "a-living", "name": "Living room", "lights": 3},
         {"id": "a-office", "name": "Office full music", "lights": 5}]


@pytest.fixture
def remote(world, monkeypatch):  # noqa: F811
    d = world.d
    monkeypatch.setattr(d.engine, "areas", lambda: list(AREAS))
    d.cfg.set("jellyfin.players", [
        {"device_id": "atv-device-id", "device_name_contains": "", "area_id": "", "area_name": ""},
        {"device_id": "s23", "device_name_contains": "", "area_id": "", "area_name": ""},
    ])
    d._rebuild_sources()
    d.jf.stream_url = lambda item_id, msid=None, **kw: "http://jf/stream/" + item_id
    d.jf.auth_header_for_mpv = lambda: "Authorization: x"
    by_dev = {b["device_id"]: b["id"] for b in d.cfg.bindings()}
    world.ids = [by_dev["atv-device-id"], by_dev["s23"]]
    return world


def _phone(world, pos=2049.0):
    s = session(pos + world.t, False, LOCAL0 + world.t, item="solarmax")
    s["DeviceId"], s["DeviceName"], s["Client"] = "s23", "S23", "Jellyfin for Android"
    return s


def _tv(world):
    return session(100.0 + world.t, False, LOCAL0 + world.t)


def test_status_carries_the_areas_the_pin_and_every_setting(remote):
    st = remote.d.status()
    assert [a["name"] for a in st["areas"]] == ["Living room", "Office full music"]
    assert st["pin"] is None
    s = st["settings"]
    for k in ("offset_s", "music_offset_s", "lights_off_delay_s", "idle_stop_delay_s", "music_gap_s",
              "music_pause_stop_s", "pause_stop_min", "time_lock", "keep_awake",
              "pixel_shift_min", "blackout_idle_min"):
        assert k in s, k


def test_the_source_picked_to_sync_now_goes_first_whenever_it_plays(remote):
    d = remote.d
    tv_id, phone_id = remote.ids
    remote.step(1.0, [_tv(remote), _phone(remote)])
    assert d.last_obs.report.device_id == "atv-device-id"      # list order: the TV first
    d.action("set", {"pin": phone_id})
    remote.step(2.0, [_tv(remote), _phone(remote)])
    assert d.last_obs.report.device_id == "s23", "picked: the phone, at once"
    assert d.status()["pin"] == phone_id
    # the pick only goes first - with the phone idle, the TV still syncs
    remote.step(3.0, [_tv(remote)])
    assert d.last_obs.report.device_id == "atv-device-id"
    d.action("set", {"pin": None})
    assert d.status()["pin"] is None


def test_picking_a_source_that_is_off_switches_it_on(remote):
    d = remote.d
    _, phone_id = remote.ids
    d.action("set", {"binding": {"key": phone_id, "enabled": False}})
    d.action("set", {"pin": phone_id})
    b = next(b for b in d.status()["bindings"] if b["id"] == phone_id)
    assert b["enabled"] is True and d.status()["pin"] == phone_id


def test_a_source_can_be_sent_to_another_area_and_given_its_own_look(remote):
    d = remote.d
    tv_id, _ = remote.ids
    d.action("set", {"binding": {"key": tv_id, "area_id": "a-office", "mode": "music",
                                 "intensity": "high"}})
    b = next(b for b in d.status()["bindings"] if b["id"] == tv_id)
    assert (b["area_name"], b["mode"], b["intensity"], b["enabled"]) == \
        ("Office full music", "music", "high", True)
    # by name works too, and "" hands each one back
    d.action("set", {"binding": {"key": tv_id, "area_id": "Living room"}})
    d.action("set", {"binding": {"key": tv_id, "area_id": "", "mode": "", "intensity": ""}})
    b = next(b for b in d.status()["bindings"] if b["id"] == tv_id)
    assert (b["area_id"], b["mode"], b["intensity"]) == (None, None, None)
    with pytest.raises(ValueError):
        d.action("set", {"binding": {"key": tv_id, "area_id": "Garage"}})
    with pytest.raises(ValueError):
        d.action("set", {"binding": {"key": tv_id, "mode": "disco"}})


def test_changing_only_the_area_does_not_switch_a_source_back_on(remote):
    d = remote.d
    tv_id, _ = remote.ids
    d.action("set", {"binding": {"key": tv_id, "enabled": False}})
    d.action("set", {"binding": {"key": tv_id, "area_id": "a-living"}})
    b = next(b for b in d.status()["bindings"] if b["id"] == tv_id)
    assert b["enabled"] is False and b["area_name"] == "Living room"


def test_the_new_timings_are_set_and_reported(remote):
    d = remote.d
    d.action("set", {"music_offset_s": 0.15, "music_gap_s": 6, "idle_stop_delay_s": 30,
                     "lights_off_delay_s": 2.5, "music_pause_stop_s": 20, "pause_stop_min": 5})
    s = d.status()["settings"]
    assert (s["music_offset_s"], s["music_gap_s"], s["idle_stop_delay_s"], s["lights_off_delay_s"],
            s["music_pause_stop_s"], s["pause_stop_min"]) == (0.15, 6.0, 30.0, 2.5, 20.0, 5.0)
    d.action("set", {"music_offset_s": None})
    assert d.status()["settings"]["music_offset_s"] is None    # back to sharing the film offset


def test_film_and_music_offsets_never_move_each_other(remote):
    """Live 2026-10-21: with music playing, a film-offset nudge was applied to
    the offset in use (the music one) and written as the film offset."""
    d = remote.d
    d.cfg.set("sync.offset_s", 0.15)
    d.cfg.set("sync.music_offset_s", 0.1)
    d._ghost_audio_only = True                     # a music ghost is playing
    assert d.offset == 0.1
    d.action("set", {"offset_delta": 0.05})
    s = d.status()["settings"]
    assert (s["offset_s"], s["music_offset_s"]) == (0.2, 0.1)
    d.action("set", {"music_offset_s": 0.25})
    s = d.status()["settings"]
    assert (s["offset_s"], s["music_offset_s"]) == (0.2, 0.25)


def test_tuning_films_while_music_shares_the_offset_leaves_music_where_it_was(remote):
    d = remote.d
    d.cfg.set("sync.offset_s", -0.1)
    d.cfg.set("sync.music_offset_s", None)        # sharing the film offset
    d.action("set", {"offset_s": 0.3})
    s = d.status()["settings"]
    assert (s["offset_s"], s["music_offset_s"]) == (0.3, -0.1)
