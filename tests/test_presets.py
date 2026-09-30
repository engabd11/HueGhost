"""Every Quick add preset is a binding that works as soon as it is added."""
from hueghost.config import DETECT_MODES, MODES, Config
from hueghost.presets import PRESETS


def test_presets_are_valid_bindings():
    names = [p["name"] for p in PRESETS]
    assert len(names) == len(set(names))
    for p in PRESETS:
        assert p["mode"] in MODES and p["detect"] in DETECT_MODES and p["hint"]
        cfg = Config({"sources": [dict(p, source="pc")]})
        b = cfg.bindings()[0]
        assert cfg.binding_problems(b) == [], p["name"]
        assert (b["mode"], b["detect"]) == (p["mode"], p["detect"])


def test_all_presets_fit_in_one_list():
    cfg = Config({})
    cfg.set_bindings([dict(p, source="pc") for p in PRESETS])
    assert len(cfg.bindings()) == len(PRESETS)
    assert cfg.problems() == []
