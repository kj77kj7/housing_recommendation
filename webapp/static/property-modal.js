"use strict";
/**
 * property-modal.js
 * ─────────────────
 * 매물 상세 모달 (예상 생활비 + 청년정책)
 * recommendation.js / mypage.html / favorites.html 에서 공통 재사용
 *
 * ※ 추천 로직과 무관. recommendation.js 에서 분리만 했으며 내부 로직은 동일.
 *
 * 외부 의존성:
 *   window.__modalResultsProvider : () => Array<PropertyData>
 *     - 매물 idx 로 어느 배열에서 조회할지 페이지마다 다르게 주입
 *   window._workplaceCoords : { lat, lon } | null
 *     - 예상 생활비 계산용 직장 좌표 (recommendation.js 가 설정)
 *
 * 글로벌 노출:
 *   window.openPropertyModal(idx)
 *   window.closePropertyModal()
 */

// ──────────────────────────────────────────────────────────
// 외부 의존성 어댑터
// ──────────────────────────────────────────────────────────
function _getResults() {
  return (window.__modalResultsProvider || (() => []))();
}
function _getWorkplaceCoords() {
  return window._workplaceCoords || null;
}

// ──────────────────────────────────────────────────────────
// 정책 아이콘 매핑
// ──────────────────────────────────────────────────────────
const POLICY_ICON_MAP = {
  "월세지원":  { icon: "💚", cls: "policy-icon-box--monthly" },
  "월세대출":  { icon: "💚", cls: "policy-icon-box--monthly" },
  "보증":      { icon: "🏦", cls: "policy-icon-box--deposit-loan" },
  "보증료":    { icon: "🏦", cls: "policy-icon-box--deposit-loan" },
  "이자지원":  { icon: "🏦", cls: "policy-icon-box--deposit-loan" },
  "전세대출":  { icon: "🏦", cls: "policy-icon-box--deposit-loan" },
  "공공임대":  { icon: "🏠", cls: "policy-icon-box--rental" },
  "감면":      { icon: "🏷️", cls: "" },
  "대출":      { icon: "💳", cls: "" },
};
function policyIconFor(supportType) {
  return POLICY_ICON_MAP[supportType] || { icon: "📋", cls: "" };
}

let _modalShownCount = 5;          // 모달 안 정책 표시 개수
let _modalCurrentRank = 0;         // 현재 보고 있는 매물 순위 (0-based)
const _expenseCache = {};          // {idx: ExpenseResponse} — 매물별 캐시

// 매물 카드 클릭 → 모달 열기
function openPropertyModal(idx) {
  const results = _getResults();
  const r = results[idx];
  if (!r) return;
  _modalCurrentRank = idx;
  _modalShownCount = 5;
  renderModalContent(r, idx);
  const overlay = document.getElementById("property-modal-overlay");
  overlay.style.display = "flex";
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  renderModalExpense(r, idx);
}

