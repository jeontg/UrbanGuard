// tot_dashboard dashboard frontend.
//
// "도로위험 블록" 탭은 flood3의 원본 service/static/app.js를 그대로 이식한
// 것이다(docs/integration_plan.md 참고) -- 카드 그리드, 강수 상황판, 교차로별
// 타임라인, 실시간 CCTV 모달(hls.js), 사건 브리핑 모달까지 동일하다. 유일한
// 추가 기능은 실시간 CCTV 모달에 ROI(도로/저지대/침수 경계선) SVG 오버레이를
// 그리는 것 -- 원본 flood3에는 없던 기능이다(사용자 요청으로 추가).
//
// "군중안전 사례"/"분석 결과" 탭은 flood3/SAM에 없던 이번 통합 프로젝트의
// 신규 화면이라 별도로 작성됐다.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ⚠️ 2026-08-31 — API 게이트웨이 Phase 1·2(인파관리·노면관리 서비스 분리)
// 이후 신설. 그 도메인의 API는 이제 게이트웨이(nginx) 뒤 별도 서비스
// (crowd-service·road-service)가 처리한다 — 그 서비스가 죽으면
// 게이트웨이가 502/503/504를 그대로 돌려준다(다른 서비스가 대신 응답하는
// 거짓 상태를 만들지 않기 위해 의도적으로 그렇게 설계했다 — 계획서
// §공통 설계 원칙: "정직한 실패"). 화면도 그 상태를 숨기지 않고 기존
// "관측 없음" 계열 문구와 같은 자리에 "서비스 연결 안 됨"으로 정직하게
// 보여준다 — 원인이 스트림 문제가 아니라 서비스 자체가 안 떠 있다는
// 사실을 관제요원이 구분할 수 있어야 한다.
async function fetchJsonOrServiceDown(url) {
  const res = await fetch(url);
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    const err = new Error(`서비스 연결 안 됨 (HTTP ${res.status})`);
    err.serviceDown = true;
    throw err;
  }
  return res.json();
}

function showTab(name) {
  const btn = $(`.tab-btn[data-tab="${name}"]`);
  const panel = $(`#tab-${name}`);
  if (!btn || !panel) return;
  $$(".tab-btn").forEach((b) => b.classList.remove("active"));
  $$(".tab-panel").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  panel.classList.add("active");
  // 노면 실시간 관제는 탭이 보이지 않을 때 폴링을 쉬므로, 돌아온 순간에는
  // 곧바로 한 번 받아 온다(10초 동안 옛 값을 보여 주지 않도록).
  if (name === "road") loadRoadLive();
}

function initTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  });
  // 메뉴에서 /flood · /crowd · /road 로 진입했을 때 해당 탭을 열어 준다.
  if (window.INITIAL_TAB) showTab(window.INITIAL_TAB);
}

// ---- 도로위험 블록 (flood3 원본 이식) --------------------------------------
const LEVEL_COLOR = { "관심": "#3fb950", "주의": "#d4a017", "경계": "#e8843b", "심각": "#e5484d" };
const colorFor = (lvl) => LEVEL_COLOR[lvl] || "#6e7681";
const HEAVY = (b) => b.intensity === "강함" || b.intensity === "매우강함";

// 실시간 CCTV 모달 (hls.js로 라이브 HLS 직접 재생, 또는 재배포 허브가 켜져
// 있으면 WHEP(WebRTC)로 재생 — 2026-08-28 신설)
let _hls = null;
let _webrtcPc = null;
let _liveBlock = null;
// domain: 침수/교통위험/노면 중 어느 탭에서 열었는지 -- ROI 오버레이가
// 올바른 도메인의 도형을 가져오는 데 쓴다(2026-08-26, 아래 loadRoiOverlay
// 참고). 안 넘기면 기존 호출부와 호환되도록 flood로 본다.
//
// whepUrl: CCTV 재배포 허브(MediaMTX)가 켜져 있고 응답할 때만 서버가
// 내려준다 — 있으면 WHEP을 우선 시도하고, 없으면(재배포 꺼짐·응답 없음)
// 기존 hls.js 경로로 그대로 떨어진다. 모달 UI(제목·닫기·ROI 오버레이)는
// 어느 쪽이든 손대지 않는다.
function openLive(id, name, url, domain, whepUrl) {
  _liveBlock = { id, name, url, domain: domain || "flood" };
  const modal = document.getElementById("livemodal");
  const video = document.getElementById("livevideo");
  const err = document.getElementById("liveerr");
  err.textContent = "";
  document.getElementById("livetitle").textContent = name + " · 실시간 CCTV";
  modal.classList.remove("hidden");
  if (_hls) { _hls.destroy(); _hls = null; }
  if (_webrtcPc) { _webrtcPc.close(); _webrtcPc = null; }
  video.removeAttribute("src");
  video.srcObject = null;

  if (whepUrl) {
    _startWhep(video, err, whepUrl);
  } else if (window.Hls && window.Hls.isSupported()) {
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
  loadRoiOverlay(id, domain || "flood");
}

// WHEP(WebRTC-HTTP Egress Protocol) 재생 — 순수 RTCPeerConnection + fetch로
// SDP offer/answer만 주고받는다(외부 라이브러리 불필요). 재배포 허브
// (MediaMTX)가 WHEP 엔드포인트를 표준으로 제공한다.
async function _startWhep(video, errEl, whepUrl) {
  try {
    const pc = new RTCPeerConnection();
    _webrtcPc = pc;
    pc.ontrack = (ev) => {
      video.srcObject = ev.streams[0];
      video.play().catch(() => {});
    };
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const resp = await fetch(whepUrl, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: offer.sdp,
    });
    if (!resp.ok) throw new Error("WHEP 서버 응답 오류: " + resp.status);
    const answerSdp = await resp.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    pc.oniceconnectionstatechange = () => {
      if (pc !== _webrtcPc) return;   // 이미 닫고 다른 지점을 연 뒤라면 무시
      if (["failed", "disconnected", "closed"].includes(pc.iceConnectionState)) {
        errEl.textContent = "실시간 연결이 끊겼습니다(재배포 서버).";
      }
    };
  } catch (e) {
    errEl.textContent = "WHEP 재생 오류: " + (e.message || e);
  }
}

function closeLive() {
  const video = document.getElementById("livevideo");
  document.getElementById("livemodal").classList.add("hidden");
  try { video.pause(); } catch (e) { /* noop */ }
  video.removeAttribute("src");
  video.srcObject = null;
  if (_hls) { _hls.destroy(); _hls = null; }
  if (_webrtcPc) { _webrtcPc.close(); _webrtcPc = null; }
  document.getElementById("liveerr").textContent = "";
  const rs = document.getElementById("recstat"); if (rs) rs.textContent = "";
  const rb = document.getElementById("recbtn"); if (rb) rb.disabled = false;
  const roi = document.getElementById("liveroi"); if (roi) roi.innerHTML = "";
  const note = document.getElementById("roi-note"); if (note) note.textContent = "";
  const legend = document.getElementById("roi-legend-items"); if (legend) legend.innerHTML = "";
}

// 화살촉 마커(SVG <marker>) -- ROI 편집기(camera_roi.html)의 통행 방향
// 화살표 표시 방식과 통일한다. svg 하나당 한 번만 정의하면 된다.
const ROI_PALETTE = ["#3fb950", "#e5484d", "#58a6ff", "#d4a017"];
function _roiArrowheadDefs(ns, idPrefix) {
  const defs = document.createElementNS(ns, "defs");
  ROI_PALETTE.forEach((c, i) => {
    const m = document.createElementNS(ns, "marker");
    m.setAttribute("id", idPrefix + "-arrowhead-" + i);
    m.setAttribute("viewBox", "0 0 10 10");
    m.setAttribute("refX", "8"); m.setAttribute("refY", "5");
    m.setAttribute("markerWidth", "6"); m.setAttribute("markerHeight", "6");
    m.setAttribute("orient", "auto-start-reverse");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", "M0,0 L10,5 L0,10 z");
    p.setAttribute("fill", c);
    m.appendChild(p);
    defs.appendChild(m);
  });
  return defs;
}

// ROI 오버레이(도메인 공통) -- flood3 원본에는 없던 추가 기능.
// SVG의 viewBox를 ROI 저장 당시의 frame_width/height로 맞추면, 비디오가
// 실제로 어떤 크기로 렌더링되든(반응형) 좌표를 그대로 써도 자동으로 맞는다.
//
// ⚠️ 2026-08-26 — 예전에는 이 함수가 침수 도메인의 도형 키
// (road_roi/low_point_roi/lane_threshold_line)만 하드코딩돼 있어, 같은
// 모달을 공유하는 교통위험·노면 카메라의 실제 ROI(congestion_roi·
// analysis_roi 등)는 절대 그려지지 않았다(강서구청/SEOUL-207 노면
// 실시간 관제에서 실측 확인). 서버가 도메인별 ROI_SHAPES 그대로
// (shapes/shape_kinds/shape_labels)를 내려주므로, 여기서는 kind
// (polygon/line/arrows)만 보고 하드코딩 없이 그린다 -- 새 도메인·새
// 도형이 추가돼도 이 함수를 다시 고칠 필요가 없다.
//
// svgId: 그릴 <svg> 엘리먼트. noteId/legendId: 안내문·범례를 넣을 곳
// (없으면 그 부분은 생략 -- 노면 "집중 감시" 인라인 영상처럼 범례 자리가
// 따로 없는 곳에서 쓴다).
async function loadRoiOverlayInto(blockId, domain, svgId, noteId, legendId) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const note = noteId ? document.getElementById(noteId) : null;
  const legend = legendId ? document.getElementById(legendId) : null;
  svg.innerHTML = "";
  if (note) note.innerHTML = "";
  if (legend) legend.innerHTML = "";
  try {
    const roi = await (await fetch(
      `/api/roi/${encodeURIComponent(blockId)}?domain=${encodeURIComponent(domain)}`)).json();
    if (roi.error || !roi.frame_width) {
      if (note) {
        note.innerHTML = `⚠ 이 지점은 ROI가 설정되어 있지 않습니다 — `
          + `<a href="/settings/cameras/${encodeURIComponent(blockId)}/roi?domain=${encodeURIComponent(domain)}" `
          + `target="_blank" rel="noopener">ROI 설정 화면 열기</a>`;
      }
      return;
    }
    svg.setAttribute("viewBox", `0 0 ${roi.frame_width} ${roi.frame_height}`);
    const ns = "http://www.w3.org/2000/svg";
    svg.appendChild(_roiArrowheadDefs(ns, svgId));

    const shapes = roi.shapes || {};
    const kinds = roi.shape_kinds || {};
    const labels = roi.shape_labels || {};
    const legendParts = [];
    let colorIdx = 0;
    Object.keys(kinds).forEach((key) => {
      const val = shapes[key];
      if (!val || !val.length) return;
      const kind = kinds[key];
      const color = ROI_PALETTE[colorIdx % ROI_PALETTE.length];
      const markerId = svgId + "-arrowhead-" + (colorIdx % ROI_PALETTE.length);
      colorIdx++;
      legendParts.push(`<span><i style="background:${color}"></i>${esc(labels[key] || key)}</span>`);

      if (kind === "line") {
        if (val.length === 2) {
          const line = document.createElementNS(ns, "line");
          line.setAttribute("x1", val[0][0]); line.setAttribute("y1", val[0][1]);
          line.setAttribute("x2", val[1][0]); line.setAttribute("y2", val[1][1]);
          line.setAttribute("stroke", color); line.setAttribute("stroke-width", "3");
          line.setAttribute("stroke-dasharray", "10,6");
          svg.appendChild(line);
        }
      } else if (kind === "arrows") {
        (val || []).forEach((arr) => {
          if (!arr || arr.length !== 2) return;
          const line = document.createElementNS(ns, "line");
          line.setAttribute("x1", arr[0][0]); line.setAttribute("y1", arr[0][1]);
          line.setAttribute("x2", arr[1][0]); line.setAttribute("y2", arr[1][1]);
          line.setAttribute("stroke", color); line.setAttribute("stroke-width", "4");
          line.setAttribute("marker-end", `url(#${markerId})`);
          svg.appendChild(line);
        });
      } else {
        (val || []).forEach((pts) => {
          if (!pts || pts.length < 3) return;
          const poly = document.createElementNS(ns, "polygon");
          poly.setAttribute("points", pts.map((p) => p.join(",")).join(" "));
          poly.setAttribute("fill", color + "33");
          poly.setAttribute("stroke", color);
          poly.setAttribute("stroke-width", "3");
          svg.appendChild(poly);
        });
      }
    });
    if (legend) legend.innerHTML = legendParts.join("");
    if (!legendParts.length && note) {
      note.innerHTML = `⚠ 이 지점은 아직 그린 도형이 없습니다 — `
        + `<a href="/settings/cameras/${encodeURIComponent(blockId)}/roi?domain=${encodeURIComponent(domain)}" `
        + `target="_blank" rel="noopener">ROI 설정 화면 열기</a>`;
    }
  } catch (e) {
    if (note) note.textContent = "ROI를 불러오지 못했습니다.";
    console.error("ROI overlay load failed", e);
  }
}

