"""Patching the Hue Sync app's own config.json: the "use audio for light
effects" switch has no Public Control command, so it is text-substituted in
the file the app reads at start-up - and the ``.cfg`` digest beside it has to
be kept in step or the app does not see our value."""
import json

import pytest

from hueghost import winutil

# Shaped like the real file: sorted keys, 4-space indent, a "Default": "Video"
# *string* that must not be mistaken for the "Video" object, and nested objects
# inside Games so the brace matching has something to chew on.
SAMPLE = r'''{
    "App": {
        "LastSyncMode": 2,
        "PreferredMonitor": [
            "MONITOR\\MTT1337\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0002"
        ],
        "WithAudio": "decoy at the wrong level"
    },
    "Core": {
        "AppMode": {
            "Default": "Video",
            "Games": {
                "CaptureOption": "Games Default",
                "PreferredAudioDevice": "decoy one level down",
                "Presets": [
                    {"Name": "Subtle", "WithAudio": false},
                    {"Name": "High"}
                ],
                "WithAudio": true
            },
            "Music": {
                "Palettes": [{"Name": "Default"}]
            },
            "Video": {
                "CaptureOption": "Video Default",
                "Default": 0,
                "WithAudio": false
            }
        },
        "AutomaticAudioDevice": true,
        "AutomaticDisplay": true,
        "PreferredAudioDevice": "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}",
        "SyncDelay": 0
    }
}
'''

VDD = r"MONITOR\MTT1337\{4d36e96e-e325-11ce-bfc1-08002be10318}\0002"
REAL = r"MONITOR\DELA212\{4d36e96e-e325-11ce-bfc1-08002be10318}\0001"


