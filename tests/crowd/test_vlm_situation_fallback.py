"""Only the rule-based fallback path is testable without a Gemini API key —
which is the normal/expected state for most environments this repo runs in.
"""
from tot_dashboard.crowd.vlm_situation import vlm_interpret


def _behavior(surge=1.0, dispersion=0.0, divergence=0.0):
    return {"mean_speed": 5.0, "surge": surge, "dispersion": dispersion, "divergence": divergence}


def test_fallback_used_when_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ko, source = vlm_interpret(None, {"count": 3, "density_pct": 10.0}, _behavior(), location="테스트구역")
    assert source == "rule-based"
    assert "테스트구역" in ko


def test_fallback_escalates_to_serious_on_panic_signals(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from tot_dashboard.crowd import vlm_situation

    ko, source = vlm_interpret(
        None, {"count": 50, "density_pct": 70.0},
        _behavior(surge=1.5, divergence=8.0), location="테스트구역",
    )
    assert source == "rule-based"
    assert vlm_situation.LAST_VLM["level"] == "심각"
