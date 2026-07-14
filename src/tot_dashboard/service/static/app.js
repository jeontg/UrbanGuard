// tot_dashboard dashboard frontend.
//
// "도로위험 블록" 탭은 flood3의 원본 service/static/app.js를 그대로 이식한
// 것이다(docs/integration_plan.md 참고) -- 카드 그리드, 강수 상황판, 교차로별
// 타임라인, 실시간 CCTV 모달(hls.js), 사건 브리핑 모달까지 동일하다. 유일한
// 추가 기능은 실시간 CCTV 모달에 ROI(도로/저지대/차선기준선) SVG 오버레이를
// 그리는 것 -- 원본 flood3에는 없던 기능이다(사용자 요청으로 추가).
//
// "군중안전 사례"/"분석 결과" 탭은 flood3/SAM에 없던 이번 통합 프로젝트의
// 신규 화면이라 별도로 작성됐다.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function initTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
    });
  });
}

// ---- 도로위험 블록 (flood3 원본 이식) --------------------------------------
const LEVEL_COLOR = { "관심": "#3fb950", "주의": "#d4a017", "경계": "#e8843b", "심각": "#e5484d" };
const colorFor = (lvl) => LEVEL_COLOR[lvl] || "#6e7681";
const HEAVY = (b) => b.intensity === "강함" || b.intensity === "매우강함";

