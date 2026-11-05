"use strict";

// ── 상태 ─────────────────────────────────────────────────
let sessionId      = null;
let isWaiting      = false;
let isRecommending = false;
let latestSlotStatus = null;  // 최신 slot_status (수정 메뉴 조건부 표시용)

// ── 슬롯 레이블 (친절한 표시용) ─────────────────────────
const SLOT_DISPLAY_LABELS = {
  "work_address":      "직장 주소",
  "transport_mode":    "이동수단",
  "rent_type":         "임대 유형",
  "deposit_manwon":    "예산 (보증금)",
  "monthly_manwon":    "예산 (월세)",
  "allowed_minutes":   "통근 시간",
  "house_type":        "주택 유형",
  "weight_preference": "우선순위",
  "vibe":              "동네 분위기",
  "use_youth_policy":  "청년정책 반영",
  "policy_employment": "취업상태",
  "policy_education":  "학력",
  "policy_income":     "월 소득",
  "policy_no_house":   "무주택여부",
};

function friendlyCommandLabel(cmd) {
  if (cmd === "__PREV__") return "이전으로 ↩";
  if (cmd === "__CANCEL_EDIT_ACTIVE__") return "수정 취소 ↩";
  if (cmd.startsWith("__EDIT:") && cmd.endsWith("__")) {
    const slot = cmd.slice(7, -2);
    return `✏️ ${SLOT_DISPLAY_LABELS[slot] || slot} 수정`;
  }
  return cmd;
}

// ── 세션스토리지 키 ──────────────────────────────────────
const CHAT_HISTORY_KEY = "chat_history";
const REC_CACHE_KEY    = "rec_results_cache";
const SESSION_ID_KEY   = "chat_session_id";
const QUICK_OPTS_KEY   = "chat_quick_options";   // 마지막 빠른선택 버튼 (페이지 이동 후 복원용)
// 추천 세션·결과 개수·조건 칩은 탭마다 따로 (예전엔 localStorage 공유 → 다른 탭 조건이 섞이고
// 다른 탭의 '새 추천 시작'이 이 탭 세션까지 지웠다). 새 탭은 서버의 '최근 추천'으로 이어본다.
const REC_SESSION_KEY  = "rec_session_id";
const REC_COUNT_KEY    = "rec_result_count";
const REC_CHIPS_KEY    = "rec_cond_chips";
const REC_POLL_MS      = 2000;

// ── 인증 토큰 ────────────────────────────────────────────
function getAuthHeaders() {
  const token = localStorage.getItem('token');
  return token
    ? { "Content-Type": "application/json", "Authorization": "Bearer " + token }
    : { "Content-Type": "application/json" };
}

// ── DOM ──────────────────────────────────────────────────
const messagesEl     = document.getElementById("messages");
const inputEl        = document.getElementById("user-input");
const sendBtn        = document.getElementById("send-btn");
const quickOptEl     = document.getElementById("quick-options");
const progressListEl = document.getElementById("progress-list");
const condTableEl    = document.getElementById("condition-table");
const condChipsEl    = document.getElementById("condition-chips");
const resultsBtnEl   = document.getElementById("result-btn");

// 진행도 항목 정의 (순서 = 챗봇 질문 순서)
const PROGRESS_STEPS = [
  { key: "work_address",     label: "직장 위치" },
  { key: "budget",           label: "예산" },
  { key: "allowed_minutes",  label: "통근 허용 시간" },
  { key: "house_type",       label: "주택 유형" },
  { key: "weight_preference",label: "우선순위 선택" },
];

// ── 초기화 ───────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  renderProgressList([]);
  renderConditionTable([]);

  // 새로고침이면 채팅 기록 초기화, 내비게이션이면 기록 복원
  const navType = (performance.getEntriesByType("navigation")[0] || {}).type;
  let restored = false;
  if (navType === "reload") {
    sessionStorage.removeItem(CHAT_HISTORY_KEY);
    sessionStorage.removeItem(SESSION_ID_KEY);
    sessionStorage.removeItem(QUICK_OPTS_KEY);
    showInitialMessage();
  } else {
    sessionId = sessionStorage.getItem(SESSION_ID_KEY) || null;
    const history = getChatHistory();
    if (history.length) {
      restoreChatHistory(history);
      restoreQuickOptions();
      restoreResultButton();
      restored = true;
    } else {
      showInitialMessage();
    }
  }
  enableRecNav();
  inputEl.focus();

  // 마이페이지 '조건 다시 설정하기' → 이전 조건을 불러와 수정 메뉴로
  if (new URLSearchParams(location.search).get("rerun") === "1") {
    history.replaceState(null, "", "/");
    await rerunFromLatest();
    return;
  }

  // 이 탭에서 진행 중이던 추천 → 진행률 polling 재개
  // 새 탭·새로고침 → 서버에 저장된 최근 추천이 진행 중/완료인지 확인
  if (!(await resumeRecommendationIfNeeded()) && !restored) {
    await offerLatestRecommendation();
  }
});

