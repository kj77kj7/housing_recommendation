"""
webapp/favorites.py
───────────────────
찜한 매물 라우터 (사용자별)
하트 클릭 = 토글 (있으면 삭제, 없으면 추가)

엔드포인트:
  GET    /favorites?user_id=1               - 찜 목록 조회 (최신순)
  POST   /favorites/toggle?user_id=1        - 찜 토글 (body=매물 스냅샷)
  GET    /favorites/keys?user_id=1          - 찜한 property_key 목록만 (하트 색칠용)
  DELETE /favorites/{fav_id}?user_id=1      - 개별 삭제 (찜목록 화면용)
"""
import hashlib
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webapp.database import get_conn

router = APIRouter()


# ── 유틸 ──────────────────────────────────────────────
def _make_property_key(address: str, area_m2: float, deposit: int, monthly: int) -> str:
    """매물 식별용 해시 키.
    같은 주소여도 면적/가격이 다르면 다른 매물로 본다."""
    raw = f"{address.strip()}|{round(area_m2 or 0, 1)}|{int(deposit or 0)}|{int(monthly or 0)}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ── 요청 스키마 ───────────────────────────────────────
class FavoriteToggleRequest(BaseModel):
    """추천 카드 스냅샷 전체. main.py _df_to_list() 결과 dict 그대로 전달."""
    address:             str
    gu:                  str = ""
    dong:                str = ""
    house_type:          str = ""
    rent_type:           str = ""
    area_m2:             float = 0.0
    deposit_manwon:      int = 0
    monthly_rent_manwon: int = 0
    commute_min:         int = 0
    score:               int = 0
    lat:                 float = 0.0
    lon:                 float = 0.0
    # 추가 필드(점수/정책 등)는 snapshot으로 통째 저장됨

    class Config:
        extra = "allow"


# ── 엔드포인트 ────────────────────────────────────────
@router.get("")
def get_favorites(user_id: int):
    """찜 목록 (최신순). snapshot_json은 파싱해서 내려준다."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM favorites
               WHERE user_id=?
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        d = dict(r)
        try:
            d["snapshot"] = json.loads(d.pop("snapshot_json") or "{}")
        except Exception:
            d["snapshot"] = {}
        result.append(d)
    return result


@router.get("/keys")
def get_favorite_keys(user_id: int):
    """이 유저가 찜한 property_key 목록만 (하트 색칠용 - 가볍게).
    추천 결과 렌더링 직후 한 번 호출 → 클라가 set으로 비교."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT property_key FROM favorites WHERE user_id=?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return {"keys": [r["property_key"] for r in rows]}


@router.post("/toggle")
def toggle_favorite(user_id: int, req: FavoriteToggleRequest):
    """찜 토글. 있으면 삭제(unfavorite), 없으면 추가(favorite).
    반환: {"action": "added"|"removed", "property_key": "...", "favorite_id": int|None}"""
    if not req.address:
        raise HTTPException(status_code=400, detail="address 필수")

    property_key = _make_property_key(
        req.address, req.area_m2, req.deposit_manwon, req.monthly_rent_manwon,
    )

    snapshot = req.model_dump()
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)

    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM favorites WHERE user_id=? AND property_key=?",
            (user_id, property_key),
        ).fetchone()

        if existing:
            conn.execute("DELETE FROM favorites WHERE id=?", (existing["id"],))
            conn.commit()
            return {
                "action":       "removed",
                "property_key": property_key,
                "favorite_id":  None,
            }

        cur = conn.execute(
            """INSERT INTO favorites
                 (user_id, property_key, address, gu, dong,
                  house_type, rent_type, area_m2,
                  deposit_manwon, monthly_rent_manwon,
                  commute_min, score, lat, lon, snapshot_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id, property_key,
                req.address, req.gu, req.dong,
                req.house_type, req.rent_type, req.area_m2,
                req.deposit_manwon, req.monthly_rent_manwon,
                req.commute_min, req.score, req.lat, req.lon,
                snapshot_json,
            ),
        )
        conn.commit()
        return {
            "action":       "added",
            "property_key": property_key,
            "favorite_id":  cur.lastrowid,
        }
    finally:
        conn.close()


@router.delete("/{fav_id}")
def delete_favorite(fav_id: int, user_id: int):
    """개별 삭제 (찜목록 화면의 X 버튼용)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM favorites WHERE id=? AND user_id=?", (fav_id, user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="찜 항목을 찾을 수 없습니다")
        conn.commit()
    finally:
        conn.close()
    return {"message": "삭제 완료"}