// 실시간 CCTV 모달 (hls.js로 라이브 HLS 직접 재생)
let _hls = null;
let _liveBlock = null;
function openLive(id, name, url) {
  _liveBlock = { id, name, url };
  const modal = document.getElementById("livemodal");
  const video = document.getElementById("livevideo");
  const err = document.getElementById("liveerr");
  err.textContent = "";
  document.getElementById("livetitle").textContent = name + " · 실시간 CCTV";
  modal.classList.remove("hidden");
  if (_hls) { _hls.destroy(); _hls = null; }
  if (window.Hls && window.Hls.isSupported()) {
    _hls = new Hls({ liveSyncDurationCount: 3 });
    _hls.loadSource(url);
    _hls.attachMedia(video);
    _hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    _hls.on(Hls.Events.ERROR, (ev, d) => {
      if (d && d.fatal) err.textContent = "재생 오류: " + d.type + " (" + (d.details || "") + ")";
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url; video.play().catch(() => {});   // Safari 등 네이티브 HLS
  } else {
    err.textContent = "이 브라우저는 HLS를 지원하지 않습니다.";
  }
  loadRoiOverlay(id);
}
function closeLive() {
  const video = document.getElementById("livevideo");
  document.getElementById("livemodal").classList.add("hidden");
  try { video.pause(); } catch (e) { /* noop */ }
  video.removeAttribute("src");
  if (_hls) { _hls.destroy(); _hls = null; }
  document.getElementById("liveerr").textContent = "";
  const rs = document.getElementById("recstat"); if (rs) rs.textContent = "";
  const rb = document.getElementById("recbtn"); if (rb) rb.disabled = false;
  const roi = document.getElementById("liveroi"); if (roi) roi.innerHTML = "";
  const note = document.getElementById("roi-note"); if (note) note.textContent = "";
}

// ROI(도로/저지대/차선기준선) 오버레이 -- flood3 원본에는 없던 추가 기능.
// SVG의 viewBox를 ROI 저장 당시의 frame_width/height로 맞추면, 비디오가
// 실제로 어떤 크기로 렌더링되든(반응형) 좌표를 그대로 써도 자동으로 맞는다.
async function loadRoiOverlay(blockId) {
  const svg = document.getElementById("liveroi");
  const note = document.getElementById("roi-note");
  svg.innerHTML = "";
  note.textContent = "";
  try {
    const roi = await (await fetch(`/api/roi/${encodeURIComponent(blockId)}`)).json();
    if (roi.error || !roi.frame_width) {
      note.textContent = "⚠ 이 블록은 ROI가 설정되어 있지 않습니다.";
      return;
    }
    svg.setAttribute("viewBox", `0 0 ${roi.frame_width} ${roi.frame_height}`);
    const ns = "http://www.w3.org/2000/svg";
    const drawPolys = (polys, color) => {
      (polys || []).forEach((pts) => {
        if (!pts || pts.length < 3) return;
        const poly = document.createElementNS(ns, "polygon");
        poly.setAttribute("points", pts.map((p) => p.join(",")).join(" "));
        poly.setAttribute("fill", color + "33");
        poly.setAttribute("stroke", color);
        poly.setAttribute("stroke-width", "3");
        svg.appendChild(poly);
      });
    };
    drawPolys(roi.road_roi, "#3fb950");
    drawPolys(roi.low_point_roi, "#e5484d");
    if (roi.lane_threshold_line && roi.lane_threshold_line.length === 2) {
      const [p1, p2] = roi.lane_threshold_line;
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", p1[0]); line.setAttribute("y1", p1[1]);
      line.setAttribute("x2", p2[0]); line.setAttribute("y2", p2[1]);
      line.setAttribute("stroke", "#58a6ff");
      line.setAttribute("stroke-width", "3");
      line.setAttribute("stroke-dasharray", "10,6");
      svg.appendChild(line);
    }
    if (!roi.road_roi || !roi.road_roi.length) {
      note.textContent = "⚠ 도로 ROI 미설정 — 프레임 전체 기준(정확도 낮음)";
    }
  } catch (e) {
    note.textContent = "ROI를 불러오지 못했습니다.";
    console.error("ROI overlay load failed", e);
  }
}

async function recordLive() {
  if (!_liveBlock) return;
  const sec = Math.max(3, Math.min(parseInt(document.getElementById("recsec").value) || 10, 30));
  const btn = document.getElementById("recbtn"), stat = document.getElementById("recstat");
  btn.disabled = true;
  stat.textContent = `⏺ 녹화 중 (${sec}초)... 완료까지 잠시 기다려 주세요`;
  try {
    const r = await fetch(`/api/record/${_liveBlock.id}?seconds=${sec}`);
    const ct = r.headers.get("content-type") || "";
    if (!r.ok || !ct.includes("video")) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || ("HTTP " + r.status));
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${_liveBlock.name}_${sec}s.mp4`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 8000);
    stat.textContent = "✓ 저장 완료 (" + Math.round(blob.size / 1024) + " KB)";
  } catch (e) {
    stat.textContent = "저장 오류: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// 사건 브리핑 보고서 모달 -- 「종합 판단」 서술만 Gemini, 등급·수치·근거·권고는 규칙 고정
let _report = null;
async function openReport(id, name) {
  const modal = document.getElementById("reportmodal");
  const body = document.getElementById("reportbody");
  const src = document.getElementById("reportsrc");
  document.getElementById("reporttitle").textContent = name + " · 사건 브리핑";
  src.textContent = "";
  body.textContent = "보고서 생성 중… (AI 서술 시 수 초 소요)";
  modal.classList.remove("hidden");
  _report = null;
  try {
    const j = await (await fetch(`/api/report/${id}`)).json();
    if (j.error) throw new Error(j.error);
    _report = j;
    body.textContent = j.markdown;
    src.innerHTML = j.narrative_source === "gemini-vlm"
      ? '「종합 판단」 서술: <span class="src-real">AI(Gemini VLM)</span> · 등급·수치·근거·권고는 규칙 고정'
      : '「종합 판단」 서술: <span class="src-mock">규칙 폴백</span> (GEMINI_API_KEY 미설정) · 등급·수치·근거·권고는 규칙 고정';
  } catch (e) {
    body.textContent = "보고서 생성 오류: " + e.message;
  }
}
function closeReport() {
  document.getElementById("reportmodal").classList.add("hidden");
  _report = null;
}
function downloadReport() {
  if (!_report) return;
  const blob = new Blob([_report.markdown], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = _report.filename || "briefing.md";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 8000);
}

function objRows(objs) {
  return objs.slice(0, 12).map(o => {
    const dc = o.drop > 0.5 ? "#e5484d" : o.drop > 0.2 ? "#e8843b" : "#3fb950";
    return `<div class="obj"><span class="oid">#${o.id}${o.stalled ? " ⏹" : ""}</span>`
      + `<span class="ospd" title="${o.speed}px/s">${o.kmh != null ? o.kmh + "km/h" : o.speed.toFixed(0) + "px/s"}</span>`
      + `<span class="obar"><span style="width:${Math.min(o.drop * 100, 100)}%;background:${dc}"></span></span>`
      + `<span class="odrop">${(o.drop * 100).toFixed(0)}%</span></div>`;
  }).join("");
}

