# tot_dashboard

Unified flood / traffic-weather / crowd-safety monitoring dashboard, merging:

- **underpath_flood_dashboard** (`Sun_YOLO/underpath_flood_dashboard`) — single-camera underpass flood monitoring prototype
- **flood3** — multi-block Busan weather×traffic semantic-fusion road-risk system, with a ported flood/water-segmentation module
- **SAM** — crowd-safety (SAM3) case dashboard + real SOLAPI notification implementation

See [docs/integration_plan.md](docs/integration_plan.md) for the full merge architecture, file-mapping, and phased implementation plan this project was built from — including the review that found and fixed several incorrect assumptions in the original merge plan (section 2/3).

## Status

Phases 0–8 of the plan (section 5-8) are implemented: common utilities, the flood domain (two entry points: a self-contained standalone pipeline and an externally-fed live engine), the traffic_weather domain, the promoted SOLAPI notifier, the crowd (SAM3) domain, the case-archive modules, and the unified FastAPI service are all in place with passing tests. The three original projects (A/B/C) are untouched at their original paths — nothing was deleted from them.

**Known gaps / deliberately deferred:**
- The crowd (SAM3) domain's GPU-dependent code (`crowd/sam3_model.py`, `crowd/sam3_pipeline.py`) could not be executed or tested in this environment (no CUDA GPU) — verify separately on a GPU machine before relying on it.
- The service dashboard's frontend is intentionally simpler than either original UI (no HLS live-video modal, no rich timeline charts) — it proves the merged backend end-to-end rather than replicating every visual feature of flood3's or SAM's dashboards.
- `underpath_flood_dashboard`'s Streamlit UI was not re-implemented as part of the FastAPI service (see integration_plan.md section 9, decision #3) — only its algorithms were ported.
- flood3's standalone `cctv_capture/` viewer app (periodic capture-to-disk + gallery) was not ported; only the two ffmpeg functions the service's `/api/record` endpoint needs were (`common/cctv_capture.py`).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -e ".[dev]"
pytest
```

Optional extras:

```bash
pip install -e ".[dev,legacy-streamlit]"   # + the legacy Streamlit ROI/single-frame tool (not yet re-wired, see gaps above)
pip install -e ".[dev,crowd-gpu]"          # + SAM3 crowd pipeline (needs local CUDA GPU + sam3.pt)
```

### Model weights (not committed to git)

`models/best.pt` (water segmentation, ~20.5MB) and `models/yolo11s.pt` (person/vehicle detection, ~19.3MB) are required for the flood and traffic_weather domains but are excluded from version control (see `.gitignore` and integration_plan.md section 5-7/8). Obtain them from one of the original projects before running anything that does inference:

```bash
cp path/to/Sun_YOLO/underpath_flood_dashboard/models/best.pt models/best.pt
cp path/to/Sun_YOLO/underpath_flood_dashboard/models/yolo11s.pt models/yolo11s.pt
```

For the optional crowd (`crowd-gpu`) domain, place SAM3's ~3.2GB `sam3.pt` checkpoint per `crowd.config.resolve_checkpoint()` (checks `$SAM3_ROOT/sam3.pt`, defaulting to `<repo>/SAM3/sam3.pt`, then the repo root as a fallback — this fallback exists because the original SAM project had the checkpoint at its repo root instead of where its own code expected it; see integration_plan.md sections 2/8).

### Environment variables

Copy `.env.example` to `.env` and fill in what you need — see that file for the full list (Gemini VLM, KMA/HRFCO/ITS/BUSAN external data APIs, SOLAPI SMS/Kakao notification). Everything has a safe default/fallback and the app runs with `.env` absent or empty.

## Running the service

```bash
uvicorn tot_dashboard.service.main:app --port 8000
# or: tot-service   (console script, installed by pip install -e .)
```

Opens a dashboard with two sections: live flood/traffic risk per configured block (`configs/blocks.json`) and a crowd-case viewer (`data/cases/`) with SOLAPI SMS/Kakao dry-run notification buttons.

Other entry points (also installed as console scripts):

```bash
tot-flood-standalone --video path/to/video.mp4   # self-contained single-camera flood pipeline (CLI)
tot-traffic-poc                                   # traffic_weather Phase-0 console PoC (synthetic scenario)
tot-traffic-poc --video path/to/video.mp4         # ... or a real video
tot-crowd-pipeline pipeline --input crowd.mp4     # crowd (SAM3) CLI -- needs the crowd-gpu extra + GPU
```
