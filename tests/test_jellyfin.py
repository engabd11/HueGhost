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
