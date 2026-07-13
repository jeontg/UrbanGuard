from tot_dashboard.common.config import (
    load_config_simple,
    load_config_with_nested_merge,
)


def test_load_config_simple_missing_file_returns_defaults():
    defaults = {"a": 1, "b": 2}
    assert load_config_simple("configs/does_not_exist.yaml", defaults) == defaults


def test_load_config_simple_merges_flat(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("b: 20\nc: 30\n", encoding="utf-8")
    defaults = {"a": 1, "b": 2}
    merged = load_config_simple(p, defaults)
    assert merged == {"a": 1, "b": 20, "c": 30}


def test_load_config_with_nested_merge_preserves_other_weights(tmp_path):
    p = tmp_path / "risk_config.yaml"
    # only overrides one weight -> the others must survive (this is exactly the
    # bug the flat `defaults.update(loaded)` pattern would introduce: it would
    # replace the whole `weights` dict and silently zero out area/low_point/etc.)
    p.write_text("weights:\n  area: 0.5\n", encoding="utf-8")
    defaults = {
        "weights": {"area": 0.28, "low_point": 0.24, "lane": 0.10},
        "expansion_ref": 0.05,
    }
    merged = load_config_with_nested_merge(p, defaults)
    assert merged["weights"] == {"area": 0.5, "low_point": 0.24, "lane": 0.10}
    assert merged["expansion_ref"] == 0.05
