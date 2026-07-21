# Phase 7 챗봇 + 웹앱 통합 설계

작성일: 2026-05-05
대상: Claude Code 다음 세션 작업 지시서
적용 시점: Phase 6 (청년정책 시스템 개선) 완료 직후

---

## 0. Phase 구분 (혼동 방지)

- **Phase 1~4**: v5 코드 정상화 (혼잡계수 제거, future API 전환 등) — 완료
- **Phase 5**: vibe 측정 근거 메모 작성 — 완료
- **Phase 6**: 청년정책 시스템 개선 (자격 검증 흐름 재설계, 산식 정밀화 등) — 완료
- **Phase 7 ← 이 문서**: 챗봇 + 웹앱 통합 (LLM 연결 + FastAPI + Vanilla JS)

---

## 1. 이 메모의 위치

기존 메모와의 관계:
- `Claude_Code_인수인계.md` — 전체 프로젝트 맥락 + Phase 1~5 완료
- `Claude_Code_인수인계_추가.md` — vibe 8개 카테고리 + 챗봇 흐름 정의
- `vibe_매핑_설계.md` — vibe 모듈 설계 + 학술 근거
- `청년정책_시스템_개선계획.md` — Phase 6 청년정책 모듈 개선
- `회원가입_설계.md` — 회원가입 최소화 (4개 입력)
- **이 문서 (Phase 7)** — 챗봇 + 웹앱 통합 작업 지시서

---

## 2. 작업 순서 (반드시 이 순서대로)

```
[Step 1] vibe_module의 v5 통합 검증
[Step 2] LLM 연결 (llm_module.py 신규)
[Step 3] LLM을 nlp_input_module의 챗봇에 통합
[Step 4] FastAPI 백엔드 서버 구축
[Step 5] HTML + Vanilla JS 프론트 (챗봇 디자인 기준)
[Step 6] 사용자 직접 테스트
```

---

## 3. Step 1 — vibe_module의 v5 통합 검증

### 목적
`vibe_module.py`가 실제로 v5의 추천 결과에 영향을 주는지 확인.

### 확인 사항
- `housing_recommendation_v5.py`에서 `apply_vibe_to_infra_scores()` 호출하는지
- 인프라 점수 계산 시 vibe 가중치 적용되는지
- 동일 조건에서 vibe만 바꿨을 때 추천 순위 달라지는지

### 검증 방법
- 시나리오 1로 v5 실행 (vibe 없음 vs vibe="조용함" 비교)
- 결과 동/매물 순위 차이 확인
- 가중치 dict 출력으로 디버깅

### 통합 안 되어 있으면
- v5의 인프라 점수 계산 부분(`infra_score = ...`)을 가중 평균으로 변경
- vibe 슬롯이 v5 함수 인자로 전달되는지 확인

---

## 4. Step 2 — LLM 연결 (llm_module.py 신규)

### 결정사항 (사용자 확정)

| 항목 | 결정 |
|---|---|
| 모델 | **Gemini 2.5 Flash-Lite** (분당 15회, 일 1,000회) |
| 호출 위치 | **별도 llm_module.py 신규** |
| 프롬프트 위치 | **외부 파일 분리** (`prompts/` 폴더) |
| 폴백 | **정규식 기반 폴백** (vibe_module의 extract_vibe_from_text 등) |
| JSON 강제 | **`response_mime_type="application/json"` 사용** |
| JSON 파싱 실패 | **1회 재시도 후 폴백** |
| 한도 모니터링 | **추가 (사용자에게 안 보이게, 콘솔 로그만)** |

### 파일 구조

```
서울살이_프로젝트/
├── llm_module.py          ← 신규
├── prompts/
│   ├── slot_extraction.txt    ← 신규 (메인 프롬프트)
│   └── vibe_classification.txt ← 신규 (vibe 분류 보조)
├── nlp_input_module.py     ← 수정 (LLM 호출 통합)
├── vibe_module.py          ← 그대로
├── housing_recommendation_v5.py
└── .env                    ← GEMINI_API_KEY 추가됨
```

### llm_module.py 주요 함수

