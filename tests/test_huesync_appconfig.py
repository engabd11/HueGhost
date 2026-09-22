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
SAMPLE = '''{
    "App": {
        "LastSyncMode": 2,
        "WithAudio": "decoy at the wrong level"
    },
    "Core": {
        "AppMode": {
            "Default": "Video",
            "Games": {
                "CaptureOption": "Games Default",
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
        "SyncDelay": 0
    }
}
'''


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


def test_fnv1a64_matches_the_apps_digest():
    # std::hash<std::string> on MSVC = FNV-1a 64; the app prints it unsigned
    assert winutil._fnv1a64(b"") == 0xCBF29CE484222325        # offset basis
    assert winutil._fnv1a64(b"a") == 0xAF63DC4C8601EC8C
    assert winutil._fnv1a64(b"foobar") == 0x85944171F73967E8
