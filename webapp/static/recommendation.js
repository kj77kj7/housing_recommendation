"use strict";

// ── 인증 토큰 ────────────────────────────────────────────
function getAuthHeaders() {
  const token = localStorage.getItem('token');
  return token
    ? { "Content-Type": "application/json", "Authorization": "Bearer " + token }
    : { "Content-Type": "application/json" };
}

// ── DOM ──────────────────────────────────────────────────
const loadingEl    = document.getElementById("rec-loading");
const errorEl      = document.getElementById("rec-error");
const errorMsgEl   = document.getElementById("rec-error-msg");
const noSessionEl  = document.getElementById("rec-no-session");
const resultsEl    = document.getElementById("rec-results");
const condBarEl    = document.getElementById("rec-condition-bar");
const topCardWrap  = document.getElementById("rec-top-card-wrap");
const gridEl       = document.getElementById("rec-grid");
const avgSectionEl = document.getElementById("rec-avg-section");
const avgContentEl = document.getElementById("rec-avg-content");
const navBadgeEl   = document.getElementById("rec-count-badge");
const condSideEl   = document.getElementById("rec-conditions-sidebar");
const condChipsEl  = document.getElementById("rec-cond-chips");
const countBtnEl   = document.getElementById("rec-count-btn");
const sortBtnEl    = document.getElementById("rec-sort-btn");
const viewBtns     = document.querySelectorAll(".rec-view-btn");
const mapViewEl    = document.getElementById("rec-map-view");
const cardsContainerSelector = "#rec-top-card-wrap, #rec-grid";

// ── 상태 ─────────────────────────────────────────────────
let _allResults = [];   // 원본 결과 (정렬 전)
let _seoulAvg   = null;
let _currentSort = "score";  // score | commute | price
let _currentView = "card";   // card | map
let _workplaceCoords = null;  // /api/recommend 응답에서 채움 {lat, lon} or null
let _allowedMinutes  = null;  // 통근 허용 분 (반경 원)
let _transportMode   = null;  // "car" | "transit"
const _expenseCache = {};     // {idx: ExpenseResponse} — 매물별 캐시

// ── 지도뷰 상태 ───────────────────────────────────────────
let _kakaoMap        = null;
let _kakaoMarkers    = [];
let _kakaoOverlays   = [];
let _kakaoCircle     = null;
let _kakaoWorkMarker = null;
let _mapInitialized  = false;

// ── 초기화 ───────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  // 사이드바 지도뷰 메뉴 활성화 (추천 페이지에 진입한 시점에는 항상 결과가 있을 수 있음)
  const navMap = document.getElementById("nav-map");
  if (navMap) {
    navMap.classList.remove("disabled");
    navMap.href = "/recommendation?view=map";
    navMap.addEventListener("click", (e) => {
      // 같은 페이지 안에서는 SPA-like 동작: URL 갱신 + 뷰 전환
      if (location.pathname === "/recommendation") {
        e.preventDefault();
        history.replaceState(null, "", "/recommendation?view=map");
        switchView("map");
      }
    });
  }

  const sessionId = localStorage.getItem("rec_session_id");
  if (!sessionId) {
    showState("no-session");
    return;
  }
  attachControlHandlers();
  attachModalHandlers();

  // 챗봇에서 자동 추천한 결과가 sessionStorage에 있으면 API 재호출 없이 바로 렌더링
  const cached = sessionStorage.getItem("rec_results_cache");
  if (cached) {
    try {
      const parsed = JSON.parse(cached);
      _allResults = parsed.results   || [];
      _seoulAvg   = parsed.seoul_avg || null;
      _workplaceCoords = (parsed.workplace_lat != null && parsed.workplace_lon != null)
        ? { lat: parsed.workplace_lat, lon: parsed.workplace_lon }
        : null;
      _allowedMinutes = parsed.allowed_minutes ?? null;
      _transportMode  = parsed.transport_mode  ?? null;
      if (_allResults.length) {
        renderPage();
        return;
      }
    } catch { /* 파싱 실패 시 API 재호출 */ }
  }

  loadResults(sessionId);
});