// 실시간 CCTV 모달(#livemodal)이 쓰는 얇은 래퍼.
function loadRoiOverlay(blockId, domain) {
  return loadRoiOverlayInto(blockId, domain || "flood", "liveroi", "roi-note", "roi-legend-items");
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
// opts.endpoint/titleSuffix로 도로 노면 보고서(openRoadReport) 등 다른 도메인도
// 같은 모달을 재사용할 수 있게 일반화했다(기본값은 기존 flood 호출과 동일).
let _report = null;
async function openReport(id, name, opts) {
  const endpoint = (opts && opts.endpoint) || "/api/report/";
  const titleSuffix = (opts && opts.titleSuffix) || "사건 브리핑";
  const modal = document.getElementById("reportmodal");
  const body = document.getElementById("reportbody");
  const src = document.getElementById("reportsrc");
  document.getElementById("reporttitle").textContent = name + " · " + titleSuffix;
  src.textContent = "";
  body.textContent = "보고서 생성 중… (AI 서술 시 수 초 소요)";
  modal.classList.remove("hidden");
  _report = null;
  try {
    const j = await (await fetch(`${endpoint}${id}`)).json();
    if (j.error) throw new Error(j.error);
    _report = j;
    body.textContent = j.markdown;
    src.innerHTML = (j.narrative_source === "gemini-vlm" || j.narrative_source === "gemini")
      ? '「종합 판단」 서술: <span class="src-real">AI(Gemini)</span> · 그 외 항목은 규칙 고정'
      : '「종합 판단」 서술: <span class="src-mock">규칙 폴백</span> (GEMINI_API_KEY 미설정) · 그 외 항목은 규칙 고정';
  } catch (e) {
    body.textContent = "보고서 생성 오류: " + e.message;
  }
}

function openRoadReport(id, name) {
  openReport(id, name, { endpoint: "/api/road-report/", titleSuffix: "노면 점검 보고서" });
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

// 도로 노면 관리 -- 화면에 표시되는 값을 만들어낸 원본 mock 데이터(API 응답)를
// 그대로 보여준다(road/mock_data.py, Phase 1/2 참고). /api/road/{id} 응답을
// 가공 없이 pretty-print만 해서 보여주는 단순 뷰어.
async function openMockData(id, name) {
  const modal = document.getElementById("mockdatamodal");
  const body = document.getElementById("mockdatabody");
  document.getElementById("mockdatatitle").textContent = name + " · Mock 데이터";
  body.textContent = "불러오는 중...";
  modal.classList.remove("hidden");
  try {
    const j = await (await fetch(`/api/road/${encodeURIComponent(id)}`)).json();
    if (j.error) throw new Error(j.error);
    body.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    body.textContent = "Mock 데이터를 불러오지 못했습니다: " + e.message;
  }
}
function closeMockData() {
  document.getElementById("mockdatamodal").classList.add("hidden");
}

// 카드 안 차량 목록. **서버가 감속이 큰 차량을 골라 보낸다** — 여기서 정하는
// 것은 고른 결과를 줄 세우는 순서뿐이라, 「번호순」으로 바꿔도 중요한 차량이
// 목록에서 빠지지 않는다.
function objRows(objs) {
  let rows = objs.slice(0, 12);
  if ((window.UG_OBJECT_ORDER || "drop") === "id") {
    rows = rows.slice().sort((a, b) => a.id - b.id);
  }
  return rows.map(o => {
    const dc = o.drop > 0.5 ? "#e5484d" : o.drop > 0.2 ? "#e8843b" : "#3fb950";
    return `<div class="obj"><span class="oid">#${o.id}${o.stalled ? " ⏹" : ""}</span>`
      + `<span class="ospd" title="${o.speed}px/s">${o.kmh != null ? o.kmh + "km/h" : o.speed.toFixed(0) + "px/s"}</span>`
      + `<span class="obar"><span style="width:${Math.min(o.drop * 100, 100)}%;background:${dc}"></span></span>`
      + `<span class="odrop">${(o.drop * 100).toFixed(0)}%</span></div>`;
  }).join("");
}

const GRADE_COLOR = { 1: "#3fb950", 2: "#79c0ff", 3: "#d4a017", 4: "#e8843b", 5: "#e5484d" };
const TREND_KO = { surge: "급상승", rising: "상승", stable: "유지", falling: "하강", insufficient: "정보 부족" };

// HTML 에 값을 넣기 전 이스케이프. 지금 넣는 것은 서버가 만든 고정 문자열이라
// 당장은 안전하지만, **화면에 넣는 값은 이스케이프한다**는 규칙을 여기서
// 깨 두면 다음 사람이 사용자 입력을 그대로 넣는다.
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 위험도를 그렇게 판정한 근거. 판정기가 이미 만들어 두는데 화면이 안 받고
// 있었다 — 관제요원은 「군중급증 0.72」만 보고 **왜 그런지는 볼 수 없었다.**
// 근거를 못 보면 오탐인지 실제인지 가릴 수 없다.
function crowdDrivers(d) {
  const list = d.drivers || [];
  if (!list.length) return "";
  const chips = list.map((x) => `<span class="drv">${esc(x)}</span>`).join("");
  return `<div class="crowd-drivers"><span class="mk">판정 근거</span>${chips}</div>`;
}

// ---- 평소 대비 지금 ---------------------------------------------------------
//
// 화면에는 「지금」만 보였다. **「15명」이 이 지점에서 많은 건지 적은 건지**
// 알 수 없었는데, 평소 중앙값과 나란히 놓으면 **예측 없이도** 판단이 된다.
//
// ⚠️ 관측이 없으면 **아무 말도 하지 않는다.** 「평소 0명」이라고 쓰면 지금
// 인원이 전부 급증으로 읽힌다 — 없는 것을 0 으로 말하지 않는다.
async function loadCrowdBaseline(cameraId, now) {
  const el = $("#crowd-baseline");
  if (!el || !cameraId) return;
  let h;
  try {
    h = await (await fetch(`/api/crowd/history/${encodeURIComponent(cameraId)}`)).json();
  } catch (e) { return; }

  const b = h.baseline || {};
  if (!b.samples) {
    el.innerHTML = '<p class="hint">평상시 기준선을 만드는 중입니다 — '
      + '관측이 쌓이면 「평소 대비」를 표시합니다.</p>';
    return;
  }
  const med = b.person_median;
  const cur = now == null ? null : Number(now);
  let verdict = "", cls = "";
  if (cur != null && med > 0) {
    const ratio = cur / med;
    if (ratio >= 1.5) { verdict = `평소의 ${ratio.toFixed(1)}배`; cls = "hi"; }
    else if (ratio <= 0.5) { verdict = `평소의 ${ratio.toFixed(1)}배`; cls = "lo"; }
    else { verdict = "평소 수준"; }
  }
  el.innerHTML = `
    <div class="crowd-baseline">
      <span class="mk">평소 대비</span>
      <b class="${cls}">${esc(verdict || "—")}</b>
      <span class="bl-sub">평소 중앙값 ${med}명 · 최대 ${b.person_max}명
        · 관측 ${b.samples}건 (${h.hours}시간)</span>
    </div>`;
}

// 침수 카드 (2026-08-21 도메인 분리로 교통 카드에서 떼어냄).
// 예전에는 `card()` 안에서 불려 **같은 <div class="card"> 안에** 이어붙었다.
// 이제 독립 카드로 `#flood-grid` 에 따로 그린다.
function floodCard(b) {
  const qBadge = b.quality_grade === "crit"
    ? '<span class="badge" style="background:#e5484d" title="상시 화질 감시 — 재배포 경로에서 반복적인 디코더 오류 감지">화질 손상</span>'
    : b.quality_grade === "warn"
      ? '<span class="badge" style="background:#d4a017" title="상시 화질 감시 — 재배포 경로에서 디코더 오류 감지">화질 저하</span>' : "";
  return `<div class="card">
    <div class="chead"><span class="name">${b.name}</span>${qBadge}
      <span class="chead-r">${b.stream_type === "hls" && b.stream_url ? `<button class="livebtn" onclick="openLive('${b.block_id}','${b.name}','${b.stream_url}','flood',${b.whep_url ? `'${b.whep_url}'` : "null"})">▶ 실시간</button>` : ""}</span></div>
    ${floodSection(b)}
  </div>`;
}

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

// S-80에서 「선택」으로 지정된 침수 지점. 상시 분석을 돌지 않아 실시간 지표가
// 없다 — 빈 값을 0으로 보여 주면 「위험 없음」으로 오해하므로, 분석을 하지
// 않고 있다는 사실 자체를 적는다.
function selectiveCard(b) {
  return `<div class="card" style="opacity:.72">
    <h3>${b.name} <span class="badge" style="background:#d4a017">선택</span></h3>
    <div class="row muted">상시 분석 대상이 아닙니다</div>
    <div class="row muted" style="font-size:.78rem">
      실시간 지표가 없습니다. 상시로 바꾸려면
      <b>설정 › CCTV 관리(S-80)</b>에서 「상시 탐지」를 켜고 서비스를 재시작하세요.</div>
    ${b.has_roi || b.has_traffic_roi ? "" :
      '<div class="row" style="color:#e5484d;font-size:.78rem">⚠ ROI 미설정</div>'}
  </div>`;
}

// 교통위험 카드 — 강우 × 차량 감속·정지 판정.
// ★ 2026-08-21: 예전 이름은 `card()` 였고 마지막에 `floodSection(b)` 를 붙여
//   한 카드에 두 도메인을 담았다. 도메인 분리로 침수는 `floodCard()` 가
//   따로 그린다.
function trafficCard(b) {
  const c = colorFor(b.level);
  const rs = srcLabel(b.rain_source), vs = srcLabel(b.river_source);
  const trend = (b.rain_trend && b.rain_trend !== "유지")
    ? `<span class="trend">${b.rain_trend === "증가" ? "▲" : "▼"}${b.rain_trend}</span>` : "";
  const objs = b.objects || [];
  const objHtml = objs.length ? objRows(objs)
    : `<div class="muted" style="font-size:12px">추적 차량 없음 (스트림 연결/검출 대기)</div>`;
  const qBadge = b.quality_grade === "crit"
    ? '<span class="badge" style="background:#e5484d" title="상시 화질 감시 — 재배포 경로에서 반복적인 디코더 오류 감지">화질 손상</span>'
    : b.quality_grade === "warn"
      ? '<span class="badge" style="background:#d4a017" title="상시 화질 감시 — 재배포 경로에서 디코더 오류 감지">화질 저하</span>' : "";
  return `<div class="card ${HEAVY(b) ? "heavy" : ""}">
    <div class="chead"><span class="name">${b.name}</span>${qBadge}
      <span class="chead-r">${b.stream_type === "hls" && b.stream_url ? `<button class="livebtn" onclick="openLive('${b.block_id}','${b.name}','${b.stream_url}','traffic',${b.whep_url ? `'${b.whep_url}'` : "null"})">▶ 실시간</button>` : ""}<button class="repbtn" onclick="openReport('${b.block_id}','${b.name}')">📄 보고서</button><span class="chip" style="background:${c}">${b.level}</span></span></div>
    <div class="risk">${b.risk_name} <span class="tag">${b.risk_code}</span> · ${b.situation_ko || ""}</div>
    <div class="metrics">
      <div><span class="mk">강수</span><b>${b.rain_mm_h.toFixed(0)}</b>mm/h ${b.intensity} ${trend}</div>
      <div><span class="mk">평균속도</span><b>${b.mean_speed_kmh != null ? b.mean_speed_kmh + "km/h" : "— (미보정)"}</b> (${b.mean_speed.toFixed(0)}px/s)</div>
      <div><span class="mk">감속</span><b style="color:${c}">${(b.speed_drop * 100).toFixed(0)}%</b></div>
      <div><span class="mk">차량수</span><b>${b.n_vehicles}</b>대</div>
      <div><span class="mk">정체/정지</span><b>${b.queue_len}/${b.stalled}</b></div>
      <div><span class="mk">상태</span><b>${b.state}</b></div>
    </div>
    <div class="srcline">데이터출처 · 강수 <span class="${rs.cls}">${rs.t}</span> · 하천 <span class="${vs.cls}">${vs.t}</span></div>
    <div class="ohdr">차량 객체 탐지 · 속도/감속 (id : 속도 / 감속)</div>
    <div class="objs">${objHtml}</div>
    <div class="rec">권고(④) · ${b.recommendation}</div>
  </div>`;
}

function rainbar(blocks) {
  const top = [...blocks].sort((a, b) => b.rain_mm_h - a.rain_mm_h)[0] || {};
  const heavy = blocks.some(HEAVY);
  const el = document.getElementById("rainbar");
  el.className = heavy ? "rainbar heavy" : "rainbar";
  const allMock = blocks.length && blocks.every(b => (b.rain_source || "").startsWith("mock"));
  const srcNote = allMock
    ? `<div class="srcwarn">⚠ 현재 <b>강수·하천은 모의(mock) 데이터</b>입니다 — 실제로 비가 와서가 아닙니다. 실측은 기상청(KMA)·홍수통제소(HRFCO) 키 연동 시 표시됩니다. 단, <b>「침수」 탭의 위험도는 실제 CCTV 프레임에 물 세그멘테이션을 실행한 결과</b>입니다.</div>`
    : `<div class="srcok">✓ 실측 데이터 연동됨</div>`;
  // ★ 2026-08-21 도메인 분리 — 예전에는 두 판정이 한 카드의 위·아래였다.
  //   이제 탭이 나뉘었으므로 안내문도 「어느 탭인지」로 고친다.
  const judge = `<div class="judge">※ <b>이 탭(교통위험)</b>은 강수 × 차량 감속·정지를 의미 융합해 판정합니다. <b>물이 실제로 도로를 덮었는지</b>는 별도 판정이며 <b>「침수」 탭</b>에 있습니다 — CCTV 프레임에서 물 영역을 직접 검출해 도로 ROI 내 면적·확산·위험도(RiskEngine)·예측(RiskPredictor)을 계산합니다. 두 판정은 서로 독립이라 <b>한쪽만 위험할 수 있습니다.</b></div>`;
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

// 지점 카드 정렬. 「고정」류는 값이 바뀌어도 자리가 그대로다.
//
// 등록순의 기준은 window.INITIAL_BLOCKS(S-80 등록 순서)다. 거기에 없는 지점은
// 뒤로 보내되 **빠뜨리지는 않는다** — 목록에서 사라지는 것이 자리 이동보다
// 훨씬 나쁘다.
function orderBlocks(blocks) {
  const mode = window.UG_CARD_ORDER || "severity";
  const arr = blocks.slice();
  if (mode === "name") {
    return arr.sort((a, b) => (a.name || "").localeCompare(b.name || "", "ko"));
  }
  if (mode === "fixed") {
    const reg = new Map((window.INITIAL_BLOCKS || [])
      .map((b, i) => [b.id, i]));
    const last = reg.size;
    return arr.sort((a, b) =>
      (reg.has(a.block_id) ? reg.get(a.block_id) : last)
      - (reg.has(b.block_id) ? reg.get(b.block_id) : last));
  }
  return arr.sort((a, b) => b.severity - a.severity);
}

// 상황판 그리기. 폴링과 SSE 가 같은 함수를 쓴다 — 갈라 두면 한쪽만 고치는
// 일이 반드시 생긴다.
function renderBoard(blocks, history) {
  // 지점 카드 순서. 기본은 위험도순이지만, 값이 흔들릴 때마다 카드가 자리를
  // 옮겨 **관제 중 읽던 지점을 놓친다**는 지적이 있어 고정 선택지를 둔다
  // (설정 → 기관 정보·화면 S-85).
  blocks = orderBlocks(blocks || []);
  const allMock = blocks.length && blocks.every(b => (b.rain_source || "").startsWith("mock"));
  const modeEl = document.getElementById("mode");
  if (modeEl) modeEl.innerHTML = allMock
    ? '<span class="bmock">● 데모(모의 데이터)</span>' : '<span class="breal">● 실측 연동</span>';
  // 상시 분석은 여기까지다. S-80에서 「선택」으로 지정한 지점은 파이프라인이
  // 돌지 않아 목록에 안 나오므로, 빠진 것처럼 보이지 않도록 뒤에 따로 붙인다.
  const live = new Set(blocks.map((b) => b.block_id));
  const selective = (window.INITIAL_BLOCKS || [])
    .filter((b) => b.mode === "selective" && !live.has(b.id));
  // ★ 2026-08-21 도메인 분리 — 한 그리드에 섞어 그리던 것을 둘로 나눈다.
  //   `/api/risk` 응답은 그대로 두고(서버 분리는 별도 과제) 화면에서만
  //   나눈다.
  const tgrid = document.getElementById("traffic-grid");
  if (tgrid) {
    // ★ 2026-08-28 — 교통위험을 지정한 지점만 교통위험 탭에 올린다(바로
    // 아래 침수 탭이 water_available로 거르는 것과 같은 이유). 이 거름이
    // 없으면 침수만 상시로 켠 카메라도 「관심(정상)」 카드로 여기 나타나
    // 실제로는 판정한 적 없는 지점을 판정한 것처럼 보여 준다.
    const traffic = blocks.filter((b) => b.traffic_enabled !== false);
    tgrid.innerHTML =
      traffic.map(trafficCard).join("") + selective.map(selectiveCard).join("");
  }
  const fgrid = document.getElementById("flood-grid");
  if (fgrid) {
    // 물 세그멘테이션이 도는 지점만 침수 탭에 올린다. 안 도는 지점을
    // 「이상 없음」처럼 늘어놓으면 보지 않은 곳을 봤다고 오해한다.
    const watched = blocks.filter((b) => b.water_available);
    fgrid.innerHTML = watched.length
      ? watched.map(floodCard).join("")
      : `<div class="muted" style="padding:14px">물 세그멘테이션이 도는 지점이 없습니다 —
         합성/폴백 소스에서는 실행하지 않습니다. 설정 › CCTV 관리(S-80)에서
         실시간 스트림 지점의 침수 탐지를 켜십시오.</div>`;
  }
  rainbar(blocks);
  if (history) renderTimelines(history);
}

function boardOk(label) {
  document.getElementById("status").textContent =
    `${label} · 갱신 ${new Date().toLocaleTimeString("ko-KR")}`;
  document.getElementById("dot").style.background = "#3fb950";
}

function boardDown() {
  document.getElementById("status").textContent = "연결 끊김 — 재시도";
  document.getElementById("dot").style.background = "#e5484d";
}

// ---- 실시간 갱신: SSE 우선, 끊기면 폴링 -------------------------------------
//
// SSE 는 **바뀔 때만** 서버가 밀어 준다. 폴링(1초)은 대부분 「바뀐 것 없음」을
// 받아 오는 낭비였고, 갱신 지연도 주기에 묶여 있었다.
//
// ⚠️ **SSE 는 개선이지 대체가 아니다.** 중간 장비가 스트림을 막거나 구형
// 브라우저면 안 뜬다. 그때 화면이 멈추면 관제가 멈춘다. 그래서 폴링을
// 지우지 않고 **되돌아갈 자리로 남겨 둔다.**
//
// ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 침수·교통위험이 별도
// 서비스(flood-service·traffic-service)로 갈라지며 스냅샷도 두
// 스트림(`/api/stream/risk`·`/api/stream/flood-risk`)으로 나뉘었다.
// `renderBoard()`는 안 고친다(카드 그리기는 예전처럼 "한 카메라 = 한
// dict"를 기대한다) — 대신 두 스트림을 받는 즉시 `mergeBoardBlocks()`
// 로 합쳐 기존 함수에 그대로 넘긴다. 침수·교통을 동시에 쓰는 카메라
// (드물다 — 실측 39개 중 1개)만 두 dict가 하나로 합쳐지고, 나머지는
// 한쪽 필드만 있는 상태로 그대로 간다.
let _pollTimer = null;
let _sseTraffic = null;
let _sseFlood = null;
let _lastTrafficBlocks = [];
let _lastTrafficHistory = {};
let _lastFloodBlocks = [];
let _lastFloodHistory = {};

function startPolling() {
  if (_pollTimer) return;
  tick();
  _pollTimer = setInterval(tick, 1000);
}

function stopPolling() {
  if (!_pollTimer) return;
  clearInterval(_pollTimer);
  _pollTimer = null;
}

// 교통위험 스냅샷(traffic-service) + 침수 스냅샷(flood-service)을 한
// 카메라 기준으로 합친다. `renderBoard()`의 필터(`traffic_enabled !==
// false`·`water_available`)가 undefined를 "포함"으로 읽으므로, 한쪽
// 서비스에만 있는 카메라는 다른 쪽 필드를 명시적으로 꺼 둬야 한다 —
// 안 그러면 침수 전용 카메라가 교통위험 탭에도 잘못 뜬다.
function mergeBoardBlocks(trafficBlocks, floodBlocks) {
  const byId = new Map();
  for (const b of (trafficBlocks || [])) {
    byId.set(b.block_id, Object.assign({ water_available: false }, b));
  }
  for (const b of (floodBlocks || [])) {
    const existing = byId.get(b.block_id);
    if (existing) {
      byId.set(b.block_id, Object.assign(existing, b));
    } else {
      byId.set(b.block_id, Object.assign({ traffic_enabled: false }, b));
    }
  }
  return Array.from(byId.values());
}

function _renderMergedBoard() {
  renderBoard(
    mergeBoardBlocks(_lastTrafficBlocks, _lastFloodBlocks),
    // 카메라 하나가 침수·교통을 동시에 쓰는 드문 경우, 타임라인은 나중에
    // 온 쪽(교통)이 이긴다 — 두 이력을 점 단위로 합치는 것은 이 규모
    // (겹치는 카메라 1개)에서 얻는 것보다 복잡도가 더 크다고 판단했다.
    Object.assign({}, _lastFloodHistory, _lastTrafficHistory));
}

function startBoardStream() {
  if (!window.EventSource) { startPolling(); return; }   // 구형 브라우저
  try {
    _sseTraffic = new EventSource("/api/stream/risk");
    _sseFlood = new EventSource("/api/stream/flood-risk");
  } catch (e) {
    startPolling();
    return;
  }
  // 구독 자리가 꽉 찼다는 서버 응답. 조용히 폴링으로 간다.
  _sseTraffic.addEventListener("full", () => { _sseTraffic.close(); startPolling(); });
  _sseFlood.addEventListener("full", () => { _sseFlood.close(); startPolling(); });
  _sseTraffic.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      stopPolling();               // 스트림이 살아 있으면 폴링은 접는다
      _lastTrafficBlocks = d.blocks || [];
      _lastTrafficHistory = d.history || {};
      _renderMergedBoard();
      boardOk("실시간");
    } catch (e) { /* 한 건 깨져도 다음 것을 기다린다 */ }
  };
  _sseFlood.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      stopPolling();
      _lastFloodBlocks = d.blocks || [];
      _lastFloodHistory = d.history || {};
      _renderMergedBoard();
      boardOk("실시간");
    } catch (e) { /* 한 건 깨져도 다음 것을 기다린다 */ }
  };
  // 둘 중 하나만 끊겨도 폴링으로 함께 내려간다 — 절반만 실시간이고 절반은
  // 멎어 있는 상태를 화면이 구분해 보여 주지 않는다(단순함을 택함).
  _sseTraffic.onerror = () => { boardDown(); startPolling(); };
  _sseFlood.onerror = () => { boardDown(); startPolling(); };
}

