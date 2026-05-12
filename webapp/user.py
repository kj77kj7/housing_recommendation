"""
webapp/user.py
──────────────
사용자 정보, 검색조건, 추천이력, 정책 정보 라우터

엔드포인트:
  GET    /user/{user_id}                  - 내 정보 전체 (회원가입 정보)
  PATCH  /user/{user_id}                  - 계정 정보 편집 (닉네임/이메일/생년월일/성별)
  PATCH  /user/{user_id}/nickname         - (호환용) 닉네임만 변경
  GET    /user/{user_id}/profile          - 정책 정보 조회 (employment/education/income 등)
  PATCH  /user/{user_id}/profile          - 정책 정보 편집/생성
  POST   /user/{user_id}/conditions       - 검색조건 저장
  GET    /user/{user_id}/conditions       - 저장된 검색조건 목록
  POST   /user/{user_id}/recommendations  - 추천 이력 저장
  GET    /user/{user_id}/recommendations  - 추천 이력 조회
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webapp.database import get_conn

router = APIRouter()


# ── 내 정보 조회 (회원가입 정보 전체) ───────────────────
@router.get("/{user_id}")
def get_user(user_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, email, nickname, birth_date, gender,
                      employment, education, kakao_id, created_at
               FROM users WHERE id=?""",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    return dict(row)


# ── 계정 정보 편집 (닉네임/이메일/생년월일/성별) ──────────
class UpdateUserRequest(BaseModel):
    nickname:   Optional[str] = None
    email:      Optional[str] = None
    birth_date: Optional[str] = None    # "YYYY-MM-DD"
    gender:     Optional[str] = None    # "M" / "F"


@router.patch("/{user_id}")
def update_user(user_id: int, req: UpdateUserRequest):
    """전달된 필드만 부분 업데이트."""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="수정할 항목이 없습니다")

    if "gender" in fields and fields["gender"] not in ("M", "F"):
        raise HTTPException(status_code=400, detail="성별은 M 또는 F만 가능합니다")
    if "email" in fields and "@" not in fields["email"]:
        raise HTTPException(status_code=400, detail="올바른 이메일 형식이 아닙니다")

    set_clause = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [user_id]

    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", values)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        conn.commit()
    finally:
        conn.close()
    return {"message": "계정 정보 수정 완료", "updated": fields}


# ── 닉네임 수정 (호환용) ─────────────────────────────────
class UpdateNicknameRequest(BaseModel):
    nickname: str


@router.patch("/{user_id}/nickname")
def update_nickname(user_id: int, req: UpdateNicknameRequest):
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE users SET nickname=? WHERE id=?", (req.nickname, user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        conn.commit()
    finally:
        conn.close()
    return {"message": "닉네임 변경 완료", "nickname": req.nickname}


# ── 정책 정보 (user_profile + users.employment/education) ─
class UpdateProfileRequest(BaseModel):
    employment_status: Optional[str] = None   # "취업자" / "미취업자" / "자영업자"
    education_level:   Optional[str] = None   # "고졸이하" / "대학재학" / "대졸" / "석박사"
    marriage_status:   Optional[str] = None   # "기혼" / "미혼"
    has_own_house:     Optional[int] = None   # 0=무주택, 1=주택소유
    income_range:      Optional[str] = None   # "200만원 이하" 등
    asset_range:       Optional[str] = None


@router.get("/{user_id}/profile")
def get_profile(user_id: int):
    """정책 추천에 사용되는 추가 정보. users + user_profile 합쳐서 반환."""
    conn = get_conn()
    try:
        u = conn.execute(
            "SELECT employment, education FROM users WHERE id=?", (user_id,)
        ).fetchone()
        p = conn.execute(
            "SELECT * FROM user_profile WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not u:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    result = {
        "employment_status": (u["employment"] if u else None),
        "education_level":   (u["education"]  if u else None),
        "marriage_status":   None,
        "has_own_house":     None,
        "income_range":      None,
        "asset_range":       None,
    }
    if p:
        # user_profile 의 값이 있으면 우선 적용
        if p["employment_status"]: result["employment_status"] = p["employment_status"]
        if p["education_level"]:   result["education_level"]   = p["education_level"]
        result["marriage_status"] = p["marriage_status"]
        result["has_own_house"]   = p["has_own_house"]
        result["income_range"]    = p["income_range"]
        result["asset_range"]     = p["asset_range"]
    return result