// ── 초기 메시지 ──────────────────────────────────────────
function showInitialMessage() {
  appendBotMessage(
    "안녕하세요! 저는 집찾봇이에요 🏠\n\n" +
    "서울시 공공데이터를 기반으로 최적의 주거지를 찾아드릴게요.\n" +
    "몇 가지 질문에 답해주시면 바로 추천해드릴게요!\n\n" +
    "어디서 일하실 예정인가요?\n직장 주소나 지하철역을 알려주세요."
  );
}

// ── 이벤트 ───────────────────────────────────────────────
sendBtn.addEventListener("click", () => sendMessage());
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
  setTimeout(() => {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + "px";
  }, 0);
});

// ── 메시지 전송 ──────────────────────────────────────────
// label: 빠른선택 버튼으로 보낸 경우 말풍선에 보여줄 버튼 이름 (실제 전송값은 text)
async function sendMessage(text, label) {
  const userText = (text !== undefined ? text : inputEl.value).trim();
  if (!userText || isWaiting) return;

  inputEl.value = "";
  inputEl.style.height = "auto";
  clearQuickOptions();

  // ── 특수 명령: API 호출 없이 직접 처리 ──────────────────
  if (userText === "__START_REC__") {
    appendUserMessage(label || "추천 시작 🏠");
    startRecommendation(sessionId);
    return;
  }
  if (userText === "__EDIT__") {
    appendUserMessage(label || "수정하기 ✏️");
    showEditOptions();
    return;
  }
  if (userText === "__CANCEL_EDIT__") {
    appendUserMessage("취소 ↩");
    appendBotMessage("수정 취소했어요. 추천을 시작하거나 다른 항목을 수정할 수 있어요.");
    renderQuickOptions([
      { label: "추천 시작 🏠", value: "__START_REC__" },
      { label: "수정하기 ✏️",  value: "__EDIT__" },
    ]);
    return;
  }

  // ── 나머지 명령 / 일반 텍스트 ────────────────────────────
  // __PREV__, __EDIT:*__ 은 raw command를 API에 전송하되
  // 사용자 버블에는 친절한 레이블을 표시
  const displayText = label || friendlyCommandLabel(userText);
  appendUserMessage(displayText);
  setWaiting(true);

  try {
    const res  = await fetch((window.API_BASE || "") + "/api/chat", {
      method:  "POST",
      headers: getAuthHeaders(),
      body:    JSON.stringify({ user_message: userText, session_id: sessionId }),
    });
    const data = await res.json();

    sessionId = data.session_id;
    sessionStorage.setItem(SESSION_ID_KEY, sessionId);

    appendBotMessage(data.bot_message);
    updatePanels(data.slot_status);
    latestSlotStatus = data.slot_status || null;
    renderQuickOptions(data.quick_options, data.quick_options_multi);

    if (data.is_complete) {
      sessionStorage.setItem(REC_SESSION_KEY, sessionId);
      saveCondChips(data.slot_status);
      enableRecNav();
      // 자동 추천 없음 — 사용자가 직접 선택
      renderQuickOptions([
        { label: "추천 시작 🏠", value: "__START_REC__" },
        { label: "수정하기 ✏️",  value: "__EDIT__" },
      ]);
    }
  } catch (err) {
    appendBotMessage("오류가 발생했습니다. 잠시 후 다시 시도해 주세요.");
    console.error(err);
  } finally {
    setWaiting(false);
  }
}