async function tick() {
  try {
    const [traffic, trafficHist, flood, floodHist] = await Promise.all([
      fetchJsonOrServiceDown("/api/risk"),
      fetchJsonOrServiceDown("/api/history"),
      fetchJsonOrServiceDown("/api/flood-risk"),
      fetchJsonOrServiceDown("/api/flood-history"),
    ]);
    _lastTrafficBlocks = traffic.blocks || [];
    _lastTrafficHistory = trafficHist.blocks || {};
    _lastFloodBlocks = flood.blocks || [];
    _lastFloodHistory = floodHist.blocks || {};
    _renderMergedBoard();
    boardOk("폴링");
  } catch (e) {
    if (e && e.serviceDown) {
      document.getElementById("status").textContent =
        "⚠ 침수/교통위험 서비스 연결 안 됨 — 잠시 후 다시 시도하세요.";
      document.getElementById("dot").style.background = "#e5484d";
      return;
    }
    boardDown();
  }
}

// ---- 인파 위험행동 실시간 탐지 ----------------------------------------------
// 배회(Loitering)·침입(Intrusion) + 군중지표. 소스가 mock이면 합성 데이터이므로
// 화면에 명확히 표기한다(가짜 데이터를 실측으로 오인하지 않도록).
// docs/crowd_behavior_detection_plan.md 3-B절 참고.
const CROWD_SEV_COLOR = { 0: "#3fb950", 1: "#79c0ff", 2: "#d4a017", 3: "#e8843b", 4: "#e5484d" };
const CROWD_SEV_LABEL = { 0: "정상", 1: "군중밀집", 2: "이동흐름혼란", 3: "군중급증위험", 4: "패닉분산" };
const EVENT_LABEL = { Loitering: "배회", Intrusion: "침입" };
const EVENT_COLOR = { Loitering: "#d4a017", Intrusion: "#e5484d" };

// 소스 전환 UI. 현재 상태는 /api/health에서 받아 캐시해 두고, 버튼 클릭 시
// POST /api/crowd/source 로 런타임 전환한다(설정 파일은 건드리지 않음).
let _crowdSrc = null;
// 전환 결과 메시지는 상태로 보관한다 -- loadCrowdLive()가 패널을 통째로 다시
// 그리기 때문에, DOM에 직접 써 넣으면 즉시 지워진다.
let _crowdSrcMsg = "";

function _crowdSourceControls() {
  if (!_crowdSrc) return "";
  const btn = (group, value, label, cur) =>
    `<button class="srcbtn ${cur === value ? "on" : ""}"
       onclick="switchCrowdSource('${group}','${value}')">${label}</button>`;
  return `
    <div class="src-switch">
      <div class="src-row">
        <span class="src-label">사람 검출</span>
        ${btn("person", "mock", "임시데이터", _crowdSrc.person_source)}
        ${btn("person", "detector", "실검출(CPU)", _crowdSrc.person_source)}
      </div>
      <div class="src-row">
        <span class="src-label">환경센서</span>
        ${btn("environment", "mock", "임시데이터", _crowdSrc.environment_mode)}
        ${btn("environment", "device", "현장장비", _crowdSrc.environment_mode)}
      </div>
      <div class="src-row">
        <span class="src-label">스테레오</span>
        ${btn("stereo", "mock", "임시데이터", _crowdSrc.stereo_mode)}
        ${btn("stereo", "device", "현장장비", _crowdSrc.stereo_mode)}
      </div>
      <div class="src-note" id="src-note">
        ${_crowdSrcMsg
          ? `<span class="src-err">${_crowdSrcMsg}</span><br>`
          : ""}
        ⓘ 런타임 전환입니다 — 서버를 재시작하면 configs/blocks.json 설정값으로 돌아갑니다.
      </div>
    </div>`;
}

window.switchCrowdSource = async function (group, value) {
  const note = document.getElementById("src-note");
  if (note) note.textContent = "전환 중… (실검출 최초 전환 시 모델 다운로드로 수십 초 걸릴 수 있습니다)";
  try {
    const r = await fetch("/api/crowd/source", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [group]: value }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    _crowdSrc = j;
    _crowdSrcMsg = (j.errors && j.errors.length)
      ? "⚠ 전환 실패 — " + j.errors.join(" / ")
      : "";
  } catch (e) {
    _crowdSrcMsg = "⚠ 전환 오류 — " + e.message;
  }
  await loadCrowdLive();   // 상태를 반영해 패널 전체 재렌더
};

async function refreshCrowdSourceState() {
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.crowd_sources && h.crowd_sources.enabled) _crowdSrc = h.crowd_sources;
  } catch (e) { /* noop */ }
}