function closePropertyModal() {
  const overlay = document.getElementById("property-modal-overlay");
  overlay.style.display = "none";
  overlay.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

function renderModalContent(r, idx) {
  // 헤더 — "1위 추천 • 중구 신당동"
  const rank = idx + 1;
  const dongText = r.dong || r.address || "";
  document.getElementById("modal-title").textContent =
    `${rank}위 추천 · ${(r.gu || "")} ${dongText}`.trim();

  // 매물 요약
  document.getElementById("modal-address").textContent = r.address || (r.gu + " " + r.dong);
  const metaParts = [];
  if (r.house_type) metaParts.push(r.house_type);
  if (r.area_m2)    metaParts.push(`${r.area_m2}㎡`);
  if (r.rent_type)  metaParts.push(r.rent_type);
  document.getElementById("modal-meta").textContent =
    metaParts.length ? metaParts.join(" · ") : "주택 유형 정보 없음";
  document.getElementById("modal-deposit").textContent = formatPriceLine(r);

  // 우측 태그 칩
  const tagsEl = document.getElementById("modal-tags");
  const score = r.score != null ? r.score : 0;
  const policyTotal = (r.applicable_policies_total != null
                       ? r.applicable_policies_total
                       : (r.applicable_policies ? r.applicable_policies.length : 0));
  const tagBits = [`<span class="tag-pill tag-pill--score">⭐ ${score}점</span>`];
  if (r.commute_min != null) tagBits.push(`<span class="tag-pill tag-pill--commute">🚇 ${r.commute_min}분</span>`);
  if (policyTotal > 0)       tagBits.push(`<span class="tag-pill tag-pill--policy">💚 청년정책 ${policyTotal}건</span>`);
  tagsEl.innerHTML = tagBits.join("");

  // 예상 생활비 placeholder 부제
  document.getElementById("modal-expense-subtitle").textContent =
    `${r.gu || ""} ${dongText} 기준 · 1인 가구`.trim();
  const now = new Date();
  document.getElementById("modal-expense-date").textContent =
    `${now.getFullYear()}년 ${now.getMonth() + 1}월`;

  // 청년정책 섹션
  renderModalPolicies(r);
}

// ──────────────────────────────────────────────────────────
// 예상 생활비 (Step 4 — /api/expense)
// ──────────────────────────────────────────────────────────

async function renderModalExpense(r, idx) {
  // 1) 캐시 hit — 즉시 렌더
  if (_expenseCache[idx]) {
    const c = _expenseCache[idx];
    renderReceipt(c.target);
    renderComparisonCard(c.target, c.neighbors, c.seoul_average, c.meta);
    return;
  }

  // 2) 좌표 검증
  const wp = _getWorkplaceCoords();
  if (!wp) {
    showExpenseError("직장 주소를 좌표로 변환하지 못했어요");
    return;
  }
  if (r.lat == null || r.lon == null || (r.lat === 0 && r.lon === 0)) {
    showExpenseError("매물 좌표 정보가 없어 계산할 수 없어요");
    return;
  }

  // 3) fetch (실패 시 캐시 안 함)
  try {
    const data = await fetchExpense(r);
    _expenseCache[idx] = data;
    renderReceipt(data.target);
    renderComparisonCard(data.target, data.neighbors, data.seoul_average, data.meta);
  } catch (err) {
    showExpenseError(err.message || "생활비 계산 중 오류가 발생했어요");
  }
}

async function fetchExpense(r) {
  const wp = _getWorkplaceCoords();
  const res = await fetch((window.API_BASE || "") + "/api/expense", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target_property: {
        gu:   r.gu,
        lat:  r.lat,
        lon:  r.lon,
        rent: (r.monthly_rent_manwon || 0) * 10000,  // 만원 → 원
        maintenance: null,                             // 매물 데이터에 관리비 없음 → fallback 사용
      },
      workplace:    wp,
      neighbor_gus: [],                                // 빈 값이면 자동 선택 (인접 2개)
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return await res.json();
}

function renderReceipt(target) {
  const body = document.getElementById("modal-receipt-body");
  const fmt = v => v.toLocaleString() + "원";

  body.innerHTML = `
    <div class="receipt-section">
      <div class="receipt-section-header">
        <span class="receipt-section-icon">🏡</span>
        <span class="receipt-section-title">주거비</span>
      </div>
      <div class="receipt-line">
        <span class="receipt-line-label">월세</span>
        <span class="receipt-line-value">${fmt(target.rent)}</span>
      </div>
      <div class="receipt-line">
        <span class="receipt-line-label">관리비 (추정)</span>
        <span class="receipt-line-value">${fmt(target.maintenance)}</span>
      </div>
    </div>

    <div class="receipt-section">
      <div class="receipt-section-header">
        <span class="receipt-section-icon">🍚</span>
        <span class="receipt-section-title">식비</span>
        <span class="receipt-section-sub">마트·외식·카페</span>
      </div>
      <div class="receipt-line receipt-line--solo">
        <span class="receipt-line-value">${fmt(target.food)}</span>
      </div>
    </div>

    <div class="receipt-section">
      <div class="receipt-section-header">
        <span class="receipt-section-icon">🚇</span>
        <span class="receipt-section-title">교통비</span>
      </div>
      <div class="receipt-line receipt-line--solo">
        <span class="receipt-line-value">${fmt(target.transport)}</span>
      </div>
    </div>

    <div class="receipt-section">
      <div class="receipt-section-header">
        <span class="receipt-section-icon">🎵</span>
        <span class="receipt-section-title">여가·문화</span>
      </div>
      <div class="receipt-line receipt-line--solo">
        <span class="receipt-line-value">${fmt(target.leisure)}</span>
      </div>
    </div>

    <div class="receipt-divider"></div>

    <div class="receipt-total">
      <span class="receipt-total-label">이번 달 합계</span>
      <span class="receipt-total-value">${fmt(target.total)}</span>
    </div>

    <div class="receipt-footnote">
      <div>※ 청년 1인가구 평균 213만원</div>
      <div class="receipt-footnote-sub">(출처: 청년의 삶 실태조사)</div>
    </div>
  `;
}

function renderComparisonCard(target, neighbors, seoulAvg, meta) {
  const body = document.getElementById("modal-comparison-body");
  const baseline = seoulAvg.total;
  const fmt = v => v.toLocaleString() + "원";
  const pct = v => Math.round((v / baseline) * 100);

  const rows = [
    { gu: target.gu,  total: target.total,  isTarget: true,  isAvg: false },
    ...neighbors.map(n => ({ gu: n.gu, total: n.total, isTarget: false, isAvg: false })),
    { gu: "서울 평균", total: seoulAvg.total, isTarget: false, isAvg: true },
  ];

  const MAX_BAR = 130;  // 130%까지가 막대 max (overflow 방지)

  const barsHtml = rows.map(row => {
    const p = pct(row.total);
    const w = Math.min(p, MAX_BAR) / MAX_BAR * 100;
    const cls = row.isTarget ? "comparison-bar-row comparison-bar-row--target"
              : row.isAvg    ? "comparison-bar-row comparison-bar-row--avg"
              : "comparison-bar-row";
    const labelPrefix = row.isTarget ? "★ " : "";
    const labelSuffix = row.isTarget ? " (현재)" : "";
    return `
      <div class="${cls}">
        <div class="comparison-bar-label">${labelPrefix}${row.gu}${labelSuffix}</div>
        <div class="comparison-bar-meta">
          <span class="comparison-bar-amount">${fmt(row.total)}</span>
          <span class="comparison-bar-pct">${p}%</span>
        </div>
        <div class="comparison-bar-track">
          <div class="comparison-bar-fill" style="width:${w}%"></div>
        </div>
      </div>
    `;
  }).join("");

  const explainHtml = `
    <div class="comparison-explain">
      <div class="comparison-explain-head">※ 같은 매물이 다른 동네라면?</div>
      <div class="comparison-explain-sub">인접 구는 동일 월세·관리비를 가정하여 식비·교통비만 비교했어요</div>
    </div>
  `;

  const donutHtml = renderDonut(target);

  body.innerHTML = `
    <div class="comparison-bars">${barsHtml}</div>
    ${explainHtml}
    ${donutHtml}
  `;
}

function renderDonut(b) {
  // 4 segments — 색상은 CSS variables 직접 사용
  const housing = b.rent + b.maintenance;
  const segments = [
    { label: "주거비", value: housing,     color: "var(--green-dark)"    },
    { label: "식비",   value: b.food,      color: "var(--green)"         },
    { label: "교통",   value: b.transport, color: "var(--green-light)"   },
    { label: "여가",   value: b.leisure,   color: "var(--fg-secondary)"  },
  ];
  const sum = segments.reduce((a, s) => a + s.value, 0);

  // SVG 도넛: viewBox 100x100, r=40, stroke-dasharray 트릭
  const r = 40;
  const C = 2 * Math.PI * r;
  let offset = 0;
  const arcs = segments.map(s => {
    const len  = (s.value / sum) * C;
    const dash = `${len} ${C - len}`;
    const dOff = -offset;
    offset += len;
    return `<circle cx="50" cy="50" r="${r}" fill="none" stroke="${s.color}"
            stroke-width="16" stroke-dasharray="${dash}" stroke-dashoffset="${dOff}"
            transform="rotate(-90 50 50)"/>`;
  }).join("");

  const totalManwon = Math.round(sum / 10000);
  const legendHtml = segments.map(s => `
    <div class="donut-legend-row">
      <span class="donut-legend-dot" style="background:${s.color}"></span>
      <span class="donut-legend-label">${s.label}</span>
      <span class="donut-legend-value">${s.value.toLocaleString()}원</span>
    </div>
  `).join("");

  return `
    <div class="comparison-donut-section">
      <div class="comparison-donut-wrap">
        <svg viewBox="0 0 100 100" class="comparison-donut">
          ${arcs}
          <text x="50" y="48" text-anchor="middle" class="donut-center-num">${totalManwon}</text>
          <text x="50" y="62" text-anchor="middle" class="donut-center-unit">만원</text>
        </svg>
      </div>
      <div class="comparison-donut-legend">${legendHtml}</div>
    </div>
  `;
}

function showExpenseError(msg) {
  const body = document.getElementById("modal-receipt-body");
  body.innerHTML = `
    <div class="expense-placeholder">
      ⚠ ${msg}
      <span class="expense-placeholder-sub">잠시 후 다시 시도해주세요</span>
    </div>`;
  const cmpBody = document.getElementById("modal-comparison-body");
  cmpBody.innerHTML = `<div class="expense-placeholder">—</div>`;
}


function renderModalPolicies(r) {
  const policies = Array.isArray(r.applicable_policies) ? r.applicable_policies : [];
  const total    = (r.applicable_policies_total != null) ? r.applicable_policies_total : policies.length;
  const subtitleEl   = document.getElementById("modal-policy-subtitle");
  const propNameEl   = document.getElementById("modal-policy-property-name");
  const savingsEl    = document.getElementById("modal-policy-savings");
  const listEl       = document.getElementById("modal-policy-list");
  const showMoreBtn  = document.getElementById("modal-policy-show-more");
  const emptyEl      = document.getElementById("modal-policy-empty");

  // 사용자 닉네임 (있으면) — "○○님께 적용 가능한"
  const nick = localStorage.getItem("nickname") || "고객";
  subtitleEl.textContent = `${nick}님께 적용 가능한 정책 ${total}건`;
  propNameEl.textContent = `${r.gu || ""} ${r.dong || ""} 기준 매물`.trim();

  // 절감액 합계 (eligible 정책의 benefit_amount_manwon 합)
  const eligibleSum = policies
    .filter(p => p.eligibility_status === "eligible")
    .reduce((sum, p) => sum + (Number(p.benefit_amount_manwon) || 0), 0);
  if (eligibleSum > 0) {
    savingsEl.textContent = `최대 ${formatBenefitAmount(eligibleSum)}`;
    savingsEl.style.display = "";
  } else {
    savingsEl.style.display = "none";
  }

  // 정책 비어있으면 안내
  if (!total || !policies.length) {
    listEl.innerHTML = "";
    showMoreBtn.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";

  // 상위 N개 표시 (_modalShownCount, 기본 5)
  const visible = policies.slice(0, _modalShownCount);
  listEl.innerHTML = visible.map(renderPolicyItem).join("");

  // "더 보기" 버튼 (전체 N건 > 표시 N건일 때만)
  if (total > visible.length) {
    showMoreBtn.style.display = "block";
    showMoreBtn.textContent = `더 보기 (${total - visible.length}건 더)`;
  } else {
    showMoreBtn.style.display = "none";
  }
}

function renderPolicyItem(p) {
  const ico = policyIconFor(p.support_type);
  const isEligible    = p.eligibility_status === "eligible";
  const needsCheck    = p.eligibility_status === "needs_check";
  const isGuSpecific  = !!p.is_gu_specific;

  const itemClass = [
    "policy-item",
    needsCheck ? "policy-item--needs-check" : "",
    isGuSpecific ? "policy-item--gu-specific" : "",
  ].filter(Boolean).join(" ");

  const iconClass = [
    "policy-icon-box",
    ico.cls,
    needsCheck ? "policy-icon-box--needs-check" : "",
  ].filter(Boolean).join(" ");

  // 상태 라벨
  const labels = [];
  if (isEligible)   labels.push(`<span class="policy-label policy-label--eligible">신청가능</span>`);
  if (needsCheck)   labels.push(`<span class="policy-label policy-label--needs-check">상세조건 확인 필요</span>`);
  if (isGuSpecific) labels.push(`<span class="policy-label policy-label--gu">이 동네 한정</span>`);

  // 자격 태그
  const tagHtml = (p.tags || []).map(t => `<span class="policy-tag">${escHtml(t)}</span>`).join("");

  // 신청 URL
  const linkHtml = p.apply_url
    ? `<a class="policy-item-link" href="${escHtml(p.apply_url)}" target="_blank" rel="noopener">자세히 보기 ↗</a>`
    : "";

  const nameClass = needsCheck ? "policy-item-name policy-item-name--disabled" : "policy-item-name";
  const descClass = needsCheck ? "policy-item-desc policy-item-desc--disabled" : "policy-item-desc";

  return `
    <div class="${itemClass}">
      <div class="${iconClass}">${ico.icon}</div>
      <div class="policy-item-body">
        <div class="policy-item-name-row">
          <span class="${nameClass}">${escHtml(p.policy_name)}</span>
          ${labels.join("")}
        </div>
        ${p.benefit_desc ? `<div class="${descClass}">${escHtml(p.benefit_desc)}</div>` : ""}
        ${tagHtml ? `<div class="policy-tag-list">${tagHtml}</div>` : ""}
        ${linkHtml}
      </div>
    </div>
  `;
}

// 만원 → "1,240만원" / "1억" 형식
function formatBenefitAmount(manwon) {
  const n = Math.round(manwon);
  if (n >= 10000) {
    const ok = Math.floor(n / 10000);
    const rem = n % 10000;
    return rem > 0 ? `${ok}억 ${rem.toLocaleString()}만원` : `${ok}억원`;
  }
  return `${n.toLocaleString()}만원`;
}

// 더 보기 버튼 클릭
function onPolicyShowMore() {
  _modalShownCount += 5;
  const r = _getResults()[_modalCurrentRank];
  if (r) renderModalPolicies(r);
}

// 모달 닫기 핸들러 (ESC, 외부 클릭, X 버튼)
function attachModalHandlers() {
  const overlay = document.getElementById("property-modal-overlay");
  const closeBtn = document.getElementById("modal-close-btn");
  const showMoreBtn = document.getElementById("modal-policy-show-more");
  if (closeBtn) closeBtn.addEventListener("click", closePropertyModal);
  if (showMoreBtn) showMoreBtn.addEventListener("click", onPolicyShowMore);
  if (overlay) {
    // 외부(오버레이 자체) 클릭 시 닫기 (모달 내부 클릭은 무시)
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closePropertyModal();
    });
  }
  // ESC 닫기
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay && overlay.style.display !== "none") {
      closePropertyModal();
    }
  });
}

