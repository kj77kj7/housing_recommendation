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
import json
import uuid
import asyncio
import contextvars
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

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
from webapp.auth import router as auth_router, decode_token, require_user
from webapp.password import router as password_router
from webapp.user import router as user_router
from webapp.checklist import router as checklist_router
from webapp.favorites import router as favorites_router
from webapp.session_store import store as session_store


# ── DB 초기화 (서버 시작 시 1회) ─────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    n = session_store.recover_interrupted()
    if n:
        print(f"[startup] 재시작으로 중단된 추천 {n}건을 오류로 표시")
    await _log_outbound_ip()
    yield


async def _log_outbound_ip():
    """서버의 외부(아웃바운드) IP 를 로그에만 남김 — ODsay 서버 IP 화이트리스트 확인용.

    Railway 는 고정 IP 가 아니라 재배포·재시작 때 바뀔 수 있어, ODsay 가
    ApiKeyAuthFailed 를 내면 이 로그의 IP 와 ODsay 에 등록된 IP 를 비교하면 된다.
    (공개 엔드포인트로 노출하지 않음. 조회 실패해도 서버 시작에는 영향 없음)
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            ip = (await client.get("https://api.ipify.org")).text.strip()
        print(f"[startup] 외부(아웃바운드) IP: {ip}  ← ODsay 서버 IP 에 등록돼 있어야 대중교통 경로 조회 가능")
    except Exception as e:
        print(f"[startup] 외부 IP 확인 실패 (무시): {type(e).__name__}")

# ──────────────────────────────────────────────────────────
app = FastAPI(title="집찾봇 API", lifespan=lifespan)

# ── CORS ───────────────────────────────────────────────────
# 운영 / 로컬 개발 origin 을 코드에 직접 박아 두어,
# Railway 환경변수가 누락되거나 오타가 있어도 인증 요청이 항상 통과하도록 보장.
# CORS_ORIGINS 환경변수가 있으면 추가 origin 으로 합쳐서 허용.
#   예: CORS_ORIGINS=https://my-other.pages.dev,https://www.mydomain.com
# ※ allow_credentials=True 는 구체적 origin 목록일 때만 유효 → "*" 값은 무시.
_PROD_ORIGINS = [
    "https://housing-recommendation.pages.dev",
]
_LOCAL_ORIGINS = [
    "http://localhost:8000",
    "http://localhost:5500",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:5500",
]
_cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
_env_origins: list[str] = []
if _cors_origins_env:
    for o in _cors_origins_env.split(","):
        o = o.strip().rstrip("/")
        # "*" 단독값은 credentials=True와 호환 안 되므로 무시
        if o and o != "*":
            _env_origins.append(o)

# 중복 제거하면서 순서 유지
_cors_origins = list(dict.fromkeys(_PROD_ORIGINS + _LOCAL_ORIGINS + _env_origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,      prefix="/auth",      tags=["인증"])
app.include_router(password_router,  prefix="/password",  tags=["비밀번호"])
# 개인 데이터 API — 토큰 필수 + 본인 user_id 만 허용 (require_user)
_auth_required = [Depends(require_user)]
app.include_router(user_router,      prefix="/user",      tags=["유저"],       dependencies=_auth_required)
app.include_router(checklist_router, prefix="/checklist", tags=["체크리스트"], dependencies=_auth_required)
app.include_router(favorites_router, prefix="/favorites", tags=["찜목록"],     dependencies=_auth_required)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────────────────
# 추천 로그 캡처 — 스레드별 stdout 라우팅
# ──────────────────────────────────────────────────────────
# 예전엔 추천 중 sys.stdout 자체를 StringIO 로 바꿨는데, 추천은 여러 스레드에서 동시에
# 돌 수 있어 두 사용자의 로그가 섞이거나, 복원 순서가 꼬이면 서버 로그가 통째로 사라졌다.
# 이제 sys.stdout 은 한 번만 이 라우터로 교체하고, capture() 한 작업의 출력만
# 해당 버퍼에도 복사한다 (서버 콘솔에도 그대로 남김). ContextVar 라서 엔진이 병렬 처리용으로
# 띄우는 작업 스레드(contextvars 복사)에도 같은 버퍼가 적용된다.
_log_buf_var: "contextvars.ContextVar[Optional[io.StringIO]]" = contextvars.ContextVar("rec_log_buf", default=None)


class _ThreadLocalStdout(io.TextIOBase):
    def __init__(self, real):
        self._real = real

    def capture(self, buf):
        _log_buf_var.set(buf)

    def release(self):
        _log_buf_var.set(None)

    def write(self, s):
        buf = _log_buf_var.get()
        if buf is not None:
            buf.write(s)
        return self._real.write(s)

    def flush(self):
        self._real.flush()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


if not isinstance(sys.stdout, _ThreadLocalStdout):
    sys.stdout = _ThreadLocalStdout(sys.stdout)
_stdout_router: _ThreadLocalStdout = sys.stdout


# ──────────────────────────────────────────────────────────
# 세션 — DB(chat_sessions) 저장 + 최근 세션 메모리 캐시 (webapp/session_store.py)
# ──────────────────────────────────────────────────────────


def _validate_workplace(address: str):
    """직장 주소를 카카오로 실제 조회해 수도권 좌표인지 확인 → (ok, error_msg).

    챗봇 단계에서 걸러야 몇 분짜리 추천을 돌린 뒤에야 실패하는 일이 없다.
    카카오 키가 없는 환경(로컬 테스트 등)에서는 검사를 건너뛴다.
    """
    try:
        from housing_recommendation_v5 import (
            geocode_address_kakao_with_fallback, KAKAO_LOCAL_REST_API_KEY,
        )
    except Exception:
        return True, ""
    if not KAKAO_LOCAL_REST_API_KEY:
        return True, ""
    try:
        x, y, _ = geocode_address_kakao_with_fallback(address, KAKAO_LOCAL_REST_API_KEY)
    except Exception:
        return False, (
            f"'{address}' 주소를 찾지 못했어요 🤔\n"
            "도로명 주소나 지하철역 이름으로 다시 알려주세요.\n(예: 강남구 테헤란로 521, 강남역)"
        )
    # 서울·수도권 범위 (대략적인 위경도 박스)
    if not (36.9 <= float(y) <= 38.3 and 126.3 <= float(x) <= 127.9):
        return False, (
            "서울로 출퇴근하실 수 있는 수도권 직장만 추천할 수 있어요.\n"
            "서울·경기·인천 지역의 직장 주소를 알려주세요."
        )
    return True, ""


def _bot_from_state(state: dict) -> ChatBot:
    bot = ChatBot.from_state(state)
    bot.address_validator = _validate_workplace
    return bot


def _user_id_from(request: Request) -> Optional[int]:
    auth = request.headers.get("Authorization", "")
    info = decode_token(auth[7:].strip()) if auth.startswith("Bearer ") else None
    try:
        return int(info.get("sub")) if info else None
    except (TypeError, ValueError):
        return None


def _load_owned_bot(session_id: str, user_id: Optional[int]):
    """(ChatBot, owner_id) — 다른 사용자의 세션이면 (None, owner) 로 취급 (세션 id 만으로 남의 대화 접근 차단)."""
    bot, owner = session_store.load_bot(session_id, _bot_from_state)
    if bot is not None and owner is not None and owner != user_id:
        return None, owner
    return bot, owner


def _params_key(v5_params: dict) -> str:
    """추천 조건 비교용 키 — 조건이 바뀌면 이전 추천 결과를 재사용하지 않는다."""
    raw = json.dumps(v5_params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cond_chips(bot: ChatBot) -> list:
    """추천 결과 페이지 요약 바에 표시할 검색 조건 (결과와 같은 세션 기준)."""
    s = bot.slots
    chips = []
    if s.get("work_address"):
        chips.append("📍 " + s["work_address"])
    if s.get("transport_mode"):
        chips.append("🚗 자가용" if s["transport_mode"] == "car" else "🚌 대중교통")
    if s.get("rent_type") == "월세":
        chips.append(f"💰 월세 보증금 {int(s.get('deposit_manwon') or 0):,}만원 / 월 {s.get('monthly_manwon') or 0}만원")
    elif s.get("deposit_manwon"):
        chips.append(f"💰 전세 {int(s['deposit_manwon']):,}만원")
    if s.get("allowed_minutes"):
        chips.append(f"🕐 통근 {s['allowed_minutes']}분 이내")
    if s.get("house_type"):
        chips.append("🏢 " + str(s["house_type"]))
    if s.get("vibe"):
        chips.append("🏘 " + ", ".join(s["vibe"]))
    if s.get("use_youth_policy"):
        chips.append("📋 청년정책 반영")
    return chips


def _error_payload(e: Exception) -> dict:
    """추천 오류 → 프론트에 보낼 JSON (사용자용 문구 + 오류 코드)."""
    from housing_recommendation_v5 import CommuteServiceError
    if isinstance(e, CommuteServiceError):
        return {"error": str(e), "error_code": "commute_service_unavailable", "user_message": e.user_message}
    return {"error": str(e)}


# ──────────────────────────────────────────────────────────
# Pydantic 모델
# ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    user_message: str
    session_id: Optional[str] = None


class RecommendRequest(BaseModel):
    session_id: str
    # True: 바로 응답(202 running) — 새 화면은 진행률을 polling.
    # False(기본): 끝날 때까지 기다렸다 결과 반환 — 배포 직후 예전 화면(캐시된 JS)과의 호환용.
    async_: bool = Field(False, alias="async")


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


# /favorites 는 찜 목록 API(GET /favorites?user_id=…)와 경로가 같아 일반 라우트로는 페이지가 가려진다.
# user_id 없는 GET 만 페이지로 응답 (운영에선 Cloudflare Pages 가 페이지를 제공하므로 로컬 개발용).
@app.middleware("http")
async def _favorites_page(request: Request, call_next):
    if (request.method == "GET" and request.url.path == "/favorites"
            and "user_id" not in request.query_params):
        return FileResponse(str(STATIC_DIR / "favorites.html"))
    return await call_next(request)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    # 로그인 토큰에서 유저 정보 추출 (청년정책 1차 적용)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user_info = decode_token(token) if token else None
    user_id: Optional[int] = None
    if user_info:
        _sub = user_info.get("sub")
        if _sub:
            try:
                user_id = int(_sub)
            except (ValueError, TypeError):
                user_id = None

    session_id = req.session_id or str(uuid.uuid4())
    bot, owner = await asyncio.to_thread(_load_owned_bot, session_id, user_id)
    if bot is None:
        if owner is not None:                       # 남의 세션 id → 새 세션으로
            session_id = str(uuid.uuid4())
        bot = _bot_from_state({})

    if user_info and not bot.user_meta:
        bot.user_meta = {
            "age":        str(user_info.get("age", "")),
            "birth_date": user_info.get("birth_date", ""),
            "gender":     user_info.get("gender", ""),
            "nickname":   user_info.get("nickname", ""),
            "employment": user_info.get("employment", ""),  # 회원가입 시 취업상태
            "education":  user_info.get("education",  ""),  # 회원가입 시 학력
        }
        # user_profile 에 저장된 값이 있으면 우선 적용
        # → 이전에 입력했던 청년정책 슬롯 자동 재사용 (질문 스킵)
        if user_id:
            from webapp.user import load_policy_profile_for_chatbot
            try:
                stored = load_policy_profile_for_chatbot(user_id)
                for k, v in stored.items():
                    if v:
                        bot.user_meta[k] = v
            except Exception as e:
                print(f"[user_profile load 실패] user_id={user_id}: {e}")

    reply, is_done, v5_params = bot.process(req.user_message)

    # 슬롯 현황
    slot_status = bot.slot_status()

    # 청년정책 사용 시 — "신규 자연흐름 입력" 슬롯만 user_profile 에 저장.
    #   - DB에서 로드된 슬롯(_db_loaded_slots) → 변경 없으면 저장 X (이미 같은 값)
    #   - "수정하기"로 변경된 슬롯(_session_only_slots) → 세션 한정, 저장 X
    #   - 위 두 집합에 모두 없는 슬롯만이 신규 입력 → 저장 후 _db_loaded_slots 로 이동
    # use_youth_policy = "아니오" 인 사용자는 호출 자체 안 함 (개인정보 최소화).
    if user_id and bot.slots.get("use_youth_policy"):
        from nlp_input_module import POLICY_SLOTS
        new_slots_to_save = {}
        for slot in POLICY_SLOTS:
            val = bot.slots.get(slot)
            if val is None:
                continue
            if slot in bot._db_loaded_slots:
                continue
            if slot in bot._session_only_slots:
                continue
            new_slots_to_save[slot] = val

        if new_slots_to_save:
            from webapp.user import upsert_policy_profile_from_chatbot
            try:
                upsert_policy_profile_from_chatbot(user_id, new_slots_to_save)
                # 저장 완료 → 다음 turn 부터 _db_loaded 로 취급하여 중복 저장 방지
                for slot in new_slots_to_save:
                    bot._db_loaded_slots.add(slot)
                # 안내 없이 저장되던 문제 — 처음 저장할 때 한 번 알려줌
                if not getattr(bot, "_profile_save_notified", False):
                    bot._profile_save_notified = True
                    reply += ("\n\nℹ️ 입력하신 청년정책 정보는 다음에 다시 묻지 않도록 '내 정보'에 저장했어요. "
                              "마이페이지에서 언제든 바꿀 수 있어요.")
            except Exception as e:
                print(f"[user_profile 저장 실패] user_id={user_id}: {e}")

    # 빠른 옵션 버튼 결정
    missing = bot._missing_required()
    quick_options = _quick_options_for(missing[0] if missing else None, bot)
    # vibe 슬롯을 묻는 단계에선 다중선택 UI 활성화 (체크박스 + "선택 완료" 버튼)
    quick_options_multi = (bot._last_asked_slot == "vibe")

    await asyncio.to_thread(session_store.save_bot, session_id, bot, user_id if owner is None else owner)

    return {
        "session_id":          session_id,
        "bot_message":         reply,
        "slot_status":         slot_status,
        "quick_options":       quick_options,
        "quick_options_multi": quick_options_multi,
        "is_complete":         is_done,
        "v5_params":           v5_params if is_done else None,
        "policy_cards":        bot.policy_cards,
        "has_more_cards":      bot.has_more_policy_cards,
        "selection_error":     bot._selection_error,
    }


def _run_recommendation_sync(v5_params: dict, cond_chips: list, progress_cb=None) -> dict:
    """실제 추천 작업 (sync). 백그라운드 작업이 스레드에서 호출. 결과 dict 반환 (예외 시 raise)."""
    t0 = time.perf_counter()

    import glob as glob_mod
    gz_files   = sorted(glob_mod.glob(str(ROOT / "사용_csv_모음" / "*주거비*.csv.gz")))
    csv_files  = sorted(glob_mod.glob(str(ROOT / "사용_csv_모음" / "*주거비*.csv")))
    raw_csv    = [f for f in csv_files if not f.endswith(".gz")]
    candidates = raw_csv + gz_files
    if not candidates:
        raise RuntimeError("주거지 CSV 없음")

    csv_path = candidates[0]

    log_buf = io.StringIO()
    _stdout_router.capture(log_buf)
    try:
        from housing_recommendation_v5 import run_recommendation
        final_df, seoul_avg = run_recommendation(
            housing_csv_path=csv_path,
            progress_cb=progress_cb,
            **v5_params,
        )
        results = _df_to_list(final_df)
        for r in results:
            r["transport_mode"] = v5_params.get("transport_mode")
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
            "workplace_lat":   work_y,
            "workplace_lon":   work_x,
            # v5_params 의 통근 허용 키는 allowed_commute_min (예전엔 없는 키를 읽어 항상 null)
            "allowed_minutes": v5_params.get("allowed_commute_min"),
            "transport_mode":  v5_params.get("transport_mode"),
            "cond_chips":      cond_chips,
            "elapsed_sec":     round(elapsed, 1),
        }
    finally:
        _stdout_router.release()


# 동시에 돌 수 있는 추천 수 (메모리·외부 API 한도 보호). 넘으면 순서를 기다림.
_REC_SLOTS: Optional[asyncio.Semaphore] = None   # 이벤트 루프 안에서 처음 쓸 때 생성
_jobs: Dict[str, "asyncio.Task"] = {}


def _rec_slots() -> asyncio.Semaphore:
    global _REC_SLOTS
    if _REC_SLOTS is None:
        _REC_SLOTS = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_RECOMMENDATIONS", "3")))
    return _REC_SLOTS


def _rec_response(session_id: str, rec: dict) -> dict:
    """저장된 추천 상태 → API 응답 형태."""
    st = rec.get("status") or "idle"
    if st == "complete" and rec.get("result"):
        return {"status": "complete", "session_id": session_id, **rec["result"]}
    if st == "running":
        return {"status": "running", "session_id": session_id,
                "progress": rec.get("progress") or {"percent": 0, "message": "추천을 준비하고 있어요"}}
    if st == "error":
        return {"status": "error", "session_id": session_id, **(rec.get("error") or {"error": "추천 처리 중 오류"})}
    return {"status": "not_started", "session_id": session_id}


async def _recommend_job(session_id: str, user_id: Optional[int], v5_params: dict, params_key: str, chips: list):
    def _still_current() -> bool:
        return session_store.get_rec(session_id).get("params_key") == params_key

    def _progress(pct: int, msg: str):
        if _still_current():
            session_store.set_rec(session_id, force_db=False,
                                  progress={"percent": pct, "message": msg, "updated_ts": time.time()})

    try:
        if _rec_slots().locked():
            _progress(0, "다른 추천이 끝나길 기다리고 있어요 (잠시만요)")
        async with _rec_slots():
            _progress(1, "추천을 시작했어요")
            result = await asyncio.to_thread(_run_recommendation_sync, v5_params, chips, _progress)
        if _still_current():
            session_store.set_rec(session_id, user_id=user_id, status="complete", result=result,
                                  progress={"percent": 100, "message": "추천을 마쳤어요"})
    except Exception as e:
        import traceback
        print(f"[추천 API] 오류 session={session_id[:8]} — {e}")
        traceback.print_exc()
        if _still_current():
            session_store.set_rec(session_id, user_id=user_id, status="error", error=_error_payload(e))
    finally:
        if _jobs.get(session_id) is asyncio.current_task():
            _jobs.pop(session_id, None)


@app.post("/api/recommend")
async def recommend(req: RecommendRequest, request: Request):
    """추천 시작 — 바로 응답하고(202) 작업은 백그라운드에서 진행. 진행률·결과는 GET /api/recommend/result.

    예전엔 이 요청 하나가 추천이 끝날 때까지(수 분) 열려 있어, 화면을 닫거나 다른 탭에서는
    진행 상황을 알 수 없었다. 이제 상태를 DB 에 저장하므로 새 탭·서버 재시작 후에도 조회된다.
      - 같은 조건으로 이미 완료 → 결과 즉시 반환 (complete)
      - 같은 조건으로 진행 중 → 진행률 반환 (running)
      - 조건이 바뀌었으면(수정하기 후) 새로 시작
    """
    user_id = _user_id_from(request)
    bot, owner = await asyncio.to_thread(_load_owned_bot, req.session_id, user_id)
    if bot is None:
        return JSONResponse(status_code=404 if owner is not None else 400,
                            content={"error": "세션을 찾을 수 없어요", "status": "not_started"})
    if not bot._done:
        return JSONResponse(status_code=400, content={"error": "슬롯 미완성"})

    v5_params  = bot.get_v5_params()
    params_key = _params_key(v5_params)
    rec = await asyncio.to_thread(session_store.get_rec, req.session_id)
    if rec.get("params_key") == params_key:
        if rec.get("status") == "complete" and rec.get("result"):
            print(f"[추천 API] 캐시 hit session={req.session_id[:8]}")
            return _rec_response(req.session_id, rec)
        if rec.get("status") == "running" and req.session_id in _jobs:
            return await _respond_recommend(req, _jobs[req.session_id])

    owner_id = owner if owner is not None else user_id
    print(f"[추천 API] 작업 시작 session={req.session_id[:8]}")
    await asyncio.to_thread(
        session_store.set_rec, req.session_id, user_id=owner_id, status="running", params_key=params_key,
        progress={"percent": 0, "message": "추천을 준비하고 있어요"}, result=None, error=None,
        started_ts=time.time(),
    )
    task = asyncio.create_task(
        _recommend_job(req.session_id, owner_id, v5_params, params_key, _cond_chips(bot)))
    _jobs[req.session_id] = task
    return await _respond_recommend(req, task)


async def _respond_recommend(req: RecommendRequest, task: "asyncio.Task"):
    if req.async_:
        return JSONResponse(status_code=202, content=_rec_response(
            req.session_id, session_store.get_rec(req.session_id)))
    # 예전 방식 클라이언트 — 작업이 끝날 때까지 기다렸다가 예전 응답 형태로 반환
    await asyncio.shield(task)
    data = _rec_response(req.session_id, session_store.get_rec(req.session_id))
    if data.get("status") == "error":
        return JSONResponse(status_code=503 if data.get("error_code") else 500, content=data)
    return data


@app.get("/api/recommend/result")
async def recommend_result(session_id: str, request: Request):
    """추천 상태·결과 조회 (트리거 안 함). running 이면 progress {percent, message} 포함."""
    if not session_id:
        return JSONResponse(status_code=400, content={"status": "error", "error": "session_id 누락"})
    user_id = _user_id_from(request)
    rec = await asyncio.to_thread(session_store.get_rec, session_id)
    owner = rec.get("user_id")
    if owner is not None and owner != user_id:
        return {"status": "not_started", "session_id": session_id}
    # 이 프로세스에서 돌고 있지 않은 'running' (재시작 등) → 중단으로 처리
    if rec.get("status") == "running" and session_id not in _jobs:
        started = rec.get("started_ts") or 0
        if time.time() - started > 60:
            err = {"error": "interrupted", "error_code": "interrupted",
                   "user_message": "추천이 중단됐어요. '다시 추천'을 눌러 주세요."}
            await asyncio.to_thread(session_store.set_rec, session_id, status="error", error=err)
            rec = session_store.get_rec(session_id)
    return _rec_response(session_id, rec)


@app.get("/api/recommend/latest")
async def recommend_latest(request: Request):
    """로그인 사용자의 가장 최근 추천 (진행 중/완료) — 새 탭·창에서 이어보기용."""
    user_id = _user_id_from(request)
    if user_id is None:
        return JSONResponse(status_code=401, content={"status": "error", "error": "로그인이 필요합니다"})
    sid = await asyncio.to_thread(session_store.latest_for_user, user_id)
    if not sid:
        return {"status": "none"}
    rec = await asyncio.to_thread(session_store.get_rec, sid)
    return _rec_response(sid, rec)


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
            # 소수점 1자리 — 정수로는 상위 매물이 동점으로 보이는 경우가 많아 표시·정렬용
            "score_precise": round(min(100.0, max(0.0, _safe_float(score) * (100 if _safe_float(score) <= 1.0 else 1))), 1),
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

    options: list = []
    asked = bot._last_asked_slot

    # ── 청년정책 세부 슬롯 (v6.0 — 카드 단계 제거) ──────────────
    # 매치되면 options 채움. 매치 안 되면 아래의 일반 슬롯 분기로 진입.
    if bot.slots.get("use_youth_policy") and asked:
        if asked == "policy_employment" and bot.slots.get("policy_employment") is None:
            options = [
                {"label": "취업자",    "value": "취업자"},
                {"label": "미취업자",  "value": "미취업자"},
                {"label": "자영업자",  "value": "자영업자"},
            ]
        elif asked == "policy_education" and bot.slots.get("policy_education") is None:
            options = [
                {"label": "고졸이하",  "value": "① 고졸이하"},
                {"label": "대학재학",  "value": "② 대학재학"},
                {"label": "대졸",      "value": "③ 대졸"},
                {"label": "석박사",    "value": "④ 석박사"},
            ]
        elif asked == "policy_income" and bot.slots.get("policy_income") is None:
            options = [
                {"label": "200만원 이하", "value": "① 200만원 이하"},
                {"label": "200~300만원", "value": "② 200~300만원"},
                {"label": "300~400만원", "value": "③ 300~400만원"},
                {"label": "400~500만원", "value": "④ 400~500만원"},
                {"label": "500만원 이상", "value": "⑤ 500만원 이상"},
                {"label": "모름 · 소득 없음", "value": "⑥ 모름"},
            ]
        elif asked == "policy_no_house" and bot.slots.get("policy_no_house") is None:
            options = [
                {"label": "무주택 (소유 없음)", "value": "① 무주택"},
                {"label": "주택 소유",     "value": "② 주택 소유"},
                {"label": "모름",          "value": "③ 모름"},
            ]

    # ── 일반 슬롯 옵션 수집 (정책 분기에서 매치 안 됐을 때만 진입) ──
    if options:
        pass
    elif slot == "transport_mode":
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

    # 수정 진행 중(_edit_backup_slot 존재)이면 "수정 취소 ↩" 만 추가
    # → 잘못 누른 슬롯 수정을 원본 복원으로 빠져나갈 수 있게 함 ('이전으로'와 같이 두면 헷갈림)
    if getattr(bot, "_edit_backup_slot", None):
        options.append({"label": "수정 취소 ↩", "value": "__CANCEL_EDIT_ACTIVE__"})
    # 필수 슬롯이 1개 이상 입력됐으면 "이전으로 ↩" 추가
    elif bot._slot_fill_order:
        options.append({"label": "이전으로 ↩", "value": "__PREV__"})

    return options


# ── 프론트 정적 파일 (/style.css, /app.js …) ─────────────────
# Cloudflare Pages 배포 기준으로 HTML 이 루트 경로(/app.js)를 참조하므로,
# 로컬에서 FastAPI 로 함께 띄울 때도 같은 경로로 제공한다.
# 반드시 파일 맨 끝 — 위에서 정의한 API 라우트가 먼저 매칭되도록.
app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="frontend")