const GRADE_COLOR = { 1: "#3fb950", 2: "#79c0ff", 3: "#d4a017", 4: "#e8843b", 5: "#e5484d" };
const TREND_KO = { surge: "급상승", rising: "상승", stable: "유지", falling: "하강", insufficient: "정보 부족" };

function floodSection(b) {
  if (!b.water_available) {
    return `<div class="flood"><div class="flood-head"><span class="ftitle">침수 위험도(물 세그멘테이션)</span></div>
      <div class="flood-na">라이브 영상 연결 대기 중 — 합성/폴백 소스에서는 물 세그멘테이션을 실행하지 않습니다.</div></div>`;
  }
  const gc = GRADE_COLOR[b.flood_risk_grade] || "#6e7681";
  const trendCls = "trend-" + (b.flood_pred_trend || "stable");
  const roiNote = b.water_roi_defined ? "" : `<div class="flood-noroi">⚠ 도로 ROI 미설정 — 프레임 전체 기준(정확도 낮음). scripts/roi_editor.py로 캘리브레이션 필요</div>`;
  const eta = (sec) => sec == null ? "—" : (sec <= 0 ? "이미 도달" : `${sec.toFixed(0)}초 후`);
  return `<div class="flood">
    <div class="flood-head"><span class="ftitle">침수 위험도(물 세그멘테이션 · RiskEngine)</span></div>
    <div class="gauge">
      <span class="gauge-score" style="color:${gc}">${b.flood_risk_score.toFixed(0)}</span>
      <span class="gauge-grade" style="background:${gc}">등급 ${b.flood_risk_grade} · ${b.flood_risk_grade_label}</span>
      <span class="gauge-bar"><span style="width:${Math.min(b.flood_risk_score, 100)}%;background:${gc}"></span></span>
    </div>
    <div class="flood-metrics">
      <div><span class="mk">물 면적 비율(도로ROI)</span><b>${(b.water_area_ratio * 100).toFixed(2)}%</b></div>
      <div><span class="mk">확산(3초창)</span><b>${(b.water_expansion_rate * 100).toFixed(2)}%/초</b></div>
      <div><span class="mk">바퀴 침수 차량</span><b>${b.vehicles_tire_in_water}대</b></div>
      <div><span class="mk">위험 보행자</span><b>${b.persons_in_danger}명</b></div>
    </div>
    <div class="flood-reason">주요 근거: ${b.flood_risk_top_reason}</div>
    ${roiNote}
    <div class="flood-pred">예측(+10초): 물비율 <b>${(b.flood_pred_ratio_10s * 100).toFixed(2)}%</b> ·
      위험도 <b>${b.flood_pred_risk_10s.toFixed(0)}</b> ·
      추세 <span class="${trendCls}">${TREND_KO[b.flood_pred_trend] || b.flood_pred_trend}</span> ·
      위험(danger) ETA <b>${eta(b.flood_eta_danger_sec)}</b></div>
  </div>`;
}

function srcLabel(s) {
  if (!s) return { t: "없음", cls: "" };
  if (s.startsWith("mock")) return { t: "모의(mock)", cls: "src-mock" };
  if (s === "kma") return { t: "기상청 실측", cls: "src-real" };
  if (s === "hrfco") return { t: "홍수통제소 실측", cls: "src-real" };
  return { t: s, cls: "" };
}