- `call_gemini(prompt, user_message, current_slots)` — Gemini API 호출
- `extract_slots_from_text(text, current_slots)` — 슬롯 추출 메인 진입점
- `_load_prompt(filename)` — prompts/ 폴더에서 프롬프트 로드
- `_count_daily_calls()` — 일일 호출 수 모니터링 (콘솔만)
- `_fallback_extraction(text)` — 폴백 (정규식 기반, vibe_module + 키워드)

### 폴백 전략 (시연 안정성)

```
시나리오 A. API 키 미설정
  → 즉시 정규식 폴백
  → 콘솔 로그: "[LLM] API 키 없음, 폴백 모드"

시나리오 B. 네트워크 오류 / 5xx
  → 지수 백오프 (1s → 2s → 4s) 3회 시도
  → 모두 실패 시 정규식 폴백
  → 콘솔 로그: "[LLM] 호출 실패, 폴백"

시나리오 C. JSON 파싱 실패
  → 1회 재시도 (같은 프롬프트)
  → 실패 시 정규식 폴백
  → 콘솔 로그: "[LLM] JSON 파싱 실패, 폴백"

시나리오 D. 일일 한도 초과 (429)
  → 즉시 폴백 (재시도 안 함)
  → 콘솔 로그: "[LLM] 일일 한도 초과, 폴백 모드"

시나리오 E. 분당 한도 초과
  → 4초 대기 후 1회 재시도
  → 실패 시 폴백
  → 콘솔 로그: "[LLM] 분당 한도, 4초 대기 후 재시도"
```

→ **폴백 발생 시 사용자에게는 보이지 않음**. 챗봇 응답은 정상 진행.

### 한도 모니터링

- 일일 호출 카운터 변수 (메모리 또는 임시 파일)
- 800회 도달 시 콘솔 경고: "[LLM] 일일 한도 80% 도달"
- 1,000회 도달 시 자동 폴백
- 자정에 카운터 리셋

### prompts/slot_extraction.txt 프롬프트 핵심

```
역할: 서울 거주지 추천 챗봇의 슬롯 추출기

입력:
- 사용자 발화
- 현재까지 채워진 슬롯 상태

작업:
1. 발화에서 새로운 슬롯 정보 추출
2. 기존 슬롯과 병합 (덮어쓰지 말고 새 정보만 추가)
3. JSON으로만 응답

슬롯 스키마:
- work_address: 직장 주소 (자연어 그대로 추출)
- transport_mode: "car" 또는 "transit"
- budget: {rent_type, deposit_manwon, monthly_manwon}
- allowed_minutes: 통근시간 분 단위
- house_type: "오피스텔" / "연립다세대" / null(둘 다)
- weight_preference: "통근우선" / "주거비우선" / "균형" / "위임"
- region_filter: 자치구명 (예: "강남구")
- vibe: 8개 카테고리 중 다중 선택 가능 리스트
- vibe_unrecognized: 매칭 안 된 표현 (재질문용)
- use_youth_policy: bool (청년정책 반영 여부)

vibe 8개 카테고리:
- 조용함, 번화함, 청년활기, 가족친화, 자연친화, 편의 우선, 운동·건강, 카페·문화

규칙:
- 명시 안 된 슬롯은 null
- 추측 금지 (예: 단순 "강남"은 region_filter나 work_address 둘 중 하나, LLM이 모호하면 null)
- 복합 vibe 표현 가능 (예: "조용하고 카페 많은" → ["조용함", "카페·문화"])
- 8개 매칭 안 되는 표현 → vibe=null, vibe_unrecognized="원본표현"
- JSON 외 다른 텍스트 절대 금지
```

---

## 5. Step 3 — LLM을 챗봇에 통합

### 목적
`nlp_input_module.py`의 ChatBot 클래스가 LLM을 사용하도록 수정.

### 변경 사항
- ChatBot 클래스의 슬롯 추출 메서드를 `llm_module.extract_slots_from_text()` 호출로 변경
- 폴백 시 기존 정규식 로직 사용 (자동)
- LLM 호출 결과 + vibe_module 결과 검증
  - LLM이 "조용한"을 추출 못 했는데 vibe_module이 추출했으면 vibe_module 결과 사용
  - 두 결과 다르면 LLM 우선 (LLM이 문맥 더 잘 이해)