// ──────────────────────────────────────────────────────────
// 유틸 (모달 내부용)
// ──────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatPriceLine(r) {
  if (r.rent_type === "월세" && r.monthly_rent_manwon) {
    return `보증금 ${formatManwon(r.deposit_manwon)} / 월세 ${r.monthly_rent_manwon}만원`;
  }
  if (r.rent_type === "전세") {
    return `전세 ${formatManwon(r.deposit_manwon)}`;
  }
  return formatManwon(r.deposit_manwon || r.price_manwon);
}
function formatManwon(manwon, compact) {
  if (!manwon) return "–";
  const n = Math.round(Number(manwon));
  if (n >= 10000) {
    const ok  = Math.floor(n / 10000);
    const rem = n % 10000;
    if (compact) return rem > 0 ? `${ok}억 ${rem.toLocaleString()}만` : `${ok}억`;
    return rem > 0 ? `${ok}억 ${rem.toLocaleString()}만원` : `${ok}억원`;
  }
  return compact ? `${n.toLocaleString()}만` : `${n.toLocaleString()}만원`;
}

// ──────────────────────────────────────────────────────────
// 글로벌 노출 + DOMContentLoaded 부착
// ──────────────────────────────────────────────────────────
window.openPropertyModal = openPropertyModal;
window.closePropertyModal = closePropertyModal;

document.addEventListener("DOMContentLoaded", () => {
  attachModalHandlers();
});
