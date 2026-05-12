"""
webapp/main.py
──────────────
FastAPI 백엔드

엔드포인트:
  GET  /              → index.html (로그인 토큰 없으면 /login 리다이렉트)
  GET  /login         → login.html
  GET  /signup        → signup.html
  GET  /static/*      → CSS / JS
  GET  /api/health    → 서버 상태
  POST /auth/signup   → 회원가입
  POST /auth/login    → 로그인
  POST /api/chat      → 챗봇 1턴 (토큰에서 birth_date/gender 추출 → 청년정책)
  POST /api/recommend → v5 추천 실행
"""

import os
import sys
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# 프로젝트 루트를 sys.path에 추가 (상위 폴더의 모듈 import)
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# .env 로딩 (auth.py 등이 os.getenv 로 읽기 전에 호출되어야 함)
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from nlp_input_module import ChatBot
from expense_module import (
    ExpenseRequest, ExpenseBreakdown,
    PropertyInfo, WorkplaceInfo,
    calculate_total_expense, calculate_seoul_average_expense,
    get_neighbor_gus,
    FOOD_BASELINE, TRANSPORT_BASELINE, LEISURE_BASELINE,
)
from webapp.database import init_db
from webapp.auth import router as auth_router, decode_token
from webapp.password import router as password_router
from webapp.user import router as user_router
from webapp.checklist import router as checklist_router
from webapp.favorites import router as favorites_router


# ── DB 초기화 (서버 시작 시 1회) ─────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

# ──────────────────────────────────────────────────────────
app = FastAPI(title="집찾봇 API", lifespan=lifespan)

# ── CORS ───────────────────────────────────────────────────
# CORS_ORIGINS 환경변수에 쉼표 구분 도메인 목록 설정.
# 예: CORS_ORIGINS=https://housing-recommendation.pages.dev
# ※ allow_credentials=True 는 반드시 구체적 origin 목록이 있을 때만 유효.
#    미설정(개발용) 시 ["*"] + credentials=False 로 자동 전환.
_cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_origins_env:
    _cors_origins      = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    _allow_credentials = True
else:
    # 로컬 개발: 와일드카드 허용, credentials는 False (브라우저 스펙 준수)
    _cors_origins      = ["*"]
    _allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,      prefix="/auth",      tags=["인증"])
