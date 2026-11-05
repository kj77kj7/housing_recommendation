/**
 * webapp/static/common.js
 * ───────────────────────
 * 로그인 후 페이지(챗봇·추천·마이페이지·찜) 공통 — config.js 다음, 페이지 스크립트보다 먼저 로드.
 *   - 로그아웃: 앱이 저장한 값을 모두 지움 (직장 주소가 담긴 검색 조건 포함)
 *   - 다른 탭에서 로그아웃하면 이 탭도 로그인 화면으로 이동
 *   - 모바일(≤768px): 사이드바를 여는 ☰ 메뉴 버튼
 */
"use strict";

// 앱이 localStorage 에 저장하는 키 (sessionStorage 는 탭 단위라 통째로 비움)
const APP_LOCAL_KEYS = [
  "token", "nickname", "user_id",
  "rec_session_id", "rec_result_count", "rec_cond_chips",
];

function clearAppStorage() {
  APP_LOCAL_KEYS.forEach(k => localStorage.removeItem(k));
  try { sessionStorage.clear(); } catch (e) { /* 무시 */ }
}

function handleLogout(e) {
  if (e) e.preventDefault();
  clearAppStorage();
  location.href = "/login";
}

// 다른 탭에서 로그아웃(토큰 삭제) → 이 탭의 대화·결과도 지우고 로그인 화면으로
window.addEventListener("storage", (e) => {
  if (e.key === "token" && !e.newValue) {
    try { sessionStorage.clear(); } catch (err) { /* 무시 */ }
    location.href = "/login";
  }
});

// ── 모바일 내비게이션 ────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  const sidebar = document.getElementById("sidebar");
  const header  = document.querySelector("#chat-header, #rec-page-header, .page-header");
  if (!sidebar || !header) return;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "mobile-nav-btn";
  btn.setAttribute("aria-label", "메뉴 열기");
  btn.textContent = "☰";
  header.insertBefore(btn, header.firstChild);

  const backdrop = document.createElement("div");
  backdrop.className = "mobile-nav-backdrop";
  document.body.appendChild(backdrop);

  const setOpen = (open) => {
    document.body.classList.toggle("nav-open", open);
    btn.setAttribute("aria-label", open ? "메뉴 닫기" : "메뉴 열기");
  };
  btn.addEventListener("click", () => setOpen(!document.body.classList.contains("nav-open")));
  backdrop.addEventListener("click", () => setOpen(false));
  sidebar.addEventListener("click", (e) => { if (e.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
});

// ── 토스트 알림 ──────────────────────────────────────────
// showToast("찜을 해제했어요", { actionLabel: "되돌리기", onAction: () => … })
function showToast(message, opts = {}) {
  let root = document.getElementById("toast-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "toast-root";
    root.setAttribute("aria-live", "polite");
    document.body.appendChild(root);
  }
  const el = document.createElement("div");
  el.className = "toast";
  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);
  let timer = null;
  const close = () => { clearTimeout(timer); el.classList.remove("toast--show"); setTimeout(() => el.remove(), 250); };
  if (opts.actionLabel && typeof opts.onAction === "function") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = opts.actionLabel;
    btn.addEventListener("click", () => { close(); opts.onAction(); });
    el.appendChild(btn);
  }
  root.appendChild(el);
  requestAnimationFrame(() => el.classList.add("toast--show"));
  timer = setTimeout(close, opts.duration || 4000);
}