### 챗봇 진행 순서 (사용자와 결정한 흐름)

```
1. 직장 주소 (필수)
2. 이동수단 (필수)
3. 예산 (필수)
4. 통근시간 (필수)
5. 주택유형 (필수)
6. 우선순위 (TOPSIS 4축 가중치) (필수)
7. 인프라 반영 여부 + vibe 선택
   - 첫 입력에 vibe 표현 있으면 → 자동 ON, 다시 안 묻기
   - 없으면 → 8개 옵션 + 균등 옵션 제시
8. 청년정책 반영 여부 (필수, 항상 묻기)
   - "현재 조건에 맞는 청년정책이 있는데 같이 확인하시겠어요?"
9. 확인 단계 (정리 후 동의 받기)
10. v5 추천 실행
```

### 시연 흐름 검증 (사용자 입력: "강남역 인근 조용한 집을 찾고 있어")

| 턴 | 사용자 입력 | LLM 호출 | 채워지는 슬롯 |
|---|---|---|---|
| 1 | "강남역 인근 조용한 집을 찾고 있어" | 1회 | work_address, vibe |
| 2 | (이동수단 답변) | 2회 | transport_mode |
| 3 | (예산 답변) | 3회 | budget |
| 4 | (통근시간 답변) | 4회 | allowed_minutes |
| 5 | (주택유형 답변) | 5회 | house_type |
| 6 | (우선순위 답변) | 6회 | weight_preference |
| 7 | (청년정책 답변) | 7회 | use_youth_policy |
| 8 | (확인) | 호출 없음 | (추천 실행) |

→ 평균 사용자 1명당 LLM 7회 호출. Gemini Flash-Lite 일 1,000회 한도 → **약 140명/일 처리 가능**.

---

## 6. Step 4 — FastAPI 백엔드 서버 구축

### 참고 구조
RaceLab 웹앱 구조 (`WEBAPP_ARCHITECTURE.md`) 참고. 검증된 패턴 그대로 차용.

### 폴더 구조

```
서울살이_프로젝트/
├── webapp/
│   ├── __init__.py
│   ├── main.py             ← FastAPI 앱 + 라우팅
│   ├── run.py              ← 서버 실행 스크립트
│   └── static/             ← 프론트엔드 (Step 5)
│       ├── index.html
│       ├── style.css
│       └── app.js
├── llm_module.py
├── nlp_input_module.py
├── vibe_module.py
├── housing_recommendation_v5.py
├── youth_policy_module.py
├── feedback_module.py
└── .env
```

### API 엔드포인트

```
GET  /                     # index.html 반환
GET  /static/*             # CSS/JS 정적 파일
GET  /api/health           # 서버 상태 확인
POST /api/chat             # 챗봇 1턴 처리 (사용자 발화 → 응답)
POST /api/recommend        # 슬롯 다 채워졌을 때 v5 추천 실행
```

### `/api/chat` 요청/응답 형식

**요청:**
```json
{
  "user_message": "강남역 인근 조용한 집을 찾고 있어",
  "current_slots": { ... },
  "session_id": "uuid-..."
}
```

**응답:**
```json
{
  "bot_message": "강남역 인근에서 조용한 동네 찾아드릴게요. 출퇴근 수단은요?",
  "updated_slots": { "work_address": "강남역", "vibe": ["조용함"], ... },
  "missing_slots": ["transport_mode", "budget", "allowed_minutes", "house_type", "weight_preference", "use_youth_policy"],
  "next_question_type": "transport_mode",
  "quick_options": [
    {"label": "자가용", "value": "car"},
    {"label": "대중교통", "value": "transit"}
  ],
  "is_complete": false
}
```

### `/api/recommend` 요청/응답

**요청:** 채워진 모든 슬롯
**응답:** v5 추천 결과 (매물 리스트, 청년정책 매칭 결과 등)

### CORS 설정
- React 같은 별도 프론트 안 쓰고 FastAPI 정적 파일 서빙이라 CORS 불필요
- 같은 도메인(localhost:8000)에서 모든 처리