// ── 추천 실행 ───────────────────────────────────────────
// POST /api/recommend 는 바로 응답(running)하고, 진행률·결과는 GET /api/recommend/result 로 polling.
// (예전엔 POST 하나가 4~7분 열려 있어 진행 상황을 알 수 없었다)
async function startRecommendation(sid) {
  if (isRecommending || !sid) return;
  isRecommending = true;
  sessionStorage.removeItem(REC_CACHE_KEY);
  sessionStorage.removeItem(REC_COUNT_KEY);
  resultsBtnEl.classList.remove("visible");
  enableRecNav();
  sessionStorage.setItem("rec_in_progress", "1");
  sessionStorage.setItem("rec_in_progress_session", sid);
  setWaiting(true);
  showRecProgress({ percent: 0, message: "추천을 준비하고 있어요" });

  try {
    const res  = await fetch((window.API_BASE || "") + "/api/recommend", {
      method:  "POST",
      headers: getAuthHeaders(),
      body:    JSON.stringify({ session_id: sid, async: true }),   // 바로 응답받고 진행률 polling
    });
    const data = await res.json();
    await followRecommendation(sid, data, res.ok || res.status === 202);
  } catch (err) {
    console.error(err);
    finishRecProgress();
    appendBotMessage("추천을 시작하지 못했어요. 네트워크를 확인하고 다시 시도해 주세요.");
    renderAfterRecommendOptions();
  } finally {
    setWaiting(false);
    isRecommending = false;
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
  }
}

// 상태(data)를 받아 완료될 때까지 polling 하며 진행률 표시 → 결과 처리
async function followRecommendation(sid, data, ok = true) {
  const MAX_MS = 20 * 60 * 1000;
  const startedAt = Date.now();
  while (true) {
    if (!ok || !data || data.status === "error") {
      finishRecProgress();
      const msg = (data && data.user_message)
        || "추천 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.";
      appendBotMessage(msg);
      renderAfterRecommendOptions();
      return;
    }
    if (data.status === "complete") {
      finishRecProgress();
      onRecommendationComplete(sid, data);
      return;
    }
    if (data.status === "not_started") {
      finishRecProgress();
      appendBotMessage("추천 정보를 찾지 못했어요. 다시 추천을 시작해 주세요.");
      renderAfterRecommendOptions();
      return;
    }
    showRecProgress(data.progress || {});
    if (Date.now() - startedAt > MAX_MS) {
      finishRecProgress();
      appendBotMessage("추천이 평소보다 오래 걸리고 있어요. 잠시 후 '주거 추천' 메뉴에서 결과를 확인해 주세요.");
      return;
    }
    await new Promise(r => setTimeout(r, REC_POLL_MS));
    try {
      const res = await fetch(
        (window.API_BASE || "") + "/api/recommend/result?session_id=" + encodeURIComponent(sid),
        { headers: getAuthHeaders() }
      );
      ok = res.ok;
      data = await res.json();
    } catch (e) {
      // 일시적인 연결 끊김 — 다음 주기에 다시 시도
      ok = true;
      data = { status: "running", progress: { percent: (data.progress || {}).percent || 0, message: "연결을 다시 확인하고 있어요" } };
    }
  }
}

function onRecommendationComplete(sid, data) {
  const results = data.results || [];
  const count   = results.length;
  // 결과 sessionStorage 캐싱 (추천 페이지·마이페이지에서 재사용) — 어느 세션 결과인지 함께 저장
  sessionStorage.setItem(REC_CACHE_KEY, JSON.stringify({
    session_id:      sid,
    results:         data.results,
    seoul_avg:       data.seoul_avg || null,
    workplace_lat:   data.workplace_lat,
    workplace_lon:   data.workplace_lon,
    allowed_minutes: data.allowed_minutes,
    transport_mode:  data.transport_mode,
    cond_chips:      data.cond_chips || null,
  }));
  sessionStorage.setItem(REC_SESSION_KEY, sid);
  sessionStorage.setItem(REC_COUNT_KEY, String(count));
  enableRecNav();
  if (count > 0) {
    appendBotMessage(`추천이 완료됐어요! 🎉\n총 ${count}곳을 추천해드릴게요.\n아래 버튼을 눌러 결과를 확인해 보세요!`);
    resultsBtnEl.classList.add("visible");
    // 조건을 바꿔 다시 찾을 수 있게 (예전엔 완료 후 버튼이 없어 '수정하기'를 직접 입력해야 했음)
    renderQuickOptions([{ label: "수정하기 ✏️", value: "__EDIT__" }]);
  } else {
    appendBotMessage("조건에 맞는 매물이 없어요 😥\n조건을 조금 완화해서 다시 시도해 보세요.");
    renderAfterRecommendOptions(false);
  }
}

