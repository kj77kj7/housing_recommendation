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
let _condChips       = null;  // 결과와 같은 세션의 검색 조건 (서버가 결과와 함께 내려줌)
// 주: _expenseCache 는 property-modal.js 에서 const 선언됨.
//     양쪽 파일이 같이 로드되면 중복 선언으로 SyntaxError 발생하므로 여기서는 선언하지 않음.

// ── 지도뷰 상태 ───────────────────────────────────────────
let _kakaoMap        = null;
let _kakaoMarkers    = [];
let _kakaoOverlays   = [];
let _kakaoCircle     = null;
let _kakaoWorkMarker = null;
let _mapInitialized  = false;

// ── 초기화 ───────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  // 사이드바 지도뷰 메뉴 활성화 (추천 페이지에 진입한 시점에는 항상 결과가 있을 수 있음)
  const navMap = document.getElementById("nav-map");
  if (navMap) {
    navMap.classList.remove("disabled");
    navMap.href = "/recommendation?view=map";
    navMap.addEventListener("click", (e) => {
      // 같은 페이지 안에서는 SPA-like 동작: URL 갱신 + 뷰 전환 (뒤로가기로 카드뷰 복귀 가능)
      if (location.pathname === "/recommendation") {
        e.preventDefault();
        if (_currentView !== "map") switchView("map", true);
      }
    });
  }
  window.addEventListener("popstate", () => {
    const v = new URLSearchParams(location.search).get("view") === "map" ? "map" : "card";
    if (v !== _currentView) switchView(v, false);
  });

  attachControlHandlers();
  // 모달은 property-modal.js 에서 자체적으로 DOMContentLoaded 시 부착됨

  // 매물 모달이 idx로 결과를 조회할 때 사용할 provider 등록 (정렬된 결과 반환)
  window.__modalResultsProvider = () => sortedResults();

  // 이 탭의 추천 세션 (탭마다 따로). 없으면(새 탭·새 창) 서버에 저장된 이 사용자의 최근 추천.
  let sessionId = sessionStorage.getItem("rec_session_id");

  // 챗봇에서 받은 결과가 이 탭에 캐시돼 있으면 API 재호출 없이 바로 렌더링 (같은 세션일 때만)
  const cached = sessionStorage.getItem("rec_results_cache");
  if (cached && sessionId) {
    try {
      const parsed = JSON.parse(cached);
      if (parsed.session_id && parsed.session_id !== sessionId) throw new Error("다른 세션 캐시");
      _allResults = parsed.results   || [];
      _seoulAvg   = parsed.seoul_avg || null;
      _workplaceCoords = (parsed.workplace_lat != null && parsed.workplace_lon != null)
        ? { lat: parsed.workplace_lat, lon: parsed.workplace_lon }
        : null;
      window._workplaceCoords = _workplaceCoords;  // property-modal.js 가 참조
      _allowedMinutes = parsed.allowed_minutes ?? null;
      _transportMode  = parsed.transport_mode  ?? null;
      _condChips      = parsed.cond_chips      ?? null;
      if (_allResults.length) {
        renderPage();
        return;
      }
    } catch { /* 파싱 실패·다른 세션 → API 조회 */ }
  }

  if (!sessionId) {
    showState("loading");
    try {
      const res = await fetch((window.API_BASE || "") + "/api/recommend/latest", { headers: getAuthHeaders() });
      const d = res.ok ? await res.json() : null;
      sessionId = d && d.session_id;
    } catch { sessionId = null; }
    if (!sessionId) { showState("no-session"); return; }
    sessionStorage.setItem("rec_session_id", sessionId);
  }
  loadResults(sessionId);
});