### 실행 방법
```
cd 서울살이_프로젝트
python -m webapp.run
```

→ `http://localhost:8000` 접속

### 외부 시연 (선택)
- Cloudflare Tunnel로 외부 노출 가능
- `cloudflared tunnel --url http://localhost:8000`
- 발급된 URL을 휴대폰/심사위원에게 공유

---

## 7. Step 5 — HTML + Vanilla JS 프론트

### 디자인 기준
사용자가 미리 만든 챗봇 UI 모형 (스크린샷 기준):

```
┌─────────────────────────────────────────────────┐
│ [좌측 사이드바] [중앙 챗봇]      [우측 패널]      │
│                                                  │
│ ▶ 챗봇으로 찾기  봇 메시지       초기 입력 진행률  │
│   주거 추천      사용자 메시지    - 직장 위치      │
│   지도뷰         빠른 옵션        - 예산           │
│   이사 체크리스트                  - 통근 가능 시간│
│   예산 생활비                                     │
│                                  입력된 조건       │
│   현재 검색 조건                  [요약 카드]      │
│   - 강남역                                        │
│   - 월세 50만원                  빠른 버튼          │
│                                  - 이사 체크리스트  │
│                                  - 예산 생활비     │
│                                  - 내 정보         │
│ 내 정보                                           │
│ 로그아웃                                          │
└─────────────────────────────────────────────────┘
```

### 마감 우선순위 — **챗봇만 작동**

사용자 결정: 사이드바 다른 메뉴(주거 추천, 지도뷰, 이사 체크리스트, 예산 생활비, 내 정보)는 마감 안에 구현 안 함.
- 클릭 시 "준비 중" 표시 또는 비활성화

### 화면 구성 (단일 페이지, SPA 방식)

RaceLab과 동일한 div 토글 방식. 주요 영역:
- 챗봇 메시지 영역 (스크롤)
- 사용자 입력창
- 빠른 옵션 버튼 (LLM이 보내준 quick_options 표시)
- 우측 슬롯 진행률 패널 (실시간 갱신)
- 추천 결과 화면 (슬롯 다 채워지면 표시)

### CSS 디자인 토큰

```css
:root {
  --bg: #fafafa;
  --bg-card: #ffffff;
  --bg-bot: #f5f5f5;
  --bg-user: #4ade80;          /* 디자인 모형의 초록 */
  --fg: #0a0a0a;
  --fg-secondary: #525252;
  --border: #e5e5e5;
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
}
```

### 반응형 처리 (모바일 대응)

- viewport 메타태그
- 데스크톱: 3열 (사이드바 / 챗봇 / 우측 패널)
- 모바일 (< 768px):
  - 사이드바: 햄버거 메뉴로 숨김
  - 우측 패널: 챗봇 위/아래로 이동 또는 토글
  - 챗봇이 메인 영역
- iPhone 노치 대응 (env safe-area-inset)
- 시스템 폰트 자동 (iOS / Android / Windows)

### Vanilla JS 주요 기능

```javascript
// 챗봇 메시지 표시
function appendMessage(text, sender)  // sender: "bot" | "user"

// 사용자 입력 전송
async function sendMessage()
  → POST /api/chat
  → 응답 받아서 메시지 추가 + 슬롯 패널 업데이트 + quick_options 표시

// 슬롯 패널 갱신
function updateSlotPanel(slots)

// 추천 결과 표시
function showRecommendation(result)
  → 별도 카드 영역에 매물 리스트

// 빠른 옵션 버튼 클릭
function onQuickOption(value)
  → sendMessage(value)와 동일하게 처리
```

### PWA는 마감 후로

- 사용자 결정: 시간 남으면 진행
- manifest.json + service worker 추가는 별도 작업

---

## 8. Step 6 — 사용자 직접 테스트

### 테스트 시나리오

**시나리오 1. 정상 흐름 (시연 케이스)**
- 입력: "강남역 인근 조용한 집을 찾고 있어"
- 검증: 7턴 만에 추천까지 완료
- 검증: vibe="조용함" 적용된 추천 순위 차이