function card(b) {
  const c = colorFor(b.level);
  const rs = srcLabel(b.rain_source), vs = srcLabel(b.river_source);
  const trend = (b.rain_trend && b.rain_trend !== "유지")
    ? `<span class="trend">${b.rain_trend === "증가" ? "▲" : "▼"}${b.rain_trend}</span>` : "";
  const objs = b.objects || [];
  const objHtml = objs.length ? objRows(objs)
    : `<div class="muted" style="font-size:12px">추적 차량 없음 (스트림 연결/검출 대기)</div>`;
  return `<div class="card ${HEAVY(b) ? "heavy" : ""}">
    <div class="chead"><span class="name">${b.name}</span>
      <span class="chead-r">${b.stream_type === "hls" && b.stream_url ? `<button class="livebtn" onclick="openLive('${b.block_id}','${b.name}','${b.stream_url}')">▶ 실시간</button>` : ""}<button class="repbtn" onclick="openReport('${b.block_id}','${b.name}')">📄 보고서</button><span class="chip" style="background:${c}">${b.level}</span></span></div>
    <div class="risk">${b.risk_name} <span class="tag">${b.risk_code}</span> · ${b.situation_ko || ""}</div>
    <div class="metrics">
      <div><span class="mk">강수</span><b>${b.rain_mm_h.toFixed(0)}</b>mm/h ${b.intensity} ${trend}</div>
      <div><span class="mk">평균속도</span><b>${b.mean_speed_kmh != null ? b.mean_speed_kmh : "—"}</b>km/h (${b.mean_speed.toFixed(0)}px/s)</div>
      <div><span class="mk">감속</span><b style="color:${c}">${(b.speed_drop * 100).toFixed(0)}%</b></div>
      <div><span class="mk">차량수</span><b>${b.n_vehicles}</b>대</div>
      <div><span class="mk">정체/정지</span><b>${b.queue_len}/${b.stalled}</b></div>
      <div><span class="mk">상태</span><b>${b.state}</b></div>
    </div>
    <div class="srcline">데이터출처 · 강수 <span class="${rs.cls}">${rs.t}</span> · 하천 <span class="${vs.cls}">${vs.t}</span></div>
    <div class="ohdr">차량 객체 탐지 · 속도/감속 (id : 속도 / 감속)</div>
    <div class="objs">${objHtml}</div>
    <div class="rec">권고(④) · ${b.recommendation}</div>
    ${floodSection(b)}
  </div>`;
}

function rainbar(blocks) {
  const top = [...blocks].sort((a, b) => b.rain_mm_h - a.rain_mm_h)[0] || {};
  const heavy = blocks.some(HEAVY);
  const el = document.getElementById("rainbar");
  el.className = heavy ? "rainbar heavy" : "rainbar";
  const allMock = blocks.length && blocks.every(b => (b.rain_source || "").startsWith("mock"));
  const srcNote = allMock
    ? `<div class="srcwarn">⚠ 현재 <b>강수·하천은 모의(mock) 데이터</b>입니다 — 실제로 비가 와서가 아닙니다. 실측은 기상청(KMA)·홍수통제소(HRFCO) 키 연동 시 표시됩니다. 단, <b>침수 위험도(카드 하단)는 실제 CCTV 프레임에 물 세그멘테이션을 실행한 결과</b>입니다.</div>`
    : `<div class="srcok">✓ 실측 데이터 연동됨</div>`;
  const judge = `<div class="judge">※ <b>도로 침수/잠김 판별</b>: 기존 방식(강수 × 차량 감속·정지 × 하천 수위 의미 융합, 위 카드 상단)에 더해, <b>YOLO11-seg 물 세그멘테이션</b>으로 CCTV 프레임에서 직접 물 영역을 검출해 도로 ROI 내 면적·확산·위험도(RiskEngine)·예측(RiskPredictor)을 계산합니다(카드 하단 "침수 위험도" 섹션).</div>`;
  const body = heavy
    ? `<div class="rtitle">⚠ 호우 감지 — 최대 강수 <b>${top.name} ${top.rain_mm_h.toFixed(0)}mm/h</b>`
      + ` (${top.intensity}${top.rain_trend === "증가" ? ", ▲증가" : ""})</div>`
    : `<div class="rtitle">강수 모니터링 — 최대 `
      + `${top.rain_mm_h ? top.name + " " + top.rain_mm_h.toFixed(0) + "mm/h (" + top.intensity + ")" : "정상"}</div>`;
  el.innerHTML = srcNote + body + judge;
}