async function loadCrowdLive() {
  const el = $("#crowd-live");
  if (!el) return;
  let d;
  try {
    d = await (await fetch("/api/crowd/live")).json();
  } catch (e) {
    el.innerHTML = '<p class="hint">실시간 분석을 불러오지 못했습니다.</p>';
    return;
  }
  if (d.error) {
    el.innerHTML = `<p class="hint">실시간 분석 비활성화: ${d.error}</p>`;
    return;
  }

  const sc = CROWD_SEV_COLOR[d.severity] || "#6e7681";
  const env = d.environment || {};
  const isMock = d.source !== "detector";

  const srcBanner = isMock
    ? `<div class="mock-hint">⚠ 사람 검출 소스가 <b>모의(mock) 데이터</b>입니다 — 실제 영상 분석 결과가 아닙니다.
       실검출 전환: blocks.json의 <code>crowd.source.type</code>을 <code>detector</code>로 변경</div>`
    : `<div class="srcok">✓ 실검출(detector) 모드</div>`;

  const evHtml = (d.events || []).length
    ? d.events.map((e) => {
        const c = EVENT_COLOR[e.eventType] || "#6e7681";
        const dwell = e.dwellTimeSec != null ? ` · 체류 ${e.dwellTimeSec}s` : "";
        return `<div class="alert-row">
          <span><span class="badge" style="background:${c}">${EVENT_LABEL[e.eventType] || e.eventType}</span>
            추적ID #${e.trackId} · ${e.evidenceText}${dwell}</span>
          <span class="muted">신뢰도 ${(e.confidence * 100).toFixed(0)}%</span>
        </div>`;
      }).join("")
    : '<p class="hint">탐지된 위험행동 없음</p>';

  el.innerHTML = `
    ${srcBanner}
    ${_crowdSpreadHtml(d.spread)}
    ${_crowdSourceControls()}
    <div class="crowd-live-head">
      <span class="ctitle">실시간 위험행동 탐지</span>
      <span class="badge" style="background:${sc}">${d.risk_name} (등급 ${d.severity})</span>
      <span class="muted">${d.action_label}</span>
    </div>
    <div class="crowd-metrics">
      <div><span class="mk">인원</span><b>${d.person_count}</b>명</div>
      <div><span class="mk">밀집지수</span><b>${d.density_index}</b></div>
      <div><span class="mk">평균속도</span><b>${d.mean_speed}</b>px/s</div>
      <div><span class="mk">궤적분산</span><b>${d.trajectory_variance}</b></div>
      <div><span class="mk">속도급증</span><b>${d.surge != null ? d.surge : "—"}</b>배</div>
      <div><span class="mk">발산도</span><b>${d.divergence != null ? d.divergence : "—"}</b></div>
      <div><span class="mk">위험점수</span><b>${d.risk_score}</b></div>
      <div><span class="mk">환경</span><b>${env.is_night ? "야간" : "주간"}</b>${env.temp_c != null ? ` ${env.temp_c}℃` : ""}</div>
    </div>
    ${crowdDrivers(d)}
    <div id="crowd-baseline"></div>
    <div class="ohdr">탐지된 위험행동 (배회 · 침입)</div>
    ${evHtml}
    <p class="status-line">환경센서: ${env.source || "-"} · 스테레오: ${(d.depth || {}).source || "-"}</p>
  `;
  // 기준선은 따로 불러 온다 — 이력 조회가 느려도 **실시간 표시를 막지
  // 않기 위해서**다. 실패하면 그 칸만 비고 나머지는 그대로 보인다.
  loadCrowdBaseline(d.block_id || d.camera_id, d.person_count);
}