// ── 결과 로드 (조회 전용 — POST 호출 안 함) ──────────────
// 추천 트리거는 챗봇의 "추천 시작" 버튼에서만 발생.
// 이 페이지는 GET /api/recommend/result 로 캐시 상태만 조회하여 표시.
//   - status="complete" : 결과 렌더
//   - status="running"  : 로딩 표시 후 일정 간격 polling
//   - status="not_started" : 챗봇으로 이동 안내
//   - status="error" : 오류 메시지
async function loadResults(sessionId) {
  showState("loading");

  const POLL_INTERVAL_MS  = 2000;     // 2초 간격 polling (진행률 갱신)
  const MAX_POLL_DURATION = 20 * 60 * 1000;  // 최대 20분 polling (이후 타임아웃 처리)
  const startedAt = Date.now();

  async function fetchStatus() {
    const res = await fetch(
      (window.API_BASE || "") + "/api/recommend/result?session_id=" + encodeURIComponent(sessionId),
      { headers: getAuthHeaders() }
    );
    return await res.json();
  }

  while (true) {
    let data;
    try {
      data = await fetchStatus();
    } catch (err) {
      showError("서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.");
      console.error(err);
      return;
    }

    if (data.status === "complete") {
      _allResults = data.results  || [];
      _seoulAvg   = data.seoul_avg || null;
      _workplaceCoords = (data.workplace_lat != null && data.workplace_lon != null)
        ? { lat: data.workplace_lat, lon: data.workplace_lon }
        : null;
      window._workplaceCoords = _workplaceCoords;
      _allowedMinutes = data.allowed_minutes ?? null;
      _transportMode  = data.transport_mode  ?? null;
      _condChips      = data.cond_chips      ?? null;

      // sessionStorage 에 캐싱 — mypage·favorites 등 다른 페이지가 활용
      // (이전엔 챗봇의 POST 완료 시점에 app.js 가 저장했으나, 사용자가 추천 중
      //  /recommendation 으로 이동해 GET 폴링으로 결과를 받는 경로에선 캐시가 비어 있어
      //  mypage 추천 매물 0건·favorites 생활비 영수증 미표시 문제 발생했음)
      sessionStorage.setItem("rec_results_cache", JSON.stringify({
        session_id:      sessionId,
        results:         data.results,
        seoul_avg:       data.seoul_avg || null,
        workplace_lat:   data.workplace_lat,
        workplace_lon:   data.workplace_lon,
        allowed_minutes: data.allowed_minutes,
        transport_mode:  data.transport_mode,
        cond_chips:      data.cond_chips || null,
      }));
      sessionStorage.setItem("rec_result_count", String(_allResults.length));

      if (!_allResults.length) {
        showError("조건에 맞는 추천 결과가 없어요. 챗봇에서 조건을 조정해 보세요.", true);
        return;
      }
      renderPage();
      return;
    }

    if (data.status === "error") {
      const rawErr = data.error || "";
      let displayErr = data.user_message || "추천 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.";
      if (!data.user_message && (rawErr.includes("kakao") || rawErr.includes("ConnectionPool") || rawErr.includes("getaddrinfo")))
        displayErr = "주소 검색 서비스에 연결할 수 없어요.\n네트워크 연결을 확인하고 다시 시도해 주세요.";
      showError(displayErr);
      return;
    }

    if (data.status === "not_started") {
      showState("no-session");
      return;
    }

    // status === "running" — 추천 처리 중. 진행률 표시 후 polling 계속.
    renderLoadingProgress(data.progress || {});
    if (Date.now() - startedAt > MAX_POLL_DURATION) {
      showError("추천이 평소보다 오래 걸리고 있어요. 잠시 후 새로고침해 주세요.");
      return;
    }
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
  }
}

// 추천 진행 중 — 로딩 화면에 진행 막대 + 안내 표시
function renderLoadingProgress(progress) {
  const pct = Math.max(0, Math.min(100, Math.round(progress.percent || 0)));
  let box = document.getElementById("rec-loading-progress");
  if (!box) {
    box = document.createElement("div");
    box.id = "rec-loading-progress";
    box.className = "rec-progress-bubble rec-progress-standalone";
    box.setAttribute("role", "status");
    box.innerHTML = `
      <div class="rec-progress-title">🔍 맞춤 주거지를 찾고 있어요</div>
      <div class="rec-progress-track"><div class="rec-progress-fill"></div></div>
      <div class="rec-progress-meta"><span class="rec-progress-msg"></span><span class="rec-progress-pct"></span></div>
      <div class="rec-progress-note">💡 이 화면을 닫거나 다른 메뉴로 이동해도 추천은 계속 진행돼요.
        나중에 다시 '주거 추천' 메뉴로 오면 결과를 볼 수 있어요.</div>`;
    loadingEl.innerHTML = "";
    loadingEl.appendChild(box);
  }
  box.querySelector(".rec-progress-fill").style.width = pct + "%";
  box.querySelector(".rec-progress-msg").textContent = progress.message || "추천을 진행하고 있어요";
  box.querySelector(".rec-progress-pct").textContent = pct + "%";
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
    btn.addEventListener("click", () => {
      if (btn.dataset.view !== _currentView) switchView(btn.dataset.view, true);
    });
  });
}