// ── 결과 로드 ────────────────────────────────────────────
async function loadResults(sessionId) {
  showState("loading");
  try {
    const res  = await fetch((window.API_BASE || "") + "/api/recommend", {
      method:  "POST",
      headers: getAuthHeaders(),
      body:    JSON.stringify({ session_id: sessionId }),
    });
    const data = await res.json();

    if (res.status === 400) {
      // 세션 만료 — 챗봇 페이지로 안내
      showError("세션이 만료됐어요. 챗봇에서 조건을 다시 입력해 주세요.", true);
      return;
    }
    if (!res.ok || data.error) {
      const rawErr = data.error || "";
      let displayErr = "추천 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.";
      if (rawErr.includes("kakao") || rawErr.includes("ConnectionPool") || rawErr.includes("getaddrinfo"))
        displayErr = "주소 검색 서비스에 연결할 수 없어요.\n네트워크 연결을 확인하고 다시 시도해 주세요.";
      else if (rawErr.includes("슬롯 미완성"))
        displayErr = "조건 입력이 완료되지 않았어요. 챗봇에서 조건을 먼저 입력해 주세요.";
      showError(displayErr);
      return;
    }

    _allResults = data.results  || [];
    _seoulAvg   = data.seoul_avg || null;
    _workplaceCoords = (data.workplace_lat != null && data.workplace_lon != null)
      ? { lat: data.workplace_lat, lon: data.workplace_lon }
      : null;
    _allowedMinutes = data.allowed_minutes ?? null;
    _transportMode  = data.transport_mode  ?? null;

    if (!_allResults.length) {
      showError("조건에 맞는 추천 결과가 없어요. 조건을 조정해 보세요.");
      return;
    }

    renderPage();
  } catch (err) {
    showError("서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.");
    console.error(err);
  }
}