// ── 진행률 표시 (말풍선 안 진행 막대) ──────────────────────
function showRecProgress(progress) {
  removeTyping();
  const pct = Math.max(0, Math.min(100, Math.round(progress.percent || 0)));
  const msg = progress.message || "추천을 진행하고 있어요";
  let row = document.getElementById("rec-progress-row");
  if (!row) {
    row = document.createElement("div");
    row.id = "rec-progress-row";
    row.className = "msg-group bot";
    row.innerHTML = `
      <div class="bubble bot rec-progress-bubble" role="status" aria-live="polite">
        <div class="rec-progress-title">🔍 맞춤 주거지를 찾고 있어요</div>
        <div class="rec-progress-track"><div class="rec-progress-fill"></div></div>
        <div class="rec-progress-meta">
          <span class="rec-progress-msg"></span>
          <span class="rec-progress-pct"></span>
        </div>
        <div class="rec-progress-note">💡 화면을 닫거나 다른 메뉴로 이동해도 추천은 계속 진행돼요.
          나중에 '주거 추천' 메뉴에서 결과를 확인할 수 있어요.</div>
      </div>`;
    messagesEl.appendChild(row);
    scrollBottom();
  }
  row.querySelector(".rec-progress-fill").style.width = pct + "%";
  row.querySelector(".rec-progress-msg").textContent = msg;
  row.querySelector(".rec-progress-pct").textContent = pct + "%";
}

function finishRecProgress() {
  const row = document.getElementById("rec-progress-row");
  if (row) row.remove();
}

// ── 페이지 진입 시 진행 중 추천 복원 ─────────────────────
// 이 탭에서 시작한 추천이 진행 중이면 polling 재개. 처리했으면 true.
async function resumeRecommendationIfNeeded() {
  if (sessionStorage.getItem("rec_in_progress") !== "1") return false;
  const sid = sessionStorage.getItem("rec_in_progress_session") || sessionId;
  if (!sid) {
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
    return false;
  }
  isRecommending = true;
  setWaiting(true);
  try {
    const res = await fetch(
      (window.API_BASE || "") + "/api/recommend/result?session_id=" + encodeURIComponent(sid),
      { headers: getAuthHeaders() }
    );
    await followRecommendation(sid, await res.json(), res.ok);
  } catch (e) {
    appendBotMessage("결과를 가져오는 중 연결 오류가 발생했어요.", true);
  } finally {
    setWaiting(false);
    isRecommending = false;
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
  }
  return true;
}

// 새 탭·새로고침: 서버에 저장된 이 사용자의 최근 추천이 있으면 안내 (진행 중이면 진행률 표시)
async function offerLatestRecommendation() {
  let data;
  try {
    const res = await fetch((window.API_BASE || "") + "/api/recommend/latest", { headers: getAuthHeaders() });
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }
  if (!data || !data.session_id) return;
  if (data.status === "running") {
    appendBotMessage("진행 중인 추천이 있어요 🔍 결과가 나오면 바로 알려드릴게요.", true);
    sessionStorage.setItem("rec_in_progress", "1");
    sessionStorage.setItem("rec_in_progress_session", data.session_id);
    await resumeRecommendationIfNeeded();
  } else if (data.status === "complete" && (data.results || []).length) {
    sessionStorage.setItem(REC_SESSION_KEY, data.session_id);
    sessionStorage.setItem(REC_COUNT_KEY, String(data.results.length));
    sessionStorage.setItem(REC_CACHE_KEY, JSON.stringify({
      session_id: data.session_id, results: data.results, seoul_avg: data.seoul_avg || null,
      workplace_lat: data.workplace_lat, workplace_lon: data.workplace_lon,
      allowed_minutes: data.allowed_minutes, transport_mode: data.transport_mode,
      cond_chips: data.cond_chips || null,
    }));
    enableRecNav();
    resultsBtnEl.classList.add("visible");
    appendBotMessage(`최근 추천 결과(${data.results.length}곳)가 있어요. 위의 '주거 추천 결과 보기'로 확인할 수 있어요.\n새로 찾으시려면 직장 주소부터 알려주세요.`, true);
  }
}

// 마이페이지 '조건 다시 설정하기' — 최근 추천의 조건을 불러와 바로 수정 메뉴 표시
async function rerunFromLatest() {
  let sid = sessionId;
  if (!sid) {
    try {
      const res = await fetch((window.API_BASE || "") + "/api/recommend/latest", { headers: getAuthHeaders() });
      const d = res.ok ? await res.json() : null;
      sid = d && d.session_id;
    } catch { sid = null; }
  }
  if (!sid) {
    appendBotMessage("이전에 입력한 조건이 없어요. 직장 주소부터 알려주세요.", true);
    return;
  }
  sessionId = sid;
  sessionStorage.setItem(SESSION_ID_KEY, sid);
  setWaiting(true);
  try {
    const res = await fetch((window.API_BASE || "") + "/api/chat", {
      method: "POST", headers: getAuthHeaders(),
      body: JSON.stringify({ user_message: "__SUMMARY__", session_id: sid }),
    });
    const data = await res.json();
    sessionId = data.session_id;
    sessionStorage.setItem(SESSION_ID_KEY, sessionId);
    updatePanels(data.slot_status);
    latestSlotStatus = data.slot_status || null;
    appendBotMessage("이전 조건을 불러왔어요 🔄\n\n" + data.bot_message);
    if (data.is_complete) {
      sessionStorage.setItem(REC_SESSION_KEY, sessionId);
      setWaiting(false);
      showEditOptions();
    } else {
      renderQuickOptions(data.quick_options, data.quick_options_multi);
    }
  } catch (e) {
    appendBotMessage("이전 조건을 불러오지 못했어요. 직장 주소부터 알려주세요.", true);
  } finally {
    setWaiting(false);
  }
}

