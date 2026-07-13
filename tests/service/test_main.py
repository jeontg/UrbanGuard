"""End-to-end check of the unified FastAPI service (docs/integration_plan.md
Phase 7): flood/traffic_weather multi-block live risk (from flood3) and
crowd case viewer + notifications (from SAM) served by ONE app.

Uses a synthetic-only blocks.json (TOT_BLOCKS_PATH) so this test never
connects to flood3's live Busan HLS streams.

One shared, module-scoped TestClient is used across all tests: ``main.py``'s
module-level ``runner`` (a ``threading.Thread``) can only be started once per
process — same as the original flood3 app, which assumes a single uvicorn
process lifetime — so each test entering/exiting its own ``TestClient``
context (which runs the FastAPI lifespan startup/shutdown) would call
``runner.start()`` more than once and crash.
"""
import os
import time
from pathlib import Path

import pytest

os.environ["TOT_BLOCKS_PATH"] = str(Path(__file__).parent / "fixtures" / "blocks_synthetic.json")
os.environ.setdefault("NOTIFICATION_DRY_RUN", "true")
os.environ.setdefault("ALERT_RECIPIENTS", "01012345678")

from fastapi.testclient import TestClient  # noqa: E402

from tot_dashboard.service.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_reports_both_domains(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["block_count"] == 1
    assert data["case_count"] >= 1  # gwangbokro-demo, copied in Phase 6
    assert "notifications" in data


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "tot_dashboard" in res.text


def test_flood_blocks_and_risk_endpoints(client):
    res = client.get("/api/blocks")
    assert res.status_code == 200
    assert res.json()["blocks"][0]["block_id"] == "BLOCK-TEST"

    # give the background PipelineRunner a moment to produce a snapshot
    for _ in range(20):
        risk = client.get("/api/risk").json()
        if risk["blocks"]:
            break
        time.sleep(0.3)
    else:
        assert False, "PipelineRunner never produced a risk snapshot"

    block = risk["blocks"][0]
    assert block["block_id"] == "BLOCK-TEST"
    assert block["level"] in ("관심", "주의", "경계", "심각")

    detail = client.get("/api/risk/BLOCK-TEST").json()
    assert "history" in detail

    history = client.get("/api/history").json()
    assert "BLOCK-TEST" in history["blocks"]


def test_crowd_case_list_and_detail_and_media(client):
    cases = client.get("/api/cases").json()
    ids = [c["id"] for c in cases["cases"]]
    assert "gwangbokro-demo" in ids

    case = client.get("/api/cases/gwangbokro-demo").json()
    assert case["available_asset_count"] > 0

    media_path = case["assets"]["input_image"]["path"]
    media_res = client.get(f"/media/gwangbokro-demo/{media_path}")
    assert media_res.status_code == 200

    missing = client.get("/api/cases/does-not-exist")
    assert missing.status_code == 404


def test_crowd_notification_dry_run_send(client):
    case = client.get("/api/cases/gwangbokro-demo").json()
    message_id = case["sms_messages"][0]["id"]
    res = client.post(
        "/api/cases/gwangbokro-demo/notifications",
        json={"message_id": message_id, "channels": ["sms"]},
    )
    assert res.status_code == 200
    assert res.json()["dry_run"] is True


def test_flood_run_archived_by_standalone_pipeline_is_visible_in_dashboard(client):
    """Reproduces the user-reported gap: running ``tot-flood-standalone
    --video ...`` must produce something visible in the dashboard, with the
    ROI/water/detection overlay actually drawn into the saved video/frames.
    """
    from tot_dashboard.common.case_archive.run_writer import RunWriter
    from tot_dashboard.common.config import PROJECT_ROOT
    from tot_dashboard.flood.standalone_pipeline import Pipeline, process_run

    sample_video = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
    pipeline = Pipeline.from_config()
    source = {"type": "video", "path": str(sample_video)}
    writer = None
    for _, writer in process_run(pipeline, source, save=True, writer_factory=RunWriter):
        pass
    summary = writer.finalize()

    listing = client.get("/api/flood-runs").json()
    run_ids = [r["run_id"] for r in listing["runs"]]
    assert summary["run_id"] in run_ids

    detail = client.get(f"/api/flood-runs/{summary['run_id']}").json()
    assert detail["video_url"] == f"/media/flood-runs/{summary['run_id']}/processed_video.mp4"
    assert len(detail["metrics"]) == summary["frames"]

    video_res = client.get(detail["video_url"])
    assert video_res.status_code == 200
    assert video_res.headers["content-type"].startswith("video/")

    # path traversal must still be rejected
    escape = client.get(f"/media/flood-runs/{summary['run_id']}/../../../pyproject.toml")
    assert escape.status_code == 404
