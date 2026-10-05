import pytest


@pytest.fixture(autouse=True)
def _pinned_audio_outputs_stay_put(monkeypatch):
    """resolve_audio_output reads the REAL audio endpoints of the PC running the
    tests; a dev box whose endpoint ids match a test's constant would see them
    swapped. Tests that want the swap patch it themselves."""
    from hueghost.sources import jellyfin, pc

    monkeypatch.setattr(jellyfin, "resolve_audio_output", lambda e: e)
    monkeypatch.setattr(pc, "resolve_audio_output", lambda e: e)