// 추천이 0건이거나 실패한 뒤 — 바로 조건을 바꾸거나 다시 시도할 수 있게
// withRetry: 오류일 때만 '다시 추천' (0건이면 같은 조건 재실행은 의미 없음 → 수정만)
function renderAfterRecommendOptions(withRetry = true) {
  const opts = [{ label: "수정하기 ✏️", value: "__EDIT__" }];
  if (withRetry) opts.push({ label: "다시 추천 🔄", value: "__START_REC__" });
  renderQuickOptions(opts);
}

// ── 수정하기: 항목 선택 버튼 표시 ───────────────────────
function showEditOptions() {
  appendBotMessage("어떤 항목을 수정하시겠어요? 🔧");

  // latestSlotStatus 에서 슬롯별 정보 추출 (rent_type, use_youth_policy 등 조건 확인용)
  const slots = {};
  (latestSlotStatus || []).forEach(s => { slots[s.key] = s; });
  const rentType = slots.rent_type && slots.rent_type.value;
  const usePolicy = slots.use_youth_policy && (
    slots.use_youth_policy.value === true ||
    slots.use_youth_policy.value === "true" ||
    slots.use_youth_policy.value === "예"
  );

  const opts = [
    { label: "직장 주소",   value: "__EDIT:work_address__" },
    { label: "이동수단",    value: "__EDIT:transport_mode__" },
    { label: "임대 유형",   value: "__EDIT:rent_type__" },
  ];
  // 월세 사용자: deposit_manwon 은 "보증금"(전세금이 아님) + 월세 금액 별도 표시
  // 전세 사용자: deposit_manwon 만 "예산" 으로 표시
  if (rentType === "월세") {
    opts.push({ label: "보증금",    value: "__EDIT:deposit_manwon__" });
    opts.push({ label: "월세 금액", value: "__EDIT:monthly_manwon__" });
  } else {
    opts.push({ label: "예산",      value: "__EDIT:deposit_manwon__" });
  }
  opts.push(
    { label: "통근 시간",   value: "__EDIT:allowed_minutes__" },
    { label: "주택 유형",   value: "__EDIT:house_type__" },
    { label: "우선순위",    value: "__EDIT:weight_preference__" },
    { label: "동네 분위기", value: "__EDIT:vibe__" },
    { label: "청년정책 반영", value: "__EDIT:use_youth_policy__" },
  );
  if (usePolicy) {
    opts.push(
      { label: "취업상태",   value: "__EDIT:policy_employment__" },
      { label: "학력",       value: "__EDIT:policy_education__" },
      { label: "월 소득",    value: "__EDIT:policy_income__" },
      { label: "무주택여부", value: "__EDIT:policy_no_house__" },
    );
  }
  // 잘못 누른 경우 빠져나갈 수 있는 취소 버튼 (마지막 위치, 보조 스타일)
  opts.push({ label: "취소 ↩", value: "__CANCEL_EDIT__" });
  renderQuickOptions(opts);
}

