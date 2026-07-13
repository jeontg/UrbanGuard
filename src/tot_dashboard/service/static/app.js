// tot_dashboard — minimal combined dashboard frontend.
// Deliberately simpler than either original UI (flood3's HLS/timeline-rich
// dashboard, SAM's tabbed case viewer): this proves the merged backend end
// to end (flood blocks + crowd cases + notifications) rather than
// replicating every original visual feature. See docs/integration_plan.md
// Phase 7.

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

// ---- Flood / traffic blocks ----------------------------------------------
function renderBlocks(riskList) {
  const container = $("#blocks");
  container.innerHTML = "";
  if (!riskList.length) {
    container.innerHTML = '<p class="hint">아직 데이터가 없습니다 (파이프라인 시작 대기 중)...</p>';
    return;
  }
  for (const b of riskList) {
    const card = document.createElement("div");
    card.className = "card";
    const flood = b.water_available
      ? `<div class="row">침수 위험도 <strong>${b.flood_risk_score?.toFixed(0)} (${b.flood_risk_grade_label || ""})</strong></div>
         <div class="row">물 비율 <strong>${(b.water_area_ratio * 100).toFixed(1)}%</strong></div>`
      : `<div class="row">침수 위험도 <strong>N/A (합성 소스)</strong></div>`;
    card.innerHTML = `
      <h3>${b.name} <span class="badge ${b.level}">${b.level}</span></h3>
      <div class="row">위험유형 <strong>${b.risk_name}</strong></div>
      <div class="row">평균속도 <strong>${b.mean_speed?.toFixed(0)} px/s</strong></div>
      <div class="row">강수 <strong>${b.rain_mm_h?.toFixed(1)} mm/h (${b.intensity})</strong></div>
      ${flood}
      <div class="row">갱신 <strong>${b.updated_at || "-"}</strong></div>
    `;
    container.appendChild(card);
  }
}

async function pollRisk() {
  try {
    const res = await fetch("/api/risk");
    const data = await res.json();
    renderBlocks(data.blocks || []);
  } catch (e) {
    console.error("risk poll failed", e);
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
pollRisk();
setInterval(pollRisk, 2000);
loadCaseList();
loadRunList();