// ── 컨트롤 이벤트 ─────────────────────────────────────────
function attachControlHandlers() {
  // 정렬 토글
  if (sortBtnEl) {
    sortBtnEl.addEventListener("click", () => {
      const order = ["score", "commute", "price"];
      const idx = order.indexOf(_currentSort);
      _currentSort = order[(idx + 1) % order.length];
      const labelMap = { score: "종합", commute: "통근", price: "가격" };
      sortBtnEl.textContent = `정렬: ${labelMap[_currentSort]} ▼`;
      renderPage();
    });
  }
  // 카드뷰/지도뷰
  viewBtns.forEach(btn => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
}

// ── 뷰 전환 (사이드바·URL 파라미터·토글 버튼 공통) ─────────
function switchView(view) {
  _currentView = view;
  viewBtns.forEach(b => b.classList.toggle("active", b.dataset.view === _currentView));
  const isMap = _currentView === "map";
  mapViewEl.style.display = isMap ? "block" : "none";
  topCardWrap.style.display = isMap ? "none" : "";
  gridEl.style.display = isMap ? "none" : "";
  avgSectionEl.style.display = isMap ? "none" : (avgContentEl.children.length ? "block" : "none");
  if (isMap) ensureMapReady();
}

// ── 지도 초기화 (lazy — 첫 토글 시 1회) ────────────────────
function ensureMapReady() {
  if (_mapInitialized) {
    renderMapMarkers();   // 결과만 갱신
    return;
  }
  if (typeof kakao === "undefined" || !kakao.maps) {
    document.getElementById("rec-map-canvas").innerHTML =
      '<div class="rec-map-error">❌ 카카오맵 SDK 로드 실패 — 네트워크/도메인 등록을 확인하세요.</div>';
    return;
  }
  kakao.maps.load(() => {
    const container = document.getElementById("rec-map-canvas");
    _kakaoMap = new kakao.maps.Map(container, {
      center: new kakao.maps.LatLng(37.5665, 126.9780),  // 서울시청 (임시)
      level: 7,
    });
    _mapInitialized = true;
    renderMapMarkers();
  });
}

// ── 지도 마커·반경 원 렌더링 ───────────────────────────────
function renderMapMarkers() {
  if (!_mapInitialized || !_kakaoMap) return;

  // 기존 요소 제거
  _kakaoMarkers.forEach(m => m.setMap(null));
  _kakaoOverlays.forEach(o => o.setMap(null));
  if (_kakaoCircle)     { _kakaoCircle.setMap(null); _kakaoCircle = null; }
  if (_kakaoWorkMarker) { _kakaoWorkMarker.setMap(null); _kakaoWorkMarker = null; }
  _kakaoMarkers = [];
  _kakaoOverlays = [];

  const bounds = new kakao.maps.LatLngBounds();
  const sorted = sortedResults();

  // 추천 매물 마커 (정렬 후 인덱스 = 모달 idx와 일치)
  sorted.forEach((r, idx) => {
    if (!r.lat || !r.lon) return;
    const pos = new kakao.maps.LatLng(r.lat, r.lon);
    bounds.extend(pos);

    const marker = new kakao.maps.Marker({ map: _kakaoMap, position: pos, title: r.address || (r.gu + " " + r.dong) });
    kakao.maps.event.addListener(marker, "click", () => openPropertyModal(idx));
    _kakaoMarkers.push(marker);

    // 라벨 (매물명·점수·가격) — 마커 위에 떠 있는 박스
    const labelHtml = renderMapLabel(r);
    const overlay = new kakao.maps.CustomOverlay({
      map: _kakaoMap,
      position: pos,
      yAnchor: 2.4,
      content: labelHtml,
      clickable: true,
    });
    _kakaoOverlays.push(overlay);
  });

  // 직장 마커 + 통근 반경 원
  if (_workplaceCoords && _workplaceCoords.lat && _workplaceCoords.lon) {
    const wp = new kakao.maps.LatLng(_workplaceCoords.lat, _workplaceCoords.lon);
    bounds.extend(wp);
    _kakaoWorkMarker = new kakao.maps.Marker({
      map: _kakaoMap,
      position: wp,
      title: "직장 위치",
      image: new kakao.maps.MarkerImage(
        "https://t1.daumcdn.net/mapjsapi/images/2x/marker_red.png",
        new kakao.maps.Size(30, 42),
      ),
    });

    // 통근반경 원 (직선거리 추정)
    const radiusM = computeCommuteRadiusMeters(_allowedMinutes, _transportMode);
    if (radiusM > 0) {
      _kakaoCircle = new kakao.maps.Circle({
        map: _kakaoMap,
        center: wp,
        radius: radiusM,
        strokeWeight: 2,
        strokeColor: "#10B981",
        strokeOpacity: 0.7,
        strokeStyle: "dashed",
        fillColor: "#10B981",
        fillOpacity: 0.06,
      });
    }
  }

  // 모든 마커가 보이도록 자동 fit
  if (!bounds.isEmpty()) _kakaoMap.setBounds(bounds);
}

// 매물 마커 위 라벨 HTML
function renderMapLabel(r) {
  const score = r.score != null ? r.score : 0;
  let priceTxt = "";
  if (r.rent_type === "월세" && r.monthly_rent_manwon) {
    priceTxt = `월 ${r.monthly_rent_manwon}만`;
  } else if (r.deposit_manwon) {
    priceTxt = `보증 ${Number(r.deposit_manwon).toLocaleString()}만`;
  }
  const name = (r.address || (r.gu + " " + r.dong) || "").replace(/"/g, "&quot;");
  return (
    '<div class="rec-map-marker-label">' +
      '<div class="rec-map-marker-name">' + escHtmlSafe(name) + '</div>' +
      '<div class="rec-map-marker-meta">' +
        '<span class="rec-map-marker-score">⭐ ' + score + '</span>' +
        (priceTxt ? '<span class="rec-map-marker-price">' + priceTxt + '</span>' : '') +
      '</div>' +
    '</div>'
  );
}

function escHtmlSafe(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// 통근 시간 → 직선거리 반경(m). 도시 평균 이동속도 기반 단순 추정.
function computeCommuteRadiusMeters(minutes, mode) {
  if (!minutes || minutes <= 0) return 0;
  const kmh = (mode === "car") ? 25 : 18;   // 자가용 25km/h, 대중교통 18km/h (도심 평균)
  return Math.round((minutes / 60) * kmh * 1000);
}

// ── 정렬 ─────────────────────────────────────────────────
function sortedResults() {
  const sorted = [..._allResults];
  if (_currentSort === "score") {
    sorted.sort((a, b) => (b.score || 0) - (a.score || 0));
  } else if (_currentSort === "commute") {
    sorted.sort((a, b) => (a.commute_min || 999) - (b.commute_min || 999));
  } else if (_currentSort === "price") {
    sorted.sort((a, b) => priceForSort(a) - priceForSort(b));
  }
  return sorted;
}
function priceForSort(r) {
  if (r.rent_type === "월세" && r.monthly_rent_manwon) return r.monthly_rent_manwon;
  return r.deposit_manwon || r.price_manwon || 0;
}

// ── 전체 페이지 렌더링 ───────────────────────────────────
function renderPage() {
  const results = sortedResults();

  // 사이드바 매물 수 배지
  if (navBadgeEl) {
    navBadgeEl.textContent = results.length;
    navBadgeEl.style.display = "";
  }
  localStorage.setItem("rec_result_count", String(results.length));

  // 컨트롤 카운트 버튼 라벨
  if (countBtnEl) countBtnEl.textContent = `추천 개수: ${results.length}개 ▼`;

  // 검색 조건 바
  renderConditionBar();

  // 1위 hero 카드
  topCardWrap.innerHTML = renderTopCard(results[0], 0);

  // 2~N위 그리드
  const rest = results.slice(1);
  gridEl.innerHTML = rest.map((r, i) => renderRestCard(r, i)).join("")
                     + renderMoreCard(results.length);

  // 서울 평균 비교
  if (_seoulAvg) renderAvgSection(_seoulAvg, results);

  // 지도뷰 활성 상태면 마커도 정렬·필터 변경 따라 갱신
  if (_currentView === "map" && _mapInitialized) renderMapMarkers();

  showState("results");

  // URL ?view=map 으로 진입한 경우 자동 전환 (1회)
  if (!_initialViewApplied) {
    _initialViewApplied = true;
    const params = new URLSearchParams(location.search);
    if (params.get("view") === "map") switchView("map");
  }
}
let _initialViewApplied = false;

// ── 조건 바 렌더링 (localStorage 기반) ──────────────────
function renderConditionBar() {
  const chips = JSON.parse(localStorage.getItem("rec_cond_chips") || "[]");
  if (!chips.length) {
    condBarEl.style.display = "none";
    // 사이드바도 숨김
    if (condSideEl) condSideEl.style.display = "none";
    return;
  }
  condBarEl.style.display = "flex";
  condBarEl.innerHTML = chips
    .map((c, i) => `<span>${escHtml(c)}</span>${i < chips.length - 1 ? '<span class="rec-cond-sep">•</span>' : ''}`)
    .join("");

  // 사이드바 조건 칩
  if (condChipsEl && chips.length) {
    condSideEl.style.display = "block";
    condChipsEl.innerHTML = chips.map(c =>
      `<div class="search-condition">${escHtml(c)}</div>`
    ).join("");
  }
}

// ── 1위 hero 카드 ───────────────────────────────────────
function renderTopCard(r, idx) {
  if (!r) return "";
  const score    = (r.score != null ? r.score : 0);
  const commute  = r.commute_min != null ? r.commute_min : "–";
  const cost     = r.cost_score != null ? r.cost_score : 0;
  const infra    = r.infra_score != null ? r.infra_score : 0;
  const safety   = r.safety_score != null ? r.safety_score : null;
  const detailParts = [];
  if (r.house_type) detailParts.push(escHtml(r.house_type));
  if (r.area_m2)    detailParts.push(`${r.area_m2}㎡`);
  if (r.rent_type)  detailParts.push(escHtml(r.rent_type));

  const priceLine = formatPriceLine(r);
  const policyTotal = (r.applicable_policies_total != null
                       ? r.applicable_policies_total
                       : (r.applicable_policies ? r.applicable_policies.length : 0));
  const policyHtml = policyTotal > 0
    ? `<div class="rec-policy">💚 청년정책 ${policyTotal}건 적용 가능 — 카드를 눌러 상세 보기</div>`
    : "";

  // 점수바 (3축, safety는 데이터 있으면만)
  const scoreRows = [
    scoreRow("통근", commute, 60, true),
    scoreRow("주거비", cost, 100, false),
    scoreRow("인프라", infra, 100, false),
  ];
  if (safety != null) scoreRows.push(scoreRow("안전", safety, 100, false));

  // 태그칩 (정책 점수는 v6.0부터 사용 안 함)
  const tags = [];
  if (r.infra_score)  tags.push(`🏪 편의시설 ${r.infra_score}점`);
  if (commute !== "–") tags.push(`🚇 통근 ${commute}분`);
  if (policyTotal > 0) tags.push(`💚 청년정책 ${policyTotal}건`);

  return `
    <div class="rec-top-card" onclick="openPropertyModal(0)">
      <div class="rec-top-image">
        <div class="rec-rank-badge">🏆 1위 추천</div>
        <div class="rec-score-badge">⭐ ${score}</div>
        <span style="font-size:64px">🏢</span>
        <button class="rec-heart-btn" data-id="${idx}" onclick="toggleFav(this); event.stopPropagation();" aria-label="찜">🤍</button>
      </div>
      <div class="rec-top-content">
        <div class="rec-address">📍 ${escHtml(r.address || (r.gu + " " + r.dong))}</div>
        <div class="rec-detail">${detailParts.join(" · ") || "주택 유형 정보 없음"}</div>
        <div class="rec-price">${priceLine}</div>
        <div class="rec-mgmt">시세 통계 기반 추정 (실제 매물은 직방·다방·네이버부동산에서 확인)</div>

        <div class="rec-scores">
          ${scoreRows.join("")}
        </div>

        ${policyHtml}

        <div class="rec-tags">
          ${tags.map(t => `<span class="rec-tag">${t}</span>`).join("")}
        </div>
      </div>
    </div>
  `;
}

// ── 2~N위 카드 ──────────────────────────────────────────
function renderRestCard(r, i) {
  const colorMap = ["blue", "green", "gray"];
  const color    = colorMap[i % colorMap.length];
  const rank     = i + 2;
  const idx      = i + 1;  // _allResults 인덱스 (1위는 0)
  const score    = r.score != null ? r.score : 0;
  const priceLine = formatPriceLineCompact(r);
  const commute  = r.commute_min != null ? r.commute_min : "–";

  const policyTotal = (r.applicable_policies_total != null
                       ? r.applicable_policies_total
                       : (r.applicable_policies ? r.applicable_policies.length : 0));
  const policyBadge = policyTotal > 0
    ? `<div class="rec-card-policy">💚 청년정책 ${policyTotal}건 적용 가능</div>`
    : "";

  return `
    <div class="rec-card" onclick="openPropertyModal(${idx})">
      <div class="rec-card-image ${color}">
        <div class="rec-rank-badge">${rank}위</div>
        <div class="rec-score-badge">⭐ ${score}</div>
        <span>🏠</span>
        <button class="rec-heart-btn" data-id="${idx}" onclick="toggleFav(this); event.stopPropagation();" aria-label="찜">🤍</button>
      </div>
      <div class="rec-card-body">
        <div class="rec-card-rank">${rank}위 추천</div>
        <div class="rec-card-addr">${escHtml(r.address || (r.gu + " " + r.dong))}</div>
        <div class="rec-card-price">${priceLine}</div>
        <div class="rec-card-info">🚇 통근 ${commute}분 · 🏪 인프라 ${r.infra_score || 0}점 · ⭐ ${score}점</div>
        ${policyBadge}
      </div>
    </div>
  `;
}

// ── 더보기 카드 ─────────────────────────────────────────
function renderMoreCard(currentCount) {
  if (currentCount >= 10) return "";
  return `
    <div class="rec-more-card" onclick="onMoreCardClick()">
      <span style="font-size:40px">🏙️</span>
      <div class="rec-more-title">추천 개수 늘리기</div>
      <div class="rec-more-sub">더 많은 매물 보기</div>
      <button class="rec-more-btn">추천 ${currentCount < 5 ? 5 : 10}개로 늘리기 ▼</button>
    </div>
  `;
}
function onMoreCardClick() {
  // TODO: 실제 서버에 final_recommend_count 늘려서 재요청
  alert("더 많은 매물 보기 기능은 준비 중이에요.\n현재 추천 결과는 상위 5개입니다.");
}

// ──────────────────────────────────────────────────────────
// v6.0 — 매물 상세 모달
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

// 매물 카드 클릭 → 모달 열기
function openPropertyModal(idx) {
  const results = sortedResults();
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
  if (!_workplaceCoords) {
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
      workplace:    _workplaceCoords,
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
  const r = sortedResults()[_modalCurrentRank];
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

window.openPropertyModal = openPropertyModal;
window.closePropertyModal = closePropertyModal;

// ── 점수바 한 줄 ────────────────────────────────────────
function scoreRow(label, value, max, asMinutes) {
  if (value == null) return "";
  let pct, displayVal;
  if (asMinutes) {
    pct = Math.max(0, 100 - (value / max) * 100);  // 통근시간은 적을수록 점수 높음
    displayVal = `${value}분`;
  } else {
    pct = Math.min(100, Math.max(0, value));
    displayVal = `${value}점`;
  }
  return `
    <div class="rec-score-row">
      <span>${label}</span>
      <div class="rec-score-bar"><div class="rec-score-fill" style="width:${pct}%"></div></div>
      <span class="rec-score-num">${displayVal}</span>
    </div>
  `;
}

// ── 가격 표시 ───────────────────────────────────────────
function formatPriceLine(r) {
  if (r.rent_type === "월세" && r.monthly_rent_manwon) {
    return `보증금 ${formatManwon(r.deposit_manwon)} / 월세 ${r.monthly_rent_manwon}만원`;
  }
  if (r.rent_type === "전세") {
    return `전세 ${formatManwon(r.deposit_manwon)}`;
  }
  return formatManwon(r.deposit_manwon || r.price_manwon);
}
function formatPriceLineCompact(r) {
  if (r.rent_type === "월세" && r.monthly_rent_manwon) {
    return `월 ${r.monthly_rent_manwon}만 / 보증 ${formatManwon(r.deposit_manwon, true)}`;
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

// ── 즐겨찾기 토글 ────────────────────────────────────────
function toggleFav(btn) {
  if (!btn) return;
  const filled = btn.textContent.trim() === "❤️";
  btn.textContent = filled ? "🤍" : "❤️";
  // localStorage 저장 (id 기반)
  const id = btn.dataset.id || "";
  const favs = JSON.parse(localStorage.getItem("rec_favs") || "[]");
  if (filled) {
    const idx = favs.indexOf(id);
    if (idx >= 0) favs.splice(idx, 1);
  } else {
    if (!favs.includes(id)) favs.push(id);
  }
  localStorage.setItem("rec_favs", JSON.stringify(favs));
}
window.toggleFav = toggleFav;
window.onMoreCardClick = onMoreCardClick;

// ── 서울 평균 비교 ──────────────────────────────────────
function renderAvgSection(seoulAvg, results) {
  try {
    const avgPrice   = seoulAvg?.avg_deposit ?? seoulAvg?.median_deposit ?? null;
    const avgCommute = seoulAvg?.avg_commute  ?? null;
    if (!avgPrice && !avgCommute) return;

    const top1 = results[0];
    const items = [];
    const top1Price = top1?.deposit_manwon || top1?.price_manwon;
    if (avgPrice && top1Price) {
      const diff = Math.round(top1Price - avgPrice);
      const sign = diff < 0 ? "▼" : "▲";
      const cls  = diff < 0 ? "rec-avg-good" : "rec-avg-bad";
      items.push(`<div class="rec-avg-row">
        <span class="rec-avg-label">💰 보증금</span>
        <span>서울 평균 <b>${formatManwon(avgPrice)}</b> 대비</span>
        <span class="${cls}">${sign} ${formatManwon(Math.abs(diff))}</span>
      </div>`);
    }
    if (avgCommute && top1?.commute_min) {
      const diff = Math.round(top1.commute_min - avgCommute);
      const sign = diff < 0 ? "▼" : "▲";
      const cls  = diff < 0 ? "rec-avg-good" : "rec-avg-bad";
      items.push(`<div class="rec-avg-row">
        <span class="rec-avg-label">🕐 통근 시간</span>
        <span>서울 평균 <b>${Math.round(avgCommute)}분</b> 대비</span>
        <span class="${cls}">${sign} ${Math.abs(diff)}분</span>
      </div>`);
    }
    if (!items.length) return;

    avgContentEl.innerHTML = items.join("");
    avgSectionEl.style.display = "block";
  } catch (e) { /* 평균 없으면 조용히 숨김 */ }
}

// ── 상태 전환 ────────────────────────────────────────────
function showState(state) {
  loadingEl.style.display   = state === "loading"    ? "flex"  : "none";
  errorEl.style.display     = state === "error"      ? "flex"  : "none";
  noSessionEl.style.display = state === "no-session" ? "flex"  : "none";
  resultsEl.style.display   = state === "results"    ? "block" : "none";
}

function showError(msg, gotoChat) {
  errorMsgEl.textContent = msg;
  if (gotoChat) {
    const retryBtn = errorEl.querySelector(".rec-retry-btn");
    if (retryBtn) retryBtn.textContent = "챗봇으로 가기";
  }
  showState("error");
}

// ── 유틸 ────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