// ── 새 추천 시작 (초기화) ────────────────────────────────
function resetChat() {
  // 대화가 진행된 상태면 한 번 확인 (실수로 눌러 대화가 통째로 사라지던 문제)
  const hasConversation = getChatHistory().some(m => m.role === "user");
  if (hasConversation && !confirm("지금까지 입력한 대화를 지우고 새로 시작할까요?")) return;
  if (isRecommending) {
    alert("추천이 진행 중이에요. 결과가 나온 뒤 새로 시작해 주세요.");
    return;
  }
  // 세션스토리지 초기화 (이 탭만 — 다른 탭의 추천에는 영향 없음)
  sessionStorage.removeItem(CHAT_HISTORY_KEY);
  sessionStorage.removeItem(REC_CACHE_KEY);
  sessionStorage.removeItem(SESSION_ID_KEY);
  sessionStorage.removeItem(QUICK_OPTS_KEY);
  sessionStorage.removeItem("rec_in_progress");
  sessionStorage.removeItem("rec_in_progress_session");
  sessionStorage.removeItem(REC_SESSION_KEY);
  sessionStorage.removeItem(REC_COUNT_KEY);
  sessionStorage.removeItem(REC_CHIPS_KEY);

  // 상태 초기화
  sessionId         = null;
  isRecommending    = false;
  latestSlotStatus  = null;

  // UI 초기화
  messagesEl.innerHTML = "";
  clearQuickOptions();
  resultsBtnEl.classList.remove("visible");
  renderProgressList([]);
  renderConditionTable([]);

  const condSection = document.getElementById("current-conditions");
  if (condSection) condSection.style.display = "none";

  // 주거추천·지도뷰는 항상 활성 상태 유지 — 배지만 초기화
  const navBadge = document.getElementById("nav-rec-badge");
  if (navBadge) { navBadge.textContent = ""; navBadge.style.display = "none"; }

  showInitialMessage();
  inputEl.focus();
}

// ── 추천 결과 페이지로 이동 ──────────────────────────────
function scrollToResults() {
  location.href = "/recommendation";
}

// 검색 조건 칩 저장 (이 탭의 추천 페이지 요약 바용 — 결과에 칩이 없을 때의 폴백)
// 순서는 챗봇 요약(_build_summary)과 일치: 직장 → 이동수단 → 예산 → 통근 → 분위기 → 청년정책
function saveCondChips(slots) {
  if (!slots) return;
  const map = {};
  slots.forEach(s => { if (s.filled) map[s.key] = s.value; });
  const chips = [];
  if (map.work_address)    chips.push("📍 " + map.work_address);
  if (map.transport_mode) {
    const isCar = map.transport_mode === "car";
    chips.push((isCar ? "🚗 " : "🚌 ") + (isCar ? "자가용" : "대중교통"));
  }
  if (map.rent_type === "월세" && map.monthly_manwon) {
    chips.push("💰 월세 " + map.monthly_manwon + "만원");
  } else if (map.deposit_manwon) {
    chips.push("💰 전세 " + Number(map.deposit_manwon).toLocaleString() + "만원");
  }
  if (map.allowed_minutes) chips.push("🕐 통근 " + map.allowed_minutes + "분 이내");
  if (map.vibe)            chips.push("🏘 " + map.vibe);
  if (map.use_youth_policy === true || map.use_youth_policy === "true") chips.push("📋 청년정책 반영");
  sessionStorage.setItem(REC_CHIPS_KEY, JSON.stringify(chips));
}

// 사이드바 추천 결과 배지 업데이트 (nav 자체는 항상 활성 상태)
function enableRecNav() {
  const navBadge = document.getElementById("nav-rec-badge");
  if (navBadge) {
    const count = parseInt(sessionStorage.getItem(REC_COUNT_KEY) || "0", 10);
    if (count > 0) {
      navBadge.textContent = count;
      navBadge.style.display = "";
    } else {
      navBadge.textContent = "";
      navBadge.style.display = "none";
    }
  }
}

// ── 말풍선 ───────────────────────────────────────────────
function appendBotMessage(text, skipSave = false, time = null) {
  removeTyping();
  if (!skipSave) saveChatMessage("bot", text);
  const group = document.createElement("div");
  group.className = "msg-group bot";
  group.innerHTML = `
    <div class="bubble bot">${nl2br(boldify(escHtml(text)))}</div>
    <div class="msg-time">${escHtml(time || nowTime())}</div>
  `;
  messagesEl.appendChild(group);
  scrollBottom();
}

function appendUserMessage(text, skipSave = false, time = null) {
  if (!skipSave) saveChatMessage("user", text);
  const group = document.createElement("div");
  group.className = "msg-group user";
  group.innerHTML = `
    <div class="bubble user">${nl2br(escHtml(text))}</div>
    <div class="msg-time">${escHtml(time || nowTime())}</div>
  `;
  messagesEl.appendChild(group);
  scrollBottom();
}

function showTyping() {
  removeTyping();
  const group = document.createElement("div");
  group.id = "typing-row";
  group.className = "msg-group bot";
  group.innerHTML = `<div class="bubble bot typing-dots"><span></span><span></span><span></span></div>`;
  messagesEl.appendChild(group);
  scrollBottom();
}

function removeTyping() {
  const el = document.getElementById("typing-row");
  if (el) el.remove();
}