// ---- 인파 상시 카메라별 모니터링 (2026-08-27, 교통위험과 같은 방식) --------
// 예전에는 「실시간 분석 (상시)」가 39개소 중 첫 번째 카메라 하나만
// 보여줬다(loadCrowdLive/#crowd-live, 위 함수 — BLOCKS[0] 고정). 지금은
// S-80에서 「인파·상시」로 지정한 카메라마다 독립된 카드를 그린다
// (/api/crowd/continuous, service/continuous.py::CrowdContinuousWatcher).
function _crowdContinuousCardHtml(p) {
  const sev = p.severity != null ? p.severity : null;
  const sevColor = sev != null ? CROWD_SEV_COLOR[sev] : "#6e7681";
  const sevLabel = sev != null ? (CROWD_SEV_LABEL[sev] || `등급 ${sev}`) : "미관측";
  let statusHtml;
  if (!p.observed) {
    statusHtml = '<span class="hint">아직 관측되지 않았습니다.</span>';
  } else if (p.failed) {
    statusHtml = '<span class="hint">최근 관측이 실패했습니다(영상을 받지 못함).</span>';
  } else {
    statusHtml = `
      <div class="crowd-metrics">
        <div><span class="mk">인원</span><b>${p.person_count}</b>명</div>
        <div><span class="mk">밀집지수</span><b>${(p.density_index ?? 0).toFixed(2)}</b></div>
        <div><span class="mk">평균속도</span><b>${p.mean_speed}</b>px/s</div>
      </div>
      ${p.drivers ? `<p class="hint" style="margin:4px 0">근거: ${p.drivers}</p>` : ""}`;
  }
  const ageTxt = p.age_sec != null
    ? (p.age_sec < 60 ? `${Math.round(p.age_sec)}초 전`
       : `${Math.round(p.age_sec / 60)}분 전`)
    : "—";
  const sourceBadge = p.source === "mock"
    ? '<span class="bmock" style="font-size:11.5px">모의(mock)</span>'
    : p.source === "detector"
      ? '<span class="breal" style="font-size:11.5px">실검출</span>' : "";
  return `
    <div class="card">
      <div class="crowd-live-head">
        <span class="ctitle">${p.name}</span>
        <span class="badge" style="background:${sevColor}">${sevLabel}</span>
        ${sourceBadge}
        ${p.quality_grade === "crit" ? '<span class="badge" style="background:#e5484d" title="상시 화질 감시 — 재배포 경로에서 반복적인 디코더 오류 감지">화질 손상</span>'
          : p.quality_grade === "warn" ? '<span class="badge" style="background:#d4a017" title="상시 화질 감시 — 재배포 경로에서 디코더 오류 감지">화질 저하</span>' : ""}
      </div>
      ${p.pending
        ? '<p class="hint">⚠ 방금 「상시」로 지정됨 — 다음 서비스 재기동부터 관측을 시작합니다.</p>'
        : ""}
      ${statusHtml}
      <p class="status-line">${ageTxt} 갱신${p.dept ? " · " + p.dept : ""}
        ${p.stale ? ' · <span class="src-err">갱신 지연</span>' : ""}</p>
      ${p.stream_url ? `<button class="ug-btn ug-btn--ghost" type="button"
          style="width:auto;padding:5px 12px;font-size:.8rem;margin-top:6px"
          onclick="openLive('${p.camera_id}','${(p.name || "").replace(/'/g, "&#39;")}',
                            '${p.stream_url}','crowd',${p.whep_url ? `'${p.whep_url}'` : "null"})">▶ 실시간</button>` : ""}
    </div>`;
}

async function loadCrowdContinuous() {
  const el = $("#crowd-continuous");
  if (!el) return;
  let d;
  try {
    d = await fetchJsonOrServiceDown("/api/crowd/continuous");
  } catch (e) {
    el.innerHTML = e.serviceDown
      ? '<p class="hint" style="color:#e5484d">⚠ 인파관리 서비스 연결 안 됨 — 잠시 후 다시 시도하세요.</p>'
      : '<p class="hint">상시 모니터링 현황을 불러오지 못했습니다.</p>';
    return;
  }
  const points = d.points || [];
  if (!points.length) {
    el.innerHTML = `<p class="hint">「상시」로 지정된 인파 카메라가 없습니다 —
      CCTV 관리에서 「탐지 지정」으로 켤 수 있습니다(켠 뒤 서비스 재기동 필요).</p>`;
    return;
  }
  el.innerHTML = points.map(_crowdContinuousCardHtml).join("");
}

// ---- 인파 CCTV 선택 탐지 (S-30) ---------------------------------------------
// 인파 검출은 타일 분할 추론이라 전 지점 상시 분석을 CPU가 감당하지 못한다.
// 그래서 관제요원이 지점을 골랐을 때만 30초간 관측한다(crowd/cctv_analysis.py).
// 대상 목록은 S-80에서 「인파 사용」으로 지정한 카메라만 내려온다.
let _crowdCams = null;
let _crowdCamBusy = false;
let _crowdCamResult = null;
let _crowdCamMsg = "";
let _crowdCamPick = "";      // 재렌더 후에도 고른 지점을 유지한다
let _crowdCamLeft = 0;       // 남은 관측 초 (진행 표시용)
let _crowdCamTimer = null;

async function loadCrowdCctv() {
  const el = $("#crowd-cctv");
  if (!el) return;
  if (!_crowdCams) {
    try {
      _crowdCams = await (await fetch("/api/crowd/cameras")).json();
    } catch (e) {
      el.innerHTML = '<p class="hint">인파 탐지 대상 목록을 불러오지 못했습니다.</p>';
      return;
    }
  }
  renderCrowdCctv();
}

function renderCrowdCctv() {
  const el = $("#crowd-cctv");
  if (!el) return;
  const d = _crowdCams || { cameras: [], duration_sec: 30 };
  const cams = d.cameras || [];
  const dur = d.duration_sec || 30;

  if (!cams.length) {
    el.innerHTML = `
      <div class="crowd-live-head"><span class="ctitle">CCTV 선택 탐지</span></div>
      <p class="hint">인파 탐지 대상으로 지정된 CCTV가 없습니다 —
        <b>설정 › CCTV 관리(S-80)</b>에서 지점을 선택한 뒤 「인파관리」를 체크하세요.</p>`;
    return;
  }

  if (_crowdCamPick && !cams.some((c) => c.id === _crowdCamPick)) _crowdCamPick = "";
  if (!_crowdCamPick) _crowdCamPick = cams[0].id;

  // S-80의 지정을 그대로 보여 준다 — 관리자 화면과 목록이 어긋나면
  // 설정이 반영되지 않은 것으로 오해한다.
  const opts = cams.map((c) =>
    `<option value="${c.id}" ${c.id === _crowdCamPick ? "selected" : ""}>${c.name}${
      c.mode === "continuous" ? " [상시]" : " [선택]"}${
      c.has_roi ? "" : " · 침입 구역 미설정"}</option>`).join("");
  const pick = cams.find((c) => c.id === _crowdCamPick) || cams[0];

  el.innerHTML = `
    <div class="crowd-live-head">
      <span class="ctitle">CCTV 선택 탐지</span>
      <span class="muted">인파 지정 ${cams.length}개소
        (상시 ${cams.filter((c) => c.mode === "continuous").length} ·
         선택 ${cams.filter((c) => c.mode !== "continuous").length}) · 관측 ${dur}초</span>
    </div>
    ${_crowdCamMsg ? `<div class="mock-hint">⚠ ${_crowdCamMsg}</div>` : ""}
    <div class="src-switch">
      <div class="src-row">
        <span class="src-label">지점 선택</span>
        <select id="crowd-cam" class="road-select" ${_crowdCamBusy ? "disabled" : ""}>${opts}</select>
        <button class="livebtn livebtn-lg" onclick="runCrowdAnalysis()" ${_crowdCamBusy ? "disabled" : ""}>
          ${_crowdCamBusy ? `관측 중… (${_crowdCamLeft}초)` : "▶ 분석 실행"}</button>
      </div>
      ${pick && !pick.has_roi
        ? `<div class="src-note">ⓘ 이 지점은 <b>침입 금지 구역이 설정되지 않았습니다</b> —
             밀집도·배회는 분석되지만 침입은 탐지되지 않습니다.
             설정 › CCTV 관리 › ROI 설정(S-81)에서 「인파관리」 탭을 여세요.</div>`
        : `<div class="src-note">ⓘ 선택한 지점을 <b>${dur}초</b> 동안 관측해 밀집도·배회·침입을
             확인합니다. 상시 분석이 아니라 <b>누를 때만</b> 실행됩니다.</div>`}
    </div>
    <div id="crowd-cctv-result">${_crowdCctvResultHtml()}</div>`;

  const sel = document.getElementById("crowd-cam");
  if (sel) sel.addEventListener("change", function () {
    _crowdCamPick = sel.value;
    renderCrowdCctv();       // 안내 문구(ROI 미설정)를 지점에 맞춰 바꾼다
  });
}

function _crowdCctvResultHtml() {
  const r = _crowdCamResult;
  if (!r) return '<p class="hint">지점을 선택한 뒤 「분석 실행」을 누르세요.</p>';
  if (r.note) return `<p class="hint">⚠ ${r.note}</p>`;

  const sc = CROWD_SEV_COLOR[r.severity] || "#6e7681";
  // 사람 검출이 mock이면 분석기가 카메라 프레임을 무시하고 모의 박스를 쓴다.
  // 스트림은 실제로 열렸으므로 결과가 진짜처럼 보이는 것이 더 위험하다.
  const fake = r.source && r.source !== "detector"
    ? `<div class="mock-hint">⚠ 사람 검출이 <b>모의(mock) 데이터</b>라
         아래 인원·위험행동은 <b>이 카메라의 영상에서 나온 값이 아닙니다.</b>
         실검출로 바꾼 뒤 다시 실행하세요(아래 「사람 검출」 전환).</div>`
    : "";
  const evHtml = (r.events || []).length
    ? r.events.map((e) => {
        const c = EVENT_COLOR[e.eventType] || "#6e7681";
        const dwell = e.dwellTimeSec != null ? ` · 체류 ${e.dwellTimeSec}s` : "";
        return `<div class="alert-row">
          <span><span class="badge" style="background:${c}">${EVENT_LABEL[e.eventType] || e.eventType}</span>
            추적ID #${e.trackId} · ${e.evidenceText}${dwell}</span>
          <span class="muted">신뢰도 ${(e.confidence * 100).toFixed(0)}%</span>
        </div>`;
      }).join("")
    : '<p class="hint">관측 구간에서 탐지된 위험행동 없음</p>';

  // 스냅샷은 관측 마지막 프레임. 사람 영역은 서버에서 픽셀화해 내려보내며
  // (core/image_mask.py), 가리지 못했으면 아예 내려오지 않는다.
  const snap = r.snapshot
    ? `<div class="ohdr">관측 마지막 화면 (사람 영역 마스킹됨)</div>
       <img class="crowd-snap" alt="관측 마지막 화면 — 사람 영역이 가려진 상태"
         src="data:image/jpeg;base64,${r.snapshot}">`
    : (r.mask_status === "unavailable" || r.mask_status === "failed"
        ? `<p class="hint">ⓘ 사람 영역을 가리지 못해 화면을 표시하지 않습니다
             (마스킹 검출기 미동작). 지표와 탐지 결과는 위 내용이 전부입니다.</p>`
        : "");

  return `
    <div class="crowd-live-head">
      <span class="ctitle">분석 결과 · ${r.camera_name || r.camera_id}</span>
      ${r.risk_level ? `<span class="badge" style="background:${sc}">${r.risk_level}</span>` : ""}
    </div>
    ${fake}
    <div class="crowd-metrics">
      <div><span class="mk">인원 최대</span><b>${r.people_max}</b>명</div>
      <div><span class="mk">인원 평균</span><b>${r.people_avg}</b>명</div>
      <div><span class="mk">밀집지수 최대</span><b>${r.density_max}</b></div>
      <div><span class="mk">분석 프레임</span><b>${r.frames_analyzed}</b></div>
      <div><span class="mk">관측 시간</span><b>${r.duration_sec}</b>초</div>
    </div>
    <div class="ohdr">탐지된 위험행동 (배회 · 침입)</div>
    ${evHtml}
    ${snap}`;
}

window.runCrowdAnalysis = async function () {
  const sel = document.getElementById("crowd-cam");
  if (!sel || !sel.value || _crowdCamBusy) return;
  const camId = sel.value;
  const dur = (_crowdCams && _crowdCams.duration_sec) || 30;

  _crowdCamBusy = true;
  _crowdCamMsg = "";
  _crowdCamResult = null;
  _crowdCamLeft = Math.round(dur);
  renderCrowdCctv();

  // 30초는 버튼만 비활성인 채로 기다리기엔 길다. 남은 시간을 초 단위로 보여
  // 준다 -- 멈춘 화면으로 오해해 새로고침하는 것을 막기 위해서다.
  clearInterval(_crowdCamTimer);
  _crowdCamTimer = setInterval(() => {
    if (_crowdCamLeft > 0) { _crowdCamLeft -= 1; renderCrowdCctv(); }
  }, 1000);

  try {
    const res = await fetch("/api/crowd/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ camera_id: camId }),
    });
    const j = await res.json();
    if (!res.ok) _crowdCamMsg = j.detail || `분석 요청 실패 (${res.status})`;
    else _crowdCamResult = j;
  } catch (e) {
    _crowdCamMsg = "분석 요청에 실패했습니다 — " + e.message;
  }
  clearInterval(_crowdCamTimer);
  _crowdCamBusy = false;
  renderCrowdCctv();
};

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

// ---- S-44 도로 노면 실시간 관제 ---------------------------------------------
// 상시 순회는 지점당 15분 주기다. 포트홀·균열에는 충분하지만 결빙·낙하물·사고
// 잔해는 분 단위로 변해, 15분 전 결과로는 대응할 수 없다. 그래서 두 가지를
// 화면에 싣는다.
//
//   1) **경과 시간** — 지금 보이는 등급이 방금 것인지 15분 전 것인지.
//      기대 주기의 2배를 넘기면 「갱신 지연」으로 표시한다.
//   2) **집중 감시** — 지점 하나를 골라 짧은 주기로 반복 관측한다.
//
// 라이브 영상을 카드마다 심지 않은 이유 — 지점이 10곳이면 HLS 스트림 10개를
// 동시에 물어 브라우저가 먼저 죽는다. **집중 감시 중인 한 곳만** 인라인으로
// 재생하고, 나머지는 기존 실시간 모달(openLive)로 연다.
let _roadLive = null;
let _roadLiveBusy = false;
let _roadCollectBusy = false;
let _roadUploadBusy = false;
let _roadUploadResult = null;
let _roadHistFor = null;      // 이력 표를 열어 둔 지점
let _roadHistData = null;

// 이력 표에서 쓰는 표시명. 등급은 road/defect_detection.py 의 GRADE_NAME 과,
// 출처는 road_results.record(source=...) 의 값과 맞춘다.
const ROAD_GRADE_NAME = { 1: "정상", 2: "관찰", 3: "보수 필요", 4: "긴급 보수" };
const ROAD_SOURCE_NAME = {
  continuous: "상시 순회",
  focus: "집중 감시",
  manual: "직접 실행",
};
let _roadFocusHls = null;
let _roadFocusVideoFor = null;   // 현재 인라인 재생 중인 지점 (재생성 방지)

const ROAD_LIVE_POLL_MS = 10000;
// 순회가 한 지점을 보고 있는 동안(15초)에는 더 자주 받는다. 10초 간격이면
// 「3/8 관측 중」이 한 번 뜨고 지나가 버려 진행 상황으로 읽히지 않는다.
const ROAD_LIVE_POLL_BUSY_MS = 3000;
let _roadLiveTimer = null;

function fmtAge(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec);
  if (s < 60) return "방금";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  return `${h}시간 ${m % 60}분 전`;
}

function fmtDur(sec) {
  const s = Math.max(Math.round(sec || 0), 0);
  const m = Math.floor(s / 60);
  return m >= 1 ? `${m}분 ${s % 60}초` : `${s}초`;
}

async function loadRoadLive() {
  const el = $("#road-live");
  if (!el) return;
  // 노면 탭을 보고 있지 않으면 굳이 DB를 두드리지 않는다.
  // ⚠️ 여기서 그냥 return 하면 다음 갱신이 예약되지 않아 폴링이 영영 끊긴다.
  const panel = $("#tab-road");
  if (_roadLive && panel && !panel.classList.contains("active")) {
    _scheduleRoadLive();
    return;
  }
  try {
    _roadLive = await fetchJsonOrServiceDown("/api/road/live");
  } catch (e) {
    el.innerHTML = e.serviceDown
      ? '<p class="hint" style="color:#e5484d">⚠ 노면관리 서비스 연결 안 됨 — 잠시 후 다시 시도하세요.</p>'
      : '<p class="hint">실시간 관제 상태를 불러오지 못했습니다.</p>';
    _scheduleRoadLive();
    return;
  }
  renderRoadLive();
  _scheduleRoadLive();
}

// 다음 갱신을 스스로 예약한다. 고정 간격(setInterval)으로 두면 순회 중에도
// 10초를 기다려, 「지금 어느 지점을 보고 있는지」가 지나가 버린다.
function _scheduleRoadLive() {
  if (_roadLiveTimer) clearTimeout(_roadLiveTimer);
  const busy = !!(_roadLive && _roadLive.continuous
                  && _roadLive.continuous.current);
  _roadLiveTimer = setTimeout(loadRoadLive,
                              busy ? ROAD_LIVE_POLL_BUSY_MS : ROAD_LIVE_POLL_MS);
}

// ★ 누적 탐지율 (2026-08-19 전수조사).
//
//    화면에는 「부산 CCTV 에서 실사용 수준이 아니다」라는 **정성적 경고**만
//    있었다. 그것이 지금도 사실인지 확인할 숫자가 없어, 관제요원은 「그래서
//    얼마나 못 찾는가」를 알 수 없었고 모델을 바꿔도 **나아졌는지 증명할
//    수단이 없었다.**
//
// ⚠️ 실패한 관측은 분모에서 뺀다 — 스트림이 끊겨 못 본 것을 「못 찾았다」로
//    세면 모델이 실제보다 나빠 보인다. 대신 실패 횟수를 함께 적는다.
// ★ 구간 보정 진척 (2026-08-19 전수조사).
//
//    39지점 **전부** 구간 길이가 비어 있어 정비 등급이 늘 「미보정」이었다.
//    입력 화면(S-80)은 있는데 아무도 채우지 않았고, **화면 어디에도 그
//    사실이 드러나지 않았다.**
//
// ⚠️ 「미보정」만 보이면 고장으로 읽거나 그냥 넘긴다. **몇 지점이 비었는지와
//    어디서 채우는지**를 함께 말한다.
function _roadCalibHtml(cal) {
  if (!cal || !cal.total) return "";
  const left = cal.total - cal.with_section;
  if (left <= 0) return "";
  const all = cal.with_section === 0;
  return `<div class="rl-stats ${all ? "rl-stats--bad" : ""}">
      <b>구간 보정 ${cal.with_section}/${cal.total}</b> —
      ${left}개 지점에 <b>구간 길이가 없습니다.</b>
      <div class="rl-stats-note">
        구간 길이가 없으면 손상 밀도(건/100m)를 계산할 수 없어
        <b>정비 등급이 「미보정」으로만 나옵니다.</b>
        ⚠ <b>「미보정」은 「양호」가 아닙니다</b> — 점검이 끝났다는 뜻이 아닙니다.<br>
        입력: <b>설정 › CCTV 관리(S-80)</b> → 지점 수정 → 「구간 길이」
      </div>
    </div>`;
}

// ★ 등급 쏠림 (2026-08-19 전수조사).
//
//    실측 인파 관측 **670회 중 627회(93.6%)가 같은 등급**이었다. 원인은
//    판정 문턱 `disp_hi=0.6` 인데, 실측 dispersion 평균이 **0.888**이라
//    거의 언제나 「이동흐름혼란」이 떴다.
//
// ⚠️ 계산이 틀린 것이 아니다 — dispersion 은 「사람들이 서로 다른 방향으로
//    가는 정도」라 **교차로에서는 본래 높다.** 문턱이 교차로에 안 맞는 것이다.
//
// ★ **등급이 늘 같으면 그 등급은 정보가 아니다.** 관제요원이 「지금 평소와
//    다른가」에 답할 수 없다. 그래서 쏠림을 숫자로 드러낸다.
function _crowdSpreadHtml(s) {
  if (!s || !s.total || s.top_ratio == null) return "";
  // 8할 넘게 한 등급이면 변별력이 사실상 없다.
  if (s.top_ratio < 0.8) return "";
  const pct = (s.top_ratio * 100).toFixed(1);
  return `<div class="rl-stats rl-stats--bad">
      <b>최근 ${s.days}일 실측</b> — 관측 <b>${s.total}회</b> 중
      <b>${pct}%</b>가 같은 등급(${s.top_severity})입니다.
      <div class="rl-stats-note">⚠ <b>등급이 거의 변하지 않아 「지금 평소와
        다른가」를 가리지 못합니다.</b> 판정 문턱이 이 지점의 특성과 맞지 않을
        수 있습니다 — 교차로처럼 흐름이 본래 갈리는 곳에서는 「이동흐름혼란」이
        상시 발동합니다. <b>현장 확인 후 문턱 조정이 필요합니다.</b></div>
    </div>`;
}

function _roadStatsHtml(s) {
  if (!s || s.analyzed == null) return "";
  if (s.analyzed <= 0) {
    return `<div class="rl-stats"><b>최근 ${s.days}일</b> — 분석이 성립한 관측이
      없습니다${s.failed ? ` (실패 ${s.failed}회)` : ""}.</div>`;
  }
  const pct = (s.detection_rate * 100).toFixed(1);
  // 탐지율이 5% 미만이면 「거의 못 찾는 중」이다. 눈에 띄게 표시한다.
  const bad = s.detection_rate < 0.05;
  return `<div class="rl-stats ${bad ? "rl-stats--bad" : ""}">
      <b>최근 ${s.days}일 실측</b> —
      관측 <b>${s.analyzed}회</b> 중 탐지 0건 <b>${s.zero_detection}회</b>,
      탐지율 <b>${pct}%</b>${s.failed ? ` · 분석 실패 ${s.failed}회` : ""}
      ${bad ? `<div class="rl-stats-note">⚠ <b>이 모델은 지금 거의 아무것도
        찾지 못하고 있습니다.</b> 탐지 0건을 「손상 없음」으로 읽으면 안 됩니다.
        자체 라벨로 재학습하기 전까지는 참고 자료로만 쓰십시오.</div>` : ""}
    </div>`;
}

function renderRoadLive() {
  const el = $("#road-live");
  const d = _roadLive;
  if (!el || !d) return;
  const pts = d.points || [];
  const c = d.counts || {};
  const cont = d.continuous || {};
  const focus = d.focus || {};

  if (!pts.length) {
    el.innerHTML = `
      <div class="crowd-live-head"><span class="ctitle">실시간 관제</span></div>
      <p class="hint">노면 탐지 대상으로 지정된 CCTV가 없습니다 —
        <b>설정 › CCTV 관리(S-80)</b>에서 지점을 선택한 뒤 「도로 노면 관리」를 체크하세요.</p>`;
    _teardownFocusVideo();
    return;
  }

  const contOn = !!cont.running;
  const periodMin = Math.round((cont.period_sec || 0) / 60);
  const nTargets = cont.target_count != null
    ? cont.target_count : (cont.targets || []).length;
  const cur = cont.current;

  // 순회는 직렬이라 한 바퀴에 「대상 수 × 15초」가 걸린다. 진행 상황이 안 보이면
  // 운영자는 15분 동안 화면이 그대로인 것을 보고 멈춘 줄 안다. 그래서 대상 총
  // 개수와 **지금 보고 있는 지점**을 함께 싣는다.
  let progress = "";
  if (contOn && cur) {
    progress = `<span class="rl-now">
        <i class="rl-dot"></i>
        <b>${cur.index}/${cur.total}</b> ${cur.name} 관측 중
        <span class="rl-sub">${cur.elapsed_sec}초 경과</span></span>`;
  } else if (contOn && nTargets === 0) {
    progress = `<span class="rl-sub">대상 없음 — CCTV 관리(S-80)에서 「상시」로 지정하세요</span>`;
  } else if (contOn) {
    const wait = cont.resume_in_sec;
    progress = `<span class="rl-sub">대기 중${
      wait != null ? ` · 다음 순회까지 ${fmtDur(wait)}` : ""}</span>`;
  }

  const head = `
    <div class="crowd-live-head">
      <span class="ctitle">실시간 관제</span>
      <span class="badge" style="background:${contOn ? "#3fb950" : "#6e7681"}">
        상시 순회 ${contOn ? "동작 중" : "정지"}</span>
      ${contOn ? `<span class="rl-sub">대상 ${nTargets}개소 · 지점당 ${periodMin}분 주기${
        cont.round ? ` · ${cont.round}회차` : ""}</span>` : ""}
    </div>
    ${progress ? `<div class="rl-progress">${progress}</div>` : ""}
    <div class="crowd-metrics rl-metrics">
      <div><span class="mk">지정 지점</span><b>${c.total || 0}</b></div>
      <div><span class="mk">상시</span><b>${c.continuous || 0}</b></div>
      <div><span class="mk">점검됨</span><b>${c.analyzed || 0}</b></div>
      <div><span class="mk">탐지 손상</span><b>${c.defects || 0}</b>건</div>
      <div><span class="mk">분석 실패</span><b class="${c.failed ? "rl-bad" : ""}">${c.failed || 0}</b></div>
      <div><span class="mk">갱신 지연</span><b class="${c.stale ? "rl-warn" : ""}">${c.stale || 0}</b></div>
      <div><span class="mk">반영 대기</span><b class="${c.pending ? "rl-warn" : ""}">${c.pending || 0}</b></div>
    </div>
    ${c.pending
      ? `<div class="src-note">ⓘ CCTV 관리(S-80)에서 방금 「상시」로 바꾼 지점이 있습니다.
           순회 스레드가 목록을 다시 읽는 중이며, 진행 중이던 관측이 끝나면 잡힙니다.</div>`
      : ""}`;

  const statsPanel = _roadStatsHtml(d.detection_stats);
  const calibPanel = _roadCalibHtml(d.calibration);
  const collectPanel = _roadCollectHtml(d.collect || {});
  const focused = focus.active ? pts.find((p) => p.block_id === focus.camera_id) : null;
  const focusPanel = focus.active
    ? `<div class="rl-focus">
         <div class="rl-focus-head">
           <span class="ctitle">◉ 집중 감시 · ${focus.camera_name || focus.camera_id}</span>
           <button class="livebtn" onclick="stopRoadFocus()">■ 감시 중지</button>
         </div>
         <div class="rl-focus-body">
           <div class="rl-focus-video">
             ${focused && focused.stream_url
               ? `<div class="video-wrap">
                    <video id="road-focus-video" muted playsinline></video>
                    <svg id="road-focus-roi" class="roi-overlay" preserveAspectRatio="none"></svg>
                  </div>`
               : '<p class="hint">이 지점은 라이브 스트림이 없습니다(업로드 동영상).</p>'}
           </div>
           <div class="rl-focus-info">
             <div class="crowd-metrics">
               <div><span class="mk">관측 주기</span><b>${Math.round(focus.period_sec || 0)}</b>초</div>
               <div><span class="mk">누적 관측</span><b>${focus.rounds || 0}</b>회</div>
               <div><span class="mk">남은 시한</span><b>${fmtDur(focus.remaining_sec)}</b></div>
               <div><span class="mk">최근 결과</span><b>${focused ? focused.grade_label : "—"}</b></div>
             </div>
             ${focus.last_note ? `<div class="src-note">⚠ ${focus.last_note}</div>` : ""}
             <div class="src-note">ⓘ 결빙·낙하물처럼 분 단위로 변하는 상황을 위한 기능입니다.
               분석기가 하나뿐이라 <b>한 번에 한 지점만</b> 감시하며,
               시한이 지나면 자동으로 멈춥니다.</div>
             ${_roadHistoryHtml(focused)}
           </div>
         </div>
       </div>`
    : (focus.last
        ? `<div class="src-note">ⓘ 직전 집중 감시(${focus.last.camera_name || "—"})가 종료되었습니다 —
             ${focus.last.stop_reason || ""} 총 ${focus.last.rounds || 0}회 관측.</div>`
        : "");

  el.innerHTML = head + statsPanel + calibPanel + collectPanel + focusPanel
    + `<div class="rl-grid">${pts.map(_roadLiveCard).join("")}</div>`;
  _syncFocusVideo(focus.active ? focused : null);
}

// 학습 데이터 자동 수집 (Phase 3). 현재 모델은 부산 CCTV에서 탐지 0건이고,
// 그 원인이 도메인 갭이라 **자체 데이터 말고는 길이 없다**. 상시 순회가 어차피
// 뽑는 프레임을 함께 남기는 기능이다.
//
// 기본이 꺼짐인 이유 — 도로 영상을 디스크에 계속 쌓고, 사람은 가리지만
// 차량번호판은 가리지 못한다. 켜는 것은 개인정보 검토를 거친 운영 판단이다.
function _roadCollectHtml(c) {
  if (!c || c.enabled === undefined) return "";
  const on = !!c.enabled;
  const pct = c.max_mb ? Math.min(100, (c.size_mb / c.max_mb) * 100) : 0;
  return `
    <div class="rl-collect ${on ? "rl-collect--on" : ""}">
      <div class="rl-collect-head">
        <span class="ctitle">학습 데이터 자동 수집
          <span class="badge" style="background:${on ? "#3fb950" : "#6e7681"}">${on ? "수집 중" : "꺼짐"}</span></span>
        <button class="livebtn" onclick="toggleRoadCollect(${on ? "false" : "true"})"
          ${_roadCollectBusy ? "disabled" : ""}>${on ? "■ 수집 중지" : "● 수집 시작"}</button>
      </div>
      <div class="crowd-metrics">
        <div><span class="mk">누적 프레임</span><b>${c.frames || 0}</b>장</div>
        <div><span class="mk">사용 용량</span><b>${c.size_mb || 0}</b> / ${c.max_mb}MB</div>
        <div><span class="mk">지점당 하루</span><b>${c.per_day}</b>장</div>
        <div><span class="mk">최소 간격</span><b>${Math.round((c.min_interval_sec || 0) / 60)}</b>분</div>
      </div>
      <div class="rl-bar"><i style="width:${pct}%"></i></div>
      ${on
        ? `<div class="mock-hint">⚠ <b>사람은 자동으로 가리지만 차량번호판은 가리지 못합니다.</b>
             수집한 프레임을 외부로 내보내기 전 육안 확인이 필요합니다.
             가리지 못한 프레임은 저장하지 않습니다.</div>`
        : `<div class="src-note">ⓘ 현재 모델은 부산 CCTV에서 탐지 0건입니다(도메인 갭).
             자체 데이터로 재학습하려면 프레임 확보가 먼저입니다.
             켜면 상시 순회가 뽑는 프레임을 함께 남깁니다 — 스트림을 새로 열지 않습니다.</div>`}
      <div class="src-note">저장 위치 <code>${c.dir || ""}</code>
        ${c.last_reason ? ` · 최근 처리: ${c.last_reason}` : ""}</div>
    </div>`;
}

// 가진 영상에서 프레임 뽑기.
// 등록된 부산 교통 CCTV는 대부분 320~352×240이라 사람이 손상 박스를 그을 수
// 없다(적합도 평가: 8곳 중 1곳만 「조건부」). 현장 촬영·블랙박스 영상이 있으면
// 그쪽이 훨씬 쓸 만하므로, 자동 수집과 별개로 직접 넣을 통로를 둔다.
//
// ⚠️ 이 폼은 **한 번만 그린다.** 실시간 관제 패널처럼 주기적으로 다시 그리면
// 선택해 둔 파일이 10초마다 사라진다(파일 입력은 값을 되돌려 놓을 수 없다).
// 바뀌는 것은 결과 영역과 버튼 상태뿐이라 그 둘만 갱신한다.
function renderRoadUploadForm() {
  const el = $("#road-train");
  if (!el || el.dataset.ready === "1") return;
  el.dataset.ready = "1";
  el.innerHTML = `
    <div class="crowd-live-head">
      <span class="ctitle">가진 영상에서 프레임 뽑기</span>
      <span class="rl-sub">현장 촬영·블랙박스 영상 (mp4/avi/mov/mkv/webm, 500MB 이하)</span>
    </div>
    <div class="src-row">
      <input type="file" id="road-train-file" accept="video/*" class="rl-file">
      <input type="text" id="road-train-label" class="road-select rl-label"
             placeholder="자료 이름 (예: 2026-08 광안대로 순찰 영상)">
    </div>
    <div class="src-row">
      <span class="src-label">추출 간격</span>
      <select id="road-train-interval" class="road-select rl-narrow">
        <option value="1">1초마다</option>
        <option value="2" selected>2초마다</option>
        <option value="5">5초마다</option>
        <option value="10">10초마다</option>
      </select>
      <span class="src-label">최대 장수</span>
      <select id="road-train-max" class="road-select rl-narrow">
        <option value="30">30장</option>
        <option value="60" selected>60장</option>
        <option value="120">120장</option>
        <option value="240">240장</option>
      </select>
      <button class="livebtn livebtn-lg" id="road-train-btn"
        onclick="uploadRoadTrainingVideo()">▶ 프레임 추출</button>
    </div>
    <div class="src-note">ⓘ 영상 전체에 고르게 퍼뜨려 뽑습니다(앞부분만 뽑으면
      촬영 초반만 학습하게 됩니다). 사람은 자동으로 가리며,
      <b>가리지 못한 프레임은 저장하지 않습니다.</b>
      <b>원본 영상은 보관하지 않습니다</b> — 프레임만 남깁니다.
      마스킹에 시간이 걸려 60장 기준 1~2분 걸릴 수 있습니다.</div>
    <div id="road-train-result"></div>`;
}

function renderRoadUploadState() {
  const btn = document.getElementById("road-train-btn");
  if (btn) {
    btn.disabled = _roadUploadBusy;
    btn.textContent = _roadUploadBusy ? "추출 중…" : "▶ 프레임 추출";
  }
  const out = document.getElementById("road-train-result");
  if (!out) return;
  const r = _roadUploadResult;
  if (_roadUploadBusy) {
    out.innerHTML = `<div class="src-note">프레임을 뽑고 사람을 가리는 중입니다.
      창을 닫지 마세요.</div>`;
  } else if (r && r.ok) {
    out.innerHTML = `<div class="src-note rl-ok">✓ ${r.saved}장 저장
      ${r.resolution ? `· ${r.resolution}` : ""}
      ${r.duration_sec ? `· 원본 ${fmtDur(r.duration_sec)}` : ""}
      ${r.mask_skipped ? `· 마스킹 실패 ${r.mask_skipped}장 제외` : ""}
      <br>폴더 <code>${r.dir_id}</code> — 라벨링:
      <code>python scripts/label_road_defects.py --folder ${r.dir || ""}</code></div>`;
  } else if (r && r.error) {
    out.innerHTML = `<div class="src-note src-err">✗ ${r.error}</div>`;
  } else {
    out.innerHTML = "";
  }
}

window.uploadRoadTrainingVideo = async function () {
  const f = document.getElementById("road-train-file");
  if (!f || !f.files || !f.files.length) {
    alert("영상 파일을 선택해 주세요.");
    return;
  }
  const fd = new FormData();
  fd.append("file", f.files[0]);
  fd.append("label", (document.getElementById("road-train-label") || {}).value || "");
  fd.append("interval_sec", document.getElementById("road-train-interval").value);
  fd.append("max_frames", document.getElementById("road-train-max").value);

  _roadUploadBusy = true;
  _roadUploadResult = null;
  renderRoadUploadState();
  try {
    const res = await fetch("/api/road/collect/video", { method: "POST", body: fd });
    if (res.status === 403) {
      _roadUploadResult = { error: "이 기능은 시스템 설정 권한이 있어야 씁니다." };
    } else {
      _roadUploadResult = await res.json();
    }
  } catch (e) {
    _roadUploadResult = { error: "업로드에 실패했습니다: " + e.message };
  }
  _roadUploadBusy = false;
  renderRoadUploadState();
  // 누적 프레임·용량이 늘었으므로 수집 현황을 다시 받아 온다.
  await loadRoadLive();
  if (_roadUploadResult && _roadUploadResult.note) alert(_roadUploadResult.note);
};

window.toggleRoadCollect = async function (enable) {
  if (_roadCollectBusy) return;
  if (enable && !confirm(
      "도로 영상을 학습 데이터로 저장하기 시작합니다.\n\n" +
      "· 사람은 자동으로 가립니다 (가리지 못한 프레임은 저장하지 않습니다)\n" +
      "· 차량번호판은 가리지 못합니다\n\n" +
      "개인정보 검토를 마쳤습니까?")) return;
  _roadCollectBusy = true;
  renderRoadLive();
  try {
    const r = await fetch("/api/road/collect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enable }),
    });
    if (r.status === 403) alert("이 설정은 시스템 설정 권한이 있어야 바꿀 수 있습니다.");
    else {
      const d = await r.json();
      if (d.note) alert(d.note);
    }
  } catch (e) {
    alert("설정을 바꾸지 못했습니다: " + e.message);
  }
  _roadCollectBusy = false;
  await loadRoadLive();
};

// 막대 하나가 관측 1회. 손상 건수가 늘고 있는지를 눈금 없이 추세로만 본다.
// `compact` 는 지점 카드용 — 좁아서 제목을 줄이고 막대도 낮춘다.
function _roadHistoryHtml(p, compact = false) {
  const h = (p && p.history) || [];
  // 1회뿐이면 「추이」라 부를 것이 없다. 다만 카드에서는 관측이 있었다는
  // 사실 자체가 정보라 한 칸이라도 그린다.
  if (!h.length || (!compact && h.length < 2)) return "";
  const max = Math.max(1, ...h.map((x) => x.defect_count));
  const bars = h.map((x) => {
    const pct = x.failed ? 100 : Math.max(6, (x.defect_count / max) * 100);
    const col = x.failed ? "#e5484d" : (x.defect_count ? "#e8843b" : "#3fb950");
    const when = x.analyzed_at
      ? new Date(x.analyzed_at).toLocaleString("ko-KR", { hour12: false }) + " · "
      : "";
    const t = when + (x.failed ? "분석 실패" : `손상 ${x.defect_count}건`);
    return `<i style="height:${pct}%;background:${col}" title="${t}"></i>`;
  }).join("");
  const label = compact ? `추이 ${h.length}회` : `최근 ${h.length}회 추이`;
  return `<div class="rl-spark ${compact ? "rl-spark--sm" : ""}">
            <span class="mk">${label}</span>
            <div class="rl-spark-bars">${bars}</div></div>`;
}

function _roadLiveCard(p) {
  const c = p.analyzed ? (ROAD_GRADE_COLOR[p.grade] || "#6e7681")
                       : (p.failed ? "#e5484d" : "#6e7681");
  const modeTag = p.mode === "continuous" ? "상시" : "선택";
  const live = p.stream_url
    ? `<button class="livebtn" onclick="event.stopPropagation(); openLive('${p.block_id}','${p.name}','${p.stream_url}','road',${p.whep_url ? `'${p.whep_url}'` : "null"})">▶ 실시간</button>`
    : `<span class="rl-nostream" title="업로드 동영상은 브라우저에서 재생할 수 없습니다">영상 없음</span>`;
  const focusBtn = p.focused
    ? `<button class="livebtn" onclick="event.stopPropagation(); stopRoadFocus()">■ 감시 중지</button>`
    : `<button class="livebtn" onclick="event.stopPropagation(); startRoadFocus('${p.block_id}')" ${_roadLiveBusy ? "disabled" : ""}>◉ 집중 감시</button>`;

  // 경과 시간은 「언제 본 것인지」다. 기대 주기의 2배를 넘기면 그 지점은
  // 실제로 관측되지 않고 있다는 뜻이라 경고로 바꾼다.
  const ageHtml = p.analyzed || p.failed
    ? `<span class="rl-age ${p.stale ? "rl-warn" : ""}">${fmtAge(p.age_sec)}${p.stale ? " · 갱신 지연" : ""}</span>`
    : `<span class="rl-age">관측 이력 없음</span>`;

  // 「상시」 배지만 보고 관측 중이라고 읽으면 안 된다 — 워처가 아직 집어 들지
  // 않았으면 그 지점은 지금 아무도 보고 있지 않다.
  const pendingTag = p.pending
    ? '<span class="badge" style="background:#d4a017">반영 대기</span>' : "";
  // 지금 순회가 보고 있는 지점. 목록에서 바로 눈에 띄어야 「어디까지 왔는지」를
  // 헤더와 카드 양쪽에서 확인할 수 있다.
  const nowTag = p.analyzing
    ? '<span class="badge rl-blink" style="background:#58a6ff">관측 중</span>' : "";

  let body;
  if (p.analyzed) {
    body = `<div class="row">탐지 손상 <strong>${p.defect_count}건</strong> · 프레임 ${p.frames_analyzed}</div>`;
  } else if (p.failed) {
    body = `<div class="row" style="color:#ff9ea1">영상을 받지 못해 분석하지 못했습니다</div>`;
  } else if (p.pending) {
    body = `<div class="row muted">설정을 방금 바꿨습니다 — 순회 목록에 곧 들어갑니다</div>`;
  } else {
    body = `<div class="row muted">${p.mode === "continuous"
      ? "상시 순회 차례를 기다리는 중" : "선택 탐지 지점 — 자동 갱신 없음"}</div>`;
  }

  return `
    <div class="rl-card ${p.focused ? "rl-card--focus" : ""} ${p.stale ? "rl-card--stale" : ""} ${p.analyzing ? "rl-card--now" : ""} ${_roadHistFor === p.block_id ? "rl-card--picked" : ""}"
         onclick="showRoadHistory('${p.block_id}')">
      <div class="rl-card-head">
        <b>${p.name}</b>
        <span class="badge" style="background:${c}">${p.grade_label}</span>
        <span class="badge" style="background:${p.mode === "continuous" ? "#3fb950" : "#d4a017"}">${modeTag}</span>
        ${pendingTag}${nowTag}
        ${p.focused ? '<span class="badge" style="background:#58a6ff">감시 중</span>' : ""}
        ${p.quality_grade === "crit" ? '<span class="badge" style="background:#e5484d" title="상시 화질 감시 — 재배포 경로에서 반복적인 디코더 오류 감지">화질 손상</span>'
          : p.quality_grade === "warn" ? '<span class="badge" style="background:#d4a017" title="상시 화질 감시 — 재배포 경로에서 디코더 오류 감지">화질 저하</span>' : ""}
      </div>
      ${body}
      <div class="row">${ageHtml}</div>
      ${_roadHistoryHtml(p, true)}
      ${p.note ? `<div class="row" style="color:#d4a017;font-size:.76rem">⚠ ${p.note}</div>` : ""}
      <div class="rl-card-btns">${live}${focusBtn}</div>
    </div>`;
}

// 집중 감시 지점의 인라인 라이브 영상. 같은 지점이면 스트림을 다시 만들지
// 않는다 — 10초마다 재생성하면 영상이 계속 끊긴다.
function _syncFocusVideo(p) {
  if (!p || !p.stream_url) { _teardownFocusVideo(); return; }
  const video = document.getElementById("road-focus-video");
  if (!video) { _teardownFocusVideo(); return; }
  // ⚠️ 2026-08-26 — "집중 감시" 인라인 영상에는 ROI 오버레이가 아예 없던
  // 결함이었다(강서구청 사례 조사 중 함께 발견). 10초 폴링마다 이 함수를
  // 부르는 el.innerHTML 재작성으로 <svg id="road-focus-roi"> 자체가 매번
  // 새로 만들어지므로("같은 지점" 분기와 무관하게), 여기서 매번 다시
  // 그린다 — 가벼운 fetch 한 번이라 성능에 영향이 없다.
  loadRoiOverlayInto(p.block_id, "road", "road-focus-roi", null, null);
  if (_roadFocusVideoFor === p.block_id && _roadFocusHls) {
    // innerHTML 을 다시 그렸으므로 media 를 새 element 에 다시 붙인다.
    _roadFocusHls.attachMedia(video);
    video.play().catch(() => {});
    return;
  }
  _teardownFocusVideo();
  _roadFocusVideoFor = p.block_id;
  if (window.Hls && window.Hls.isSupported()) {
    _roadFocusHls = new Hls({ liveSyncDurationCount: 3 });
    _roadFocusHls.loadSource(p.stream_url);
    _roadFocusHls.attachMedia(video);
    _roadFocusHls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
  } else {
    video.src = p.stream_url;          // Safari 등 네이티브 HLS
    video.play().catch(() => {});
  }
}

function _teardownFocusVideo() {
  if (_roadFocusHls) { _roadFocusHls.destroy(); _roadFocusHls = null; }
  _roadFocusVideoFor = null;
}

// ---- 지점별 관측 이력 (S-44) ------------------------------------------------
// 「지금 상태」만으로는 나빠지고 있는지 알 수 없다. 손상 4건이 어제도 4건이었는지
// 0건에서 늘어난 것인지가 보수 우선순위를 가른다.
window.showRoadHistory = async function (cameraId) {
  if (_roadHistFor === cameraId) {   // 같은 카드를 다시 누르면 닫는다
    _roadHistFor = null;
    _roadHistData = null;
    renderRoadHistory();
    renderRoadLive();
    return;
  }
  _roadHistFor = cameraId;
  _roadHistData = null;
  renderRoadHistory();
  renderRoadLive();               // 선택 표시를 카드에 반영
  try {
    const r = await fetch(`/api/road/history/${encodeURIComponent(cameraId)}`);
    const d = await r.json();
    if (_roadHistFor === cameraId) _roadHistData = d;   // 그새 바뀌었으면 버린다
  } catch (e) {
    if (_roadHistFor === cameraId) _roadHistData = { error: e.message };
  }
  renderRoadHistory();
};

function renderRoadHistory() {
  const el = $("#road-history");
  if (!el) return;
  if (!_roadHistFor) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");

  const d = _roadHistData;
  if (!d) {
    el.innerHTML = '<p class="hint">이력을 불러오는 중…</p>';
    return;
  }
  if (d.error) {
    el.innerHTML = `<p class="hint">이력을 불러오지 못했습니다: ${d.error}</p>`;
    return;
  }

  const rows = d.history || [];
  const body = rows.length
    ? rows.map((x) => {
        const when = x.analyzed_at
          ? new Date(x.analyzed_at).toLocaleString("ko-KR", { hour12: false })
          : "—";
        const cls = x.failed ? "rl-bad" : (x.defect_count ? "rl-warn" : "");
        const label = x.failed ? "분석 실패"
          : (ROAD_GRADE_NAME[x.grade] || (x.grade == null ? "—" : x.grade));
        return `<tr>
            <td>${when}</td>
            <td class="${cls}">${label}</td>
            <td class="rl-num">${x.failed ? "—" : x.defect_count}</td>
            <td class="rl-num">${x.frames_analyzed}</td>
            <td>${ROAD_SOURCE_NAME[x.source] || x.source || "—"}</td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="5" class="hint">아직 관측 이력이 없습니다.</td></tr>`;

  el.innerHTML = `
    <div class="crowd-live-head">
      <span class="ctitle">관측 이력 · ${d.name}</span>
      <span class="rl-sub">${d.count}회</span>
      <button class="livebtn" onclick="showRoadHistory('${d.camera_id}')">✕ 닫기</button>
    </div>
    ${d.persisted ? "" :
      `<div class="src-note">⚠ 이 이력은 <b>메모리에만 있습니다.</b>
         서비스를 다시 시작하면 사라집니다. 손상이 잡힌 건은 이벤트 목록에 남습니다.</div>`}
    <div class="rl-table-wrap">
      <table class="rl-table">
        <thead><tr><th>관측 시각</th><th>등급</th><th>손상</th><th>프레임</th><th>출처</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