@router.patch("/{user_id}/profile")
def update_profile(user_id: int, req: UpdateProfileRequest):
    """전달된 필드만 user_profile에 upsert. employment/education은 users에도 동기화."""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="수정할 항목이 없습니다")

    conn = get_conn()
    try:
        # 1) users.employment / users.education 동기화
        if "employment_status" in fields:
            conn.execute("UPDATE users SET employment=? WHERE id=?",
                         (fields["employment_status"], user_id))
        if "education_level" in fields:
            conn.execute("UPDATE users SET education=? WHERE id=?",
                         (fields["education_level"], user_id))

        # 2) user_profile upsert (존재 여부 확인 후 INSERT or UPDATE)
        existing = conn.execute(
            "SELECT id FROM user_profile WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if existing:
            set_clause = ", ".join(f"{k}=?" for k in fields.keys())
            values = list(fields.values()) + [existing["id"]]
            conn.execute(f"UPDATE user_profile SET {set_clause} WHERE id=?", values)
        else:
            cols = ["user_id"] + list(fields.keys())
            vals = [user_id] + list(fields.values())
            placeholders = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO user_profile ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )

        conn.commit()
    finally:
        conn.close()
    return {"message": "정책 정보 저장 완료", "updated": fields}


# ── 검색조건 저장 ────────────────────────────────────────
class SearchConditionRequest(BaseModel):
    workplace:      Optional[str] = None
    transport:      Optional[str] = None
    contract_type:  Optional[str] = None
    deposit:        Optional[int] = None
    monthly_rent:   Optional[int] = None
    commute_time:   Optional[int] = None
    preferred_area: Optional[str] = None
    departure_time: Optional[str] = None
    arrival_time:   Optional[str] = None
    house_type:     Optional[str] = None
    priority:       Optional[str] = None
    amenities:      Optional[str] = None
    youth_policy:   Optional[int] = None
    age:            Optional[int] = None


_COND_COLS = [
    "workplace", "transport", "contract_type", "deposit", "monthly_rent",
    "commute_time", "preferred_area", "departure_time", "arrival_time",
    "house_type", "priority", "amenities", "youth_policy", "age",
]


@router.post("/{user_id}/conditions")
def save_condition(user_id: int, req: SearchConditionRequest):
    data = req.dict()
    cols = ["user_id"] + _COND_COLS
    vals = [user_id] + [data[c] for c in _COND_COLS]
    placeholders = ",".join("?" * len(cols))

    conn = get_conn()
    try:
        cur = conn.execute(
            f"INSERT INTO search_conditions ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        cid = cur.lastrowid
    finally:
        conn.close()
    return {"message": "검색 조건 저장 완료", "condition_id": cid}


@router.get("/{user_id}/conditions")
def get_conditions(user_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM search_conditions WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── 추천 이력 ────────────────────────────────────────────
class RecommendationRequest(BaseModel):
    workplace:     Optional[str] = None
    contract_type: Optional[str] = None
    budget:        Optional[str] = None
    commute_time:  Optional[int] = None
    result_count:  Optional[int] = None
    top_result:    Optional[str] = None


_REC_COLS = ["workplace", "contract_type", "budget", "commute_time", "result_count", "top_result"]


@router.post("/{user_id}/recommendations")
def save_recommendation(user_id: int, req: RecommendationRequest):
    data = req.dict()
    cols = ["user_id"] + _REC_COLS
    vals = [user_id] + [data[c] for c in _REC_COLS]
    placeholders = ",".join("?" * len(cols))

    conn = get_conn()
    try:
        cur = conn.execute(
            f"INSERT INTO recommendation_history ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        rid = cur.lastrowid
    finally:
        conn.close()
    return {"message": "추천 이력 저장 완료", "id": rid}


@router.get("/{user_id}/recommendations")
def get_recommendations(user_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM recommendation_history WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
