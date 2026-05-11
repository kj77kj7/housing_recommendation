"""
expense_module.py
─────────────────
"예상 생활비" 계산 모듈.

청년 1인가구 월평균 생활비를 매물 위치(자치구)와 직장 위치(좌표) 기반으로 추정.

데이터 출처:
  - 비목별 절대값: 국무조정실 「2024년 청년의 삶 실태조사」
                  (국가승인통계 제170002호, 만 19~34세 약 15,000가구)
    · 식료품비(외식+마트+카페 통합): 800,000원/월
    · 교통비:                         220,000원/월
    · 오락·문화비:                    180,000원/월
  - 구별 식료품 단가 가중치: 서울시 상권분석서비스 추정매출 (2024 4분기)
    · 4개 업종(한식음식점 / 커피-음료 / 슈퍼마켓 / 편의점) 평균단가 비율
  - 통근 평균거리 10km: 서울연구원 통근통계 참고

공개 API:
  - calculate_food_expense(gu)
  - calculate_transport_expense(home_lat, home_lon, work_lat, work_lon)
  - calculate_total_expense(property_info, workplace)
  - get_neighbor_gus(target_gu, n)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pydantic import BaseModel

# 프로젝트 루트 모듈 재사용
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from housing_recommendation_v5 import GU_CENTER, haversine_km


# ─────────────────────────────────────────────────────────
# 상수: 비목별 baseline (청년의 삶 실태조사) + 교통비 공식 계수
# ─────────────────────────────────────────────────────────

FOOD_BASELINE        = 800_000   # 식료품비 (외식+마트+카페 통합)
LEISURE_BASELINE     = 180_000   # 오락·문화비 (구별 차이 미미, 고정)
TELECOM_BASELINE     = 140_000   # 통신비 (전국 균일, 영수증 표시 제외)

TRANSPORT_BASELINE       = 220_000   # 교통비 baseline
AVG_COMMUTE_KM           = 10        # 서울 통근 평균거리
TRANSPORT_KM_ADJUSTMENT  = 2_000     # 평균 대비 km당 ±보정
TRANSPORT_CLIP_LOWER     = 110_000   # baseline ±50% — 극단치 안전장치
TRANSPORT_CLIP_UPPER     = 330_000

MAINTENANCE_FALLBACK    = 35_000     # 관리비 추정값 (매물 데이터·DB 모두에 없음)
SEOUL_AVG_RENT_FALLBACK = 500_000    # 서울 매물 DB 미발견 시 fallback (테스트 환경 보호)

# 전처리 산출물 / 매물 DB
DATA_DIR    = ROOT / "data"
SALES_CSV   = DATA_DIR / "sales_q4_processed.csv"
HOUSING_CSV_GLOB = "사용_csv_모음/*주거비*.csv"      # 서울 매물 DB (lazy 평균 계산용)
HOUSING_CSV_GZ_GLOB = "사용_csv_모음/*주거비*.csv.gz"  # 배포용 압축본 (pandas 자동 감지)


# ─────────────────────────────────────────────────────────
# 모듈 캐시: 구별 식료품 단가 가중치 (모듈 import 시 1회 계산)
# ─────────────────────────────────────────────────────────

def _build_gu_food_price_index() -> Dict[str, float]:
    """sales_q4_processed.csv → {자치구명: 가중치(서울평균=1.0)}.

    가중치 = (해당구 4개업종 평균단가) / (서울전체 4개업종 평균단가).
    avg_price NaN(매출건수<30) 행은 자동 제외.
    전처리 미실행 환경에서는 모든 구를 1.0으로 fallback.
    """
    if not SALES_CSV.exists():
        return {gu: 1.0 for gu in GU_CENTER}

    df = pd.read_csv(SALES_CSV, encoding="utf-8-sig")
    df = df.dropna(subset=["avg_price", "gu"])

    gu_mean    = df.groupby("gu")["avg_price"].mean()
    seoul_mean = df["avg_price"].mean()
    return {gu: float(price / seoul_mean) for gu, price in gu_mean.items()}


_GU_FOOD_PRICE_INDEX: Dict[str, float] = _build_gu_food_price_index()


# ─────────────────────────────────────────────────────────
# 서울 매물 평균 월세 (lazy 캐시 — 첫 호출 시 1회만 계산)
# ─────────────────────────────────────────────────────────

_SEOUL_AVG_RENT_WON_CACHE: Optional[int] = None


def _seoul_avg_rent_won() -> int:
    """서울 매물 DB(주거비 통합 CSV)의 월세 평균을 원 단위로 반환.

    첫 호출 시 1회만 계산해 모듈 글로벌에 캐시. CSV가 없으면 fallback 500,000원.
    597k 행이지만 단일 컬럼만 로드해 1초 내 처리.
    """
    global _SEOUL_AVG_RENT_WON_CACHE
    if _SEOUL_AVG_RENT_WON_CACHE is not None:
        return _SEOUL_AVG_RENT_WON_CACHE

    import glob as _glob
    # 원본 .csv 우선 (로컬에서 빠른 처리), 없으면 .csv.gz (배포 환경용; pandas 가 .gz 자동 감지)
    gz_paths  = sorted(_glob.glob(str(ROOT / HOUSING_CSV_GZ_GLOB)))
    csv_paths = sorted(_glob.glob(str(ROOT / HOUSING_CSV_GLOB)))
    paths = [p for p in csv_paths if not p.endswith(".gz")] + gz_paths
    if not paths:
        _SEOUL_AVG_RENT_WON_CACHE = SEOUL_AVG_RENT_FALLBACK
        return _SEOUL_AVG_RENT_WON_CACHE

    # 주거비 CSV에 일부 잘못된 utf-8 byte sequence가 섞여있어 errors='replace'로 안전 처리
    df = pd.read_csv(paths[0], usecols=["월세금(만원)"],
                     encoding="utf-8", encoding_errors="replace")
    # 월세 0(전세) 행 제외 — 월세 평균 의미 보존
    rent = df["월세금(만원)"].astype(float)
    rent = rent[rent > 0]
    avg_manwon = float(rent.mean())
    _SEOUL_AVG_RENT_WON_CACHE = int(round(avg_manwon * 10_000))
    return _SEOUL_AVG_RENT_WON_CACHE


# ─────────────────────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────────────────────

class PropertyInfo(BaseModel):
    gu: str
    lat: float
    lon: float
    rent: int                                # 월세 (원)
    maintenance: Optional[int] = None        # 관리비 (없으면 fallback)


class WorkplaceInfo(BaseModel):
    lat: float
    lon: float


class ExpenseBreakdown(BaseModel):
    gu: str
    rent: int
    maintenance: int
    food: int
    transport: int
    leisure: int
    total: int


class ExpenseRequest(BaseModel):
    target_property: PropertyInfo
    workplace: WorkplaceInfo
    neighbor_gus: List[str] = []     # 빈 값이면 자동 선택


class ExpenseResponse(BaseModel):
    target: ExpenseBreakdown
    neighbors: List[ExpenseBreakdown]
    seoul_average: ExpenseBreakdown
    meta: dict


# ─────────────────────────────────────────────────────────
# 1. 식비
# ─────────────────────────────────────────────────────────

def calculate_food_expense(gu: str) -> int:
    """식비 = 청년의 삶 식료품비 800k × 구별 단가 가중치.

    가중치는 4개 업종(한식/커피/슈퍼/편의점) 평균단가 기준.
    가중치 데이터 없는 구는 1.0 (서울 평균) 적용.
    """
    weight = _GU_FOOD_PRICE_INDEX.get(gu, 1.0)
    return int(round(FOOD_BASELINE * weight))


# ─────────────────────────────────────────────────────────
# 2. 교통비
# ─────────────────────────────────────────────────────────

def calculate_transport_expense(
    home_lat: float, home_lon: float,
    work_lat: float, work_lon: float,
) -> int:
    """교통비 = 220k baseline + (직선거리 - 평균 10km) × 2,000원/km.

    근거:
      - 220,000원: 국무조정실 「2024년 청년의 삶 실태조사」 교통비
      - 10km:      서울 통근 평균거리 (서울연구원 통근통계)
      - 2,000원/km: 평균 대비 거리 보정 계수

    검증:
      5km → 210k, 10km → 220k, 20km → 240k, 30km → 260k
    클리핑 [110k, 330k]: 직선거리 5km 미만/60km 초과 극단치 방지.
    """
    dist_km = haversine_km(home_lat, home_lon, work_lat, work_lon)
    adjustment = (dist_km - AVG_COMMUTE_KM) * TRANSPORT_KM_ADJUSTMENT
    estimated = int(round(TRANSPORT_BASELINE + adjustment))
    return max(TRANSPORT_CLIP_LOWER, min(TRANSPORT_CLIP_UPPER, estimated))


# ─────────────────────────────────────────────────────────
# 3. 통합 영수증
# ─────────────────────────────────────────────────────────

def calculate_total_expense(
    property_info: PropertyInfo,
    workplace: WorkplaceInfo,
) -> ExpenseBreakdown:
    """매물 비용(월세/관리비) + 청년의 삶 4개 비목(식비/교통/여가) 합산."""
    food = calculate_food_expense(property_info.gu)
    transport = calculate_transport_expense(
        property_info.lat, property_info.lon,
        workplace.lat, workplace.lon,
    )
    leisure = LEISURE_BASELINE
    rent = int(property_info.rent)
    maintenance = (
        int(property_info.maintenance)
        if property_info.maintenance is not None
        else MAINTENANCE_FALLBACK
    )
    total = rent + maintenance + food + transport + leisure
    return ExpenseBreakdown(
        gu=property_info.gu,
        rent=rent,
        maintenance=maintenance,
        food=food,
        transport=transport,
        leisure=leisure,
        total=total,
    )


# ─────────────────────────────────────────────────────────
# 3-b. 서울 평균 영수증
# ─────────────────────────────────────────────────────────

def calculate_seoul_average_expense() -> ExpenseBreakdown:
    """서울 평균 영수증 — 청년의 삶 baseline + 매물 DB 월세 평균.

    출처:
      - food=800k, transport=220k, leisure=180k: 청년의 삶 실태조사 (전국 청년 평균)
      - rent: 서울 매물 DB(주거비 통합 CSV) 월세 평균 (lazy 1회 계산)
      - maintenance: 35,000원 (실거래가 데이터에 관리비 없음 → fallback)

    target/neighbors와 직접 비교용. gu="서울 평균"으로 식별.
    """
    rent = _seoul_avg_rent_won()
    return ExpenseBreakdown(
        gu="서울 평균",
        rent=rent,
        maintenance=MAINTENANCE_FALLBACK,
        food=FOOD_BASELINE,
        transport=TRANSPORT_BASELINE,
        leisure=LEISURE_BASELINE,
        total=rent + MAINTENANCE_FALLBACK + FOOD_BASELINE + TRANSPORT_BASELINE + LEISURE_BASELINE,
    )


# ─────────────────────────────────────────────────────────
# 4. 인접 구 자동 선택
# ─────────────────────────────────────────────────────────

def get_neighbor_gus(target_gu: str, n: int = 2) -> List[str]:
    """GU_CENTER 거리 기준 가장 가까운 n개 구 반환 (자기 자신 제외)."""
    if target_gu not in GU_CENTER:
        return []
    t_lat, t_lon = GU_CENTER[target_gu]
    others = [
        (gu, haversine_km(t_lat, t_lon, lat, lon))
        for gu, (lat, lon) in GU_CENTER.items()
        if gu != target_gu
    ]
    others.sort(key=lambda x: x[1])
    return [gu for gu, _ in others[:n]]