function setWaiting(v) {
  isWaiting = v;
  sendBtn.disabled = v;
  // 입력창은 막지 않음 — 기다리는 동안 다음 답을 미리 써 둘 수 있게 (전송은 응답 후)
  inputEl.classList.toggle("is-waiting", v);
  if (v && !document.getElementById("rec-progress-row")) showTyping();
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── 채팅 기록 (sessionStorage) ──────────────────────────
function saveChatMessage(role, text) {
  const history = getChatHistory();
  history.push({ role, text, time: nowTime() });
  sessionStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(history));
}

function getChatHistory() {
  try {
    return JSON.parse(sessionStorage.getItem(CHAT_HISTORY_KEY) || "[]");
  } catch { return []; }
}

function restoreChatHistory(history) {
  history.forEach(msg => {
    if (msg.role === "bot") appendBotMessage(msg.text, true, msg.time);
    else                    appendUserMessage(msg.text, true, msg.time);
  });
}

// 페이지 이동 후 돌아왔을 때 마지막 빠른선택 버튼 복원
// (없으면 버튼 없이 텍스트만 남아 '추천 시작'을 누를 방법이 없었음)
function restoreQuickOptions() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(QUICK_OPTS_KEY) || "null");
    if (saved && saved.options && saved.options.length) {
      renderQuickOptions(saved.options, saved.multi);
    }
  } catch { /* 무시 */ }
}

// 이미 받은 추천 결과가 있으면 '주거 추천 결과 보기' 버튼 다시 표시
function restoreResultButton() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(REC_CACHE_KEY) || "null");
    if (cached && cached.results && cached.results.length) resultsBtnEl.classList.add("visible");
  } catch { /* 무시 */ }
}

// ── 빠른 옵션 ────────────────────────────────────────────
// multiSelect 가 true 면 옵션 중 일반(__로 시작 안 함) 항목들이 체크박스 모드로 표시되고,
// 마지막에 "선택 완료" 버튼이 추가됨. 명령 옵션(__PREV__, __CANCEL_*__) 은 단일 클릭 유지.
function renderQuickOptions(options, multiSelect) {
  clearQuickOptions();
  if (!options || !options.length) return;
  try { sessionStorage.setItem(QUICK_OPTS_KEY, JSON.stringify({ options, multi: !!multiSelect })); } catch { /* 무시 */ }

  const cmdOpts   = [];
  const multiOpts = [];
  options.forEach(opt => {
    if (multiSelect && typeof opt.value === "string" && !opt.value.startsWith("__")) {
      multiOpts.push(opt);
    } else {
      cmdOpts.push(opt);
    }
  });

  // 다중선택 버튼 (체크박스 스타일)
  multiOpts.forEach(opt => {
    const btn = document.createElement("button");
    btn.className = "quick-btn quick-btn-multi";
    btn.textContent = opt.label;
    btn.dataset.value = opt.value;
    btn.dataset.selected = "0";
    btn.onclick = () => {
      const isOn = btn.dataset.selected === "1";
      // '상관없음'은 다른 항목과 함께 고를 수 없음 — 고르면 나머지 해제, 다른 항목을 고르면 '상관없음' 해제
      const isAny = opt.value.includes("상관없음");
      if (!isOn) {
        quickOptEl.querySelectorAll('.quick-btn-multi[data-selected="1"]').forEach(other => {
          if (other !== btn && (isAny || other.dataset.value.includes("상관없음"))) {
            other.dataset.selected = "0";
            other.classList.remove("quick-btn--checked");
          }
        });
      }
      btn.dataset.selected = isOn ? "0" : "1";
      btn.classList.toggle("quick-btn--checked", !isOn);
    };
    quickOptEl.appendChild(btn);
  });

  // "선택 완료 →" 버튼 (다중선택일 때만)
  if (multiSelect && multiOpts.length) {
    const submitBtn = document.createElement("button");
    submitBtn.className = "quick-btn quick-btn-submit";
    submitBtn.textContent = "선택 완료 →";
    submitBtn.onclick = () => {
      const chosen = Array.from(quickOptEl.querySelectorAll('.quick-btn-multi[data-selected="1"]'));
      if (!chosen.length) {
        appendBotMessage("1개 이상 선택해주세요.", true);
        return;
      }
      sendMessage(chosen.map(b => b.dataset.value).join(" "), chosen.map(b => b.textContent).join(", "));
    };
    quickOptEl.appendChild(submitBtn);
  }

  // 명령 옵션 (단일 클릭)
  cmdOpts.forEach(opt => {
    const btn = document.createElement("button");
    const isPrev = (
      opt.value === "__PREV__"
      || opt.value === "__CANCEL_EDIT__"
      || opt.value === "__CANCEL_EDIT_ACTIVE__"
    );
    btn.className = "quick-btn" + (isPrev ? " quick-btn-prev" : "");
    btn.textContent = opt.label;
    btn.onclick = () => sendMessage(opt.value, opt.label);
    quickOptEl.appendChild(btn);
  });
}

