# 버그 수정 대기 목록

> 작성일: 2026-05-13
> 브랜치: `claude/cool-archimedes-e871e3`
> 상태: 더 심각한 다른 이슈 수정 후 돌아와서 처리 예정

전체 11개 분석 (중복 1개 = 실질 10개). 우선순위·작업 순서는 각 항목 마지막 표 참고.

---

## 🔴 Bug 1 — 새로고침 시 추천 무한 로딩

**증상**: 추천 진행 중(6분+) 페이지 새로고침하면 추천 결과 못 받고 로딩 계속됨.

**원인**: `/api/recommend` 가 idempotent하지 않음.
- 새로고침 → `recommendation.js` 재실행 → `sessionStorage.rec_results_cache` 없음 → `/api/recommend` 다시 호출
- 백엔드는 첫 요청 계속 처리 중 + 새 요청도 받음 → **처음부터 6분+ 다시 실행**
- 진행 중 작업을 세션별로 캐싱·재사용하는 로직 없음

**해결안**:
- 백엔드 `_get_bot()` 으로 가져온 `bot` 인스턴스에 `_recommend_in_progress: bool` + `_recommend_future` 필드 추가
- `/api/recommend` 진입 시:
  - 이미 진행 중이면 → 동일 future await → 같은 결과 반환
  - 완료된 결과 있으면 → 즉시 반환
- 또는 더 간단하게: 결과를 `bot._last_recommend_result` 에 캐싱 후 30분 만료

**위치**: `webapp/main.py /api/recommend`, `webapp/main.py _get_bot`

---

## 🔴 Bug 2 — 지도뷰 안 열림

**증상**: 사이드바의 "지도뷰" 클릭 시 URL은 `?view=map` 으로 바뀌나 카드뷰만 보임.

**원인**: `recommendation.js` DOMContentLoaded 에서 `?view=map` URL 파라미터를 읽어 `switchView("map")` 호출하는 로직이 **없음**.
- 클릭 핸들러는 `location.pathname === "/recommendation"` 일 때만 SPA-like로 동작
- 챗봇 페이지 등 다른 페이지에서 클릭 시: 정상 navigate → recommendation.js 로드 → 기본값 `_currentView = "card"` 로만 시작 → 지도 안 보임

**해결안**: `recommendation.js` DOMContentLoaded에서 `new URLSearchParams(location.search).get("view") === "map"` 일 때 렌더링 완료 후 `switchView("map")` 호출.

```javascript
// renderPage() 끝부분 또는 DOMContentLoaded 마지막
const initView = new URLSearchParams(location.search).get("view");
if (initView === "map") switchView("map");
```

**위치**: `webapp/static/recommendation.js` DOMContentLoaded 핸들러 및 renderPage 함수

---

## 🔴 Bug 3 — 정책정보 편집 시 주택소유 자동 "무주택" 박힘

**증상**: 마이페이지에서 취업상태·학력·월소득만 수정 → 저장 후 주택소유가 "무주택"으로 자동 설정됨.

**원인**: MySQL 스키마 DEFAULT 값.

`webapp/database.py` 라인 160:
```sql
CREATE TABLE IF NOT EXISTS user_profile (
    ...
    has_own_house TINYINT DEFAULT 0,
    ...
)
```

`user_profile` 행이 없는 사용자가 마이페이지에서 다른 필드만 수정하면 → `PATCH /user/{id}/profile` 이 INSERT 수행 → `has_own_house` 컬럼 미지정 → MySQL이 DEFAULT 값 0 (= 무주택) 자동 입력.

**해결안 (택 1)**:
- (A) 스키마 변경: `has_own_house TINYINT DEFAULT NULL` 로 ALTER. 기존 행 0인 것들은 그대로 둠 (사용자가 마이페이지에서 다시 채울 때까지)
- (B) 코드 측: `webapp/user.py update_profile` INSERT 시 `has_own_house` 미지정이면 명시적으로 NULL 삽입

(A)가 더 깨끗함. 마이그레이션 1줄 추가.

**위치**: `webapp/database.py` line 160, `webapp/user.py update_profile`

---

## 🟡 Bug 4 — 추천 개수 0개여도 초록 배지 표시

**증상**: 추천 결과 페이지에서 사이드바 "주거 추천" 메뉴에 빈 초록색 pill이 표시됨.

**원인 (2곳)**:
1. `webapp/static/recommendation.html` 라인 31: `<span class="nav-badge" id="rec-count-badge"></span>` — 초기 `style="display:none"` 누락 → JS 실행 전 빈 pill 보임
2. `webapp/static/recommendation.js` 라인 329-331: `results.length` 가 0이어도 `navBadgeEl.style.display = ""` 로 무조건 노출

**해결안**:
- HTML: `style="display:none"` 추가
- JS: `if (results.length > 0) { navBadgeEl.style.display = ""; } else { navBadgeEl.style.display = "none"; }`

**위치**: `webapp/static/recommendation.html` line 31, `webapp/static/recommendation.js` renderPage

---