window.startRoadFocus = async function (cameraId) {
  if (_roadLiveBusy) return;
  _roadLiveBusy = true;
  renderRoadLive();
  try {
    const r = await fetch("/api/road/live/focus", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ camera_id: cameraId }),
    });
    const data = await r.json();
    if (data.error) alert(data.error);
  } catch (e) {
    alert("집중 감시를 시작하지 못했습니다: " + e.message);
  }
  _roadLiveBusy = false;
  await loadRoadLive();
};

window.stopRoadFocus = async function () {
  if (_roadLiveBusy) return;
  _roadLiveBusy = true;
  try {
    await fetch("/api/road/live/focus/stop", { method: "POST" });
  } catch (e) { /* 상태는 다음 폴링에서 맞춰진다 */ }
  _teardownFocusVideo();
  _roadLiveBusy = false;
  await loadRoadLive();
};

// ---- 도로 노면 AI 탐지 실행 (기존 영상 / 실시간 CCTV 선택) -------------------
// 관리자가 모드와 대상을 고르고 실행하면 실제 모델 추론 결과를 보여준다.
// ⚠️ 현재 모델은 도메인 갭으로 탐지 0건이다(docs/road_surface_management_plan.md
// Phase 2). 0건 표시는 버그가 아니라 모델 한계이며, 화면에도 그렇게 안내한다.
const ROAD_CCTV_DURATION_SEC = 15;   // road/live_analyzer.py의 CCTV_DURATION_SEC와 일치
let _roadTargets = null;
let _roadMode = "video";
let _roadBusy = false;
let _roadResult = null;