function clearQuickOptions() {
  quickOptEl.innerHTML = "";
  try { sessionStorage.removeItem(QUICK_OPTS_KEY); } catch { /* 무시 */ }
}

// ── 패널 업데이트 ────────────────────────────────────────
function updatePanels(slots) {
  if (!slots) return;
  renderProgressList(slots);
  renderConditionTable(slots);
  updateSidebarConditions(slots);
}

// 진행도 리스트
function renderProgressList(slots) {
  const filled = {};
  (slots || []).forEach(s => { if (s.filled) filled[s.key] = s.value; });

  const hasBudget = filled["deposit_manwon"] || filled["rent_type"];
  if (hasBudget) filled["budget"] = budgetLabel(filled);

  let html = "";
  let foundCurrent = false;
  PROGRESS_STEPS.forEach((step, i) => {
    const isDone = !!filled[step.key];
    let cls, icon;
    if (isDone) {
      cls = "done"; icon = "✓";
    } else if (!foundCurrent) {
      foundCurrent = true;
      cls = "current"; icon = String(i + 1);
    } else {
      cls = "pending"; icon = String(i + 1);
    }
    const valueHtml = isDone && filled[step.key] !== true
      ? `<span class="prog-value">${escHtml(String(filled[step.key]))}</span>` : "";
    html += `
      <div class="progress-item">
        <div class="prog-badge ${cls}">${icon}</div>
        <span class="prog-label ${cls}">${escHtml(step.label)}</span>
        ${valueHtml}
      </div>
    `;
  });
  progressListEl.innerHTML = html;
}

// 입력된 조건 테이블
function renderConditionTable(slots) {
  const map = {};
  (slots || []).forEach(s => { map[s.key] = s; });

  const rows = [
    { key: "work_address",  label: "직장" },
    { key: "budget",        label: "예산",   virtual: true },
    { key: "allowed_minutes", label: "통근" },
    { key: "house_type",    label: "유형" },
  ];

  let html = "";
  rows.forEach(r => {
    let val = null;
    if (r.virtual && r.key === "budget") {
      val = budgetLabel(Object.fromEntries((slots||[]).filter(s=>s.filled).map(s=>[s.key, s.value])));
    } else {
      const s = map[r.key];
      val = s && s.filled ? s.value : null;
    }
    const valHtml = val
      ? `<span class="cond-val">${escHtml(String(val))}</span>`
      : `<span class="cond-empty">미입력</span>`;
    html += `<div class="condition-row"><span class="cond-label">${r.label}</span>${valHtml}</div>`;
  });
  condTableEl.innerHTML = html;
}

// 사이드바 현재 검색 조건
function updateSidebarConditions(slots) {
  const map = {};
  (slots || []).forEach(s => { if (s.filled) map[s.key] = s.value; });

  const chips = [];
  if (map.work_address)  chips.push("📍 " + map.work_address);
  if (map.deposit_manwon || map.rent_type) chips.push("💰 " + budgetLabel(map));
  if (map.allowed_minutes) chips.push("🕐 통근 " + map.allowed_minutes + "분 이내");

  if (chips.length) {
    document.getElementById("current-conditions").style.display = "block";
    condChipsEl.innerHTML = chips.map(c =>
      `<div class="search-condition"><span class="dot">•</span>${escHtml(c)}</div>`
    ).join("");
  }
}

function budgetLabel(map) {
  const rt  = map.rent_type;
  const dep = map.deposit_manwon;
  const mon = map.monthly_manwon;
  if (!rt && !dep) return null;
  if (rt === "월세" && mon) return `월세 ${mon}만원↓`;
  if (rt === "월세") return "월세";
  if (dep) return `전세 ${Number(dep).toLocaleString()}만원`;
  return rt || null;
}

// ── 유틸 ─────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function nl2br(s) {
  return s.replace(/\n/g, "<br>");
}

// 이스케이프된 문자열의 **굵게** 를 <b> 로 (봇 메시지 마크다운 별표가 그대로 보이던 문제)
function boldify(s) {
  return s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

function nowTime() {
  const d = new Date();
  const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  const ampm = h < 12 ? "오전" : "오후";
  return `${ampm} ${h % 12 || 12}:${m}`;
}
