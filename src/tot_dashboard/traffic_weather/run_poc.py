#!/usr/bin/env python
"""Phase-0 PoC — weather x traffic semantic-fusion road risk (console).

Ported from flood3's ``run_poc.py``. Reproduces the 5-stage pipeline over a
synthetic road scene (rain up -> vehicles slow/queue):
  (1) Perception  : rainfall provider + detection source (synthetic/YOLO) -> TrafficBehaviorTracker
  (2) Interpretation: VLM Situation Agent (rule fallback by default) -> situation sentence
  (3) Inference   : Semantic Agent (rule/ontology) -> WeatherImpactRisk
  (4) Decision    : Risk & Decision Agent -> 4-level grade + recommendation
  (5) Orchestration: department notification (SOLAPI, dry-run by default) +
      incident briefing report

Run:
  python -m tot_dashboard.traffic_weather.run_poc                    # synthetic (no video/GPU needed)
  python -m tot_dashboard.traffic_weather.run_poc --video road.mp4   # real video (needs ultralytics)

Notification is DRY-RUN unless NOTIFICATION_DRY_RUN=false and full SOLAPI
credentials are set in the environment (see .env.example) — see Phase 4,
docs/integration_plan.md section 5-8.
"""
from __future__ import annotations

import argparse

from ..common.notifier import AlertNotifier, DuplicateNotificationError, NotificationConfigurationError
from .agents.risk_decision import RiskDecisionAgent
from .agents.semantic_agent import SemanticAgent
from .agents.vlm_situation import VlmSituationAgent
from .knowledge.ontology import TRAFFIC_RISK_CATALOG
from .report_generator import build_briefing, save_briefing
from .perception.detection_source import SyntheticDetectionSource, YoloDetectionSource
from .perception.rainfall_provider import MockRainfallProvider
from .perception.traffic_tracker import TrafficBehaviorTracker


def _notify(notifier: AlertNotifier, decision, block: str) -> None:
    """Build a generic alert message from a Decision and send it (dry-run by
    default). Errors (missing config, cooldown) are logged, not raised — a
    console PoC must not crash because notification isn't fully configured."""
    body = f"[{decision.level.value}] {block} — {decision.risk_name}: {decision.recommendation}"
    message = {
        "body": body,
        "severity_label": decision.level.value,
        "location": block,
        "context": decision.context,
        "recommendation": decision.recommendation,
        "video_time": f"{decision.t_sec:.1f}s",
        "risk_score": decision.score,
    }
    try:
        result = notifier.send(
            event_key=f"{block}:{decision.risk_code}", message=message, channels=["sms"]
        )
        status = "dry-run" if result["dry_run"] else "sent"
        print(f"   [통보({status}) -> 도로관리과] {body}")
    except DuplicateNotificationError:
        pass  # within cooldown, silently skip (same as the original gate behavior)
    except NotificationConfigurationError as e:
        print(f"   [통보 생략: {e}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="traffic_weather Phase-0 PoC")
    ap.add_argument("--video", help="path to a real CCTV/recorded video (uses YOLO)")
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=24.0, help="synthetic scenario length (sec)")
    ap.add_argument("--block", default="부산 도심 블록-A")
    ap.add_argument("--bytetrack", action="store_true",
                    help="use supervision ByteTrack (falls back to centroid if not installed)")
    args = ap.parse_args()

    rainfall = MockRainfallProvider()
    if args.video:
        source = YoloDetectionSource(args.video, fps=args.fps)
        frame_wh = source.frame_wh
        in_label = f"real video ({args.video})"
    else:
        source = SyntheticDetectionSource(rainfall, fps=args.fps, duration_sec=args.duration)
        frame_wh = source.frame_wh
        in_label = "synthetic scenario"

    tracker = TrafficBehaviorTracker(fps=args.fps, use_bytetrack=args.bytetrack)
    vlm = VlmSituationAgent(use_vlm=False)
    semantic = SemanticAgent()
    decider = RiskDecisionAgent(TRAFFIC_RISK_CATALOG, alert_min_severity=2)
    notifier = AlertNotifier()
    context = {"block": args.block}
    notify_count = 0

    print("=" * 96)
    print(f" traffic_weather PoC · weather x traffic fusion · input={in_label} · {args.block}")
    print("=" * 96)
    print(f"{'t(s)':>5} | {'강수':>6} {'강도':>5} | "
          f"{'속도(px/s)':>10} {'감소':>5} {'상태':>5} | {'상황':<22} | {'위험':<14} {'등급':<5}")
    print("-" * 96)

    last_level = None
    peak: tuple[int, object, object, object] | None = None
    sample_eps = 0.5 / args.fps
    for t, dets, _frame in source.frames():
        weather = rainfall.at(t)
        metrics = tracker.update(dets, t, frame_wh=frame_wh)
        situation = vlm.interpret(None, metrics, weather)
        risk = semantic.infer(weather, metrics, situation, context)
        decision = decider.decide(risk, t)

        if t == 0 or abs(t - round(t)) < sample_eps:  # sample output at ~1s intervals
            print(f"{t:5.1f} | {weather.rain_mm_h:5.1f}  {weather.intensity.value:>5} | "
                  f"{metrics.mean_speed:10.1f} {metrics.speed_drop * 100:4.0f}% "
                  f"{metrics.state.value:>5} | {situation['situation_ko'][:22]:<22} | "
                  f"{decision.risk_name:<14} {decision.level.value:<5}")

        if decision.level != last_level:  # show grade transitions
            prev = last_level.value if last_level else "—"
            print(f"   >> 위험등급 전이: {prev} -> {decision.level.value} "
                  f"(t={t:.1f}s, {decision.risk_name})")
            last_level = decision.level

        if decision.alert:  # (5) alert -> department notification
            _notify(notifier, decision, args.block)
            notify_count += 1

        if peak is None or (decision.severity, decision.score) > (peak[0], peak[1].score):
            peak = (decision.severity, decision, weather, metrics)

    print("-" * 96)
    print(f"요약 · 통보 시도 {notify_count}건 · "
          f"최고위험 [{peak[1].level.value}] {peak[1].risk_name} (t={peak[1].t_sec:.1f}s)")

    if peak and peak[0] >= 2:  # alert (경계) or worse -> generate report
        md = build_briefing(peak[1], peak[2], peak[3], location=args.block)
        path = save_briefing(md, peak[1])
        print(f"[보고서] 사건 브리핑 생성: {path}")
        print()
        print(md)


if __name__ == "__main__":
    main()
