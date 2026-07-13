"""Verify common/case_archive/catalog.py against the real gwangbokro-demo case
(copied from SAM's data/cases/, docs/integration_plan.md Phase 6)."""
from tot_dashboard.common.case_archive.catalog import CaseCatalog
from tot_dashboard.common.config import PROJECT_ROOT

CASES_ROOT = PROJECT_ROOT / "data" / "cases"


def test_list_cases_finds_the_demo_case():
    catalog = CaseCatalog(CASES_ROOT)
    cases = catalog.list_cases()
    ids = [c["id"] for c in cases]
    assert "gwangbokro-demo" in ids


def test_get_case_resolves_assets_and_builds_sms_messages():
    catalog = CaseCatalog(CASES_ROOT)
    case = catalog.get_case("gwangbokro-demo")
    assert case["title"] == "광복로 군중 안전 분석"
    assert case["available_asset_count"] > 0
    assert case["assets"]["input_image"]["available"] is True
    assert len(case["alerts"]) == 5  # summary.alert_count in the manifest
    assert len(case["sms_messages"]) == len(case["alerts"])
    assert "위험" in case["sms_messages"][0]["body"]


def test_unknown_case_raises_file_not_found():
    catalog = CaseCatalog(CASES_ROOT)
    try:
        catalog.get_case("does-not-exist")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_path_traversal_is_rejected():
    catalog = CaseCatalog(CASES_ROOT)
    try:
        catalog.resolve_media("gwangbokro-demo", "../../etc/passwd")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