async function loadRoadAnalysis() {
  const el = $("#road-analysis");
  if (!el) return;
  if (!_roadTargets) {
    try {
      _roadTargets = await fetchJsonOrServiceDown("/api/road-analysis/targets");
    } catch (e) {
      el.innerHTML = e.serviceDown
        ? '<p class="hint" style="color:#e5484d">⚠ 노면관리 서비스 연결 안 됨 — 잠시 후 다시 시도하세요.</p>'
        : '<p class="hint">분석 대상 목록을 불러오지 못했습니다.</p>';
      return;
    }
  }
  renderRoadAnalysis();
}

function renderRoadAnalysis() {
  const el = $("#road-analysis");
  const t = _roadTargets || { videos: [], cctv: [], model: {} };
  const opts = _roadMode === "video"
    ? (t.videos || []).map((v) =>
        `<option value="${v.id}">${v.name} (${v.resolution}, ${v.frames}프레임)</option>`).join("")
    : (t.cctv || []).map((c) =>
        `<option value="${c.id}">${c.name}${
          c.mode === "continuous" ? " [상시]" : " [선택]"}</option>`).join("");

  const m = t.model || {};
  const modelWarn = !m.exists
    ? `<div class="mock-hint">⚠ 학습 모델 파일이 없습니다: <code>${m.model_path || "-"}</code></div>`
    : `<div class="src-note">모델: <code>${m.model_path}</code></div>`;

  el.innerHTML = `
    <div class="crowd-live-head"><span class="ctitle">AI 손상 탐지 실행</span></div>
    ${modelWarn}
    <div class="mock-hint">⚠ <b>현재 모델은 확보한 영상·부산 CCTV 모두에서 탐지 0건</b>입니다
      (학습 데이터와의 도메인 갭 — Phase 2 검증 결과). 결과가 0건인 것은 오류가 아니라
      모델의 한계이며, 자체 데이터 확보 후 재학습이 필요합니다.</div>
    <div class="src-switch">
      <div class="src-row">
        <span class="src-label">분석 모드</span>
        <button class="srcbtn ${_roadMode === "video" ? "on" : ""}" onclick="setRoadMode('video')">기존 영상</button>
        <button class="srcbtn ${_roadMode === "cctv" ? "on" : ""}" onclick="setRoadMode('cctv')">실시간 CCTV</button>
      </div>
      <div class="src-row">
        <span class="src-label">${_roadMode === "video" ? "영상 선택" : "지점 선택"}</span>
        <select id="road-target" class="road-select">${opts}</select>
        <button class="livebtn livebtn-lg" onclick="runRoadAnalysis()" ${_roadBusy ? "disabled" : ""}>
          ${_roadBusy
            ? (_roadMode === "cctv" ? "관측 중… (15초)" : "분석 중…")
            : "▶ 분석 실행"}</button>
      </div>
      ${_roadMode === "cctv"
        ? `<div class="src-note">ⓘ 실시간 모드는 <b>${ROAD_CCTV_DURATION_SEC}초</b> 동안 스트림을
             관측하며 표본을 뽑습니다(순간 지나가는 차량·그림자에 결과가 좌우되지 않도록).
             표본 장수는 스트림 속도에 따라 달라지며, <b>모델 추론 시간이 더해져 전체 응답은
             20~30초</b>가 걸릴 수 있습니다.</div>`
        : `<div class="src-note">ⓘ 영상 모드는 파일 전체를 균등 간격으로 12장 표본 추출합니다.</div>`}
    </div>
    <div id="road-analysis-result">${_roadResultHtml()}</div>`;
}

function _roadResultHtml() {
  const r = _roadResult;
  if (!r) return '<p class="hint">모드와 대상을 선택한 뒤 「분석 실행」을 누르세요.</p>';
  if (r.note) return `<p class="hint">⚠ ${r.note}</p>`;
  const c = ROAD_GRADE_COLOR[r.grade] || "#6e7681";
  const rows = (r.defects || []).length
    ? r.defects.map((d) => `<div class="alert-row">
        <span>프레임 ${d.frame} · [${d.type}] 신뢰도 ${(d.confidence * 100).toFixed(0)}%
          · 위치 (${d.box[0]}, ${d.box[1]})</span></div>`).join("")
    : `<p class="hint">탐지된 손상 없음 — 위 안내대로 <b>현재 모델의 한계</b>일 가능성이 높습니다.</p>`;
  return `
    <div class="crowd-live-head">
      <span class="ctitle">분석 결과 · ${r.target_name}</span>
      <span class="badge" style="background:${c}">${r.grade_label}</span>
    </div>
    <div class="crowd-metrics">
      <div><span class="mk">모드</span><b>${r.mode === "video" ? "기존 영상" : "실시간 CCTV"}</b></div>
      <div><span class="mk">분석 프레임</span><b>${r.frames_analyzed}</b></div>
      <div><span class="mk">탐지 손상</span><b>${r.defect_count}</b>건</div>
      <div><span class="mk">신뢰도 임계</span><b>${r.conf}</b></div>
      <div><span class="mk">소요</span><b>${r.elapsed_sec}</b>초</div>
    </div>
    ${rows}`;
}

window.setRoadMode = function (mode) {
  _roadMode = mode;
  _roadResult = null;
  renderRoadAnalysis();
};