## 🟡 Bug 5 — 수정하기 메뉴에 옵션 부족 (월세·정책 버튼 누락)

**증상**: 챗봇 → 다른 메뉴 → 다시 챗봇 → 수정하기 클릭 시 월세·정책 슬롯 옵션이 빠짐.

**원인**: `latestSlotStatus` 가 페이지 재진입 시 초기화됨 (`null`).

`webapp/static/app.js`:
- `let latestSlotStatus = null` (초기값)
- 채팅 응답에서 `latestSlotStatus = data.slot_status` 로 갱신
- 페이지 navigate 후 다시 진입하면 모듈 재실행 → null로 리셋
- `showEditOptions()` 는 `latestSlotStatus.rent_type` / `use_youth_policy` 를 보고 월세·정책 옵션 조건부 표시 → null이면 표시 안 함

**해결안 (Bug 7과 함께)**:
- 채팅 응답마다 `sessionStorage` 에 마지막 `slot_status` 저장
- 페이지 진입 시 sessionStorage에서 복원 → `latestSlotStatus` 채움
- 또는 페이지 진입 시 `/api/chat` 에 빈 메시지로 핑 보내서 현재 상태 받기

**위치**: `webapp/static/app.js`

---

## 🔴 Bug 6 / Bug 3-신규 (중복) — `_quick_options_for()` 정책 분기 fallthrough 누락

**증상**: 청년정책=예 사용자가 일반 슬롯(주택 유형, 우선순위, 동네 분위기, 이동수단, 임대 유형 등) 수정 시 아래 빠른 버튼이 0개로 표시. 사용자가 텍스트로 직접 답해야 함.

**원인**: `webapp/main.py` `_quick_options_for()` 라인 523-553:

```python
if bot.slots.get("use_youth_policy") and bot._last_asked_slot:
    asked = bot._last_asked_slot
    if asked == "policy_employment" ...
    if asked == "policy_education" ...
    if asked == "policy_income" ...
    if asked == "policy_no_house" ...
    return []   # ← 정책 슬롯 4개에 매치 안 되면 빈 배열로 빠짐
```

청년정책=예 + asked가 정책 슬롯이 아닌 경우 (예: house_type, weight_preference) → 4개 if 모두 false → 최종 `return []` 으로 빠짐. 일반 슬롯 분기로 fallthrough 못 함.

**해결안**: 정책 슬롯 매치된 경우에만 return, 아니면 일반 슬롯 분기로 진입.

```python
if bot.slots.get("use_youth_policy") and bot._last_asked_slot:
    asked = bot._last_asked_slot
    if asked == "policy_employment" and ...:
        return [...]
    if asked == "policy_education" and ...:
        return [...]
    if asked == "policy_income" and ...:
        return [...]
    if asked == "policy_no_house" and ...:
        return [...]
    # 정책 슬롯이 아니면 fallthrough — 아래 일반 분기로
    # (return [] 제거)

# ── 일반 슬롯 옵션 수집 ───────────────────────
options: list = []
if slot == "transport_mode":
    ...
```

**위치**: `webapp/main.py` line 523-553

---

## 🟡 Bug 7 — 다른 메뉴 다녀온 후 챗봇에 빠른 버튼 안 뜸

**증상**: 챗봇 사용 중 → 사이드바로 다른 페이지(찜 목록/마이페이지/주거추천) → 다시 챗봇 진입 → 입력 박스만 있고 빠른 선택 버튼 없음.

**원인**: 채팅 UI 상태(`quick_options`)가 sessionStorage에 저장 안 됨.

`webapp/static/app.js`:
- 채팅 히스토리는 `sessionStorage.chat_history` 에서 복원 ✓
- `quick_options` / `latestSlotStatus` 복원 로직 없음
- 서버에 현재 봇 상태를 다시 묻는 로직도 없음

**해결안 (Bug 5와 통합)**:
- 채팅 응답마다 `sessionStorage` 에 `last_quick_options`, `last_slot_status` 저장
- 페이지 진입 시 복원
- 또는 신규 엔드포인트 `GET /api/chat/state?session_id=...` 만들어서 현재 상태 조회 (slot_status + 마지막 quick_options + is_complete)

**위치**: `webapp/static/app.js`, 선택적으로 `webapp/main.py` 에 상태 조회 엔드포인트

---

## 🔴 Bug 8 (4번 신규) — 직접설정 → 비율 질문 누락 (★ LLM 프롬프트 결함)

**증상**: weight_preference에서 "④ 직접 설정" 선택 시 → 비율(weight_custom) 질문 안 하고 곧장 동네 분위기로 넘어감.

**원인**: `prompts/slot_extraction.txt` 의 LLM 프롬프트 enum 불완전.

라인 20:
```
weight_preference: "통근우선" / "주거비우선" / "균형" / "위임"
```

