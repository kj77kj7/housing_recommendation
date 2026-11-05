"""
webapp/auth.py
──────────────
회원가입 / 로그인 API
- 이메일+비밀번호
- 카카오 OAuth
JWT 토큰에 birth_date, gender, nickname 포함 → 청년정책 1차 적용용
"""
import os
from datetime import datetime, timedelta, date
from typing import Optional

import bcrypt
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from jose import jwt

from webapp.database import get_conn, IntegrityError

router = APIRouter()


# ── bcrypt 직접 사용 (passlib 미사용: bcrypt 4.x 호환성) ──
def _hash_password(password: str) -> str:
    # bcrypt는 72바이트 제한이 있으므로 자르기
    pw_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        pw_bytes = password.encode("utf-8")[:72]
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except Exception:
        return False

# JWT 서명 키 — 운영에서는 반드시 SECRET_KEY 환경변수로 설정.
# 코드에 적힌 기본값은 저장소를 본 누구나 알 수 있으므로 운영(Railway)에서 미설정이면
# 프로세스마다 무작위 키를 써서 토큰 위조를 막는다 (재시작 시 재로그인 필요).
_DEV_SECRET_KEY = "seoul-salai-dev-secret-key-2026"
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        import secrets
        SECRET_KEY = secrets.token_urlsafe(48)
        print("[auth] ⚠ SECRET_KEY 미설정 — 임시 무작위 키 사용 중. Railway 환경변수에 SECRET_KEY 를 설정하세요.")
    else:
        SECRET_KEY = _DEV_SECRET_KEY
ALGORITHM  = "HS256"
TOKEN_EXPIRE_HOURS = 72

# 카카오 OAuth
KAKAO_CLIENT_ID     = os.getenv("KAKAO_CLIENT_ID")
KAKAO_CLIENT_SECRET = os.getenv("KAKAO_CLIENT_SECRET")
KAKAO_REDIRECT_URI  = os.getenv("KAKAO_REDIRECT_URI", "http://localhost:8000/auth/kakao/callback")
# 카카오 콜백 후 토큰을 전달할 프론트엔드 주소
# 운영: FRONTEND_URL=https://housing-recommendation.pages.dev
FRONTEND_URL        = os.getenv("FRONTEND_URL", "http://localhost:8000")


# ── 요청 스키마 ──────────────────────────────────────────
class SignupRequest(BaseModel):
    email:      str
    password:   str
    birth_date: str    # "YYYY-MM-DD"
    gender:     str    # "M" / "F"
    nickname:   str = ""
    employment: str = ""  # "취업자" / "미취업자" / "자영업자"
    education:  str = ""  # "고졸이하" / "대학재학" / "대졸" / "석박사"


class LoginRequest(BaseModel):
    email:    str
    password: str


# ── 유틸 ─────────────────────────────────────────────────
def _create_token(payload: dict) -> str:
    data = payload.copy()
    data["exp"] = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)


def _calc_age(birth_date_str: str) -> int:
    try:
        birth = date.fromisoformat(birth_date_str)
        today = date.today()
        return today.year - birth.year - (
            (today.month, today.day) < (birth.month, birth.day)
        )
    except Exception:
        return 0


# ── 회원가입 ─────────────────────────────────────────────
@router.post("/signup")
def signup(req: SignupRequest):
    if "@" not in req.email:
        raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다")
    if req.gender not in ("M", "F"):
        raise HTTPException(status_code=400, detail="성별을 선택해주세요")

    _VALID_EMPLOYMENT = {"취업자", "미취업자", "자영업자"}
    _VALID_EDUCATION  = {"고졸이하", "대학재학", "대졸", "석박사"}
    if req.employment and req.employment not in _VALID_EMPLOYMENT:
        raise HTTPException(status_code=400, detail="올바른 취업상태 값이 아닙니다")
    if req.education and req.education not in _VALID_EDUCATION:
        raise HTTPException(status_code=400, detail="올바른 학력 값이 아닙니다")

    hashed_pw = _hash_password(req.password)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password, birth_date, gender, nickname, employment, education) VALUES (?,?,?,?,?,?,?)",
            (req.email, hashed_pw, req.birth_date, req.gender, req.nickname,
             req.employment or None, req.education or None),
        )
        conn.commit()
        user_id = cur.lastrowid
    except IntegrityError:
        raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다")
    finally:
        conn.close()

    token = _create_token({
        "sub":        str(user_id),
        "email":      req.email,
        "nickname":   req.nickname,
        "birth_date": req.birth_date,
        "gender":     req.gender,
        "age":        _calc_age(req.birth_date),
        "employment": req.employment or "",
        "education":  req.education  or "",
    })
    return {
        "message":      "회원가입 완료",
        "access_token": token,
        "nickname":     req.nickname,
        "user_id":      user_id,
    }