window.runRoadAnalysis = async function () {
  const sel = document.getElementById("road-target");
  if (!sel || !sel.value) return;
  const target = sel.value;
  const label = sel.options[sel.selectedIndex].textContent;

  // 실시간 모드는 15초간 관측하므로, 그동안 무엇을 보고 있는지 확인할 수 있도록
  // 같은 지점의 CCTV를 팝업으로 함께 띄운다(기존 실시간 모달 재사용).
  if (_roadMode === "cctv") {
    const b = (window.INITIAL_BLOCKS || []).find((x) => x.id === target);
    // ★ 2026-08-28 — CCTV 재배포 허브가 켜져 있으면 source.url이 내부용
    // RTSP 주소로 치환돼 있다(core/cameras.py::to_block_dict 참고) — hls.js
    // 는 그걸 재생할 수 없으므로, 원본이 남아 있는 origin_url을 우선한다.
    const url = b && b.source && b.source.type === "hls"
      ? (b.source.origin_url || b.source.url) : null;
    if (url) openLive(target, (b.name || target) + " · 분석 중", url, "road", null);
  }

  _roadBusy = true;
  renderRoadAnalysis();
  try {
    const r = await fetch("/api/road-analysis/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: _roadMode, target }),
    });
    _roadResult = await r.json();
  } catch (e) {
    _roadResult = { note: "분석 오류: " + e.message };
  }
  _roadBusy = false;
  renderRoadAnalysis();

  // 분석이 끝나면 결과를 볼 수 있게 팝업 제목만 갱신하고 열어 둔다
  // (자동으로 닫으면 방금 무엇을 봤는지 확인할 기회가 없어진다).
  const t = document.getElementById("livetitle");
  if (_roadMode === "cctv" && t && _roadResult) {
    const n = _roadResult.defect_count;
    t.textContent = `${_roadResult.target_name} · 분석 완료 — 손상 ${n == null ? "?" : n}건`;
  }
};

// ---- 도로 노면 관리 (Phase 1: UI 뼈대 + mock 데이터) ------------------------
// docs/road_surface_management_plan.md 참고. /api/road/* 는 아직 실제 AI 탐지
// 모델이 없어 road/mock_data.py가 만든 가짜 데이터를 내려준다(응답에 항상
// mock:true 포함). 실제 모델이 붙으면 이 함수들은 그대로 두고 백엔드만 교체하면 된다.
const ROAD_GRADE_COLOR = { 1: "#3fb950", 2: "#79c0ff", 3: "#d4a017", 4: "#e5484d" };

async function loadRoadList() {
  const container = $("#road-list");
  let data;
  try {
    data = await fetchJsonOrServiceDown("/api/road/blocks");
  } catch (e) {
    // ⚠️ 2026-08-31 — 이전에는 여기 오류 처리가 아예 없었다(발견·수정).
    // 노면관리 서비스(road-service)가 안 떠 있으면 이 목록이 예외로
    // 조용히 빈 채 남아 원인을 알 수 없었다.
    container.innerHTML = e.serviceDown
      ? '<p class="hint" style="color:#e5484d">⚠ 노면관리 서비스 연결 안 됨 — 잠시 후 다시 시도하세요.</p>'
      : '<p class="hint">노면 현황을 불러오지 못했습니다.</p>';
    return;
  }
  container.innerHTML = "";
  const blocks = data.blocks || [];
  if (!blocks.length) {
    container.innerHTML =
      '<p class="hint">노면 탐지 대상으로 지정된 CCTV가 없습니다 — ' +
      '<b>설정 › CCTV 관리(S-80)</b>에서 지점을 선택한 뒤 「도로 노면 관리」를 ' +
      '체크하세요.</p>';
    return;
  }
  for (const b of blocks) {
    // 분석한 적이 없으면 등급을 매기지 않는다 — 0등급으로 두면 「정상」처럼
    // 보여 점검하지 않은 구간을 안전한 것으로 오해한다.
    const c = b.analyzed ? (ROAD_GRADE_COLOR[b.grade] || "#6e7681")
                         : (b.failed ? "#e5484d" : "#6e7681");
    const when = b.analyzed_at
      ? new Date(b.analyzed_at).toLocaleString("ko-KR", { hour12: false })
      : "";
    const modeTag = b.mode === "continuous" ? "상시" : "선택";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h3>${b.name}
        <span class="badge" style="background:${c}">${b.grade_label}</span>
        <span class="badge" style="background:${b.mode === "continuous" ? "#3fb950" : "#d4a017"}">${modeTag}</span></h3>
      ${b.analyzed
        ? `<div class="row">탐지된 손상 <strong>${b.defect_count}건</strong>
             · 분석 프레임 ${b.frames_analyzed}</div>
           <div class="row muted" style="font-size:.76rem">${when}</div>
           ${b.note ? `<div class="row" style="color:#d4a017;font-size:.76rem">⚠ ${b.note}</div>` : ""}`
        : b.failed
          ? `<div class="row" style="color:#ff9ea1">영상을 받지 못해 분석하지 못했습니다</div>
             <div class="row muted" style="font-size:.76rem">${b.note}</div>
             <div class="row muted" style="font-size:.74rem">${when}</div>`
          : `<div class="row muted">아직 분석하지 않았습니다</div>
             <div class="row muted" style="font-size:.76rem">
               ${b.mode === "continuous" ? "상시 순회 차례를 기다리는 중" : "아래 버튼으로 분석하세요"}</div>`}
      <div class="row" style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="livebtn" onclick="event.stopPropagation(); runRoadFor('${b.block_id}','${b.name}')">▶ 분석 실행</button>
        <button class="livebtn" onclick="event.stopPropagation(); openRoadReport('${b.block_id}','${b.name}')">📄 보고서</button>
      </div>
    `;
    card.addEventListener("click", () => loadRoadDetail(b.block_id));
    container.appendChild(card);
  }
}

// 노면 현황 카드에서 바로 그 지점을 분석한다. S-41로 옮겨 다시 고르는 수고를
// 없앤다 — 관제요원이 보고 있는 지점이 곧 분석할 지점이다.
window.runRoadFor = async function (blockId, name) {
  // 진행 상황을 **오른쪽 상세 패널**에 그린다. 목록 위에 띄우면 방금 무엇을
  // 눌렀는지와 결과가 서로 떨어져 있어 읽히지 않는다.
  const detail = document.getElementById("road-detail");
  const t0 = Date.now();
  const tick = setInterval(() => {
    const el = document.getElementById("road-run-sec");
    if (el) el.textContent = Math.round((Date.now() - t0) / 1000);
  }, 1000);
  detail.innerHTML = `
    <h2>${name}</h2>
    <p class="hint">분석 중입니다 — 약 15초 관측 후 모델 추론이 이어집니다.<br>
      경과 <b id="road-run-sec">0</b>초</p>`;

  let res = null, err = null;
  try {
    const r = await fetch("/api/road-analysis/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "cctv", target: blockId }),
    });
    res = await r.json();
  } catch (e) {
    err = e.message;
  }
  clearInterval(tick);

  // 프레임을 한 장도 못 받았으면 왜 그런지까지 알려 준다. 「실패」만 띄우면
  // 운영자가 다음에 무엇을 할지 알 수 없다.
  if (err || (res && (res.frames_analyzed ?? 0) === 0)) {
    const why = err || res.note || "영상을 받지 못했습니다.";
    detail.innerHTML = `
      <h2>${name} <span class="badge" style="background:#e5484d">분석 실패</span></h2>
      <p class="ug-error" role="alert">${why}</p>
      <p class="hint" style="font-size:.84rem">
        <b>흔한 원인</b><br>
        · 같은 카메라를 <b>침수·인파가 이미 상시로 보고 있어</b> 연결이 거부됨
        — 설정 › CCTV 관리에서 상시 지정을 줄여 보세요<br>
        · 스트림이 일시적으로 끊김 — 잠시 뒤 다시 실행<br>
        · 동영상 지점인데 파일이 없음 — CCTV 관리에서 경로 확인
      </p>
      <div class="road-live-bar">
        <button class="livebtn livebtn-lg" onclick="runRoadFor('${blockId}','${name}')">↻ 다시 실행</button>
      </div>`;
    await loadRoadList();
    return;
  }

  await loadRoadList();
  await loadRoadDetail(blockId);      // 결과를 상세 패널에 그대로 남긴다
};

// 등급 3(보수 필요) 이상을 "대응이 필요한 상황"으로 간주해 실시간 CCTV 진입을 노출한다.
const ROAD_ACTION_GRADE = 3;

function roadStreamSource(blockId) {
  const b = (window.INITIAL_BLOCKS || []).find((x) => x.id === blockId);
  const src = b && b.source;
  if (!src || src.type !== "hls" || !src.url) return null;
  // ★ 2026-08-28 — 재배포 허브가 켜져 있으면 src.url이 내부용 RTSP
  // 주소로 치환돼 있다 — hls.js가 재생할 수 있는 원본을 우선한다
  // (core/cameras.py::to_block_dict, runRoadAnalysis()의 같은 처리 참고).
  return { name: b.name, url: src.origin_url || src.url };
}

async function loadRoadDetail(blockId) {
  const detail = $("#road-detail");
  detail.innerHTML = '<p class="hint">불러오는 중...</p>';
  let b;
  try {
    const res = await fetch(`/api/road/${encodeURIComponent(blockId)}`);
    b = await res.json();
    if (!res.ok || b.error) throw new Error(b.error || res.status);
  } catch (e) {
    detail.innerHTML = '<p class="hint">지점을 불러오지 못했습니다.</p>';
    return;
  }

  const stream = roadStreamSource(blockId);
  const when = b.analyzed_at
    ? new Date(b.analyzed_at).toLocaleString("ko-KR", { hour12: false })
    : "";
  const modeTag = b.mode === "continuous"
    ? '<span class="badge" style="background:#3fb950">상시</span>'
    : '<span class="badge" style="background:#d4a017">선택</span>';

  const buttons =
    `<button class="livebtn livebtn-lg" onclick="runRoadFor('${blockId}','${b.name}')">▶ 분석 실행</button>`
    + (stream
        ? `<button class="livebtn livebtn-lg" onclick="openLive('${blockId}','${stream.name}','${stream.url}','road',null)">▶ 실시간 CCTV</button>`
        : "")
    + `<button class="livebtn livebtn-lg" onclick="openRoadReport('${blockId}','${b.name}')">📄 보고서</button>`;

  // ── 아직 보지 않은 지점 ──
  if (!b.analyzed) {
    const why = b.failed
      ? `<p class="ug-error" role="alert">영상을 받지 못해 분석하지 못했습니다.<br>${b.note || ""}</p>
         <p class="status-line">마지막 시도 ${when}</p>`
      : `<p class="hint">아직 분석하지 않은 지점입니다.
           ${b.mode === "continuous"
             ? "상시 순회 차례가 되면 자동으로 분석됩니다."
             : "「분석 실행」을 누르면 지금 확인합니다."}</p>`;
    detail.innerHTML = `
      <h2>${b.name} ${modeTag}
        <span class="badge" style="background:${b.failed ? "#e5484d" : "#6e7681"}">${b.grade_label}</span></h2>
      <div class="road-live-bar">${buttons}</div>
      ${why}
      <p class="status-line">소스 ${b.source_type} · 담당 ${b.dept || "-"}</p>`;
    return;
  }

  // ── 분석 결과가 있는 지점 ──
  const c = ROAD_GRADE_COLOR[b.grade] || "#6e7681";
  const defects = b.defects || [];

  // 실제 탐지 결과의 형태는 {frame, type, confidence, box[4]} 다.
  // 예전 모의 데이터의 {x, y, grade, detected_minutes_ago} 와 다르다.
  const defectsHtml = defects.length
    ? defects.map((d) => `
        <div class="alert-row">
          <span>[${d.type}] 신뢰도 ${((d.confidence || 0) * 100).toFixed(0)}%
            · 표본 ${d.frame}번 프레임
            ${Array.isArray(d.box) ? ` · 위치 (${d.box[0]}, ${d.box[1]})` : ""}</span>
        </div>`).join("")
    : `<p class="hint">이번 관측에서 탐지된 손상이 없습니다.
         <br><span style="font-size:.8rem">⚠ 현재 모델은 부산 CCTV에서 탐지가 잘 되지 않습니다 —
         <b>0건이 「손상 없음」을 뜻하지 않습니다.</b></span></p>`;

  detail.innerHTML = `
    <h2>${b.name} ${modeTag}
      <span class="badge" style="background:${c}">${b.grade_label}</span></h2>
    <div class="road-live-bar">${buttons}</div>
    <div class="crowd-metrics">
      <div><span class="mk">탐지 손상</span><b>${b.defect_count ?? defects.length}</b>건</div>
      <div><span class="mk">분석 프레임</span><b>${b.frames_analyzed ?? 0}</b></div>
      <div><span class="mk">신뢰도 임계</span><b>${b.conf ?? "-"}</b></div>
      <div><span class="mk">소요</span><b>${b.elapsed_sec ?? "-"}</b>초</div>
    </div>
    ${b.note ? `<p class="mock-hint">⚠ ${b.note}</p>` : ""}
    <h3>탐지된 손상 목록</h3>
    ${defectsHtml}
    <p class="status-line">분석 시각 ${when} · 소스 ${b.source_type} · 담당 ${b.dept || "-"}</p>`;
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
// 첫 화면은 폴링으로 즉시 채운다 — SSE 는 첫 값이 올 때까지 빈 화면이다.
tick();
startBoardStream();
loadCaseList();
loadRunList();
loadRoadList();
loadRoadAnalysis();
// 이후 갱신은 loadRoadLive 가 스스로 예약한다(순회 중에는 더 자주).
loadRoadLive();
renderRoadHistory();
// 업로드 폼은 한 번만 그린다 — 다시 그리면 선택해 둔 파일이 사라진다.
renderRoadUploadForm();
loadCrowdCctv();
// 2026-08-27 — 「실시간 분석 (상시)」를 카메라별 그리드로 교체했다
// (loadCrowdContinuous, #crowd-continuous). loadCrowdLive()/#crowd-live는
// BLOCKS[0] 카메라 하나만 보여주던 예전 경로라 더 이상 부르지 않는다
// (엔드포인트 자체는 다른 통합이 쓸 수 있어 남겨 뒀다).
loadCrowdContinuous();
setInterval(loadCrowdContinuous, 6000);
