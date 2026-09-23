from hueghost.jellyfin import item_display_name, parse_jf_datetime


def test_parse_jf_datetime():
    assert parse_jf_datetime("0001-01-01T00:00:00.0000000Z") is None
    assert parse_jf_datetime(None) is None
    t = parse_jf_datetime("2026-09-19T11:42:18.1881649Z")
    assert abs(t - 1789818138.188165) < 1e-3
    assert parse_jf_datetime("2026-09-19T11:42:18Z") == 1789818138.0
    assert abs(parse_jf_datetime("2026-09-19T13:42:18+02:00") - 1789818138.0) < 1e-6


def test_item_display_name():
    assert item_display_name({"Name": "Movie"}) == "Movie"
    ep = {"Name": "Pilot", "Type": "Episode", "SeriesName": "Show", "ParentIndexNumber": 2, "IndexNumber": 7}
    assert item_display_name(ep) == "Show - S02E07 - Pilot"
    assert item_display_name({"Name": "X", "Type": "Episode", "SeriesName": "S"}) == "S - X"


def test_label_sessions_tells_two_identical_looking_phones_apart():
    """Jellyfin reports most phones as a bare 'Android', so the device name on
    its own is never enough to pick the right one out of a list."""
    from hueghost.jellyfin import label_sessions
    rows = [
        {"device_id": "aaa111", "device_name": "Android", "client": "CAMusic", "user": "Mariam"},
        {"device_id": "bbb222", "device_name": "Android", "client": "CAMusic", "user": "Abdullah"},
        {"device_id": "ccc333", "device_name": "AppleTV", "client": "Swiftfin tvOS", "user": "apple"},
    ]
    label_sessions(rows)
    assert rows[0]["label"] == "Android - CAMusic (Mariam)"
    assert rows[1]["label"] == "Android - CAMusic (Abdullah)"
    assert rows[2]["label"] == "AppleTV - Swiftfin tvOS (apple)"
    assert len({r["label"] for r in rows}) == 3


def test_label_sessions_falls_back_to_the_tail_of_the_device_id():
    """Same device, same app, same user - one stale registration and one live.
    The ids share a long prefix (the Android app changed its scheme), so only
    the end of the string actually distinguishes them."""
    from hueghost.jellyfin import label_sessions
    rows = [
        {"device_id": "fa9d65da834b107fa169025a-c30f-4a91-9ad1-834e8efe9b2a",
         "device_name": "S23", "client": "Jellyfin for Android", "user": "Abdullah"},
        {"device_id": "fa9d65da834b107f",
         "device_name": "S23", "client": "Jellyfin for Android", "user": "Abdullah"},
    ]
    label_sessions(rows)
    assert rows[0]["label"].endswith("[...fe9b2a]")
    assert rows[1]["label"].endswith("[...4b107f]")
    assert rows[0]["label"] != rows[1]["label"]