# ── 로그인 ───────────────────────────────────────────────
@router.post("/login")
def login(req: LoginRequest):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (req.email,)
        ).fetchone()
    finally:
        conn.close()

    if not row or not _verify_password(req.password, row["password"] or ""):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 틀렸습니다")

    token = _create_token({
        "sub":        str(row["id"]),
        "email":      row["email"],
        "nickname":   row["nickname"] or "",
        "birth_date": row["birth_date"] or "",
        "gender":     row["gender"] or "",
        "age":        _calc_age(row["birth_date"] or ""),
        "employment": row["employment"] or "",
        "education":  row["education"]  or "",
    })
    return {
        "message":      "로그인 성공",
        "access_token": token,
        "nickname":     row["nickname"] or "",
        "user_id":      row["id"],
    }


# ── 카카오 OAuth ─────────────────────────────────────────
@router.get("/kakao")
def kakao_login():
    if not KAKAO_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail="카카오 로그인은 현재 비활성화 상태입니다 (KAKAO_CLIENT_ID 미설정)"
        )
    kakao_auth_url = (
        f"https://kauth.kakao.com/oauth/authorize"
        f"?client_id={KAKAO_CLIENT_ID}"
        f"&redirect_uri={KAKAO_REDIRECT_URI}"
        f"&response_type=code"
        f"&prompt=login"
    )
    return RedirectResponse(kakao_auth_url)


@router.get("/kakao/callback")
async def kakao_callback(code: str = None):
    if not code:
        raise HTTPException(status_code=400, detail="code가 없습니다")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://kauth.kakao.com/oauth/token",
            data={
                "grant_type":    "authorization_code",
                "client_id":     KAKAO_CLIENT_ID,
                "client_secret": KAKAO_CLIENT_SECRET,
                "redirect_uri":  KAKAO_REDIRECT_URI,
                "code":          code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        token_data   = token_res.json()
        access_token = token_data.get("access_token")
        user_res = await client.get(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_info = user_res.json()

    kakao_id = str(user_info.get("id"))
    nickname = user_info.get("properties", {}).get("nickname", "사용자")

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE kakao_id=?", (kakao_id,)).fetchone()
        if not row:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (kakao_id, nickname) VALUES (?,?)",
                (kakao_id, nickname),
            )
            conn.commit()
            user_id = cur.lastrowid
            birth, gender = "", ""
        else:
            user_id = row["id"]
            nickname = row["nickname"] or nickname
            birth    = row["birth_date"] or ""
            gender   = row["gender"] or ""
    finally:
        conn.close()

    token = _create_token({
        "sub":        str(user_id),
        "nickname":   nickname,
        "birth_date": birth,
        "gender":     gender,
        "age":        _calc_age(birth),
    })
    # 프론트엔드 로그인 페이지로 리다이렉트하면서 토큰 전달 (login.html JS가 처리)
    # FRONTEND_URL 이 없으면 상대경로 → Railway URL로 가버리므로 반드시 설정 필요
    redirect_url = f"{FRONTEND_URL}/login?token={token}&nickname={nickname}&user_id={user_id}"
    return RedirectResponse(redirect_url)


# ── 토큰 검증 (내부 유틸) ────────────────────────────────
def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def require_user(request: Request) -> int:
    """라우터 의존성 — 로그인 토큰 필수 + 요청한 user_id 가 토큰의 본인인지 검사.

    /user/{user_id}, /favorites?user_id=, /checklist?user_id= 처럼 user_id 를
    경로·쿼리로 받는 API 에서 다른 사람의 데이터를 조회·수정하지 못하게 막는다.
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    info = decode_token(token) if token else None
    try:
        uid = int(info.get("sub")) if info else None
    except (TypeError, ValueError):
        uid = None
    if uid is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    claimed = request.path_params.get("user_id", request.query_params.get("user_id"))
    if claimed is not None:
        try:
            claimed_id = int(claimed)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="잘못된 user_id 입니다")
        if claimed_id != uid:
            raise HTTPException(status_code=403, detail="본인 정보만 조회·수정할 수 있습니다")
    return uid