function renderTimelines(hist) {
  const SC = ["#3fb950", "#d4a017", "#e8843b", "#e5484d"];
  document.getElementById("timelines").innerHTML = Object.keys(hist).map(bid => {
    const h = hist[bid], pts = h.points || [];
    const bars = pts.map(p =>
      `<span style="background:${SC[p.severity] || "#6e7681"}" title="t=${p.t_sec}s · sev=${p.severity}${p.flood_risk_score != null ? ' · 침수위험도=' + p.flood_risk_score : ''}"></span>`).join("");
    return `<div class="tl"><span class="tlname">${h.name}</span><span class="tlbars">${bars}</span></div>`;
  }).join("");
}

async function tick() {
  try {
    const data = await (await fetch("/api/risk")).json();
    const blocks = (data.blocks || []).sort((a, b) => b.severity - a.severity);
    const allMock = blocks.length && blocks.every(b => (b.rain_source || "").startsWith("mock"));
    const modeEl = document.getElementById("mode");
    if (modeEl) modeEl.innerHTML = allMock
      ? '<span class="bmock">● 데모(모의 데이터)</span>' : '<span class="breal">● 실측 연동</span>';
    document.getElementById("grid").innerHTML = blocks.map(card).join("");
    rainbar(blocks);
    const hist = await (await fetch("/api/history")).json();
    renderTimelines(hist.blocks || {});
    document.getElementById("status").textContent = `실시간 · 갱신 ${new Date().toLocaleTimeString("ko-KR")}`;
    document.getElementById("dot").style.background = "#3fb950";
  } catch (e) {
    document.getElementById("status").textContent = "연결 끊김 — 재시도";
    document.getElementById("dot").style.background = "#e5484d";
  }
}