@pytest.fixture
def appdir(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(SAMPLE, encoding="utf-8", newline="")
    (tmp_path / ".cfg").write_text(str(winutil._fnv1a64(SAMPLE.encode("utf-8"))), encoding="ascii")
    monkeypatch.setattr(winutil, "hue_sync_dir", lambda: str(tmp_path))
    return tmp_path


def read(appdir):
    return (appdir / "config.json").read_text(encoding="utf-8")


def digest(appdir):
    return (appdir / ".cfg").read_text(encoding="ascii").strip()


def test_reads_the_switch_of_each_mode(appdir):
    assert winutil.hue_sync_with_audio("video") is False
    assert winutil.hue_sync_with_audio("games") is True
    assert winutil.hue_sync_with_audio("music") is None      # music mode has no switch
    assert winutil.hue_sync_info()["with_audio"] == {"video": False, "games": True}


def test_write_flips_one_token_and_leaves_the_rest_byte_identical(appdir):
    before = read(appdir)
    assert winutil.hue_sync_write_with_audio("video", True) is True
    after = read(appdir)
    assert after == before.replace('"Default": 0,\n                "WithAudio": false',
                                   '"Default": 0,\n                "WithAudio": true')
    assert winutil.hue_sync_with_audio("video") is True
    assert winutil.hue_sync_with_audio("games") is True      # untouched
    assert json.loads(after)["App"]["WithAudio"] == "decoy at the wrong level"


def test_write_keeps_the_hash_file_in_step(appdir):
    winutil.hue_sync_write_with_audio("games", False)
    assert digest(appdir) == str(winutil._fnv1a64(read(appdir).encode("utf-8")))
    assert winutil.hue_sync_with_audio("games") is False


def test_writing_the_value_it_already_has_changes_nothing(appdir):
    before, before_hash = read(appdir), digest(appdir)
    assert winutil.hue_sync_write_with_audio("games", True) is False
    assert read(appdir) == before and digest(appdir) == before_hash


def test_music_has_no_audio_switch(appdir):
    with pytest.raises(ValueError):
        winutil.hue_sync_write_with_audio("music", True)


def test_missing_key_is_reported_not_guessed(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"Core": {"AppMode": {"Video": {"Default": 0}}}}', encoding="utf-8")
    monkeypatch.setattr(winutil, "hue_sync_dir", lambda: str(tmp_path))
    assert winutil.hue_sync_with_audio("video") is None
    with pytest.raises(RuntimeError):
        winutil.hue_sync_write_with_audio("video", True)


def test_reads_the_capture_display_and_the_music_input(appdir):
    # the stored value is JSON-escaped; callers want the real device id back
    assert winutil.hue_sync_preferred_monitor() == VDD
    assert winutil.hue_sync_audio_device() == "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}"


def test_switching_the_capture_display_also_pins_it(appdir):
    before = read(appdir)
    assert winutil.hue_sync_write_preferred_monitor(REAL) is True
    after = read(appdir)
    # exactly two tokens moved: the device id and the automatic flag
    assert after == before.replace(VDD.replace("\\", "\\\\"), REAL.replace("\\", "\\\\")) \
                          .replace('"AutomaticDisplay": true', '"AutomaticDisplay": false')
    assert json.loads(after)["App"]["PreferredMonitor"] == [REAL]
    assert winutil.hue_sync_preferred_monitor() == REAL
    assert digest(appdir) == str(winutil._fnv1a64(after.encode("utf-8")))


def test_audio_device_write_ignores_the_decoy_one_level_down(appdir):
    want = "{0.0.0.00000000}.{bbbbbbbb-0000-0000-0000-000000000000}"
    assert winutil.hue_sync_write_audio_device(want) is True
    after = json.loads(read(appdir))
    assert after["Core"]["PreferredAudioDevice"] == want
    assert after["Core"]["AppMode"]["Games"]["PreferredAudioDevice"] == "decoy one level down"
    assert after["Core"]["AutomaticAudioDevice"] is False


def test_auto_hands_the_choice_back_to_the_app(appdir):
    winutil.hue_sync_write_audio_device("{0.0.0.00000000}.{cccccccc-0000-0000-0000-000000000000}")
    assert json.loads(read(appdir))["Core"]["AutomaticAudioDevice"] is False
    # a PC-music session wants the app's own default output again
    assert winutil.hue_sync_patch(audio_device=winutil.AUTO) is True
    after = json.loads(read(appdir))
    assert after["Core"]["AutomaticAudioDevice"] is True
    assert after["Core"]["PreferredAudioDevice"] == "{0.0.0.00000000}.{cccccccc-0000-0000-0000-000000000000}"


def test_one_restart_means_one_write_and_one_digest(appdir):
    assert winutil.hue_sync_patch(with_audio=("video", True), monitor=REAL,
                                  audio_device="{0.0.0.00000000}.{dddddddd-0000-0000-0000-000000000000}") is True
    after = read(appdir)
    parsed = json.loads(after)
    assert parsed["Core"]["AppMode"]["Video"]["WithAudio"] is True
    assert parsed["App"]["PreferredMonitor"] == [REAL]
    assert parsed["Core"]["PreferredAudioDevice"].endswith("{dddddddd-0000-0000-0000-000000000000}")
    assert parsed["Core"]["AutomaticDisplay"] is False and parsed["Core"]["AutomaticAudioDevice"] is False
    assert digest(appdir) == str(winutil._fnv1a64(after.encode("utf-8")))


def test_pinning_the_display_it_already_shows_still_turns_automatic_off(appdir):
    # same device id, but the app was picking its own - that flag is the whole point
    assert winutil.hue_sync_patch(monitor=VDD) is True
    assert json.loads(read(appdir))["Core"]["AutomaticDisplay"] is False
    assert winutil.hue_sync_preferred_monitor() == VDD


def test_patching_values_it_already_has_changes_nothing(appdir):
    winutil.hue_sync_patch(monitor=VDD)                  # settle: id pinned, automatic off
    before, before_hash = read(appdir), digest(appdir)
    assert winutil.hue_sync_patch(monitor=VDD) is False
    assert read(appdir) == before and digest(appdir) == before_hash


def test_missing_display_key_is_reported_not_guessed(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"App": {"LastSyncMode": 2}, "Core": {"SyncDelay": 0}}',
                                          encoding="utf-8")
    monkeypatch.setattr(winutil, "hue_sync_dir", lambda: str(tmp_path))
    assert winutil.hue_sync_preferred_monitor() is None
    with pytest.raises(RuntimeError):
        winutil.hue_sync_write_preferred_monitor(REAL)


def test_fnv1a64_matches_the_apps_digest():
    # std::hash<std::string> on MSVC = FNV-1a 64; the app prints it unsigned
    assert winutil._fnv1a64(b"") == 0xCBF29CE484222325        # offset basis
    assert winutil._fnv1a64(b"a") == 0xAF63DC4C8601EC8C
    assert winutil._fnv1a64(b"foobar") == 0x85944171F73967E8
