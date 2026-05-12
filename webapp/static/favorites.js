"use strict";
/**
 * favorites.js
 * ────────────
 * 찜 목록 페이지
 *   - GET /favorites?user_id=X       → 카드 리스트 렌더
 *   - 카드 클릭                       → property-modal.js 모달
 *   - 채워진 ❤️ 클릭 (토글)           → POST /favorites/toggle → 빈 하트 + 카드 제거
 */

(function () {
  const token  = localStorage.getItem("token");
  const userId = parseInt(localStorage.getItem("user_id") || "0");
  if (!token || !userId) {
    location.href = "/login";
    return;
  }

  let _items = [];   // 매물 모달이 idx로 조회

  // 찜목록의 모달에서는 "N위 추천" 머리표 숨김 (찜 정렬 순서일 뿐 추천 순위가 아니므로)
  window.__modalShowRank = false;

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    const nick = localStorage.getItem("nickname");
    if (nick) {
      const el = document.getElementById("sidebar-username");
      if (el) el.textContent = nick + "님";
    }
    // 추천 결과 배지 (있으면 표시)
    const cached = sessionStorage.getItem("rec_results_cache");
    if (cached) {
      try {
        const p = JSON.parse(cached);
        const n = (p.results || []).length;
        const badge = document.getElementById("sidebar-rec-badge");
        if (badge && n > 0) { badge.textContent = n; badge.style.display = ""; }
        // 모달 expense 계산용 직장 좌표 (찜 목록에서는 추천 세션 정보가 있을 때만 사용 가능)
        window._workplaceCoords = (p.workplace_lat != null && p.workplace_lon != null)
          ? { lat: p.workplace_lat, lon: p.workplace_lon }
          : null;
      } catch {}
    }
    await loadFavorites();
  }

  // ── 목록 로드 ───────────────────────────────────────
  async function loadFavorites() {
    const listEl = document.getElementById("fav-list");
    try {
      const res = await fetch((window.API_BASE || "") + `/favorites?user_id=${userId}`, {
        headers: { "Authorization": "Bearer " + token },
      });
      if (!res.ok) throw new Error();
      const raw = await res.json();

      _items = raw.map(f => {
        const snap = f.snapshot || {};
        return {
          fav_id:               f.id,
          property_key:         f.property_key,
          address:              f.address,
          gu:                   f.gu,
          dong:                 f.dong,
          house_type:           f.house_type,
          rent_type:            f.rent_type,
          area_m2:              f.area_m2,
          deposit_manwon:       f.deposit_manwon,
          monthly_rent_manwon:  f.monthly_rent_manwon,
          commute_min:          f.commute_min,
          score:                f.score,
          lat:                  f.lat,
          lon:                  f.lon,
          // snapshot 에 있는 청년정책·세부 점수 그대로
          applicable_policies:        snap.applicable_policies        || [],
          applicable_policies_total:  snap.applicable_policies_total  || 0,
          commute_score:        snap.commute_score,
          cost_score:           snap.cost_score,
          infra_score:          snap.infra_score,
          // 찜 시점에 저장한 직장 좌표 — 모달 생활비 영수증 계산용
          workplace_lat:        snap.workplace_lat,
          workplace_lon:        snap.workplace_lon,
        };
      });

      // 모달 외부 의존성 등록
      window.__modalResultsProvider = () => _items;

      // 모달 열릴 때 매물별 workplace 좌표를 window._workplaceCoords 에 주입.
      // (sessionStorage 캐시가 비어 있어도 작동하도록 각 찜 매물의 snapshot 우선 사용)
      const _origOpenModal = window.openPropertyModal;
      window.openPropertyModal = function(idx) {
        const item = _items[idx];
        if (item && item.workplace_lat != null && item.workplace_lon != null) {
          window._workplaceCoords = { lat: item.workplace_lat, lon: item.workplace_lon };
        }
        if (typeof _origOpenModal === "function") _origOpenModal(idx);
      };

      renderList();
      updateBadge();
    } catch (e) {
      console.error("찜 목록 로드 실패:", e);
      listEl.innerHTML = `<div class="fav-empty">찜 목록을 불러오지 못했어요.</div>`;
    }
  }

  function renderList() {
    const el = document.getElementById("fav-list");
    if (!_items.length) {
      el.innerHTML = `<div class="fav-empty">찜한 매물이 없어요.<br>추천 결과에서 🤍를 눌러 매물을 찜해보세요.</div>`;
      return;
    }
    el.innerHTML = _items.map((f, i) => renderCard(f, i)).join("");
  }

  function renderCard(f, idx) {
    const colors = ["green", "blue", "olive"];
    const emojis = ["🏢", "🏠", "🏘️"];
    const color  = colors[idx % colors.length];
    const emoji  = emojis[idx % emojis.length];
    const score  = f.score || 0;
    const policyTotal = f.applicable_policies_total || (f.applicable_policies?.length || 0);

    return `
      <div class="fav-card" data-idx="${idx}" onclick="window.openPropertyModal(${idx})">
        <div class="fav-card-img fav-card-img--${color}">
          <span class="fav-card-emoji">${emoji}</span>
        </div>
        <div class="fav-card-body">
          <div class="fav-card-addr">${escHtml(f.address || (f.gu + " " + f.dong))}</div>
          <div class="fav-card-meta">${escHtml(f.house_type || "")} · ${f.area_m2 || 0}㎡${f.rent_type ? " · " + escHtml(f.rent_type) : ""}</div>
          <div class="fav-card-price">${formatPrice(f)}</div>
          <div class="fav-card-chips">
            <span class="fav-chip fav-chip--score">⭐ ${score}점</span>
            ${f.commute_min != null ? `<span class="fav-chip fav-chip--commute">🚇 ${f.commute_min}분</span>` : ""}
            ${policyTotal > 0 ? `<span class="fav-chip fav-chip--policy">💚 청년정책 ${policyTotal}건</span>` : ""}
          </div>
        </div>
        <button class="fav-heart" data-fav-id="${f.fav_id}" data-idx="${idx}"
                onclick="event.stopPropagation(); window.toggleHeart(${idx}, this);"
                aria-label="찜 토글">❤️</button>
      </div>
    `;
  }

  // ── 채워진 ❤️ 다시 클릭 → DB 토글 → 빈하트 + 카드 제거 ──
  async function toggleHeart(idx, btn) {
    const f = _items[idx];
    if (!f) return;

    // 낙관적 UI: 일단 빈하트로 바꿈
    btn.textContent = "🤍";
    btn.disabled = true;

    try {
      const res = await fetch((window.API_BASE || "") + `/favorites/toggle?user_id=${userId}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token,
        },
        body: JSON.stringify({
          address:             f.address || "",
          gu:                  f.gu || "",
          dong:                f.dong || "",
          house_type:          f.house_type || "",
          rent_type:           f.rent_type || "",
          area_m2:             Number(f.area_m2 || 0),
          deposit_manwon:      f.deposit_manwon || 0,
          monthly_rent_manwon: f.monthly_rent_manwon || 0,
          commute_min:         f.commute_min || 0,
          score:               f.score || 0,
          lat:                 f.lat || 0,
          lon:                 f.lon || 0,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "토글 실패");

      // 목록에서 제거 (removed 든 added 든 새로 fetch 가 정답이지만, 비용 절감 위해 클라이언트 갱신)
      _items.splice(idx, 1);
      window.__modalResultsProvider = () => _items;
      renderList();
      updateBadge();
    } catch (e) {
      console.error(e);
      btn.textContent = "❤️";
      btn.disabled = false;
      alert("삭제에 실패했어요.");
    }
  }
  window.toggleHeart = toggleHeart;

  function updateBadge() {
    const badge = document.getElementById("sidebar-fav-badge");
    if (!badge) return;
    if (_items.length) { badge.textContent = _items.length; badge.style.display = ""; }
    else { badge.style.display = "none"; }
  }

  // ── 유틸 ───────────────────────────────────────────
  function formatPrice(f) {
    if (f.rent_type === "월세" && f.monthly_rent_manwon) {
      const won = Number(f.monthly_rent_manwon) * 10000;
      return `보증금 ${formatManwon(f.deposit_manwon)} / 월세 ${won.toLocaleString()}원`;
    }
    if (f.rent_type === "전세") return `전세 ${formatManwon(f.deposit_manwon)}`;
    return formatManwon(f.deposit_manwon);
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