// ---- Crowd cases -----------------------------------------------------------
async function loadCaseList() {
  const container = $("#case-list");
  const res = await fetch("/api/cases");
  const data = await res.json();
  container.innerHTML = "";
  for (const c of data.cases || []) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h3>${c.title}</h3>
      <div class="row">위치 <strong>${c.location}</strong></div>
      <div class="row">최고 심각도 <strong>${c.highest_severity}</strong></div>
      <div class="row">경보 건수 <strong>${c.alert_count}</strong></div>
    `;
    card.addEventListener("click", () => loadCaseDetail(c.id));
    container.appendChild(card);
  }
}

async function loadCaseDetail(caseId) {
  const detail = $("#case-detail");
  detail.innerHTML = '<p class="hint">불러오는 중...</p>';
  const res = await fetch(`/api/cases/${encodeURIComponent(caseId)}`);
  if (!res.ok) {
    detail.innerHTML = '<p class="hint">사례를 불러오지 못했습니다.</p>';
    return;
  }
  const c = await res.json();
  const assetEntries = Object.entries(c.assets || {}).filter(([, a]) => a.available);
  const assetsHtml = assetEntries
    .map(([key, a]) => {
      const isVideo = a.kind === "video";
      const media = isVideo
        ? `<video src="${a.url}" controls></video>`
        : a.kind === "image"
        ? `<img src="${a.url}" alt="${a.label}">`
        : "";
      return `<div>${media}<div class="asset-label">${a.label}</div></div>`;
    })
    .join("");

  const alertsHtml = (c.sms_messages || [])
    .map(
      (m) => `
      <div class="alert-row" data-message-id="${m.id}">
        <span>[${m.severity_label}] ${m.location} · ${m.video_time}</span>
        <button onclick="sendNotification('${c.id}', '${m.id}', this)">SMS 발송(dry-run)</button>
      </div>`
    )
    .join("") || '<p class="hint">경보 없음</p>';

  detail.innerHTML = `
    <h2>${c.title}</h2>
    <p>${c.description || ""}</p>
    <div class="asset-grid">${assetsHtml}</div>
    <h3>경보 / 알림</h3>
    ${alertsHtml}
    <p class="status-line">사용 가능한 자산 ${c.available_asset_count}개</p>
  `;
}

// ---- Flood standalone-pipeline runs (tot-flood-standalone --video ...) ----
async function loadRunList() {
  const container = $("#run-list");
  const res = await fetch("/api/flood-runs");
  const data = await res.json();
  container.innerHTML = "";
  const runs = data.runs || [];
  if (!runs.length) {
    container.innerHTML = '<p class="hint">아직 분석 결과가 없습니다. tot-flood-standalone --video ... 로 실행해 보세요.</p>';
    return;
  }
  for (const r of runs) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h3>${r.run_id}</h3>
      <div class="row">입력 <strong>${r.source_name}</strong></div>
      <div class="row">프레임 수 <strong>${r.frames}</strong></div>
      <div class="row">최고 경보 <strong>${r.max_alert_level}</strong></div>
    `;
    card.addEventListener("click", () => loadRunDetail(r.run_id));
    container.appendChild(card);
  }
}

async function loadRunDetail(runId) {
  const detail = $("#run-detail");
  detail.innerHTML = '<p class="hint">불러오는 중...</p>';
  const res = await fetch(`/api/flood-runs/${encodeURIComponent(runId)}`);
  if (!res.ok) {
    detail.innerHTML = '<p class="hint">분석 결과를 불러오지 못했습니다.</p>';
    return;
  }
  const r = await res.json();
  const videoHtml = r.video_url
    ? `<video src="${r.video_url}" controls style="max-width:100%"></video>`
    : '<p class="hint">주석 영상 없음</p>';

  const alertsHtml = (r.alerts || [])
    .map(
      (a) => `<div class="alert-row"><span>t=${a.timestamp_sec}s · 레벨 ${a.alert_level} · ${a.alert_reason}</span></div>`
    )
    .join("") || '<p class="hint">경보 이벤트 없음</p>';

  const lastMetric = (r.metrics || [])[r.metrics.length - 1];
  const summaryHtml = lastMetric
    ? `<div class="row">최종 물 비율 <strong>${(lastMetric.water_area_ratio * 100).toFixed(1)}%</strong></div>
       <div class="row">최종 위험도 <strong>${lastMetric.risk_score} (등급 ${lastMetric.risk_grade})</strong></div>`
    : "";

  detail.innerHTML = `
    <h2>${r.run_id}</h2>
    <div class="asset-grid">${videoHtml}</div>
    ${summaryHtml}
    <h3>경보 로그</h3>
    ${alertsHtml}
    <p class="status-line">총 ${r.metrics.length}개 프레임 분석됨</p>
  `;
}

window.sendNotification = async function (caseId, messageId, btn) {
  btn.disabled = true;
  btn.textContent = "발송 중...";
  try {
    const res = await fetch(`/api/cases/${encodeURIComponent(caseId)}/notifications`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId, channels: ["sms"] }),
    });
    const data = await res.json();
    if (res.ok) {
      btn.textContent = data.dry_run ? "발송됨 (dry-run)" : "발송 완료";
    } else {
      btn.textContent = `실패: ${data.detail || res.status}`;
      btn.disabled = false;
    }
  } catch (e) {
    btn.textContent = "발송 실패";
    btn.disabled = false;
  }
};

initTabs();
tick();
setInterval(tick, 1000);
loadCaseList();
loadRunList();