// ── 뷰 전환 (사이드바·URL 파라미터·토글 버튼 공통) ─────────
function switchView(view, pushHistory) {
  _currentView = view;
  viewBtns.forEach(b => b.classList.toggle("active", b.dataset.view === _currentView));
  const isMap = _currentView === "map";
  if (pushHistory) history.pushState(null, "", isMap ? "/recommendation?view=map" : "/recommendation");
  // 사이드바 active 표시 (지도뷰인데 '주거 추천'이 선택돼 보이던 문제)
  const navRecEl = document.querySelector('#sidebar a.nav-item[href="/recommendation"]');
  const navMapEl = document.getElementById("nav-map");
  if (navRecEl) navRecEl.classList.toggle("active", !isMap);
  if (navMapEl) navMapEl.classList.toggle("active", isMap);
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
  // 타일이 그려질 때까지 '불러오는 중' 표시 (전환 직후 1~4초 흰 화면이던 문제)
  const loadingTip = document.createElement("div");
  loadingTip.className = "rec-map-loading";
  loadingTip.textContent = "🗺️ 지도를 불러오는 중…";
  mapViewEl.appendChild(loadingTip);
  kakao.maps.load(() => {
    const container = document.getElementById("rec-map-canvas");
    _kakaoMap = new kakao.maps.Map(container, {
      center: new kakao.maps.LatLng(37.5665, 126.9780),  // 서울시청 (임시)
      level: 7,
    });
    _mapInitialized = true;
    kakao.maps.event.addListener(_kakaoMap, "tilesloaded", () => loadingTip.remove());
    setTimeout(() => loadingTip.remove(), 8000);
    // 줌아웃(넓게 보기)에서는 라벨끼리 겹치므로 숨기고, 확대하면 표시
    kakao.maps.event.addListener(_kakaoMap, "zoom_changed", updateMapLabelVisibility);
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

    const marker = new kakao.maps.Marker({ map: _kakaoMap, position: pos,
      title: `${idx + 1}위 · ${r.address || (r.gu + " " + r.dong)}` });
    kakao.maps.event.addListener(marker, "click", () => openPropertyModal(idx));
    _kakaoMarkers.push(marker);

    // 라벨 (매물명·점수·가격) — 마커 위에 떠 있는 박스
    const labelHtml = renderMapLabel(r, idx);
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
    // 직장 마커 — 빨간 핀 (SVG data URI, 외부 CDN 의존 제거)
    // 이전엔 https://t1.daumcdn.net/mapjsapi/images/2x/marker_red.png 사용했으나
    // 카카오 CDN에서 해당 이미지가 사라져 404 발생 → SVG로 대체
    const _workMarkerSvg = encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="42" viewBox="0 0 30 42">' +
      '<path d="M15 0C6.7 0 0 6.7 0 15c0 11 15 27 15 27s15-16 15-27C30 6.7 23.3 0 15 0z" fill="#EF4444"/>' +
      '<circle cx="15" cy="15" r="6" fill="#fff"/>' +
      '</svg>'
    );
    _kakaoWorkMarker = new kakao.maps.Marker({
      map: _kakaoMap,
      position: wp,
      title: "직장 위치",
      image: new kakao.maps.MarkerImage(
        "data:image/svg+xml;charset=utf-8," + _workMarkerSvg,
        new kakao.maps.Size(30, 42),
        { offset: new kakao.maps.Point(15, 42) },
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
  updateMapLabelVisibility();
}

// 라벨 표시 기준 — 카카오맵 level 은 작을수록 확대. 화면이 좁으면 한 단계 더 확대돼야 표시.
function updateMapLabelVisibility() {
  if (!_kakaoMap) return;
  const maxLevel = window.innerWidth <= 768 ? 4 : 5;
  const show = _kakaoMap.getLevel() <= maxLevel;
  _kakaoOverlays.forEach(o => o.setMap(show ? _kakaoMap : null));
  const hint = document.getElementById("rec-map-label-hint");
  if (hint) hint.style.display = show ? "none" : "";
}

// 매물 마커 위 라벨 HTML
function renderMapLabel(r, idx) {
  const score = r.score != null ? r.score : 0;
  let priceTxt = "";
  if (r.rent_type === "월세" && r.monthly_rent_manwon) {
    priceTxt = `월 ${r.monthly_rent_manwon}만`;
  } else if (r.deposit_manwon) {
    priceTxt = `보증 ${Number(r.deposit_manwon).toLocaleString()}만`;
  }
  const name = r.address || (r.gu + " " + r.dong) || "";
  return (
    '<div class="rec-map-marker-label" onclick="openPropertyModal(' + Number(idx) + ')">' +
      '<div class="rec-map-marker-name">' + escHtml(name) + '</div>' +
      '<div class="rec-map-marker-meta">' +
        '<span class="rec-map-marker-score">⭐ ' + scoreText(r) + '</span>' +
        (priceTxt ? '<span class="rec-map-marker-price">' + priceTxt + '</span>' : '') +
      '</div>' +
    '</div>'
  );
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
    sorted.sort((a, b) => (b.score_precise ?? b.score ?? 0) - (a.score_precise ?? a.score ?? 0));
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

  // 사이드바 매물 수 배지 — 0건이면 숨김 (의미 없는 빈 pill 방지)
  if (navBadgeEl) {
    if (results.length > 0) {
      navBadgeEl.textContent = results.length;
      navBadgeEl.style.display = "";
    } else {
      navBadgeEl.textContent = "";
      navBadgeEl.style.display = "none";
    }
  }
  sessionStorage.setItem("rec_result_count", String(results.length));

  // 컨트롤 카운트 버튼 라벨
  if (countBtnEl) countBtnEl.textContent = `추천 ${results.length}개`;

  // 검색 조건 바
  renderConditionBar();

  // 1위 hero 카드
  topCardWrap.innerHTML = renderTopCard(results[0], 0);

  // 2~N위 그리드
  const rest = results.slice(1);
  gridEl.innerHTML = rest.map((r, i) => renderRestCard(r, i)).join("");

  // 서울 평균 비교
  if (_seoulAvg) renderAvgSection(_seoulAvg, results);

  // 지도뷰 활성 상태면 마커도 정렬·필터 변경 따라 갱신
  if (_currentView === "map" && _mapInitialized) renderMapMarkers();

  showState("results");

  // 찜한 매물 키 로드 → 하트 색칠 동기화
  loadFavKeys();

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
  let chips = Array.isArray(_condChips) ? _condChips : null;
  if (!chips) {
    try { chips = JSON.parse(sessionStorage.getItem("rec_cond_chips") || "[]"); } catch { chips = []; }
  }
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
  if (commute !== "–") tags.push(`${commuteIcon(r)} 통근 ${commute}분`);
  if (policyTotal > 0) tags.push(`💚 청년정책 ${policyTotal}건`);

  return `
    <div class="rec-top-card" onclick="openPropertyModal(0)">
      <div class="rec-top-image">
        <div class="rec-rank-badge">🏆 1위 추천</div>
        <div class="rec-score-badge">⭐ ${scoreText(r)}</div>
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
        <div class="rec-score-badge">⭐ ${scoreText(r)}</div>
        <span>🏠</span>
        <button class="rec-heart-btn" data-id="${idx}" onclick="toggleFav(this); event.stopPropagation();" aria-label="찜">🤍</button>
      </div>
      <div class="rec-card-body">
        <div class="rec-card-rank">${rank}위 추천</div>
        <div class="rec-card-addr">${escHtml(r.address || (r.gu + " " + r.dong))}</div>
        <div class="rec-card-price">${priceLine}</div>
        <div class="rec-card-info">${commuteIcon(r)} 통근 ${commute}분 · 🏪 인프라 ${r.infra_score || 0}점 · ⭐ ${scoreText(r)}점</div>
        ${policyBadge}
      </div>
    </div>
  `;
}

// ── 매물 상세 모달은 property-modal.js 로 분리됨 ──


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
    return `보증금 ${formatManwon(r.deposit_manwon, true)} / 월세 ${r.monthly_rent_manwon}만`;
  }
  if (r.rent_type === "전세") return `전세 ${formatManwon(r.deposit_manwon, true)}`;
  return formatManwon(r.deposit_manwon || r.price_manwon);
}

// 종합 점수 표시 — 정수로 반올림하면 1~3위가 모두 같은 점수로 보여 소수점 1자리
function scoreText(r) {
  const v = r.score_precise ?? r.score ?? 0;
  return Number(v).toFixed(1);
}

function commuteIcon(r) {
  return (r && (r.transport_mode || _transportMode)) === "car" ? "🚗" : "🚇";
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

// ── 찜하기 토글 (DB 저장 + 하트 색칠 동기화) ──────────────
let _favKeySet = {};   // { raw: property_key } 로컬 캐시

function _favRaw(r) {
  return `${(r.address || '').trim()}|${r.area_m2 || 0}|${r.deposit_manwon || 0}|${r.monthly_rent_manwon || 0}`;
}

// 카드 하트 클릭
async function toggleFav(btn) {
  if (!btn) return;
  const r = sortedResults()[parseInt(btn.dataset.id || '0')];
  if (!r) return;
  btn.disabled = true;
  btn.textContent = _favKeySet[_favRaw(r)] ? '🤍' : '❤️';   // 낙관적 표시
  try { await toggleFavForResult(r); } finally { btn.disabled = false; syncHeartButtons(); }
}

// 매물 하나 찜 토글 (카드·모달 공용) + 토스트 (해제는 되돌리기 가능). 성공 시 찜 여부 반환.
async function toggleFavForResult(r, { quiet = false } = {}) {
  const userId = parseInt(localStorage.getItem('user_id') || '0');
  if (!userId) {
    alert('로그인이 필요해요.');
    return null;
  }
  try {
    const res = await fetch((window.API_BASE || "") + `/favorites/toggle?user_id=${userId}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + (localStorage.getItem('token') || ''),
      },
      body: JSON.stringify({
        address:             r.address || '',
        gu:                  r.gu || '',
        dong:                r.dong || '',
        house_type:          r.house_type || '',
        rent_type:           r.rent_type || '',
        area_m2:             r.area_m2 || 0,
        deposit_manwon:      r.deposit_manwon || 0,
        monthly_rent_manwon: r.monthly_rent_manwon || 0,
        commute_min:         r.commute_min || 0,
        score:               r.score || 0,
        lat:                 r.lat || 0,
        lon:                 r.lon || 0,
        commute_score:       r.commute_score,
        cost_score:          r.cost_score,
        infra_score:         r.infra_score,
        applicable_policies:       r.applicable_policies || [],
        applicable_policies_total: r.applicable_policies_total || 0,
        transport_mode:            r.transport_mode || _transportMode || null,
        // 찜 매물에 직장 좌표 영구 저장 — 찜목록 모달의 생활비 영수증 계산용
        // (sessionStorage 휘발성 의존 제거)
        workplace_lat:       _workplaceCoords && _workplaceCoords.lat,
        workplace_lon:       _workplaceCoords && _workplaceCoords.lon,
      }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '요청 실패');

    const raw = _favRaw(r);
    if (data.action === 'added') {
      _favKeySet[raw] = data.property_key;
    } else {
      delete _favKeySet[raw];
    }
    syncHeartButtons();
    if (!quiet) {
      if (data.action === 'added') showToast('❤️ 찜 목록에 담았어요');
      else showToast('찜을 해제했어요', { actionLabel: '되돌리기', onAction: () => toggleFavForResult(r, { quiet: true }) });
    }
    return data.action === 'added';
  } catch (e) {
    console.error('찜 토글 실패:', e);
    showToast('찜 처리에 실패했어요. 잠시 후 다시 시도해 주세요.');
    syncHeartButtons();
    return null;
  }
}

// 매물 모달의 찜 버튼 (property-modal.js 가 사용)
window.__modalFav = {
  isFav: (r) => !!_favKeySet[_favRaw(r)],
  toggle: (r) => toggleFavForResult(r),
};

async function loadFavKeys() {
  const userId = parseInt(localStorage.getItem('user_id') || '0');
  if (!userId) return;
  try {
    const res = await fetch((window.API_BASE || "") + `/favorites?user_id=${userId}`, {
      headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || '') },
    });
    const favs = await res.json();
    _favKeySet = {};
    favs.forEach(f => {
      const raw = _favRaw(f);
      _favKeySet[raw] = f.property_key;
    });
    syncHeartButtons();
  } catch (e) {
    console.error('찜 키 로드 실패:', e);
  }
}

function syncHeartButtons() {
  if (typeof window.__repaintModalFav === "function") window.__repaintModalFav();
  const results = sortedResults();
  document.querySelectorAll('.rec-heart-btn').forEach(btn => {
    const idx = parseInt(btn.dataset.id || '0');
    const r = results[idx];
    if (!r) return;
    btn.textContent = _favKeySet[_favRaw(r)] ? '❤️' : '🤍';
  });
}

window.toggleFav = toggleFav;

// ── 서울 평균 비교 ──────────────────────────────────────
// seoulAvg: /api/recommend 의 seoul_avg — 서울 실거래 평균 (전세/월세 따로)
//   { by_rent_type: { 전세: {avg_deposit, sample_size}, 월세: {avg_deposit, avg_monthly, sample_size} },
//     price_per_m2 }
// 1위 매물과 "같은 임대유형"의 서울 평균을 비교한다 (월세 검색 결과에도 전세 매물이 섞일 수 있음).
function renderAvgSection(seoulAvg, results) {
  try {
    const top1 = results[0];
    if (!seoulAvg || !seoulAvg.by_rent_type || !top1) return;
    const rentType = top1.rent_type || (top1.monthly_rent_manwon > 0 ? "월세" : "전세");
    const ref = seoulAvg.by_rent_type[rentType];
    if (!ref) return;

    const rows = [];
    const addRow = (label, avg, mine, fmt) => {
      if (!avg || !mine) return;
      const diff = Math.round(mine - avg);
      const cls  = diff < 0 ? "rec-avg-good" : "rec-avg-bad";
      const diffTxt = diff === 0 ? "같음" : `${diff < 0 ? "▼" : "▲"} ${fmt(Math.abs(diff))}`;
      rows.push(`<div class="rec-avg-row">
        <span class="rec-avg-label">${label}</span>
        <span>1위 매물 vs 서울 ${escHtml(rentType)} 평균 <b>${fmt(avg)}</b></span>
        <span class="${cls}">${diffTxt}</span>
      </div>`);
    };
    addRow("💰 보증금", ref.avg_deposit, top1.deposit_manwon, v => formatManwon(v));
    if (rentType === "월세") {
      addRow("📅 월세", ref.avg_monthly, top1.monthly_rent_manwon, v => `${Math.round(v)}만원`);
    }
    if (!rows.length) return;

    avgContentEl.innerHTML = rows.join("");
    avgSectionEl.style.display = "block";
  } catch (e) { /* 평균 없으면 조용히 숨김 */ }
}

// ── 상태 전환 ────────────────────────────────────────────
const STATE_SUBTITLES = {
  loading:      "추천 결과를 불러오는 중이에요",
  error:        "추천 결과를 보여드리지 못했어요",
  "no-session": "아직 추천 결과가 없어요",
  results:      "최적의 동네를 찾았어요",
};

function showState(state) {
  const subEl = document.getElementById("rec-page-subtitle");
  if (subEl && STATE_SUBTITLES[state]) subEl.textContent = STATE_SUBTITLES[state];
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
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
