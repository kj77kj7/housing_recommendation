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
document.addEventListener("DOMContentLoaded", () => {
  if (localStorage.getItem("rec_session_id")) enableRecNav();

  renderProgressList([]);
  renderConditionTable([]);

  // 새로고침이면 채팅 기록 초기화, 내비게이션이면 기록 복원
  const navType = (performance.getEntriesByType("navigation")[0] || {}).type;
  if (navType === "reload") {
    sessionStorage.removeItem(CHAT_HISTORY_KEY);
    sessionStorage.removeItem(REC_CACHE_KEY);
    sessionStorage.removeItem(SESSION_ID_KEY);
    // 새로고침 = 새 세션 → 이전 추천 결과 배지 클리어
    localStorage.removeItem("rec_result_count");
    showInitialMessage();
  } else {
    sessionId = sessionStorage.getItem(SESSION_ID_KEY) || null;
    const history = getChatHistory();
    if (history.length) {
      restoreChatHistory(history);
    } else {
      // 새 세션(채팅 히스토리 없음) → 이전 추천 결과 배지 클리어
      localStorage.removeItem("rec_result_count");
      showInitialMessage();
    }
  }
  // 페이지 진입 시 현재 배지 상태 동기화 (rec_result_count 가 0이면 배지 숨김)
  enableRecNav();
  inputEl.focus();

  // 페이지 떠났다가 돌아온 사이 백엔드에서 추천 진행 중이면 결과 polling
  resumeRecommendationIfNeeded();
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
async function sendMessage(text) {
  const userText = (text !== undefined ? text : inputEl.value).trim();
  if (!userText || isWaiting) return;

  inputEl.value = "";
  inputEl.style.height = "auto";
  clearQuickOptions();

  // ── 특수 명령: API 호출 없이 직접 처리 ──────────────────
  if (userText === "__START_REC__") {
    appendUserMessage("추천 시작하기 🏠");
    startRecommendation(sessionId);
    return;
  }
  if (userText === "__EDIT__") {
    appendUserMessage("수정하기 ✏️");
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
  const displayText = friendlyCommandLabel(userText);
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
      localStorage.setItem("rec_session_id", sessionId);
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

// ── 자동 추천 실행 ───────────────────────────────────────
async function startRecommendation(sid) {
  if (isRecommending) return;
  isRecommending = true;

  // 새 추천 시작 → 이전 결과 카운트·캐시·배지 클리어
  localStorage.removeItem("rec_result_count");
  sessionStorage.removeItem(REC_CACHE_KEY);
  enableRecNav();

  // 진행 상태 플래그 — 페이지 이동 후 돌아왔을 때 복원에 사용
  sessionStorage.setItem("rec_in_progress", "1");
  sessionStorage.setItem("rec_in_progress_session", sid);

  appendBotMessage(
    "추천을 시작할게요 🔍\n" +
    "공공데이터·외부 API 호출이 많아 약 4~7분 정도 걸려요.\n" +
    "이 화면을 닫거나 다른 메뉴로 이동해도 추천은 계속 진행되니 잠시만 기다려 주세요."
  );
  setWaiting(true);

  try {
    const res  = await fetch((window.API_BASE || "") + "/api/recommend", {
      method:  "POST",
      headers: getAuthHeaders(),
      body:    JSON.stringify({ session_id: sid }),
    });
    const data = await res.json();

    if (!res.ok || data.error) {
      const rawErr = data.error || "";
      if (rawErr.includes("kakao") || rawErr.includes("ConnectionPool") || rawErr.includes("getaddrinfo")) {
        appendBotMessage(
          "주소 검색 서비스 연결에 실패했어요.\n" +
          "네트워크를 확인하고 '주거 추천 결과 보기' 버튼으로 다시 시도해 주세요."
        );
      } else {
        appendBotMessage(
          "추천 중 오류가 발생했어요.\n" +
          "'주거 추천 결과 보기' 버튼을 눌러 다시 시도해 보세요."
        );
      }
      return;
    }

    const results = data.results || [];
    const count   = results.length;

    // 결과 sessionStorage 캐싱 (추천 페이지에서 재사용)
    // workplace 좌표 포함 — 매물 모달의 예상 생활비 계산에 필요
    sessionStorage.setItem(REC_CACHE_KEY, JSON.stringify({
      results:         data.results,
      seoul_avg:       data.seoul_avg || null,
      workplace_lat:   data.workplace_lat,
      workplace_lon:   data.workplace_lon,
      allowed_minutes: data.allowed_minutes,
      transport_mode:  data.transport_mode,
    }));
    localStorage.setItem("rec_result_count", String(count));
    enableRecNav();

    if (count > 0) {
      appendBotMessage(
        `추천이 완료됐어요! 🎉\n총 ${count}곳을 추천해드릴게요.\n아래 버튼을 눌러 결과를 확인해 보세요!`
      );
      // 추천 완료 후에야 버튼 활성화 (문제 5·6 fix)
      resultsBtnEl.classList.add("visible");
    } else {
      appendBotMessage(
        "조건에 맞는 매물이 없어요 😥\n조건을 조금 완화해서 다시 시도해 보세요."
      );
    }
  } catch (err) {
    appendBotMessage("추천 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.");
    console.error(err);
  } finally {
    setWaiting(false);
    isRecommending = false;
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
  }
}

// ── 페이지 진입 시 진행 중 추천 복원 ─────────────────────
// 챗봇 페이지를 떠났다가 돌아왔을 때 (또는 새로고침 후),
// 백엔드에서 추천이 계속 처리 중일 수 있음. GET 폴링으로 결과 가져옴.
async function resumeRecommendationIfNeeded() {
  if (sessionStorage.getItem("rec_in_progress") !== "1") return;
  const sid = sessionStorage.getItem("rec_in_progress_session") || sessionId;
  if (!sid) {
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
    return;
  }

  isRecommending = true;
  setWaiting(true);
  appendBotMessage(
    "추천이 진행 중이에요 🔍\n" +
    "전체 약 4~7분 정도 걸리며, 결과 도착 시 자동으로 안내됩니다.",
    true
  );

  const POLL_INTERVAL_MS = 5000;
  const MAX_DURATION_MS  = 15 * 60 * 1000;
  const startedAt = Date.now();

  try {
    while (true) {
      let data;
      try {
        const res = await fetch(
          (window.API_BASE || "") + "/api/recommend/result?session_id=" + encodeURIComponent(sid),
          { headers: getAuthHeaders() }
        );
        data = await res.json();
      } catch (e) {
        appendBotMessage("결과를 가져오는 중 연결 오류가 발생했어요.");
        return;
      }

      if (data.status === "complete") {
        const results = data.results || [];
        const count   = results.length;
        sessionStorage.setItem(REC_CACHE_KEY, JSON.stringify({
          results:         data.results,
          seoul_avg:       data.seoul_avg || null,
          workplace_lat:   data.workplace_lat,
          workplace_lon:   data.workplace_lon,
          allowed_minutes: data.allowed_minutes,
          transport_mode:  data.transport_mode,
        }));
        localStorage.setItem("rec_result_count", String(count));
        enableRecNav();
        if (count > 0) {
          appendBotMessage(`추천이 완료됐어요! 🎉\n총 ${count}곳을 추천해드릴게요.\n아래 버튼을 눌러 결과를 확인해 보세요!`);
          resultsBtnEl.classList.add("visible");
        } else {
          appendBotMessage("조건에 맞는 매물이 없어요 😥\n조건을 조금 완화해서 다시 시도해 보세요.");
        }
        return;
      }
      if (data.status === "error") {
        appendBotMessage("추천 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.");
        return;
      }
      if (data.status === "not_started") {
        // 백엔드 메모리에 봇 세션이 없음 (서버 재시작 등). 플래그 정리.
        return;
      }
      // running — 계속 polling
      if (Date.now() - startedAt > MAX_DURATION_MS) {
        appendBotMessage("추천이 평소보다 오래 걸리고 있어요. 잠시 후 새로고침해 주세요.");
        return;
      }
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }
  } finally {
    setWaiting(false);
    isRecommending = false;
    sessionStorage.removeItem("rec_in_progress");
    sessionStorage.removeItem("rec_in_progress_session");
  }
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
  // 세션스토리지 초기화
  sessionStorage.removeItem(CHAT_HISTORY_KEY);
  sessionStorage.removeItem(REC_CACHE_KEY);
  sessionStorage.removeItem(SESSION_ID_KEY);
  sessionStorage.removeItem("rec_in_progress");
  sessionStorage.removeItem("rec_in_progress_session");
  // localStorage 추천 관련 초기화
  localStorage.removeItem("rec_session_id");
  localStorage.removeItem("rec_result_count");
  localStorage.removeItem("rec_cond_chips");

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

// 검색 조건 칩 localStorage 저장 (추천 페이지 요약 바용)
function saveCondChips(slots) {
  if (!slots) return;
  const map = {};
  slots.forEach(s => { if (s.filled) map[s.key] = s.value; });
  const chips = [];
  if (map.work_address)  chips.push("📍 " + map.work_address);
  if (map.rent_type === "월세" && map.monthly_manwon) {
    chips.push("💰 월세 " + map.monthly_manwon + "만원");
  } else if (map.deposit_manwon) {
    chips.push("💰 전세 " + Number(map.deposit_manwon).toLocaleString() + "만원");
  }
  if (map.allowed_minutes) chips.push("🕐 통근 " + map.allowed_minutes + "분 이내");
  if (map.transport_mode)  chips.push("🚌 " + (map.transport_mode === "car" ? "자가용" : "대중교통"));
  if (map.vibe)            chips.push("🏘 " + map.vibe);
  if (map.use_youth_policy === true || map.use_youth_policy === "true") chips.push("📋 청년정책 반영");
  localStorage.setItem("rec_cond_chips", JSON.stringify(chips));
}

// 사이드바 추천 결과 배지 업데이트 (nav 자체는 항상 활성 상태)
function enableRecNav() {
  const navBadge = document.getElementById("nav-rec-badge");
  if (navBadge) {
    const count = parseInt(localStorage.getItem("rec_result_count") || "0", 10);
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
function appendBotMessage(text, skipSave = false) {
  removeTyping();
  if (!skipSave) saveChatMessage("bot", text);
  const group = document.createElement("div");
  group.className = "msg-group bot";
  group.innerHTML = `
    <div class="bubble bot">${nl2br(escHtml(text))}</div>
    <div class="msg-time">${nowTime()}</div>
  `;
  messagesEl.appendChild(group);
  scrollBottom();
}

function appendUserMessage(text, skipSave = false) {
  if (!skipSave) saveChatMessage("user", text);
  const group = document.createElement("div");
  group.className = "msg-group user";
  group.innerHTML = `
    <div class="bubble user">${nl2br(escHtml(text))}</div>
    <div class="msg-time">${nowTime()}</div>
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
  inputEl.disabled = v;
  if (v) showTyping();
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
    if (msg.role === "bot") appendBotMessage(msg.text, true);
    else                    appendUserMessage(msg.text, true);
  });
}

// ── 빠른 옵션 ────────────────────────────────────────────
// multiSelect 가 true 면 옵션 중 일반(__로 시작 안 함) 항목들이 체크박스 모드로 표시되고,
// 마지막에 "선택 완료" 버튼이 추가됨. 명령 옵션(__PREV__, __CANCEL_*__) 은 단일 클릭 유지.
function renderQuickOptions(options, multiSelect) {
  clearQuickOptions();
  if (!options || !options.length) return;

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
      const selected = Array.from(quickOptEl.querySelectorAll('.quick-btn-multi[data-selected="1"]'))
        .map(b => b.dataset.value);
      if (!selected.length) {
        appendBotMessage("1개 이상 선택해주세요.", true);
        return;
      }
      sendMessage(selected.join(" "));
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
    btn.onclick = () => sendMessage(opt.value);
    quickOptEl.appendChild(btn);
  });
}

function clearQuickOptions() {
  quickOptEl.innerHTML = "";
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
    .replace(/"/g, "&quot;");
}

function nl2br(s) {
  return s.replace(/\n/g, "<br>");
}

function nowTime() {
  const d = new Date();
  const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  const ampm = h < 12 ? "오전" : "오후";
  return `${ampm} ${h % 12 || 12}:${m}`;
}
