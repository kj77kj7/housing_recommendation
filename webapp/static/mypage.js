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
      const res = await fetch((window.API_BASE||"") + `/user/${userId}`, {
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
  let _profile = null;
  async function loadProfile() {
    try {
      const res = await fetch((window.API_BASE||"") + `/user/${userId}/profile`, {
        headers: { "Authorization": "Bearer " + token },
      });
      if (!res.ok) return;
      const p = await res.json();
      _profile = p;   // 편집 모달에서 현재 값을 미리 선택하는 데 사용

      document.getElementById("mp-policy-employment").textContent = p.employment_status || "–";
      document.getElementById("mp-policy-education").textContent  = p.education_level   || "–";
      document.getElementById("mp-policy-own-house").textContent  =
        p.has_own_house === 1 ? "주택 소유" : (p.has_own_house === 0 ? "무주택" : "–");
      document.getElementById("mp-policy-income").textContent     = p.income_range      || "–";
    } catch (e) { console.error("정책 정보 로드 실패:", e); }
  }

  // ── 추천 매물 (이 탭 캐시 → 없으면 서버의 최근 추천) ──────
  async function loadRecResults() {
    let cached    = sessionStorage.getItem("rec_results_cache");
    const listEl  = document.getElementById("mp-rec-list");
    const countEl = document.getElementById("mp-rec-count");

    // 이 탭에 결과가 없으면(새 탭·다른 기기 등) 서버에 저장된 최근 추천 결과 사용
    if (!cached) {
      try {
        const res = await fetch((window.API_BASE || "") + "/api/recommend/latest",
                                { headers: { "Authorization": "Bearer " + token } });
        const d = res.ok ? await res.json() : null;
        if (d && d.status === "complete") cached = JSON.stringify(d);
      } catch { /* 무시 */ }
    }

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
            ${r.commute_min != null ? `<span class="mp-rec-chip mp-rec-chip--commute">${r.transport_mode === "car" ? "🚗" : "🚇"} ${r.commute_min}분</span>` : ""}
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
    fetch((window.API_BASE||"") + `/user/${userId}/conditions`, {
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
    fetch((window.API_BASE||"") + `/favorites/keys?user_id=${userId}`, {
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

  // ── 편집 (모달 기반) ────────────────────────────────
  async function onEdit(target) {
    if (!target || target === "profile" || target === "account") {
      openAccountEditModal();
    } else if (target === "policy") {
      openPolicyEditModal();
    }
  }

  // ── 계정 정보 편집 모달 ─────────────────────────────
  function openAccountEditModal() {
    const overlay = document.getElementById("account-edit-overlay");
    if (!overlay) return;

    // 현재 값으로 폼 채우기
    const nick  = document.getElementById("mp-account-nickname").textContent.trim();
    const email = document.getElementById("mp-account-email").textContent.trim();
    const birth = document.getElementById("mp-account-birth").textContent.trim();
    const gender = document.getElementById("mp-account-gender").textContent.trim();

    document.getElementById("account-edit-nickname").value = (nick === "–") ? "" : nick;
    document.getElementById("account-edit-email").value    = (email === "–") ? "" : email;
    document.getElementById("account-edit-birth").value    = (/^\d{4}-\d{2}-\d{2}$/.test(birth)) ? birth : "";

    overlay.querySelectorAll('.policy-edit-options[data-field="gender"] .policy-edit-opt')
      .forEach(b => b.classList.remove("selected"));
    if (gender === "남성") {
      const m = overlay.querySelector('.policy-edit-options[data-field="gender"] [data-value="M"]');
      if (m) m.classList.add("selected");
    } else if (gender === "여성") {
      const f = overlay.querySelector('.policy-edit-options[data-field="gender"] [data-value="F"]');
      if (f) f.classList.add("selected");
    }

    // 단일 선택 + 닫기/취소/저장 이벤트 (1회 바인딩)
    if (!overlay.dataset.bound) {
      overlay.dataset.bound = "1";
      overlay.querySelectorAll(".policy-edit-options").forEach(group => {
        group.addEventListener("click", (e) => {
          const btn = e.target.closest(".policy-edit-opt");
          if (!btn) return;
          group.querySelectorAll(".policy-edit-opt").forEach(o => o.classList.remove("selected"));
          btn.classList.add("selected");
        });
      });
      document.getElementById("account-edit-close").addEventListener("click", closeAccountEditModal);
      document.getElementById("account-edit-cancel").addEventListener("click", closeAccountEditModal);
      document.getElementById("account-edit-save").addEventListener("click", saveAccountEditModal);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) closeAccountEditModal(); });
    }

    overlay.style.display = "flex";
    overlay.setAttribute("aria-hidden", "false");
  }

  function closeAccountEditModal() {
    const overlay = document.getElementById("account-edit-overlay");
    if (document.activeElement && overlay.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    overlay.style.display = "none";
    overlay.setAttribute("aria-hidden", "true");
  }

  async function saveAccountEditModal() {
    const overlay = document.getElementById("account-edit-overlay");
    const body = {};

    // 현재 값과 비교해서 실제 변경된 항목만 PATCH (사용자가 안 바꾼 필드는 보내지 않음)
    const curNick   = document.getElementById("mp-account-nickname").textContent.trim();
    const curEmail  = document.getElementById("mp-account-email").textContent.trim();
    const curBirth  = document.getElementById("mp-account-birth").textContent.trim();
    const curGender = document.getElementById("mp-account-gender").textContent.trim();

    const newNick  = document.getElementById("account-edit-nickname").value.trim();
    const newEmail = document.getElementById("account-edit-email").value.trim();
    const newBirth = document.getElementById("account-edit-birth").value.trim();
    const genderSel = overlay.querySelector('.policy-edit-options[data-field="gender"] .policy-edit-opt.selected');
    const newGender = genderSel ? genderSel.dataset.value : "";

    if (newNick  && newNick  !== curNick)                      body.nickname   = newNick;
    if (newEmail && newEmail !== curEmail)                     body.email      = newEmail;
    if (newBirth && newBirth !== curBirth)                     body.birth_date = newBirth;
    // 성별: 변경 안 된 경우(현재와 동일 표시) 또는 미선택 시 보내지 않음
    const curGenderCode = curGender === "남성" ? "M" : (curGender === "여성" ? "F" : "");
    if (newGender && newGender !== curGenderCode)              body.gender     = newGender;

    if (!Object.keys(body).length) {
      alert("변경된 항목이 없어요.");
      return;
    }

    // 클라이언트 측 기본 검증
    if (body.email && !body.email.includes("@")) {
      alert("올바른 이메일 형식이 아닙니다.");
      return;
    }

    const saveBtn = document.getElementById("account-edit-save");
    saveBtn.disabled = true;
    try {
      const res = await fetch((window.API_BASE||"") + `/user/${userId}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token,
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "수정 실패");
      }
      // 닉네임 변경 시 localStorage 도 갱신
      if (body.nickname) localStorage.setItem("nickname", body.nickname);
      closeAccountEditModal();
      location.reload();
    } catch (e) {
      alert("계정 정보 저장에 실패했어요: " + (e.message || ""));
    } finally {
      saveBtn.disabled = false;
    }
  }

  // ── 정책 정보 편집 모달 ─────────────────────────────
  function openPolicyEditModal() {
    const overlay = document.getElementById("policy-edit-overlay");
    if (!overlay) return;

    // 현재 저장된 값을 미리 선택 (없으면 "변경 안 함")
    // — 예전엔 항상 "변경 안 함"만 선택돼 지금 값이 무엇인지 모달에서 알 수 없었음
    overlay.querySelectorAll(".policy-edit-options").forEach(group => {
      const optsInGroup = group.querySelectorAll(".policy-edit-opt");
      optsInGroup.forEach(o => o.classList.remove("selected"));
      const cur = _profile ? _profile[group.dataset.field] : null;
      const curBtn = (cur !== null && cur !== undefined && cur !== "")
        ? group.querySelector(`[data-value="${String(cur)}"]`) : null;
      (curBtn || group.querySelector('[data-value=""]'))?.classList.add("selected");
    });

    // 클릭 시 그룹 내 단일 선택 (이벤트 위임 — 한 번만 바인딩)
    if (!overlay.dataset.bound) {
      overlay.dataset.bound = "1";
      overlay.querySelectorAll(".policy-edit-options").forEach(group => {
        group.addEventListener("click", (e) => {
          const btn = e.target.closest(".policy-edit-opt");
          if (!btn) return;
          group.querySelectorAll(".policy-edit-opt").forEach(o => o.classList.remove("selected"));
          btn.classList.add("selected");
        });
      });
      document.getElementById("policy-edit-close").addEventListener("click", closePolicyEditModal);
      document.getElementById("policy-edit-cancel").addEventListener("click", closePolicyEditModal);
      document.getElementById("policy-edit-save").addEventListener("click", savePolicyEditModal);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) closePolicyEditModal(); });
    }

    overlay.style.display = "flex";
    overlay.setAttribute("aria-hidden", "false");
  }

  function closePolicyEditModal() {
    const overlay = document.getElementById("policy-edit-overlay");
    if (document.activeElement && overlay.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    overlay.style.display = "none";
    overlay.setAttribute("aria-hidden", "true");
  }

  async function savePolicyEditModal() {
    const overlay = document.getElementById("policy-edit-overlay");
    const body = {};
    overlay.querySelectorAll(".policy-edit-options").forEach(group => {
      const field = group.dataset.field;
      const selected = group.querySelector(".policy-edit-opt.selected");
      if (!selected) return;
      const val = selected.dataset.value;
      if (val === "" || val == null) return;  // "변경 안 함" → 스킵
      const cur = _profile ? _profile[field] : null;
      if (cur !== null && cur !== undefined && String(cur) === val) return;  // 현재 값 그대로 → 스킵
      if (field === "has_own_house") {
        body[field] = parseInt(val, 10);
      } else {
        body[field] = val;
      }
    });

    if (!Object.keys(body).length) {
      alert("변경할 항목을 1개 이상 선택해주세요.");
      return;
    }

    const saveBtn = document.getElementById("policy-edit-save");
    saveBtn.disabled = true;
    try {
      const res = await fetch((window.API_BASE||"") + `/user/${userId}/profile`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token,
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error("저장 실패");
      closePolicyEditModal();
      location.reload();
    } catch (e) {
      alert("정책 정보 저장에 실패했어요.");
    } finally {
      saveBtn.disabled = false;
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
      return `보증금 ${formatManwon(r.deposit_manwon)} / 월세 ${Number(r.monthly_rent_manwon).toLocaleString()}만원`;
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