app.include_router(password_router,  prefix="/password",  tags=["비밀번호"])
app.include_router(user_router,      prefix="/user",      tags=["유저"])
app.include_router(checklist_router, prefix="/checklist", tags=["체크리스트"])
app.include_router(favorites_router, prefix="/favorites", tags=["찜목록"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────────────────
# 세션 저장소 (프로세스 내 메모리)
# ──────────────────────────────────────────────────────────
_sessions: Dict[str, ChatBot] = {}


def _get_bot(session_id: str) -> ChatBot:
    if session_id not in _sessions:
        _sessions[session_id] = ChatBot()
    return _sessions[session_id]


# ──────────────────────────────────────────────────────────
# Pydantic 모델
# ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    user_message: str
    session_id: Optional[str] = None


class RecommendRequest(BaseModel):
    session_id: str


# ──────────────────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/login")
async def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/signup")
async def signup_page():
    return FileResponse(str(STATIC_DIR / "signup.html"))


@app.get("/forgot-password")
async def forgot_password_page():
    return FileResponse(str(STATIC_DIR / "forgot-password.html"))


@app.get("/recommendation")
async def recommendation_page():
    return FileResponse(str(STATIC_DIR / "recommendation.html"))


@app.get("/mypage")
async def mypage_page():
    return FileResponse(str(STATIC_DIR / "mypage.html"))


@app.get("/favorites")
async def favorites_page():
    return FileResponse(str(STATIC_DIR / "favorites.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/myip")
async def myip():
    """Railway 서버 아웃바운드 IP 확인용 (ODsay 화이트리스트 등록 후 삭제 예정)"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.ipify.org?format=json")
            return r.json()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    session_id = req.session_id or str(uuid.uuid4())
    bot = _get_bot(session_id)

    # 로그인 토큰에서 유저 정보 추출 (청년정책 1차 적용)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user_info = decode_token(token) if token else None
    if user_info and not bot.user_meta:
        bot.user_meta = {
            "age":        str(user_info.get("age", "")),
            "birth_date": user_info.get("birth_date", ""),
            "gender":     user_info.get("gender", ""),
            "nickname":   user_info.get("nickname", ""),
            "employment": user_info.get("employment", ""),  # 회원가입 시 취업상태
            "education":  user_info.get("education",  ""),  # 회원가입 시 학력
        }

    reply, is_done, v5_params = bot.process(req.user_message)

    # 슬롯 현황
    slot_status = bot.slot_status()

    # 빠른 옵션 버튼 결정
    missing = bot._missing_required()
    quick_options = _quick_options_for(missing[0] if missing else None, bot)

    return {
        "session_id":    session_id,
        "bot_message":   reply,
        "slot_status":   slot_status,
        "quick_options": quick_options,
        "is_complete":   is_done,
        "v5_params":     v5_params if is_done else None,
        "policy_cards":  bot.policy_cards,
        "has_more_cards": bot.has_more_policy_cards,
        "selection_error": bot._selection_error,
    }


@app.post("/api/recommend")
async def recommend(req: RecommendRequest):
    import time
    t0 = time.perf_counter()
    print(f"[추천 API] 시작 session={req.session_id[:8]}...")

    bot = _get_bot(req.session_id)
    if not bot._done:
        return JSONResponse(status_code=400, content={"error": "슬롯 미완성"})

    v5_params = bot.get_v5_params()

    import glob as glob_mod
    # 원본 .csv 우선 (로컬에서 빠른 처리), 없으면 .csv.gz (배포 환경 — GitHub 100MB 한도 회피용).
    # pandas 는 .gz 확장자 자동 감지.
    gz_files   = sorted(glob_mod.glob(str(ROOT / "사용_csv_모음" / "*주거비*.csv.gz")))
    csv_files  = sorted(glob_mod.glob(str(ROOT / "사용_csv_모음" / "*주거비*.csv")))
    raw_csv    = [f for f in csv_files if not f.endswith(".gz")]
    candidates = raw_csv + gz_files
    if not candidates:
        return JSONResponse(status_code=500, content={"error": "주거지 CSV 없음"})

    csv_path = candidates[0]

    # stdout 캡처
    log_buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = log_buf

    try:
        from housing_recommendation_v5 import run_recommendation
        final_df, seoul_avg = run_recommendation(
            housing_csv_path=csv_path,
            **v5_params,
        )

        results = _df_to_list(final_df)

        # expense API 호출 시 클라이언트가 함께 전달할 직장 좌표 (지오코딩 1회 추가)
        # run_recommendation 내부에서 이미 동일 호출 발생하지만, 외부 노출 안 되어 별도 호출.
        # 추천은 빈도 낮은 액션이므로 +1회 부담 미미. 실패 시 None → 프론트에서 expense 비활성화.
        try:
            from housing_recommendation_v5 import (
                geocode_address_kakao_with_fallback, KAKAO_LOCAL_REST_API_KEY,
            )
            work_x, work_y, _ = geocode_address_kakao_with_fallback(
                v5_params["work_address"], KAKAO_LOCAL_REST_API_KEY,
            )
        except Exception as exc:
            print(f"[추천 API] 직장 좌표 지오코딩 실패: {exc}")
            work_x, work_y = None, None

        elapsed = time.perf_counter() - t0
        print(f"[추천 API] 완료 {elapsed:.1f}s, 결과={len(results)}건")
        return {
            "results":         results,
            "seoul_avg":       seoul_avg,
            "log":             log_buf.getvalue()[-2000:],
            "workplace_lat":   work_y,   # 위도
            "workplace_lon":   work_x,   # 경도
            # 지도뷰: 통근반경 원 그리기용
            "allowed_minutes": v5_params.get("allowed_minutes"),
            "transport_mode":  v5_params.get("transport_mode"),
        }
    except Exception as e:
        # stdout을 먼저 복원해야 traceback이 실제 콘솔로 출력됨
        sys.stdout = old_stdout
        elapsed = time.perf_counter() - t0
        import traceback
        print(f"[추천 API] 오류 {elapsed:.1f}s — {e}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        sys.stdout = old_stdout


@app.post("/api/expense")
async def expense(req: ExpenseRequest):
    """매물 1건의 영수증 + 인접 구 비교 + 서울 평균.

    - target: 사용자가 클릭한 매물 (직장까지 거리 기반 교통비 계산)
    - neighbors: 인접 구 — 동일 매물(rent/maintenance) 가정 + gu만 바꿔 식비/교통 재계산
                 (좌표는 GU_CENTER 사용. neighbor_gus 비어있으면 자동 선택 2개)
    - seoul_average: 청년의 삶 baseline + 매물 DB 월세 평균
    """
    try:
        from housing_recommendation_v5 import GU_CENTER

        target = calculate_total_expense(req.target_property, req.workplace)

        # 인접 구 결정: 명시되지 않으면 자동 선택, 명시됐으면 GU_CENTER에 있는 것만 채택
        if req.neighbor_gus:
            neighbor_gus = [g for g in req.neighbor_gus if g in GU_CENTER]
        else:
            neighbor_gus = get_neighbor_gus(req.target_property.gu, n=2)

        neighbors: list[ExpenseBreakdown] = []
        for ngu in neighbor_gus:
            ng_lat, ng_lon = GU_CENTER[ngu]
            nb_prop = PropertyInfo(
                gu=ngu, lat=ng_lat, lon=ng_lon,
                rent=req.target_property.rent,
                maintenance=req.target_property.maintenance,
            )
            neighbors.append(calculate_total_expense(nb_prop, req.workplace))

        seoul_avg = calculate_seoul_average_expense()

        return {
            "target":        target.model_dump(),
            "neighbors":     [n.model_dump() for n in neighbors],
            "seoul_average": seoul_avg.model_dump(),
            "meta": {
                "data_source": {
                    "food":        "국무조정실 청년의 삶 실태조사 2024 (국가승인 제170002호)",
                    "transport":   "청년의 삶 실태조사 + 서울연구원 통근통계",
                    "leisure":     "청년의 삶 실태조사",
                    "food_weight": "서울시 상권분석서비스 추정매출 2024 4분기 (4업종 평균단가)",
                },
                "baseline_food":      FOOD_BASELINE,
                "baseline_transport": TRANSPORT_BASELINE,
                "baseline_leisure":   LEISURE_BASELINE,
                "comparison_basis": {
                    "neighbors":     "동일 매물 가정 (target rent/maintenance 사용, 좌표는 구 중심)",
                    "seoul_average": "서울 매물 DB 월세 평균 + 청년의 삶 baseline",
                },
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ──────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────

def _df_to_list(df) -> list:
    """final_df → 프론트 JSON 직렬화 (recommendation.js renderCard에서 사용).

    레퍼런스 레포(min05ji26/Seoul_housing) 카드 UI에 맞춰 필드 확장:
      - commute_score / cost_score / infra_score / safety_score / green_score (4축 점수)
      - area_m2 / monthly_rent_manwon / rent_type (상세 정보)
      - address (display_address) — 매물 도로명/동 표시용
    """
    import math

    def _safe_float(v, default=0.0):
        try:
            f = float(v)
            return default if math.isnan(f) else f
        except (TypeError, ValueError):
            return default

    def _to_pct(v, default=0):
        """0~1 점수 → 0~100 정수. 이미 0~100이면 그대로."""
        f = _safe_float(v, 0)
        if f <= 1.0:
            f *= 100
        return int(round(min(100, max(0, f))))

    rows = []
    for _, row in df.iterrows():
        # stage3(MOLIT) 컬럼과 stage2 fallback(CSV) 컬럼 모두 대응
        deposit = (row.get("deposit_manwon")
                   or row.get("conv_deposit_manwon")
                   or row.get("median_deposit")
                   or row.get("환산보증금(만원)")
                   or 0)
        monthly = (row.get("monthly_rent_manwon")
                   or row.get("월세")
                   or 0)
        commute = (row.get("commute_time_min")
                   or row.get("예상_통근시간(분)")
                   or 0)
        score   = (row.get("final_score")
                   or row.get("topsis_score")
                   or 0)
        # 매물별 적용 가능 정책 (v6.0 — match_policies_for_property 결과)
        raw_ap = row.get("applicable_policies")
        applicable_policies = []
        if isinstance(raw_ap, list):
            for p in raw_ap:
                if not isinstance(p, dict):
                    continue
                applicable_policies.append({
                    "policy_id":             p.get("policy_id", ""),
                    "policy_name":           p.get("policy_name", ""),
                    "support_type":          p.get("support_type", ""),
                    "benefit_desc":          p.get("benefit_desc", ""),
                    "benefit_amount_manwon": _safe_float(p.get("benefit_amount_manwon", 0)),
                    "benefit_period_months": int(_safe_float(p.get("benefit_period_months", 0))),
                    "eligibility_status":    p.get("eligibility_status", "needs_check"),
                    "extra_conditions":      p.get("extra_conditions", ""),
                    "tags":                  list(p.get("tags") or []),
                    "apply_url":             p.get("apply_url", ""),
                    "is_gu_specific":        bool(p.get("is_gu_specific", False)),
                    "source_org":            p.get("source_org", ""),
                })
        applicable_total = int(_safe_float(row.get("applicable_policies_total", len(applicable_policies))))

        # 호환용 (구 policy_matched 키, 추후 제거 예정)
        policy_matched = [
            {"name": p["policy_name"],
             "saving": round(p["benefit_amount_manwon"]),
             "benefit": p["benefit_desc"],
             "url": p["apply_url"],
             "dup": False}
            for p in applicable_policies[:3]
        ]

        gu     = str(row.get("시군구_2") or row.get("gu") or "")
        dong   = str(row.get("읍면동")   or row.get("dong") or "")
        addr   = str(row.get("display_address") or row.get("candidate_address") or "")
        # 도로명 주소가 없으면 "구 동" 폴백
        if not addr:
            addr = (gu + " " + dong).strip()

        rows.append({
            "gu":            gu,
            "dong":          dong,
            "address":       addr,
            "house_type":    str(row.get("housing_type") or row.get("주택유형") or ""),
            "rent_type":     str(row.get("rent_type") or ""),
            "area_m2":       round(_safe_float(row.get("area_m2"), 0), 1),
            # 가격
            "deposit_manwon":     int(_safe_float(deposit)),
            "monthly_rent_manwon": int(_safe_float(monthly)),
            "price_manwon":  int(_safe_float(deposit)),  # 호환용 (구 키)
            # 통근
            "commute_min":   int(round(_safe_float(commute))),
            # 좌표 (expense API 호출용 — cand_y=위도, cand_x=경도)
            "lat":           _safe_float(row.get("cand_y"), 0.0),
            "lon":           _safe_float(row.get("cand_x"), 0.0),
            # 점수 (0~100 정수)
            "score":         _to_pct(score),               # 종합 (final_score)
            "commute_score": _to_pct(row.get("commute_score")),
            "cost_score":    _to_pct(row.get("housing_score")),
            "infra_score":   _to_pct(row.get("infra_score")),
            "safety_score":  None,  # 데이터 없음
            "green_score":   None,  # 데이터 없음
            "policy_score":  _to_pct(row.get("policy_score")),  # v6.0: 항상 0
            # 청년정책 (v6.0 — applicable_policies 신규)
            "applicable_policies":       applicable_policies,
            "applicable_policies_total": applicable_total,
            # 호환용 (구 키)
            "policy_matched": policy_matched,
            "policy_count":   len(policy_matched),
        })
    return rows


def _quick_options_for(slot: Optional[str], bot: ChatBot) -> list:
    # 추천 완료 후에는 빠른 옵션 미표시
    if bot._done:
        return []

    # ── 청년정책 세부 슬롯 (v6.0 — 카드 단계 제거) ──────────────
    if bot.slots.get("use_youth_policy") and bot._last_asked_slot:
        asked = bot._last_asked_slot
        if asked == "policy_employment" and bot.slots.get("policy_employment") is None:
            return [
                {"label": "취업자",    "value": "취업자"},
                {"label": "미취업자",  "value": "미취업자"},
                {"label": "자영업자",  "value": "자영업자"},
            ]
        if asked == "policy_education" and bot.slots.get("policy_education") is None:
            return [
                {"label": "고졸이하",  "value": "① 고졸이하"},
                {"label": "대학재학",  "value": "② 대학재학"},
                {"label": "대졸",      "value": "③ 대졸"},
                {"label": "석박사",    "value": "④ 석박사"},
            ]
        if asked == "policy_income" and bot.slots.get("policy_income") is None:
            return [
                {"label": "200만원 이하", "value": "① 200만원 이하"},
                {"label": "200~300만원", "value": "② 200~300만원"},
                {"label": "300~400만원", "value": "③ 300~400만원"},
                {"label": "400~500만원", "value": "④ 400~500만원"},
                {"label": "500만원 이상", "value": "⑤ 500만원 이상"},
                {"label": "모름(없음)",     "value": "⑥ 모름"},
            ]
        if asked == "policy_no_house" and bot.slots.get("policy_no_house") is None:
            return [
                {"label": "무주택 (없음)", "value": "① 무주택"},
                {"label": "주택 소유",     "value": "② 주택 소유"},
                {"label": "모름",          "value": "③ 모름"},
            ]
        return []

    # ── 일반 슬롯 옵션 수집 → 마지막에 "이전으로" 추가 ────────────────
    options: list = []

    if slot == "transport_mode":
        options = [{"label": "자가용", "value": "자가용"}, {"label": "대중교통", "value": "대중교통"}]
    elif slot == "rent_type":
        options = [{"label": "전세", "value": "전세"}, {"label": "월세", "value": "월세"}]
    elif slot == "house_type":
        options = [
            {"label": "오피스텔",   "value": "① 오피스텔"},
            {"label": "연립·다세대","value": "② 연립다세대"},
            {"label": "상관없음",   "value": "③"},
        ]
    elif slot == "weight_preference":
        options = [
            {"label": "통근 우선",   "value": "① 통근 우선"},
            {"label": "주거비 우선", "value": "② 주거비 우선"},
            {"label": "균형",        "value": "③ 균형"},
            {"label": "직접 설정",   "value": "④ 직접 설정"},
        ]
    elif bot._asked_vibe and bot.slots.get("vibe") is None and not bot._asked_policy:
        options = [
            {"label": "조용함",    "value": "① 조용함"},
            {"label": "번화함",    "value": "② 번화함"},
            {"label": "청년활기",  "value": "③ 청년활기"},
            {"label": "가족친화",  "value": "④ 가족친화"},
            {"label": "자연친화",  "value": "⑤ 자연친화"},
            {"label": "편의 우선", "value": "⑥ 편의 우선"},
            {"label": "운동·건강", "value": "⑦ 운동·건강"},
            {"label": "카페·문화", "value": "⑧ 카페·문화"},
            {"label": "상관없음",  "value": "⑨ 상관없음"},
        ]
    elif bot._asked_policy and bot.slots.get("use_youth_policy") is None:
        options = [{"label": "예", "value": "예"}, {"label": "아니요", "value": "아니요"}]

    # 필수 슬롯이 1개 이상 입력됐으면 "이전으로 ↩" 추가
    if bot._slot_fill_order:
        options.append({"label": "이전으로 ↩", "value": "__PREV__"})

    return options