**시나리오 2. vibe 매칭 실패**
- 입력: "역세권 좋은 동네"
- 검증: 챗봇이 "역세권 표현은 처리 어려움" 안내 후 8개 옵션 제시

**시나리오 3. LLM 폴백**
- API 키 빼고 실행
- 검증: 정규식 폴백으로 챗봇 정상 작동

**시나리오 4. 청년정책 거부**
- 청년정책 단계에서 "아니요" 응답
- 검증: 청년정책 없이 추천 진행

**시나리오 5. 모바일 화면**
- 휴대폰 또는 브라우저 개발자 도구 모바일 뷰
- 검증: 사이드바 햄버거 메뉴, 우측 패널 토글

### 테스트 환경
- 데스크톱 PC에서 `python -m webapp.run` 실행
- 브라우저: `http://localhost:8000`
- 모바일 테스트: Cloudflare Tunnel + 휴대폰 (선택)

### 검증 포인트 체크리스트

- [ ] 챗봇 메시지 정상 표시
- [ ] 빠른 옵션 버튼 클릭 시 입력 자동
- [ ] 우측 슬롯 패널 실시간 갱신
- [ ] LLM 호출 + 폴백 모두 정상 작동
- [ ] vibe가 v5 추천에 반영
- [ ] 청년정책 매칭 결과 표시
- [ ] 모바일 화면에서 깨지지 않음
- [ ] 사이드바 메뉴 비활성화 표시 정상

---

## 9. 주의사항

### 주의 1. 사용자 작업 스타일 (이전 메모와 동일)
- "내가 묻기 전엔 먼저 묻지 말 것"
- "방향 결정 전 코드부터 올리지 말 것"
- "답변이 너무 길면 안 됨, 핵심만 짧게"
- 큰 변경(파일 삭제/함수 제거)은 적용 전 확인받기

### 주의 2. .env 파일 관리
- `GEMINI_API_KEY` 추가됨
- AnySign4PC 같은 보안 프로그램이 .env 확장자 가로채는 경우 있음
- 파일 연결을 메모장 또는 VSCode로 명시 권장

### 주의 3. LLM 호출 한도
- Gemini Flash-Lite 무료: 분당 15회, 일 1,000회
- 시연 중 한도 도달 시 자동 폴백 (사용자 안 보임)
- 콘솔 로그로만 한도 모니터링

### 주의 4. v5 함수 분리
- v5의 `main()`은 콘솔 input() 기반
- FastAPI에서는 input() 사용 불가
- 추천 함수만 분리해서 인자로 받도록 리팩토링 필요할 수 있음
- 적용 전에 사용자에게 확인

### 주의 5. RaceLab 구조 차용
- `WEBAPP_ARCHITECTURE.md` 참고
- 폴더 구조, viewport 설정, max-width 패턴 등 그대로 활용

---

## 10. 발표 시 메시지 (전체 통합)

> "서울살이는 청년 거주지 의사결정 지원 시스템입니다. Gemini 2.5 Flash-Lite로 자연어 입력을 8개 vibe 카테고리와 슬롯으로 분류하고, Jacobs(1961) 도시활력 이론 기반 가중치 표를 적용하여 v5 추천 엔진(통근/주거비/인프라/청년정책 4축 TOPSIS)을 실행합니다. LLM 장애 시 정규식 기반 폴백으로 시연 안정성 확보. RaceLab 검증된 웹앱 구조(FastAPI + Vanilla JS + Cloudflare Tunnel) 차용으로 모바일 반응형 + 외부 접속 시연 가능."

---

## 11. 다음 행동

Claude Code 다음 세션 시작 시:

```
이 폴더의 다음 메모를 순서대로 읽고 작업 시작:

1. Claude_Code_인수인계.md (전체 맥락)
2. Claude_Code_인수인계_추가.md (vibe 8개, 챗봇 흐름)
3. vibe_매핑_설계.md
4. 청년정책_시스템_개선계획.md (Phase 6 완료)
5. 회원가입_설계.md
6. phase7_챗봇_웹앱_설계.md (이 문서, Phase 7 작업)

Phase 7 Step 1부터 Step 6까지 순차 진행.
각 Step 완료 후 사용자 검증 요청.
큰 변경은 적용 전 확인.
```