"직접설정"이 enum에 **없음**. 사용자가 "④ 직접 설정" 입력하면:
- Gemini가 다른 값(예: "균형")으로 매핑하거나 null 반환
- LLM이 다른 값으로 채우면 → 현재 가드는 `_last_asked_slot == "weight_preference"` 이므로 통과 → `slots["weight_preference"] = "균형"` 으로 잘못 세팅
- 번호 폴백(`_parse_weight_choice` → "직접설정")은 `new_slots.get("weight_preference") is None` 조건으로만 동작 → LLM이 이미 잘못된 값 채웠으면 override 안 됨
- 결과: `_next_question` 의 `weight_preference == "직접설정"` 체크 false → weight_custom 건너뜀

**해결안 (2단계)**:
1. `prompts/slot_extraction.txt` 라인 20에 "직접설정" 추가:
   ```
   weight_preference: "통근우선" / "주거비우선" / "균형" / "직접설정" / "위임"
   ```
2. 더 안전하게 — `nlp_input_module.py process()` 에서 파서 결과가 LLM 결과를 **무조건 override** 하도록 변경 (이미 `monthly_manwon` 에 적용한 방식 확장):
   ```python
   if self._last_asked_slot == "weight_preference":
       parsed = self._parse_weight_choice(user_text)
       if parsed:
           new_slots["weight_preference"] = parsed   # ← 무조건 override
   ```
   동일 패턴을 `house_type`, `vibe`, `use_youth_policy` 까지 확장

**위치**: `prompts/slot_extraction.txt` line 20, `nlp_input_module.py process()` 번호 폴백 부분

---

## 🟡 Bug 9 (4번 신규) — 채팅 시간이 페이지 이동 후 현재시간으로 바뀜

**증상**: 채팅을 친 시간이 표시되어 있다가 다른 페이지 다녀오면 모든 메시지의 시간이 현재시간으로 통일됨.

**원인**: `restoreChatHistory()` 가 저장된 timestamp를 무시.

`webapp/static/app.js`:
- `saveChatMessage()`: `history.push({ role, text, time: nowTime() })` — time이 sessionStorage에 저장됨 ✓
- `restoreChatHistory()`: 단순히 `appendBotMessage(msg.text, true)` 만 호출
- `appendBotMessage` / `appendUserMessage`: 항상 `<div class="msg-time">${nowTime()}</div>` — 호출 시점 현재 시간으로 렌더링

저장된 시간을 무시함.

**해결안**:
- `appendBotMessage(text, skipSave, time)` / `appendUserMessage(text, skipSave, time)` 시그니처에 optional `time` 파라미터 추가
- `restoreChatHistory` 에서 `msg.time` 을 전달
- 신규 메시지일 때만 `nowTime()` 호출 (`time = time || nowTime()`)

**위치**: `webapp/static/app.js` line 376-395 (appendBot/UserMessage), line 437-441 (restoreChatHistory)

---

## 🟢 Bug 10 (1번 신규) — 수정 시 다중 선택 (UX 개선)

**증상**: 수정하기에서 한 번에 한 슬롯만 수정 가능. 여러 항목 동시 수정 시 매번 메뉴 들어갔다 나와야 함.

**원인**: 설계 한계. `__EDIT:slot__` 명령이 단일 슬롯만 처리.

**해결안 (UI 재설계)**:
1. `showEditOptions()` 를 체크박스 UI로 재설계
   - 각 항목 옆에 현재 값 표시 (예: "주택 유형: 오피스텔")
   - 다중 체크 가능
   - 하단에 "수정 시작 →" / "취소 ↩" 버튼
2. 새 명령어 `__EDIT_MULTI:slot1,slot2,...__` 도입
3. 서버 `_edit_slot_multi(slots)` 신설:
   - 모든 지정 슬롯 pop
   - 첫 번째 슬롯에 대해 `_last_asked_slot` 설정 + 질문 반환
4. 사용자가 답할 때마다 정상 흐름으로 다음 누락 슬롯 자동 질문 (기존 `_next_question` 로직 재활용)

UI 규모는 quick options 영역 안쪽이라 중간 규모. 별도 커밋 권장.

**위치**: `webapp/static/app.js showEditOptions`, `nlp_input_module.py` 에 `_edit_slot_multi` 신설, `webapp/main.py` 명령 라우팅 추가

---

## 작업 순서 권장

| 단계 | 버그 | 비고 |
|------|------|------|
| 1 | Bug 6 (수정 후 버튼 안 뜸) + Bug 8 (직접설정) | 추천 핵심 기능, 같이 처리 가능 |
| 2 | Bug 3 (주택소유 자동 무주택) | 마이페이지 UX, DB 스키마 단순 변경 |
| 3 | Bug 2 (지도뷰) + Bug 4 (배지) | recommendation 페이지 묶음 |
| 4 | Bug 5 + Bug 7 (UI state 복원) | 공통 원인, sessionStorage 정비 |
| 5 | Bug 9 (시간 정보) | 작고 독립적 |
| 6 | Bug 1 (새로고침 무한 로딩) | 백엔드 idempotent 처리, 중간 규모 |
| 7 | Bug 10 (다중 선택 수정) | 별도 커밋, UI 재설계 |

각 단계 이후 사용자 확인 받고 다음 진행.
