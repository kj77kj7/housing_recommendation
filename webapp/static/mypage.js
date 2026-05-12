"use strict";
/**
 * mypage.js
 * ─────────
 * 내 정보 페이지
 *   - 계정 정보 / 정책 정보 로드 (GET /user/{id}, GET /user/{id}/profile)
 *   - 추천 매물은 sessionStorage("rec_results_cache")에서 일시적으로 표시
 *   - 매물 카드 클릭 → property-modal.js 가 처리
 *   - "조건 다시 설정하기" → sessionStorage 비우고 챗봇으로 이동
 */

(function () {
  const token  = localStorage.getItem("token");
  const userId = parseInt(localStorage.getItem("user_id") || "0");
  if (!token || !userId) {
    location.href = "/login";
    return;
  }

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    await Promise.all([loadAccount(), loadProfile()]);
    loadRecResults();
    updateSidebar();

    document.getElementById("mp-rerun-btn")
      .addEventListener("click", onRerun);
    document.querySelectorAll(".mypage-card-edit, .mypage-hero-edit")
      .forEach(btn => btn.addEventListener("click", () => onEdit(btn.dataset.target)));
  }

  // ── 계정 정보 ───────────────────────────────────────
  async function loadAccount() {
    try {
      const res = await fetch(`/user/${userId}`, {
        headers: { "Authorization": "Bearer " + token },
      });
      if (!res.ok) return;
      const u = await res.json();

      document.getElementById("mp-nickname").textContent           = u.nickname || "사용자";
      document.getElementById("mp-account-nickname").textContent   = u.nickname || "–";
      document.getElementById("mp-account-email").textContent      = u.email    || "–";
      document.getElementById("mp-account-birth").textContent      = u.birth_date || "–";
      document.getElementById("mp-account-created").textContent    = formatDate(u.created_at);
      document.getElementById("mp-account-gender").textContent     =
        u.gender === "M" ? "남성" : (u.gender === "F" ? "여성" : "–");

      if (u.birth_date) {
        const age = calcAge(u.birth_date);
        const ageEl = document.getElementById("mp-account-age");
        ageEl.textContent = `만 ${age}세`;
        ageEl.style.display = "";
      }

      buildHeaderMeta(u);
    } catch (e) { console.error("계정 정보 로드 실패:", e); }
  }

  // ── 정책 정보 ───────────────────────────────────────
  async function loadProfile() {
    try {
      const res = await fetch(`/user/${userId}/profile`, {
        headers: { "Authorization": "Bearer " + token },
      });
      if (!res.ok) return;
      const p = await res.json();

      document.getElementById("mp-policy-employment").textContent = p.employment_status || "–";
      document.getElementById("mp-policy-education").textContent  = p.education_level   || "–";
      document.getElementById("mp-policy-marriage").textContent   = p.marriage_status   || "–";
      document.getElementById("mp-policy-own-house").textContent  =
        p.has_own_house === 1 ? "주택 소유" : (p.has_own_house === 0 ? "무주택" : "–");
      document.getElementById("mp-policy-income").textContent     = p.income_range      || "–";
    } catch (e) { console.error("정책 정보 로드 실패:", e); }
  }

  // ── 추천 매물 (sessionStorage — 일시적) ──────────────
  function loadRecResults() {
    const cached  = sessionStorage.getItem("rec_results_cache");
    const listEl  = document.getElementById("mp-rec-list");
    const countEl = document.getElementById("mp-rec-count");

    if (!cached) {
      listEl.innerHTML = `<div class="mp-rec-empty">아직 추천받은 매물이 없어요.<br>아래 '조건 다시 설정하기'로 챗봇에서 조건을 입력해 주세요.</div>`;
      countEl.textContent = "0건";
      return;
    }

    let results = [];
    let wpLat = null, wpLon = null;
    try {
      const p = JSON.parse(cached);
      results = p.results || [];
      wpLat   = p.workplace_lat ?? null;
      wpLon   = p.workplace_lon ?? null;
    } catch {}

    if (!results.length) {
      listEl.innerHTML = `<div class="mp-rec-empty">아직 추천받은 매물이 없어요.</div>`;
      countEl.textContent = "0건";
      return;
    }

    // 모달 외부 의존성 등록
    window.__modalResultsProvider = () => results;
    window._workplaceCoords = (wpLat != null && wpLon != null) ? { lat: wpLat, lon: wpLon } : null;

    listEl.innerHTML = results.map((r, i) => renderRecCard(r, i)).join("");
    countEl.textContent = `총 ${results.length}건`;
  }

  function renderRecCard(r, idx) {
    const colors = ["green", "blue", "olive"];
    const emojis = ["🏢", "🏠", "🏘️"];
    const color  = colors[idx % colors.length];
    const emoji  = emojis[idx % emojis.length];
    const score  = r.score != null ? r.score : 0;
    const policyTotal = r.applicable_policies_total != null
      ? r.applicable_policies_total
      : (Array.isArray(r.applicable_policies) ? r.applicable_policies.length : 0);

    return `
      <div class="mp-rec-card" onclick="window.openPropertyModal(${idx})">
        <div class="mp-rec-card-img mp-rec-card-img--${color}">
          <span class="mp-rec-card-emoji">${emoji}</span>
        </div>
        <div class="mp-rec-card-body">
          <div class="mp-rec-card-addr">${escHtml(r.address || (r.gu + " " + r.dong))}</div>
          <div class="mp-rec-card-meta">${escHtml(r.house_type || "")} · ${r.area_m2 || 0}㎡${r.rent_type ? " · " + escHtml(r.rent_type) : ""}</div>
          <div class="mp-rec-card-price">${formatPrice(r)}</div>
          <div class="mp-rec-card-chips">
            <span class="mp-rec-chip mp-rec-chip--score">⭐ ${score}점</span>
            ${r.commute_min != null ? `<span class="mp-rec-chip mp-rec-chip--commute">🚇 ${r.commute_min}분</span>` : ""}
            ${policyTotal > 0 ? `<span class="mp-rec-chip mp-rec-chip--policy">💚 청년정책 ${policyTotal}건</span>` : ""}
          </div>
        </div>
      </div>
    `;
  }

  // ── 헤더 메타 (만 N세 • 성별 | 직장 | 가입) ──────────
  function buildHeaderMeta(u) {
    const parts = [];
    if (u.birth_date) parts.push(`만 ${calcAge(u.birth_date)}세`);
    if (u.gender)     parts.push(u.gender === "M" ? "남성" : "여성");

    // 가장 최근 검색조건의 workplace
    fetch(`/user/${userId}/conditions`, {
      headers: { "Authorization": "Bearer " + token },
    })
      .then(r => r.ok ? r.json() : [])
      .then(conds => {
        const wp = (conds.length && conds[0].workplace) ? conds[0].workplace : null;
        const created = u.created_at ? formatDate(u.created_at, true) : null;
        const meta = [];
        if (parts.length) meta.push(parts.join(" • "));
        if (wp)      meta.push(`직장: ${wp}`);
        if (created) meta.push(`가입 ${created}`);
        document.getElementById("mp-meta").textContent = meta.join(" | ") || "–";

        // 사이드바 user-sub 도 직장 정보 반영
        if (wp) {
          const sub = document.getElementById("sidebar-usersub");
          if (sub) sub.textContent = `${wp.split(" ")[0]} 직장인`;
        }
      })
      .catch(() => {
        document.getElementById("mp-meta").textContent = parts.join(" • ") || "–";
      });
  }

  // ── 사이드바 (닉네임/배지) ──────────────────────────
  function updateSidebar() {
    const nick = localStorage.getItem("nickname");
    if (nick) {
      const el = document.getElementById("sidebar-username");
      if (el) el.textContent = nick + "님";
    }

    // 찜 개수 배지
    fetch(`/favorites/keys?user_id=${userId}`, {
      headers: { "Authorization": "Bearer " + token },
    })
      .then(r => r.ok ? r.json() : { keys: [] })
      .then(d => {
        const n = (d.keys || []).length;
        const badge = document.getElementById("sidebar-fav-badge");
        if (badge && n > 0) { badge.textContent = n; badge.style.display = ""; }
      })
      .catch(() => {});

    // 추천 결과 개수 배지
    const cached = sessionStorage.getItem("rec_results_cache");
    if (cached) {
      try {
        const p = JSON.parse(cached);
        const n = (p.results || []).length;
        const badge = document.getElementById("sidebar-rec-badge");
        if (badge && n > 0) { badge.textContent = n; badge.style.display = ""; }
      } catch {}
    }
  }

  // ── 재추천 ─────────────────────────────────────────
  function onRerun() {
    sessionStorage.removeItem("rec_results_cache");
    // 챗봇으로 이동 (재추천 플래그)
    location.href = "/?rerun=1";
  }

  // ── 편집 (간단 prompt 기반) ─────────────────────────
  async function onEdit(target) {
    if (!target || target === "profile" || target === "account") {
      // 계정 정보 편집 — 닉네임만 우선 지원
      const cur = localStorage.getItem("nickname") || "";
      const newNick = prompt("새 닉네임을 입력하세요", cur);
      if (newNick == null || newNick.trim() === "" || newNick === cur) return;
      try {
        const res = await fetch(`/user/${userId}`, {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
          },
          body: JSON.stringify({ nickname: newNick.trim() }),
        });
        if (!res.ok) throw new Error("수정 실패");
        localStorage.setItem("nickname", newNick.trim());
        location.reload();
      } catch (e) {
        alert("닉네임 변경에 실패했어요.");
      }
    } else if (target === "policy") {
      const employment = prompt("취업상태 (취업자/미취업자/자영업자)", "");
      if (employment == null) return;
      const education  = prompt("학력 (고졸이하/대학재학/대졸/석박사)", "");
      if (education == null) return;
      const income     = prompt("월소득 (예: 200~300만원)", "");
      if (income == null) return;

      const body = {};
      if (employment.trim()) body.employment_status = employment.trim();
      if (education.trim())  body.education_level   = education.trim();
      if (income.trim())     body.income_range      = income.trim();
      if (!Object.keys(body).length) return;

      try {
        const res = await fetch(`/user/${userId}/profile`, {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
          },
          body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error("저장 실패");
        location.reload();
      } catch (e) {
        alert("정책 정보 저장에 실패했어요.");
      }
    }
  }

  // ── 유틸 ───────────────────────────────────────────
  function calcAge(birthStr) {
    try {
      const b = new Date(birthStr);
      const t = new Date();
      let age = t.getFullYear() - b.getFullYear();
      const m = t.getMonth() - b.getMonth();
      if (m < 0 || (m === 0 && t.getDate() < b.getDate())) age--;
      return age;
    } catch { return 0; }
  }
  function formatDate(s, compact) {
    if (!s) return "–";
    try {
      const d = new Date(s);
      const y = d.getFullYear();
      const m = d.getMonth() + 1;
      const day = d.getDate();
      return compact ? `${y}.${String(m).padStart(2, "0")}` : `${y}년 ${m}월 ${day}일`;
    } catch { return s; }
  }
  function formatPrice(r) {
    if (r.rent_type === "월세" && r.monthly_rent_manwon) {
      const won = Number(r.monthly_rent_manwon) * 10000;
      return `보증금 ${formatManwon(r.deposit_manwon)} / 월세 ${won.toLocaleString()}원`;
    }
    if (r.rent_type === "전세") return `전세 ${formatManwon(r.deposit_manwon)}`;
    return formatManwon(r.deposit_manwon || r.price_manwon);
  }
  function formatManwon(manwon) {
    if (!manwon) return "–";
    const n = Math.round(Number(manwon));
    if (n >= 10000) {
      const ok  = Math.floor(n / 10000);
      const rem = n % 10000;
      return rem > 0 ? `${ok}억 ${rem.toLocaleString()}만원` : `${ok}억원`;
    }
    return `${n.toLocaleString()}만원`;
  }
  function escHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();
