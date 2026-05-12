import os
import re
import time
import math
import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv
load_dotenv()

from job_search_module import job_search_flow
from youth_policy_module import (
    is_youth_api_configured, collect_youth_info,
    fetch_policies_for_gu, print_policy_section,
    reset_policy_cache,
    match_policies_for_property,   # v6.0 — 매물별 정책 매칭 (점수 X)
)
from feedback_module import collect_feedback, print_feedback_summary, get_feedback_count
from vibe_module import get_vibe_weights, apply_vibe_to_infra_scores


# =========================================================
# 1. 파일 경로
# =========================================================
_DATA_DIR = r"C:\Users\kj77k\Downloads\서울살이_프로젝트\사용_csv_모음"
HOUSING_CSV_PATH = os.path.join(_DATA_DIR, "주거비_데이터_최종통합버전.csv")
SAVE_DIR  = r"C:\Users\kj77k\Downloads\서울살이_프로젝트\api코드결과"
SAVE_PATH = os.path.join(SAVE_DIR, "integrated_recommendation_result_v5.csv")


# =========================================================
# 2. API 키 (.env 파일에서 로드)
# =========================================================
KAKAO_LOCAL_REST_API_KEY    = os.getenv("KAKAO_LOCAL_REST_API_KEY")
KAKAO_MOBILITY_REST_API_KEY = os.getenv("KAKAO_MOBILITY_REST_API_KEY")
ODSAY_API_KEY               = os.getenv("ODSAY_API_KEY")
MOLIT_API_KEY               = os.getenv("MOLIT_API_KEY")

_missing_keys = [k for k, v in {
    "KAKAO_LOCAL_REST_API_KEY": KAKAO_LOCAL_REST_API_KEY,
    "KAKAO_MOBILITY_REST_API_KEY": KAKAO_MOBILITY_REST_API_KEY,
    "ODSAY_API_KEY": ODSAY_API_KEY,
    "MOLIT_API_KEY": MOLIT_API_KEY,
}.items() if not v]
if _missing_keys:
    raise RuntimeError(f"[.env] 누락된 API 키: {', '.join(_missing_keys)}")


# =========================================================
# 3. 공통 설정
# =========================================================
REQUEST_TIMEOUT       = 20
KAKAO_TIMEOUT_SEC     = 10   # 카카오 API 단일 호출 타임아웃 (초)
KAKAO_RETRY_ATTEMPTS  = 3    # 최대 시도 횟수 (첫 시도 + 재시도 2회)
SLEEP_BETWEEN_CALLS   = 0.5
ODSAY_RETRY_COUNT     = 2
ODSAY_RETRY_SLEEP_SEC = 0.7

# ── 1단계: 구 단위 필터링 ────────────────────────────────
STAGE1_TOP_GU = 5           # 상위 N개 구 선정

# ── 2단계: 동 단위 필터링 ────────────────────────────────
STAGE2_TOP_DONG = 10        # 상위 N개 동 선정
# 동 중심 예상 통근시간 계산용 파라미터 (직선거리 기반)
ROAD_CURVE_FACTOR  = 1.4    # 직선거리 → 실제 도로거리 보정계수
CAR_AVG_SPEED_KMH  = 20.0   # 자가용 도심 평균속도
TRANSIT_AVG_SPEED_KMH = 15.0  # 대중교통 평균속도
# 통근시간 여유분: 동 중심 오차 보정 (1차 필터용)
COMMUTE_BUFFER_MIN  = 15    # 기본 여유분
COMMUTE_BUFFER_FALLBACK_MIN = 30  # fallback 시 여유분

# ── 3단계: 매물 단위 필터링 ──────────────────────────────
# 두 상수는 추천 소요 시간에 직접 영향. 운영 중 미세조정 가능하도록 env var 로 분리.
#   - MOLIT_RECENT_MONTHS: 국토부 API 에서 가져올 최근 N개월 데이터
#     기본 3 (이전 6) — 호출 수 절반, 시세 정확도는 오히려 ↑
#   - STAGE3_MAX_PROPERTY_PER_DONG: 동당 최대 매물 수 (geocoding·ODsay 호출 cap)
#     기본 10 (이전 20) — 최종 5건 추천 품질에 영향 거의 없음
# 롤백·튜닝: Railway 환경변수만 변경하면 즉시 적용 (재배포 자동 트리거).
MOLIT_RECENT_MONTHS          = int(os.getenv("MOLIT_RECENT_MONTHS", "3"))
STAGE3_MAX_PROPERTY_PER_DONG = int(os.getenv("STAGE3_MAX_PROPERTY_PER_DONG", "10"))
MOLIT_ROW_URL = "http://apis.data.go.kr/1613000/RTMSDataSvcRHRent/getRTMSDataSvcRHRent"
MOLIT_OFT_URL = "http://apis.data.go.kr/1613000/RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent"

# ── 베이즈 평활화 (2단계에서 사용) ───────────────────────
BAYESIAN_K = 10

# ── 대기/패널티 기본값 ────────────────────────────────────
DEFAULT_BUS_WAIT_MIN                      = 4.0
DEFAULT_SUBWAY_WAIT_MIN                   = 3.0
DEFAULT_TRANSFER_PENALTY_PER_TRANSFER_MIN = 2.0


# =========================================================
# 4. 서울 구별 법정동코드 / 구 중심 좌표
# =========================================================
SEOUL_LAWD_CD: Dict[str, str] = {
    "종로구": "11110", "중구":    "11140", "용산구":   "11170", "성동구":  "11200",
    "광진구": "11215", "동대문구":"11230", "중랑구":   "11260", "성북구":  "11290",
    "강북구": "11305", "도봉구":  "11320", "노원구":   "11350", "은평구":  "11380",
    "서대문구":"11410","마포구":  "11440", "양천구":   "11470", "강서구":  "11500",
    "구로구": "11530", "금천구":  "11545", "영등포구": "11560", "동작구":  "11590",
    "관악구": "11620", "서초구":  "11650", "강남구":   "11680", "송파구":  "11710",
    "강동구": "11740",
}

# (위도, 경도)
GU_CENTER: Dict[str, Tuple[float, float]] = {
    "종로구":  (37.5730, 126.9790), "중구":    (37.5640, 126.9975),
    "용산구":  (37.5311, 126.9810), "성동구":  (37.5633, 127.0369),
    "광진구":  (37.5384, 127.0822), "동대문구":(37.5744, 127.0396),
    "중랑구":  (37.6063, 127.0927), "성북구":  (37.5894, 127.0167),
    "강북구":  (37.6396, 127.0257), "도봉구":  (37.6688, 127.0471),
    "노원구":  (37.6542, 127.0568), "은평구":  (37.6026, 126.9291),
    "서대문구":(37.5791, 126.9368), "마포구":  (37.5663, 126.9014),
    "양천구":  (37.5170, 126.8665), "강서구":  (37.5509, 126.8496),
    "구로구":  (37.4954, 126.8874), "금천구":  (37.4601, 126.9001),
    "영등포구":(37.5264, 126.8962), "동작구":  (37.5124, 126.9393),
    "관악구":  (37.4784, 126.9516), "서초구":  (37.4837, 127.0324),
    "강남구":  (37.4979, 127.0276), "송파구":  (37.5145, 127.1059),
    "강동구":  (37.5301, 127.1238),
}


# =========================================================
# 6. 유틸
# =========================================================
class APIError(Exception):
    pass

def require_key(name, value):
    if not value or value.startswith("YOUR_"):
        raise ValueError(f"{name} 값이 비어 있습니다.")

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def safe_to_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)): return None
        return float(v)
    except: return None

def safe_to_int(v, default=0):
    f = safe_to_float(v)
    return default if f is None else int(f)

def ensure_list(v):
    if isinstance(v, list): return v
    if isinstance(v, tuple): return list(v)
    return [] if v is None else [v]

def first_dict(v):
    if isinstance(v, dict): return v
    if isinstance(v, list):
        for i in v:
            if isinstance(i, dict): return i
    return {}

def safe_get(c, k, d=None):
    return c.get(k, d) if isinstance(c, dict) else d

def normalize_name(v):
    if pd.isna(v): return ""
    return re.sub(r"\s+", "", str(v).strip().replace("역","").replace(".",""))

def parse_mmss_to_min(v):
    if pd.isna(v): return None
    s = str(v).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        hh, mm, ss = map(int, s.split(":")); return hh*60+mm+ss/60
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        mm, ss = map(int, s.split(":")); return mm+ss/60
    return None

def min_max_scale_inverse(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    mn, mx = s.min(), s.max()
    if pd.isna(mn) or pd.isna(mx): return pd.Series([50.0]*len(s), index=s.index)
    if mx == mn: return pd.Series([100.0]*len(s), index=s.index)
    return (1-(s-mn)/(mx-mn))*100

def commute_score(minutes, allowed):
    if minutes is None or pd.isna(minutes) or allowed <= 0: return 0.0
    if minutes > allowed: return 0.0
    return max(0.0, (1-minutes/allowed)*100)

def subway_load_penalty(avg):
    if avg is None or pd.isna(avg): return 0.0
    if avg >= 80000: return 5.0
    if avg >= 50000: return 3.0
    if avg >= 25000: return 1.5
    return 0.5

def truncate_text(text, max_len=300):
    if text is None: return ""
    text = str(text).replace("\n"," ").replace("\r"," ").strip()
    return text if len(text)<=max_len else text[:max_len]+"..."


def _kakao_request(tag: str, url: str, headers: dict, params: dict) -> requests.Response:
    """카카오 API GET 요청 공통 헬퍼 (재시도 + 타임아웃 보강).

    재시도 대상: DNS 실패(ConnectionError), 타임아웃(Timeout), 서버 오류(5xx)
    재시도 안함: 4xx 응답 (인증 실패·잘못된 요청 — 재시도해도 결과 동일)
    최대 시도: KAKAO_RETRY_ATTEMPTS회, 지수 백오프 (1초 → 2초)
    최종 실패 시 APIError를 raise해 상위 fallback 로직이 캐치할 수 있도록 함.
    """
    last_exc: Exception = RuntimeError("알 수 없는 오류")
    for attempt in range(1, KAKAO_RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=KAKAO_TIMEOUT_SEC)
            if resp.status_code >= 500:
                raise APIError(f"[{tag}] 서버 오류 HTTP {resp.status_code}: {truncate_text(resp.text)}")
            return resp  # 2xx or 4xx → 즉시 반환 (재시도 불필요)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                APIError) as exc:
            last_exc = exc
            if attempt < KAKAO_RETRY_ATTEMPTS:
                wait = 2 ** (attempt - 1)  # 1초 → 2초
                print(f"[{tag}] 시도 {attempt}/{KAKAO_RETRY_ATTEMPTS} 실패: "
                      f"{type(exc).__name__}. {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                print(f"[{tag}] 시도 {attempt}/{KAKAO_RETRY_ATTEMPTS} 실패: "
                      f"{type(exc).__name__}. 최종 실패.")
    raise APIError(f"[{tag}] 최종 실패: {last_exc}") from last_exc

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a), math.sqrt(1-a))

def save_csv_safely(df, base_path):
    ensure_dir(os.path.dirname(base_path))
    try:
        df.to_csv(base_path, index=False, encoding="utf-8-sig"); return base_path
    except PermissionError:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = base_path.replace(".csv", f"_{ts}.csv")
        df.to_csv(alt, index=False, encoding="utf-8-sig"); return alt

def fmt_manwon(v):
    try: return f"{int(round(float(v))):,}"
    except: return str(v)

def fmt_area(v):
    try:
        m2 = float(v)
        return f"{m2:.1f}㎡ (약 {m2/3.3058:.1f}평)"
    except: return str(v)


# =========================================================
# 7. 카카오 지오코딩
# =========================================================
def clean_work_address(addr: str) -> str:
    """
    직장 주소에서 지오코딩 실패 원인이 되는 상세 정보 제거.
    도로명 주소 또는 지번 주소까지만 남기고 층·호·방향 등 제거.

    예)
      "서울특별시 강북구 삼양로74길 1층 우측"  → "서울특별시 강북구 삼양로74길"
      "서울 강남구 테헤란로 123 4층 401호"    → "서울 강남구 테헤란로 123"
      "서울 종로구 종로 1가 123번지 2동 305호" → "서울 종로구 종로 1가 123번지"
    """
    if not addr:
        return addr

    # 괄호 내용 제거 (예: (역삼동))
    s = re.sub(r"\([^)]*\)", "", addr).strip()

    # 층/호 이후 잡음 제거 (순서 중요: 지하/B 먼저 처리)
    s = re.sub(r"\s*지하\s*\d+층?.*$", "", s).strip()   # "지하1층", "지하 2층"
    s = re.sub(r"\s*[Bb]\s*\d+층?.*$", "", s).strip()   # "B1층", "B 2"
    s = re.sub(r"\s*\d+층.*$", "", s).strip()            # "4층", "1층 우측"
    # 숫자-한글+숫자+호 혼합 상세주소 제거 (예: 302-에이32호, 에이32호)
    s = re.sub(r"\s*\d+\s*-\s*[가-힣]+\d*\s*호?.*$", "", s).strip()
    s = re.sub(r"\s*[가-힣]+\d+\s*호.*$", "", s).strip()
    s = re.sub(r"\s*\d+호.*$", "", s).strip()            # "401호"

    # 방향 키워드 제거
    s = re.sub(r"\s*(우측|좌측|우편|좌편|우|좌|앞|뒤|옆)$", "", s).strip()

    # 중복 공백 정리
    s = re.sub(r"\s+", " ", s).strip()
    return s


def geocode_address_kakao(address, kakao_local_key):
    require_key("KAKAO_LOCAL_REST_API_KEY", kakao_local_key)
    resp = _kakao_request(
        "Kakao Local",
        "https://dapi.kakao.com/v2/local/search/address.json",
        headers={"Authorization": f"KakaoAK {kakao_local_key}"},
        params={"query": address, "analyze_type": "similar", "size": 1},
    )
    if resp.status_code != 200:
        raise APIError(f"[Kakao Local] HTTP {resp.status_code}: {truncate_text(resp.text)}")
    docs = ensure_list(safe_get(resp.json(), "documents", []))
    if not docs or not isinstance(docs[0], dict):
        raise APIError(f"[Kakao Local] 주소 변환 실패: {address}")
    doc = docs[0]
    matched = (safe_get(doc,"address_name")
               or safe_get(first_dict(safe_get(doc,"road_address",{})),"address_name")
               or address)
    return float(doc["x"]), float(doc["y"]), matched


def geocode_keyword_kakao(query, kakao_local_key, category_code=None):
    """카카오 키워드 검색으로 장소 좌표 조회 (역명·건물명 등).

    address.json은 도로명/지번 주소만 처리하므로, 역명("강남역")처럼 장소명은
    keyword.json 엔드포인트로 조회한다. category_group_code=SW8은 지하철역.
    """
    require_key("KAKAO_LOCAL_REST_API_KEY", kakao_local_key)
    params = {"query": query, "size": 1}
    if category_code:
        params["category_group_code"] = category_code
    resp = _kakao_request(
        "Kakao Local Keyword",
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        headers={"Authorization": f"KakaoAK {kakao_local_key}"},
        params=params,
    )
    if resp.status_code != 200:
        raise APIError(f"[Kakao Keyword] HTTP {resp.status_code}: {truncate_text(resp.text)}")
    docs = ensure_list(safe_get(resp.json(), "documents", []))
    if not docs or not isinstance(docs[0], dict):
        raise APIError(f"[Kakao Keyword] 좌표 조회 실패: {query}")
    doc = docs[0]
    matched = safe_get(doc, "place_name", query) or query
    return float(doc["x"]), float(doc["y"]), matched


def geocode_address_kakao_with_fallback(address, kakao_local_key):
    """
    1차: clean_work_address 적용 후 도로명/지번 주소 검색
    2차: 역명 패턴이면 키워드 검색 (지하철역 카테고리)
    3차: 일반 키워드 검색
    4차: 마지막 토큰 잘라가며 재시도 (최소 3토큰)
    모두 실패 시 APIError raise
    """
    cleaned = clean_work_address(address)
    # 서울 한정 — "○○역" 단독 입력(출구 미지정)은 카카오 주소 DB의
    # "○○역 1번출구"로 치환하여 1차 주소 검색에서 잡히게 함.
    # (역명 키워드 검색 fallback 회피 → 카카오 API 호출 1회로 끝남)
    if re.search(r"[가-힣]+역\s*$", cleaned) and "출구" not in cleaned:
        cleaned = cleaned.rstrip() + " 1번출구"
    # 1차 시도 — 도로명/지번 주소
    try:
        return geocode_address_kakao(cleaned, kakao_local_key)
    except APIError:
        pass
    # 원본과 다르면 원본도 시도
    if cleaned != address:
        try:
            return geocode_address_kakao(address, kakao_local_key)
        except APIError:
            pass
    # 2차: 역명 패턴 → 지하철역 카테고리 검색 (출구 무관, 역 중심 좌표)
    if re.search(r"[가-힣]+역(\s|$)", cleaned):
        try:
            return geocode_keyword_kakao(cleaned, kakao_local_key, category_code="SW8")
        except APIError:
            pass
    # 3차: 일반 키워드 검색 (건물명·장소명)
    try:
        return geocode_keyword_kakao(cleaned, kakao_local_key)
    except APIError:
        pass
    # 4차: 토큰 줄여가며 재시도
    tokens = cleaned.split()
    while len(tokens) >= 3:
        tokens = tokens[:-1]
        try:
            return geocode_address_kakao(" ".join(tokens), kakao_local_key)
        except APIError:
            continue
    raise APIError(f"[Kakao Local] 주소 변환 최종 실패: {address}")


# =========================================================
# 8. 카카오모빌리티 자차 경로
# =========================================================
def _next_weekday_at(hour: int, minute: int) -> str:
    """주어진 시각 기준 다음 평일 출발 시각을 YYYYMMDDHHMM 형식으로 반환 (future/directions용)"""
    now = datetime.datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now or now.weekday() >= 5:
        candidate += datetime.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += datetime.timedelta(days=1)
    return candidate.strftime("%Y%m%d%H%M")


def get_drive_route_kakaomobility(ox, oy, dx, dy, key, departure_time: str = None):
    require_key("KAKAO_MOBILITY_REST_API_KEY", key)
    if departure_time:
        endpoint = "https://apis-navi.kakaomobility.com/v1/future/directions"
        params = {"origin": f"{ox},{oy}", "destination": f"{dx},{dy}",
                  "priority": "TIME", "summary": "true",
                  "alternatives": "false", "road_details": "false",
                  "departure_time": departure_time}
    else:
        endpoint = "https://apis-navi.kakaomobility.com/v1/directions"
        params = {"origin": f"{ox},{oy}", "destination": f"{dx},{dy}",
                  "priority": "TIME", "summary": "true",
                  "alternatives": "false", "road_details": "false"}
    resp = _kakao_request(
        "Kakao Mobility",
        endpoint,
        headers={"Authorization": f"KakaoAK {key}", "Content-Type": "application/json"},
        params=params,
    )
    if resp.status_code != 200:
        raise APIError(f"[Kakao Mobility] HTTP {resp.status_code}: {truncate_text(resp.text)}")
    routes = ensure_list(safe_get(resp.json(), "routes", []))
    if not routes or not isinstance(routes[0], dict):
        raise APIError("[Kakao Mobility] 경로 없음")
    summary = first_dict(safe_get(routes[0], "summary", {}))
    fare    = first_dict(safe_get(summary, "fare", {}))
    return {
        "distance_m":   safe_to_float(safe_get(summary, "distance")),
        "duration_sec": safe_to_float(safe_get(summary, "duration")),
        "toll_fare":    safe_to_float(safe_get(fare, "toll")),
    }


# =========================================================
# 9. ODsay 대중교통 경로
# =========================================================
def parse_odsay_error(data, status_code, raw):
    if not isinstance(data, dict):
        return f"[ODsay] HTTP {status_code}: {truncate_text(raw)}"
    err = data.get("error")
    if err is None: return None
    e = first_dict(err)
    return f"[ODsay] {safe_get(e,'code')}: {safe_get(e,'msg') or safe_get(e,'message') or truncate_text(raw)}"


def get_transit_route_odsay_once(ox, oy, dx, dy, api_key):
    require_key("ODSAY_API_KEY", api_key)
    resp     = requests.get(
        "https://api.odsay.com/v1/api/searchPubTransPathT",
        params={"apiKey": api_key, "SX": ox, "SY": oy, "EX": dx, "EY": dy,
                "OPT": 0, "SearchType": 0, "SearchPathType": 0, "output": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    raw_text = resp.text
    try:    data = resp.json()
    except: raise APIError(f"[ODsay] JSON 파싱 실패 / body={truncate_text(raw_text)}")
    err = parse_odsay_error(data, resp.status_code, raw_text)
    if err: raise APIError(err)
    if resp.status_code != 200:
        raise APIError(f"[ODsay] HTTP {resp.status_code}: {truncate_text(raw_text)}")
    result = first_dict(data.get("result"))
    if not result:
        raise APIError(f"[ODsay] result 없음 / body={truncate_text(raw_text)}")
    paths = [p for p in ensure_list(result.get("path",[])) if isinstance(p, dict)]
    if not paths: raise APIError("[ODsay] 경로 없음")
    fp   = paths[0]
    info = first_dict(fp.get("info", {}))
    sub  = [s for s in ensure_list(fp.get("subPath",[])) if isinstance(s, dict)]
    payment = safe_get(info,"payment")
    if isinstance(payment, list): payment = payment[0] if payment else None
    total_time = safe_to_float(safe_get(info,"totalTime")) or safe_to_float(safe_get(fp,"totalTime"))

    if total_time is None:
        print(f"  [ODsay 파싱 경고] totalTime 없음. info keys={list(info.keys())[:8]}")

    return {
        "total_time_min":       total_time,
        "payment":              payment,
        "total_walk_m":         safe_to_float(safe_get(info,"totalWalk")) or safe_to_float(safe_get(fp,"totalWalk")) or 0.0,
        "bus_transit_count":    safe_to_int(safe_get(info,"busTransitCount"),0),
        "subway_transit_count": safe_to_int(safe_get(info,"subwayTransitCount"),0),
        "sub_paths":            sub,
        "raw_path_count":       len(paths),
    }


def get_transit_route_odsay(ox, oy, dx, dy, api_key):
    last = None
    for attempt in range(1, ODSAY_RETRY_COUNT+1):
        try:
            return get_transit_route_odsay_once(ox, oy, dx, dy, api_key)
        except Exception as e:
            last = e
            if attempt < ODSAY_RETRY_COUNT: time.sleep(ODSAY_RETRY_SLEEP_SEC)
    raise APIError(str(last))


# =========================================================
# 10. 국토부 실거래가 API
# =========================================================
def _molit_fetch_raw(url, lawd_cd, deal_ymd):
    try:
        resp = requests.get(url, params={
            "serviceKey": MOLIT_API_KEY,
            "LAWD_CD":    lawd_cd,
            "DEAL_YMD":   deal_ymd,
            "pageNo":     1,
            "numOfRows":  1000,
            # resultType 파라미터 제거 → XML 응답 (이 API는 XML만 지원)
        }, timeout=REQUEST_TIMEOUT)

        if not resp.text or not resp.text.strip():
            print(f"  [국토부 API 경고] 빈 응답 (lawd_cd={lawd_cd}, ym={deal_ymd})")
            return []

        # XML 파싱
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as pe:
            print(f"  [국토부 API XML오류] {pe} / body={resp.text[:100]}")
            return []

        # resultCode 확인 (이 API는 "00" 또는 "000"이 정상)
        result_code = (root.findtext(".//resultCode") or root.findtext(".//ResultCode") or "").strip()
        if result_code and result_code not in ("00","000","0",""):
            msg = root.findtext(".//resultMsg") or root.findtext(".//ResultMsg") or ""
            print(f"  [국토부 API 오류] resultCode={result_code}, msg={msg}")
            return []

        # items > item 추출
        items = []
        for item in root.iter("item"):
            d = {}
            for child in item:
                d[child.tag.strip()] = child.text.strip() if child.text else ""
            if d:
                items.append(d)
        return items

    except Exception as e:
        print(f"  [국토부 API 예외] {str(e)[:100]}")
        return []


def _normalize_dong(name: str) -> str:
    """동 이름 정규화: 공백 제거, '동' 제거, 숫자 통일"""
    s = re.sub(r"\s+", "", str(name).strip())
    # '가' 앞 숫자 유지, 나머지 후처리
    return s


def _dong_match(dong_name: str, api_dong: str) -> bool:
    """
    dong_name(CSV 기준)과 api_dong(국토부 반환값)의 일치 여부.
    완전 포함 OR 정규화 후 부분 일치로 판단.
    예) "금호동4가" vs "금호4가", "금호동 4가" 등 처리
    """
    if not dong_name:
        return True
    d1 = _normalize_dong(dong_name)
    d2 = _normalize_dong(api_dong)
    # 완전 포함
    if d1 in d2 or d2 in d1:
        return True
    # 숫자+가 제거 후 비교 (예: "금호동4가" → "금호동", "금호4가" → "금호")
    d1_base = re.sub(r"\d+가?$", "", d1).rstrip("동")
    d2_base = re.sub(r"\d+가?$", "", d2).rstrip("동")
    if d1_base and d2_base and (d1_base in d2_base or d2_base in d1_base):
        return True
    return False


def fetch_molit_rentals(gu_name, dong_name, n_months=MOLIT_RECENT_MONTHS,
                        housing_types=None):
    if housing_types is None: housing_types = ["연립다세대","오피스텔"]
    lawd_cd = SEOUL_LAWD_CD.get(gu_name)
    if not lawd_cd: return pd.DataFrame()

    url_map = {"연립다세대": MOLIT_ROW_URL, "오피스텔": MOLIT_OFT_URL}
    now = datetime.datetime.now()
    all_rows = []

    for htype in housing_types:
        url = url_map.get(htype)
        if not url: continue
        for i in range(n_months):
            ym    = (now - datetime.timedelta(days=30*i)).strftime("%Y%m")
            items = _molit_fetch_raw(url, lawd_cd, ym)
            for it in items:
                if not isinstance(it, dict): continue
                # 실제 XML 영문 필드명 (오피스텔/연립다세대 공통)
                # dong: 법정동명
                dong  = str(it.get("법정동","") or it.get("umdNm","") or it.get("dong","")).strip()
                if dong_name and not _dong_match(dong_name, dong): continue
                jibun = str(it.get("jibun","") or it.get("지번","")).strip()
                name  = str(it.get("offiNm","") or it.get("mhouseNm","")
                            or it.get("건물명","")).strip()
                area  = safe_to_float(it.get("excluUseAr") or it.get("전용면적"))
                dep   = str(it.get("deposit","") or it.get("보증금액","")).replace(",","").strip()
                rent  = str(it.get("monthlyRent","") or it.get("월세금액","")).replace(",","").strip()
                floor = str(it.get("floor","") or it.get("층","")).strip()
                dep_val  = safe_to_float(dep)  or 0.0
                rent_val = safe_to_float(rent) or 0.0
                conv_dep = dep_val + rent_val * 200
                all_rows.append({
                    "housing_type":        htype,
                    "rent_type":           "전세" if rent_val == 0 else "월세",
                    "gu":                  gu_name,
                    "dong":                dong,
                    "jibun":               jibun,
                    "building_name":       name,
                    "area_m2":             area,
                    "floor":               floor,
                    "deposit_manwon":      dep_val,
                    "monthly_rent_manwon": rent_val,
                    "conv_deposit_manwon": conv_dep,
                    "deal_ym":             ym,
                    "geocode_addr":        f"서울특별시 {gu_name} {dong} {jibun}".strip(),
                })
            time.sleep(SLEEP_BETWEEN_CALLS)

    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df = df.dropna(subset=["area_m2","conv_deposit_manwon"])
    df = df[df["area_m2"] > 0]
    df["price_per_m2"] = (df["conv_deposit_manwon"] / df["area_m2"]).round(2)
    return df.reset_index(drop=True)


def stage2_geocode_and_filter(molit_df, budget_manwon, kakao_local_key,
                               rent_type: str = "", monthly_rent_manwon: float = 0.0):
    if molit_df.empty: return molit_df
    # 계약 유형 필터 (월세/전세 구분)
    if rent_type in ("월세", "전세") and "rent_type" in molit_df.columns:
        molit_df = molit_df[molit_df["rent_type"] == rent_type].copy()
    if molit_df.empty: return molit_df
    # 월세 금액 필터
    if rent_type == "월세" and monthly_rent_manwon > 0 and "monthly_rent_manwon" in molit_df.columns:
        molit_df = molit_df[molit_df["monthly_rent_manwon"] <= monthly_rent_manwon].copy()
    if molit_df.empty: return molit_df
    # 환산보증금 예산 필터
    if budget_manwon is not None:
        molit_df = molit_df[molit_df["conv_deposit_manwon"] <= budget_manwon].copy()
    if molit_df.empty: return molit_df
    unique_addrs = molit_df["geocode_addr"].drop_duplicates().tolist()
    addr_coord = {}
    for addr in unique_addrs:
        try:
            x, y, matched = geocode_address_kakao(addr, kakao_local_key)
            addr_coord[addr] = (x, y, matched)
        except:
            addr_coord[addr] = (None, None, None)
        time.sleep(SLEEP_BETWEEN_CALLS)
    molit_df = molit_df.copy()
    molit_df["cand_x"]          = molit_df["geocode_addr"].map(lambda a: addr_coord.get(a,(None,None,None))[0])
    molit_df["cand_y"]          = molit_df["geocode_addr"].map(lambda a: addr_coord.get(a,(None,None,None))[1])
    molit_df["matched_address"] = molit_df["geocode_addr"].map(lambda a: addr_coord.get(a,(None,None,None))[2])
    molit_df["geocode_ok"]      = molit_df["cand_x"].notna()
    return molit_df.reset_index(drop=True)


# =========================================================
# 11. 주거비 CSV 로드
# =========================================================
def load_housing_data(csv_path):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"주거비 CSV 없음: {csv_path}")
    # gz 파일은 pandas가 자동 압축 해제 — encoding만 지정
    if str(csv_path).endswith(".gz"):
        df = pd.read_csv(csv_path, encoding="utf-8", encoding_errors="replace")
    else:
        for enc in ["utf-8-sig", "cp949", "euc-kr"]:
            try:
                df = pd.read_csv(csv_path, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            import io as _io
            with open(csv_path, "rb") as _f:
                _content = _f.read().decode("utf-8-sig", errors="replace")
            df = pd.read_csv(_io.StringIO(_content))
    required = ["시도","시군구_2","읍면동","환산보증금(만원)","㎡당환산보증금_재계산(만원)"]
    missing  = [c for c in required if c not in df.columns]
    if missing: raise ValueError(f"CSV 필수 컬럼 없음: {missing}")
    df["환산보증금(만원)"]           = pd.to_numeric(df["환산보증금(만원)"],           errors="coerce")
    df["㎡당환산보증금_재계산(만원)"] = pd.to_numeric(df["㎡당환산보증금_재계산(만원)"], errors="coerce")
    return df


# =========================================================
# 12. 예상 통근시간 추정 (직선거리 기반, API 없음)
# ─────────────────────────────────────────────────────────
# 동 중심 좌표(위도,경도) → 직장까지 직선거리
# → 도로굴곡계수 × 이동수단 평균속도로 예상 소요시간 산출
# 혼잡계수도 적용해 출근/퇴근 시간대를 반영
# =========================================================
def estimate_commute_min(dist_km: float, transport_mode: str,
                          hour: int) -> float:
    speed = CAR_AVG_SPEED_KMH if transport_mode == "car" else TRANSIT_AVG_SPEED_KMH
    base  = dist_km * ROAD_CURVE_FACTOR / speed * 60
    return round(base, 1)


# =========================================================
# 13. 1단계: 구 단위 필터링 → 상위 N개 구 선정
# ─────────────────────────────────────────────────────────
# 예산 필터 + 구별 평균 주거비 + 구 중심 직선거리
# 복잡한 통계 없음, 순수 CSV 연산
# =========================================================
def stage1_filter_gu(df: pd.DataFrame,
                     budget_manwon: Optional[float],
                     region_filter: Optional[str],
                     work_x: float, work_y: float) -> pd.DataFrame:

    # 서울 전체 데이터 기준으로 점수 계산 (지역 필터 전)
    seoul_all = df[df["시도"].astype(str).str.contains("서울", na=False)].copy()
    if budget_manwon is not None:
        seoul_budget = seoul_all[seoul_all["환산보증금(만원)"] <= budget_manwon].copy()
    else:
        seoul_budget = seoul_all.copy()

    if seoul_budget.empty:
        raise ValueError("1단계 후보 없음: 예산 조건을 완화해 주세요.")

    # 구 단위 집계 (서울 전체 기준)
    gu_agg = (
        seoul_budget.groupby("시군구_2", dropna=False)
        .agg(
            sample_count    = ("환산보증금(만원)",             "size"),
            median_deposit  = ("환산보증금(만원)",             "median"),
            median_price_m2 = ("㎡당환산보증금_재계산(만원)", "median"),
        )
        .reset_index()
    )
    gu_agg = gu_agg.dropna(subset=["시군구_2","median_deposit","median_price_m2"])

    # 구 중심 직선거리
    def gu_dist(gu_name):
        coord = GU_CENTER.get(str(gu_name).strip())
        return haversine_km(work_y, work_x, coord[0], coord[1]) if coord else 99.0

    gu_agg["dist_km"]    = gu_agg["시군구_2"].apply(gu_dist)
    gu_agg["dist_score"] = min_max_scale_inverse(gu_agg["dist_km"])

    # 주거비 점수 — 서울 전체 기준 (지역 필터 전에 계산해야 비교 기준이 일정)
    sp = min_max_scale_inverse(gu_agg["median_price_m2"])
    sd = min_max_scale_inverse(gu_agg["median_deposit"])
    gu_agg["housing_score"] = (sp*0.6 + sd*0.4).round(2)

    # 선정점수
    gu_agg["selection_score"] = (
        gu_agg["dist_score"]*0.5 + gu_agg["housing_score"]*0.5
    ).round(2)

    # 지역 필터는 점수 계산 후에 적용 (점수 기준은 서울 전체로 유지)
    if region_filter:
        rf = region_filter.strip()
        gu_agg = gu_agg[gu_agg["시군구_2"].astype(str).str.contains(rf, na=False)]

    if gu_agg.empty:
        raise ValueError(f"1단계 후보 없음: '{region_filter}' 지역에 해당하는 구가 없습니다.")

    result = gu_agg.sort_values("selection_score", ascending=False).head(STAGE1_TOP_GU)
    return result.reset_index(drop=True)


# =========================================================
# 14. 2단계: 동 단위 필터링 → 상위 10개 동 선정
# ─────────────────────────────────────────────────────────
# 1단계에서 선정된 구 내 모든 동을 대상으로
# 동 중심 좌표(카카오 지오코딩) 기반 예상 통근시간 계산
# 사용자 가중치에 따라 선정 방식 결정:
#   통근 우선: 예상통근시간 기준 상위 10개 → 그 안에서 주거비 점수
#   주거비 우선: 주거비 기준 상위 10개 → 그 안에서 예상통근시간 필터
#   동일(5:5): 복합점수(직선거리50% + 주거비50%)로 상위 10개
#
# 단점 개선:
#   - 통근시간 예상값 = 직선거리 × 도로굴곡계수 ÷ 평균속도 × 혼잡계수
#     (API 없음, 필터링 용도로 충분)
#   - 주거비 우선이어도 허용통근시간 초과 동 먼저 제거 (절대 기준 강제)
#   - 필터 후 10개 미만 시 여유분 확대해서 fallback
# =========================================================
def stage2_filter_dong(df: pd.DataFrame,
                        selected_gu: pd.DataFrame,
                        budget_manwon: Optional[float],
                        work_x: float, work_y: float,
                        allowed_commute_min: int,
                        transport_mode: str,
                        depart_hour: int,
                        weight_commute: float,
                        weight_housing: float,
                        kakao_local_key: str) -> pd.DataFrame:

    # 선정된 구 목록
    gu_list = selected_gu["시군구_2"].tolist()

    # 해당 구의 동 단위 데이터 추출
    # 주거비 점수는 서울 전체 기준으로 계산 후 구 필터 적용
    seoul_all = df[df["시도"].astype(str).str.contains("서울", na=False)].copy()
    if budget_manwon is not None:
        seoul_all = seoul_all[seoul_all["환산보증금(만원)"] <= budget_manwon].copy()

    # 서울 전체 동 집계 (점수 기준 통일)
    dong_all = (
        seoul_all.groupby(["시군구_2","읍면동"], dropna=False)
        .agg(
            sample_count    = ("환산보증금(만원)",             "size"),
            median_deposit  = ("환산보증금(만원)",             "median"),
            median_price_m2 = ("㎡당환산보증금_재계산(만원)", "median"),
        )
        .reset_index()
    )
    dong_all = dong_all.dropna(subset=["시군구_2","읍면동","median_deposit","median_price_m2"])

    # 서울 전체 기준으로 주거비 점수 계산
    sp_all = min_max_scale_inverse(dong_all["median_price_m2"])
    sd_all = min_max_scale_inverse(dong_all["median_deposit"])
    dong_all["housing_score"] = (sp_all*0.6 + sd_all*0.4).round(2)

    # 이후 선정된 구로 필터링 (점수는 이미 전체 기준으로 계산됨)
    work = dong_all[dong_all["시군구_2"].isin(gu_list)].copy()

    if work.empty:
        return pd.DataFrame()

    work["candidate_address"] = "서울특별시 " + work["시군구_2"] + " " + work["읍면동"]
    dong_agg = work.copy()

    # 동 중심 좌표 지오코딩 (카카오)
    coords = {}
    for addr in dong_agg["candidate_address"].tolist():
        try:
            x, y, _ = geocode_address_kakao(addr, kakao_local_key)
            coords[addr] = (x, y)
        except:
            coords[addr] = (None, None)
        time.sleep(SLEEP_BETWEEN_CALLS)

    dong_agg["dong_x"] = dong_agg["candidate_address"].map(lambda a: coords.get(a,(None,None))[0])
    dong_agg["dong_y"] = dong_agg["candidate_address"].map(lambda a: coords.get(a,(None,None))[1])

    # 지오코딩 실패 동 제거
    dong_agg = dong_agg[dong_agg["dong_x"].notna()].copy()
    if dong_agg.empty:
        return pd.DataFrame()

    # 직선거리 + 예상 통근시간 계산
    def calc_dist(row):
        return haversine_km(work_y, work_x, float(row["dong_y"]), float(row["dong_x"]))

    dong_agg["dist_km"] = dong_agg.apply(calc_dist, axis=1)
    dong_agg["est_commute_min"] = dong_agg["dist_km"].apply(
        lambda d: estimate_commute_min(d, transport_mode, depart_hour)
    )

    # 통근 점수 (예상시간 기반)
    dong_agg["commute_score"] = dong_agg["est_commute_min"].apply(
        lambda x: round(commute_score(x, allowed_commute_min), 2)
    )

    # ── 단점 개선: 허용 통근시간 초과 동 먼저 제거 (절대 기준 강제) ──
    def filter_by_commute(df_in, buffer_min):
        limit = allowed_commute_min + buffer_min
        filtered = df_in[df_in["est_commute_min"] <= limit].copy()
        return filtered

    candidates = filter_by_commute(dong_agg, COMMUTE_BUFFER_MIN)

    # fallback: 기본 여유분으로 10개 미만이면 여유분 확대
    if len(candidates) < STAGE2_TOP_DONG:
        candidates = filter_by_commute(dong_agg, COMMUTE_BUFFER_FALLBACK_MIN)

    # 그래도 부족하면 전체 사용
    if candidates.empty:
        candidates = dong_agg.copy()

    # ── 가중치에 따른 동 선정 방식 ────────────────────────
    if weight_commute == weight_housing:
        # 방법 A (5:5): commute_score + housing_score 복합 점수
        candidates["selection_score"] = (
            candidates["commute_score"] * 0.5 + candidates["housing_score"] * 0.5
        ).round(2)
        top_dong = candidates.sort_values("selection_score", ascending=False).head(STAGE2_TOP_DONG)

    elif weight_commute > weight_housing:
        # 통근 우선: 예상통근시간 기준 상위 선정 → 그 안에서 주거비 점수
        top_dong = (
            candidates.sort_values("est_commute_min")
            .head(STAGE2_TOP_DONG)
        )
        top_dong = top_dong.sort_values("housing_score", ascending=False)

    else:
        # 주거비 우선: 주거비 기준 상위 선정 → 그 안에서 통근 점수
        top_dong = (
            candidates.sort_values("housing_score", ascending=False)
            .head(STAGE2_TOP_DONG)
        )
        top_dong = top_dong.sort_values("commute_score", ascending=False)

    return top_dong.reset_index(drop=True)




# =========================================================
# 14. ODsay subPath 파싱
# =========================================================
def extract_transit_features(sub_paths):
    bus_stops, subway_stns, bus_routes, transfer_stns = [],[],[],[]
    prev = None
    for sp in sub_paths:
        if not isinstance(sp,dict): continue
        tt = sp.get("trafficType"); sn = str(sp.get("startName","")).strip()
        if tt == 2:
            if sn: bus_stops.append(sn)
            for li in ensure_list(sp.get("lane",[])):
                if isinstance(li,dict):
                    for k in ["busNo","name","busNo1"]:
                        if li.get(k): bus_routes.append(str(li[k]).strip()); break
            if prev in (1,2) and sn: transfer_stns.append(sn)
            prev = 2
        elif tt == 1:
            if sn: subway_stns.append(sn)
            if prev in (1,2) and sn: transfer_stns.append(sn)
            prev = 1
    return {
        "bus_stop_names":         list(dict.fromkeys(bus_stops)),
        "subway_station_names":   list(dict.fromkeys(subway_stns)),
        "bus_route_nos":          list(dict.fromkeys(bus_routes)),
        "transfer_station_names": list(dict.fromkeys(transfer_stns)),
    }




# =========================================================
# 15. 통근시간 계산 (출근 + 퇴근, 혼잡계수 적용)
# =========================================================
def calc_commute_both_ways(cand_x, cand_y, work_x, work_y,
                            transport_mode, depart_hour, depart_min,
                            arrive_hour, arrive_min):
    result = {}

    if transport_mode == "car":
        am_dep = _next_weekday_at(depart_hour, depart_min)
        pm_dep = _next_weekday_at(arrive_hour, arrive_min)

        # 출근: 집→직장
        try:
            am     = get_drive_route_kakaomobility(cand_x, cand_y, work_x, work_y, KAKAO_MOBILITY_REST_API_KEY, departure_time=am_dep)
            am_min = round(am["duration_sec"]/60, 2) if am["duration_sec"] else None
        except Exception as e:
            am_min = None; result["am_error"] = str(e)
            print(f"  [카카오모빌리티 출근 오류] {str(e)[:100]}")

        # 퇴근: 직장→집
        try:
            pm     = get_drive_route_kakaomobility(work_x, work_y, cand_x, cand_y, KAKAO_MOBILITY_REST_API_KEY, departure_time=pm_dep)
            pm_min = round(pm["duration_sec"]/60, 2) if pm["duration_sec"] else None
        except Exception as e:
            pm_min = None; result["pm_error"] = str(e)
            print(f"  [카카오모빌리티 퇴근 오류] {str(e)[:100]}")

        valid = [v for v in [am_min, pm_min] if v]
        result.update({
            "commute_time_am_min": am_min,
            "commute_time_pm_min": pm_min,
            "commute_time_min":    round(max(valid), 2) if valid else None,
            "car_coeff_am":        None,
            "car_coeff_pm":        None,
            "distance_m":          am.get("distance_m") if "am" in dir() and am_min else None,
            "api_ok":              am_min is not None,
        })

    else:  # transit — ODsay pathTime 그대로 사용
        def _transit(ox, oy, dx, dy):
            return get_transit_route_odsay(ox, oy, dx, dy, ODSAY_API_KEY)

        am_r = pm_r = None

        try:
            am_r = _transit(cand_x, cand_y, work_x, work_y)
        except Exception as e:
            err_str = str(e)
            if "-98" in err_str:
                dist_m   = haversine_km(work_y, work_x, cand_y, cand_x) * 1000
                walk_min = round(dist_m / 80, 1)
                am_r = {"total_time_min": walk_min, "total_walk_m": dist_m,
                        "bus_transit_count": 0, "subway_transit_count": 0, "sub_paths": []}
                print(f"  [도보 처리] 700m 이내 → 도보 약 {walk_min:.0f}분")
            else:
                result["am_error"] = err_str
                print(f"  [ODsay 출근 오류] {err_str[:100]}")

        try:
            pm_r = _transit(work_x, work_y, cand_x, cand_y)
        except Exception as e:
            err_str = str(e)
            if "-98" in err_str:
                dist_m   = haversine_km(work_y, work_x, cand_y, cand_x) * 1000
                walk_min = round(dist_m / 80, 1)
                pm_r = {"total_time_min": walk_min, "total_walk_m": dist_m,
                        "bus_transit_count": 0, "subway_transit_count": 0, "sub_paths": []}
            else:
                result["pm_error"] = err_str
                print(f"  [ODsay 퇴근 오류] {err_str[:100]}")

        am_min = am_r["total_time_min"] if am_r else None
        pm_min = pm_r["total_time_min"] if pm_r else None
        if am_min is not None: am_min = round(am_min, 2)
        if pm_min is not None: pm_min = round(pm_min, 2)

        am_ft = extract_transit_features(am_r.get("sub_paths", [])) if am_r else None
        valid  = [v for v in [am_min, pm_min] if v]
        result.update({
            "commute_time_am_min":          am_min,
            "commute_time_pm_min":          pm_min,
            "commute_time_min":             round(max(valid), 2) if valid else None,
            "basic_commute_time_min":       am_min,
            "transfer_count":               (safe_to_int(am_r.get("bus_transit_count"), 0)
                                             + safe_to_int(am_r.get("subway_transit_count"), 0)) if am_r else None,
            "walk_distance_m":              am_r.get("total_walk_m") if am_r else None,
            "bus_stop_names_used":          ", ".join(am_ft.get("bus_stop_names", []))          if am_ft else None,
            "subway_station_names_used":    ", ".join(am_ft.get("subway_station_names", []))    if am_ft else None,
            "transfer_station_names_used":  ", ".join(am_ft.get("transfer_station_names", []))  if am_ft else None,
            "bus_route_nos_used":           ", ".join(map(str, am_ft.get("bus_route_nos", []))) if am_ft else None,
            "api_ok":                       am_min is not None,
        })

    return result


# =========================================================
# TOPSIS + 가격 적정성 함수 (v5 신규)
# =========================================================

# =========================================================
# 16. 2단계 신뢰도 계산 (매물 단위 CV)
# ─────────────────────────────────────────────────────────
# 1단계에서는 하지 않고, 2단계 실거래 매물들을 동 단위로 묶어서 계산.
# 같은 동의 매물들끼리 비교하므로 전세/월세/반지하가 섞인 1단계 집계보다 의미있음.
# =========================================================

# =========================================================
# 16-1. 생활 인프라 점수 (카카오 Local 카테고리 검색)
# =========================================================
INFRA_CATEGORY_MAP = {
    "편의점":  "CS2",
    "병원":    "HP8",
    "약국":    "PM9",
    "마트":    "MT1",
    "카페":    "CE7",
    "헬스장":  "SW8",
    "공원":    "AT4",
}

INFRA_DISTANCE_SCORE = [
    (300,  100),   # 300m 이내 → 100점
    (500,   60),   # 500m 이내 →  60점
    (1000,  30),   # 1km  이내 →  30점
]  # 1km 초과 → 0점


def _get_nearest_infra_distance(x, y, category_code, kakao_key, radius=1000):
    """
    카카오 Local 카테고리 검색으로 반경 내 가장 가까운 시설 거리(m) 반환.
    없으면 None 반환.
    """
    try:
        resp = _kakao_request(
            "Kakao Infra",
            "https://dapi.kakao.com/v2/local/search/category.json",
            headers={"Authorization": f"KakaoAK {kakao_key}"},
            params={
                "category_group_code": category_code,
                "x": x, "y": y,
                "radius": radius,
                "sort": "distance",
                "size": 1,
            },
        )
        if resp.status_code != 200:
            return None
        docs = resp.json().get("documents", [])
        if not docs:
            return None
        return float(docs[0].get("distance", 9999))
    except Exception:
        return None


def _distance_to_score(dist_m):
    """거리(m) → 인프라 점수 (0/30/60/100)"""
    if dist_m is None:
        return 0
    for threshold, score in INFRA_DISTANCE_SCORE:
        if dist_m <= threshold:
            return score
    return 0


def calc_infra_score(x, y, required_infra, kakao_key, vibe_weights=None):
    """
    매물 좌표(x, y)에서 필수 편의시설 반경 조회.

    Parameters
    ----------
    x, y           : 건물 좌표 (경도, 위도)
    required_infra : 사용자가 선택한 필수 편의시설 이름 리스트
                     예) ["편의점", "병원"]
    kakao_key      : 카카오 REST API 키

    Returns
    -------
    dict:
        infra_score      : 0~100 점 (필수 시설 거리 점수 평균)
        infra_detail     : {시설명: (거리m, 점수)} 딕셔너리
        infra_fail       : 1km 초과로 탈락한 시설 이름 리스트 (비어있으면 통과)
    """
    if not required_infra:
        return {"infra_score": 100, "infra_detail": {}, "infra_fail": []}

    detail = {}
    fail   = []

    for name in required_infra:
        code = INFRA_CATEGORY_MAP.get(name)
        if not code:
            continue
        dist  = _get_nearest_infra_distance(x, y, code, kakao_key)
        score = _distance_to_score(dist)
        detail[name] = (dist, score)
        if score == 0:          # 1km 초과 또는 없음
            fail.append(name)
        time.sleep(SLEEP_BETWEEN_CALLS)

    if vibe_weights:
        raw_scores = {name: s for name, (_, s) in detail.items()}
        avg = apply_vibe_to_infra_scores(raw_scores, vibe_weights)
    else:
        scores = [s for (_, s) in detail.values()]
        avg    = round(sum(scores) / len(scores), 2) if scores else 0

    return {
        "infra_score":  avg,
        "infra_detail": detail,
        "infra_fail":   fail,
    }


def topsis_score(df, weight_commute, weight_housing, weight_infra=0.0, weight_policy=0.0):
    """Fuzzy TOPSIS (v6.0 — 3축: 통근/주거비/인프라).

    weight_policy 파라미터는 호환용으로 유지하지만 무시됩니다.
    청년정책은 매물별 표시용으로만 매칭되고 추천 점수에 반영하지 않습니다.
    """
    import numpy as np
    c  = df["commute_score"].fillna(0)
    h  = df["housing_score"].fillna(0)
    wc, wh, wi = weight_commute, weight_housing, weight_infra
    # weight_policy는 v6.0부터 무시 — 정책은 추천 점수에 반영 X

    # 축 목록 구성 (가중치 > 0인 축만 포함)
    axes = [(c, wc)]   # 통근
    axes.append((h, wh))  # 주거비

    if wi > 0 and "infra_score" in df.columns:
        axes.append((df["infra_score"].fillna(0), wi))

    # TOPSIS 계산 (N축 일반화)
    d_best_sq  = None
    d_worst_sq = None
    for vals, w in axes:
        best_v  = vals.max() * w
        worst_v = vals.min() * w
        comp = (vals * w)
        b = (comp - best_v) ** 2
        wr = (comp - worst_v) ** 2
        d_best_sq  = b  if d_best_sq  is None else d_best_sq  + b
        d_worst_sq = wr if d_worst_sq is None else d_worst_sq + wr

    d_best  = d_best_sq.pow(0.5)
    d_worst = d_worst_sq.pow(0.5)

    total = d_best + d_worst
    score = d_worst / total.replace(0, float("nan"))
    return (score.fillna(0) * 100).round(2)


def calc_price_context(mrow, dong_molit_df):
    """가격 적정성: 거래 최신성 + 동네 중위가 대비 + 유사 면적 범위"""
    result = {}
    area   = safe_to_float(mrow.get("area_m2"))
    conv   = safe_to_float(mrow.get("conv_deposit_manwon"))
    deal_ym = str(mrow.get("deal_ym", ""))
    if deal_ym:
        try:
            now = datetime.datetime.now()
            deal_dt = datetime.datetime.strptime(deal_ym, "%Y%m")
            m = (now.year - deal_dt.year)*12 + (now.month - deal_dt.month)
            if m <= 1:   result["deal_recency"] = f"{m}개월 전 거래 (최신)"
            elif m <= 3: result["deal_recency"] = f"{m}개월 전 거래 (보통)"
            else:        result["deal_recency"] = f"{m}개월 전 거래 (오래됨)"
        except: pass
    if dong_molit_df.empty or conv is None: return result
    dong_prices = dong_molit_df["conv_deposit_manwon"].dropna()
    if len(dong_prices) >= 2:
        med = dong_prices.median()
        if med > 0:
            d = (conv - med)/med*100
            lvl = "비슷" if abs(d)<10 else ("저렴" if d<0 else "고가")
            result["vs_dong_median"] = f"동네 중위가 대비 {'+' if d>=0 else ''}{d:.0f}% ({lvl})"
    if area is not None:
        sim = dong_molit_df[
            (dong_molit_df["area_m2"] >= area-10) &
            (dong_molit_df["area_m2"] <= area+10)
        ]["conv_deposit_manwon"].dropna()
        if len(sim) >= 1:
            result["similar_area_range"] = (
                f"유사면적({area-10:.0f}~{area+10:.0f}㎡) 최근 {len(sim)}건: "
                f"{fmt_manwon(sim.min())}~{fmt_manwon(sim.max())}만원"
            )
    return result

def calc_stage2_confidence(dong_df: pd.DataFrame) -> str:
    if dong_df.empty or len(dong_df) < 2:
        return "낮음 (매물 1건)"
    prices = dong_df["conv_deposit_manwon"].dropna()
    if len(prices) < 2:
        return "낮음 (가격 데이터 부족)"
    mean = prices.mean()
    if mean == 0:
        return "낮음 (평균 0)"
    cv = prices.std() / mean * 100
    n  = len(prices)
    if n >= 10 and cv < 30:
        return "높음 (가격 균일)"
    elif n >= 5 and cv < 60:
        return "보통 (가격 다양)"
    elif n >= 10 and cv < 60:
        return "보통 (가격 다양)"
    else:
        return f"낮음 (가격 편차 큼, CV={cv:.0f}%)"


def apply_bayesian_smoothing(dong_df: pd.DataFrame,
                              global_mean_price: float,
                              global_mean_dep: float) -> pd.DataFrame:
    """2단계 매물에 베이즈 평활화 적용 — 소수 매물 동네 과대평가 방지"""
    k = BAYESIAN_K
    n = len(dong_df)
    dong_df = dong_df.copy()
    # ㎡당 가격 평활화
    raw_ppm = dong_df["price_per_m2"].mean() if "price_per_m2" in dong_df else global_mean_price
    dong_df["smoothed_price_per_m2"] = (raw_ppm * n + global_mean_price * k) / (n + k)
    # 환산보증금 평활화
    raw_dep = dong_df["conv_deposit_manwon"].mean()
    dong_df["smoothed_deposit"] = (raw_dep * n + global_mean_dep * k) / (n + k)
    return dong_df


# =========================================================
# 17. 특징 태그
# =========================================================
def generate_feature_tags(row: pd.Series, all_df: pd.DataFrame) -> str:
    tags = []
    ct  = row.get("commute_time_min")
    ppm = row.get("price_per_m2") or row.get("smoothed_price_per_m2")

    if ct is not None and not pd.isna(ct):
        ct_rank = (all_df["commute_time_min"].dropna() > ct).mean()
        if ct_rank >= 0.85:   tags.append("통근 최단거리")
        elif ct_rank >= 0.70: tags.append("이동이 편리함")

    if ppm is not None and not pd.isna(ppm):
        col = "price_per_m2" if "price_per_m2" in all_df.columns else "smoothed_price_per_m2"
        pr_rank = (all_df[col].dropna() > ppm).mean()
        if pr_rank >= 0.85:   tags.append("가성비 최상위")
        elif pr_rank >= 0.70: tags.append("가성비 우수")

    if ct is not None and ppm is not None:
        ct_ok  = (all_df["commute_time_min"].dropna() > ct).mean() >= 0.65
        ppm_ok = (all_df.get("price_per_m2", pd.Series(dtype=float)).dropna() > ppm).mean() >= 0.65
        if ct_ok and ppm_ok: tags.append("통근+가격 균형")

    ht   = row.get("housing_type","")
    conf = row.get("data_confidence","")
    if ht: tags.append(ht)
    if "낮음" in conf: tags.append("⚠ 데이터 적음")

    return " / ".join(tags) if tags else "특징 없음"


# =========================================================
# 18. 최종 출력
# =========================================================
def print_final_results(final_df, transport_mode,
                         depart_h, depart_m, arrive_h, arrive_m,
                         seoul_avg_ppm=None, weight_commute=0.5, weight_housing=0.5,
                         weight_infra=0.0, allowed_commute_min=90,
                         user_info=None, weight_policy=0.0,
                         max_policy_display=3, monthly_rent_manwon=50.0):
    print("\n" + "="*70)
    print(" 최종 주거지 추천 결과  (v5 — TOPSIS 점수 기준)")
    infra_str = f" + 인프라 {weight_infra*100:.0f}%" if weight_infra > 0 else ""
    policy_str = f" + 정책 {weight_policy*100:.0f}%" if weight_policy > 0 else ""
    print(f" 종합점수 기준: 통근시간 {weight_commute*100:.0f}% + 주거비 {weight_housing*100:.0f}%{infra_str}{policy_str}  (100점 만점)")
    print("="*70)

    all_commute = final_df["commute_time_min"].dropna()
    all_ppm     = final_df["price_per_m2"].dropna() if "price_per_m2" in final_df.columns else pd.Series(dtype=float)

    for i, row in final_df.iterrows():
        addr   = row.get("display_address") or row.get("matched_address") or row.get("candidate_address","")
        htype  = row.get("housing_type","")
        src    = row.get("data_source","CSV")
        is_api = str(src) == "국토부API"

        am_min = row.get("commute_time_am_min")
        pm_min = row.get("commute_time_pm_min")
        worst  = row.get("commute_time_min")

        rent_type = row.get("rent_type","")
        dep_val   = row.get("deposit_manwon")
        rent_val  = row.get("monthly_rent_manwon")
        conv_dep  = row.get("conv_deposit_manwon")
        dep_med   = row.get("median_deposit")
        area_m2   = row.get("area_m2")
        ppm       = row.get("price_per_m2") or row.get("smoothed_price_per_m2")
        sample_cnt= row.get("sample_count")
        tags      = row.get("feature_tags","")
        score     = row.get("final_score","")
        commute_sc= row.get("commute_score", 0)
        housing_sc= row.get("housing_score", 0)

        car_coeff_am = row.get("car_coeff_am")
        car_coeff_pm = row.get("car_coeff_pm")
        tr_coeff_am  = row.get("transit_coeff_am")
        tr_coeff_pm  = row.get("transit_coeff_pm")

        # 가격 적정성 정보
        deal_recency      = row.get("deal_recency","")
        vs_dong_median    = row.get("vs_dong_median","")
        similar_area_range= row.get("similar_area_range","")

        print(f"\n{'─'*70}")
        print(f"  순위 {i+1}.  {addr}")
        print(f"{'─'*70}")

        info_parts = []
        if htype:     info_parts.append(f"주택 유형: {htype}")
        if rent_type: info_parts.append(f"계약 형태: {rent_type}")
        if area_m2:   info_parts.append(f"전용면적: {fmt_area(area_m2)}")
        if info_parts:
            print("  [주택 정보]  " + "  |  ".join(info_parts))

        # ── 통근 시간 ─────────────────────────────────────
        print("  [통근 시간]")
        has_commute = False
        if am_min is not None and not (isinstance(am_min, float) and pd.isna(am_min)):
            coeff_str = f"  혼잡계수 {(car_coeff_am or tr_coeff_am):.1f}×" if (car_coeff_am or tr_coeff_am) else ""
            print(f"    · 출근: {am_min:.0f}분  ({depart_h:02d}:{depart_m:02d} 출발 기준{coeff_str})")
            has_commute = True
        if pm_min is not None and not (isinstance(pm_min, float) and pd.isna(pm_min)):
            coeff_str = f"  혼잡계수 {(car_coeff_pm or tr_coeff_pm):.1f}×" if (car_coeff_pm or tr_coeff_pm) else ""
            print(f"    · 퇴근: {pm_min:.0f}분  ({arrive_h:02d}:{arrive_m:02d} 출발 기준{coeff_str})")
            has_commute = True
        if worst is not None and not (isinstance(worst, float) and pd.isna(worst)):
            print(f"    · 최대 통근시간: {worst:.0f}분")
        if not has_commute:
            est = row.get("est_commute_min") or row.get("commute_time_min")
            if est:
                print(f"    · 예상 통근시간: 약 {est:.0f}분 (직선거리 기반 추정, 참고용)")
            else:
                print(f"    · 통근시간 계산 불가 (API 미응답 — 참고용)")

        if transport_mode == "transit":
            basic_min   = row.get("basic_commute_time_min")
            transfer_cnt= row.get("transfer_count")
            walk_m      = row.get("walk_distance_m")
            bus_wait    = row.get("bus_wait_min")
            subway_wait = row.get("subway_wait_min")
            tr_pen      = row.get("transfer_penalty_min")
            bus_cong    = row.get("bus_congestion_penalty_min")
            sub_cong    = row.get("subway_congestion_penalty_min")
            if am_min is not None and basic_min is not None:
                try:
                    extra = float(am_min) - float(basic_min)
                    parts = []
                    if bus_wait    and float(bus_wait)    > 0: parts.append(f"버스대기 {float(bus_wait):.1f}분")
                    if subway_wait and float(subway_wait) > 0: parts.append(f"지하철대기 {float(subway_wait):.1f}분")
                    if tr_pen      and float(tr_pen)      > 0: parts.append(f"환승이동 {float(tr_pen):.1f}분")
                    if bus_cong    and float(bus_cong)    > 0: parts.append(f"버스혼잡 {float(bus_cong):.1f}분")
                    if sub_cong    and float(sub_cong)    > 0: parts.append(f"지하철혼잡 {float(sub_cong):.1f}분")
                    print(f"    · 순수 이동시간: {float(basic_min):.0f}분")
                    print(f"    · 체감 증가시간: +{extra:.0f}분  ({' + '.join(parts) if parts else '-'})")
                except: pass
            if transfer_cnt is not None and not pd.isna(transfer_cnt):
                print(f"    · 환승 횟수: {int(transfer_cnt)}회")
            if walk_m and not pd.isna(walk_m) and float(walk_m) > 0:
                print(f"    · 도보 거리: {float(walk_m):.0f}m")

        # ── 비용 정보 ─────────────────────────────────────
        print("  [비용 정보]")
        if is_api and rent_type:
            if rent_type == "전세":
                if dep_val:  print(f"    · 전세 보증금: {fmt_manwon(dep_val)}만원")
                if conv_dep: print(f"    · 환산보증금: {fmt_manwon(conv_dep)}만원")
            else:
                if dep_val is not None and rent_val is not None:
                    print(f"    · 보증금: {fmt_manwon(dep_val)}만원 / 월세: {fmt_manwon(rent_val)}만원")
                if conv_dep:
                    print(f"    · 환산보증금 (보증금 + 월세×200): {fmt_manwon(conv_dep)}만원")
        else:
            if dep_med: print(f"    · 이 동네 중간 가격 (중위값): {fmt_manwon(dep_med)}만원")
        if ppm is not None and not pd.isna(ppm):
            ppm_str = f"{fmt_manwon(ppm)}만원/㎡"
            if seoul_avg_ppm and seoul_avg_ppm > 0:
                diff = (ppm - seoul_avg_ppm) / seoul_avg_ppm * 100
                ppm_str += f"  (서울 평균 대비 {'+' if diff>=0 else ''}{diff:.0f}%)"
            print(f"    · 면적당 환산보증금: {ppm_str}")

        # ── 가격 분석 (v5 신규) ───────────────────────────
        if is_api and any([deal_recency, vs_dong_median, similar_area_range]):
            print("  [가격 분석]")
            if deal_recency:       print(f"    · 거래 최신성:  {deal_recency}")
            if vs_dong_median:     print(f"    · 시세 적정성:  {vs_dong_median}")
            if similar_area_range: print(f"    · 유사 조건:    {similar_area_range}")

        # ── 추천 이유 (v5 신규) ───────────────────────────
        print("  [추천 이유]")
        if worst is not None and not pd.isna(worst) and len(all_commute) > 1:
            ct_pct = int((all_commute > worst).mean() * 100)
            print(f"    · 통근시간 {worst:.0f}분 — 추천 결과 중 상위 {ct_pct}%")
        if conv_dep is not None and not pd.isna(conv_dep):
            if dep_med:
                ratio = conv_dep / float(dep_med) * 100
                print(f"    · 환산보증금 {fmt_manwon(conv_dep)}만원 — 동네 중위 대비 {ratio:.0f}%")
        if commute_sc and housing_sc:
            print(f"    · 통근점수 {commute_sc:.0f}점 / 주거비점수 {housing_sc:.0f}점")
            dominant = "통근시간" if weight_commute >= weight_housing else "주거비"
            print(f"    · {dominant} {max(weight_commute,weight_housing)*100:.0f}% 우선 기준으로 선정")

        # ── 생활 인프라 (인프라 선택 시) ─────────────────────
        infra_detail = row.get("infra_detail", {})
        infra_score  = row.get("infra_score")
        if infra_detail:
            print("  [생활 인프라]")
            if infra_score is not None:
                print(f"    · 인프라 점수: {infra_score:.0f}점")
            for name, (dist, sc) in infra_detail.items():
                if dist is None:
                    marker = "❌ 1km 내 없음"
                elif dist <= 300:
                    marker = f"{dist:.0f}m  ✅ (300m 이내)"
                elif dist <= 500:
                    marker = f"{dist:.0f}m  🟢 (500m 이내)"
                elif dist <= 1000:
                    marker = f"{dist:.0f}m  🟡 (1km 이내)"
                else:
                    marker = f"{dist:.0f}m  ❌ (1km 초과)"
                print(f"    · {name}: {marker}")

        # ── 신뢰도 (v5: 거래 최신성 기반으로 교체) ────────
        print("  [신뢰도]")
        if deal_recency:
            print(f"    · {deal_recency}")
        elif src == "CSV (2단계 fallback)":
            print(f"    · CSV 집계 기반 (실거래 미조회)")
            if sample_cnt: print(f"    · 동네 집계 매물 수: {int(sample_cnt)}건")
        else:
            print(f"    · 거래 시점 정보 없음")

        if tags and tags != "특징 없음":
            print(f"  [특징]  {tags}")
        else:
            print(f"  [특징]  특별히 두드러진 장점 없음")
        if score: print(f"  [종합점수]  {score:.1f}점  (TOPSIS 기준)")

        # ── 청년정책 (온통청년 API 연동) ─────────────────
        policy_score  = row.get("policy_score")
        policy_list   = row.get("policy_matched", [])
        gu_for_policy = row.get("시군구_2", "")
        if policy_score is not None and policy_score > 0:
            print_policy_section(gu_for_policy, policy_score,
                                  policy_list if isinstance(policy_list, list) else [],
                                  max_display=max_policy_display,
                                  conv_deposit=conv_dep,
                                  user_info=user_info,
                                  user_budget_monthly=monthly_rent_manwon)
        elif user_info and is_youth_api_configured():
            print("  [청년정책 혜택]")
            print("    · 이 지역에서 주거비 절감에 도움되는 정책이 없습니다.")

    # ── 데이터 한계 명시 (전체 결과 끝에 1회 출력) ────────
    print("\n" + "─"*70)
    print("  [데이터 한계 안내]")
    print("    · 실거래가: 계약 완료 기준이며 현재 호가와 다를 수 있습니다.")
    print("    · 통근시간: 출발 시각 기준 추정값이며 ±10분 오차가 발생할 수 있습니다.")
    print("    · 건물 좌표: 지번 기준 지오코딩이며 실제 출입구 위치와 다를 수 있습니다.")
    print("    · 인프라 거리: 카카오 반경 검색 기준이며 실제 도보 거리와 다를 수 있습니다.")
    print("    · 청년정책: API 시점 기준이며 정책 변경·마감 여부는 반드시 원문 확인 필요합니다.")
    print("─"*70)

    print("\n" + "="*70)


# =========================================================
# 19. 메인 추천 로직 (1단계 구→2단계 동→3단계 매물)
# =========================================================
def run_recommendation(housing_csv_path, work_address, transport_mode,
                        budget_manwon, allowed_commute_min, final_recommend_count,
                        depart_hour, depart_min, arrive_hour, arrive_min,
                        region_filter=None, housing_filter=None,
                        weight_commute=0.5, weight_housing=0.5,
                        required_infra=None, weight_infra=0.0,
                        user_info=None, weight_policy=0.0,
                        max_policy_display=3,
                        monthly_rent_manwon=50.0,
                        chatbot_mode=False,
                        vibe_list=None,
                        selected_policies=None):

    if transport_mode not in ("car","transit"):
        raise ValueError("이동수단은 'car' 또는 'transit'")

    # 정책 캐시 초기화 (새 추천 시작)
    reset_policy_cache()


    # 직장 좌표 (층·호·방향 등 상세정보 제거 후 지오코딩, 실패 시 단계적 fallback)
    work_address_clean = clean_work_address(work_address)
    if work_address_clean != work_address:
        print(f"  [주소 정제] {work_address} → {work_address_clean}")
    work_x, work_y, _ = geocode_address_kakao_with_fallback(work_address, KAKAO_LOCAL_REST_API_KEY)

    housing_df        = load_housing_data(housing_csv_path)
    seoul_avg_ppm     = float(housing_df["㎡당환산보증금_재계산(만원)"].mean())
    global_mean_price = float(housing_df["㎡당환산보증금_재계산(만원)"].mean())
    global_mean_dep   = float(housing_df["환산보증금(만원)"].mean())
    htypes            = housing_filter or ["연립다세대","오피스텔"]

    # ══════════════════════════════════════════════════════
    # 1단계: 구 단위 필터링 → 상위 5개 구
    # 예산 + 구 중심 직선거리 + 구별 평균 주거비
    # API 호출 없음, 순수 CSV 연산
    # ══════════════════════════════════════════════════════
    print(f"\n[1단계] 구 단위 필터링 중...")
    selected_gu = stage1_filter_gu(
        housing_df, budget_manwon, region_filter, work_x, work_y
    )
    gu_names = selected_gu["시군구_2"].tolist()
    print(f"  → 선정된 구: {', '.join(gu_names)}")

    # ══════════════════════════════════════════════════════
    # 2단계: 동 단위 필터링 → 상위 10개 동
    # 동 중심 좌표 지오코딩 + 직선거리 기반 예상 통근시간
    # 가중치에 따라 선정 방식 결정 (방법 A / 통근우선 / 주거비우선)
    # 허용 통근시간 초과 동 먼저 제거 (절대 기준 강제)
    # ══════════════════════════════════════════════════════
    print(f"\n[2단계] 동 단위 필터링 중 (선정된 구 내 동 지오코딩)...")
    selected_dong = stage2_filter_dong(
        housing_df, selected_gu, budget_manwon,
        work_x, work_y,
        allowed_commute_min, transport_mode, depart_hour,
        weight_commute, weight_housing,
        KAKAO_LOCAL_REST_API_KEY,
    )

    if selected_dong.empty:
        raise ValueError("2단계 후보 없음: 허용 통근시간·예산 조건을 완화해 주세요.")

    dong_list = [f"{r['시군구_2']} {r['읍면동']}" for _, r in selected_dong.iterrows()]
    print(f"  → 선정된 동: {', '.join(dong_list)}")

    # ══════════════════════════════════════════════════════
    # 3단계: 매물 단위 필터링 → 최종 추천
    # 국토부 API 실거래 매물 조회 → 건물 좌표 지오코딩
    # → 정밀 통근시간(혼잡계수 포함) → CV·베이즈·신뢰도 → 최종 점수
    # ══════════════════════════════════════════════════════
    print(f"\n[3단계] 실거래 매물 조회 및 정밀 통근시간 계산 중...")
    stage3_all = []

    for _, dong_row in selected_dong.iterrows():
        gu_name   = str(dong_row["시군구_2"]).strip()
        dong_name = str(dong_row["읍면동"]).strip()

        molit_df = fetch_molit_rentals(gu_name, dong_name,
                                        n_months=MOLIT_RECENT_MONTHS,
                                        housing_types=htypes)
        if molit_df.empty:
            print(f"  [{gu_name} {dong_name}] 국토부 API 매물 없음")
            continue

        rent_type_for_filter = "월세" if monthly_rent_manwon > 0 else "전세"
        molit_geo = stage2_geocode_and_filter(
            molit_df, budget_manwon, KAKAO_LOCAL_REST_API_KEY,
            rent_type=rent_type_for_filter,
            monthly_rent_manwon=monthly_rent_manwon,
        )
        if molit_geo.empty or "geocode_ok" not in molit_geo.columns:
            print(f"  [{gu_name} {dong_name}] 지오코딩 결과 없음 (예산 필터 또는 빈 응답)")
            continue
        ok_molit  = molit_geo[molit_geo["geocode_ok"]==True].copy()
        if ok_molit.empty:
            print(f"  [{gu_name} {dong_name}] 지오코딩 성공 매물 없음 (전체 {len(molit_geo)}건)")
            continue

        # 동일 지번 → 최저가 대표 매물 (API 호출 절감)
        if "geocode_addr" in ok_molit.columns:
            ok_molit = (ok_molit.sort_values("conv_deposit_manwon")
                        .groupby("geocode_addr").first().reset_index())

        # 동당 최대 매물 수 제한
        ok_molit = ok_molit.head(STAGE3_MAX_PROPERTY_PER_DONG)
        print(f"  [{gu_name} {dong_name}] 매물 {len(ok_molit)}건 → 통근시간 계산")

        # 동 단위 신뢰도 (CV 기반, 3단계에서만 계산)
        dong_confidence = calc_stage2_confidence(ok_molit)

        # 베이즈 평활화
        ok_molit = apply_bayesian_smoothing(ok_molit, global_mean_price, global_mean_dep)

        for _, mrow in ok_molit.iterrows():
            base = mrow.to_dict()
            base["data_source"]     = "국토부API"
            base["시군구_2"]         = gu_name
            base["읍면동"]           = dong_name
            base["data_confidence"]  = dong_confidence
            base["display_address"]  = (
                f"서울특별시 {gu_name} {mrow.get('dong', dong_name)} {mrow.get('jibun','')}"
                + (f" {mrow.get('building_name','')}" if mrow.get("building_name") else "")
            ).strip()

            # 가격 적정성 정보 추가
            price_ctx = calc_price_context(mrow.to_dict(), ok_molit)
            base.update({
                "deal_recency":       price_ctx.get("deal_recency",""),
                "vs_dong_median":     price_ctx.get("vs_dong_median",""),
                "similar_area_range": price_ctx.get("similar_area_range",""),
            })

            # 생활 인프라 점수 계산 (필수 편의시설 선택 시)
            if required_infra:
                _vibe_w = get_vibe_weights(vibe_list) if vibe_list else None
                infra = calc_infra_score(
                    float(mrow["cand_x"]), float(mrow["cand_y"]),
                    required_infra, KAKAO_LOCAL_REST_API_KEY,
                    vibe_weights=_vibe_w,
                )
                base["infra_score"]  = infra["infra_score"]
                base["infra_detail"] = infra["infra_detail"]
                base["infra_fail"]   = infra["infra_fail"]
            else:
                base["infra_score"]  = None
                base["infra_detail"] = {}
                base["infra_fail"]   = []

            # 실제 건물 좌표로 정밀 통근시간 계산 (혼잡계수 포함)
            commute = calc_commute_both_ways(
                float(mrow["cand_x"]), float(mrow["cand_y"]), work_x, work_y,
                transport_mode, depart_hour, depart_min, arrive_hour, arrive_min,
            )
            base.update(commute)
            stage3_all.append(base)
            time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"  → 3단계 총 {len(stage3_all)}개 매물 처리 완료")

    # ── 3단계 점수화 및 최종 선정 ─────────────────────────
    if stage3_all:
        s3_df = pd.DataFrame(stage3_all)

        # 통근시간 허용 범위 필터 (정밀값 기준)
        s3_ok = s3_df[
            s3_df["commute_time_min"].notna() &
            (s3_df["commute_time_min"] <= allowed_commute_min)
        ].copy()

        # fallback: 전부 초과 시 완화
        if s3_ok.empty:
            s3_ok = s3_df[s3_df["commute_time_min"].notna()].copy()

        # ── 필수 인프라 필터 (1km 초과 시설 있으면 탈락) ──
        if required_infra:
            before = len(s3_ok)
            s3_ok = s3_ok[
                s3_ok["infra_fail"].apply(lambda x: len(x) == 0)
            ].copy()
            removed = before - len(s3_ok)
            if removed > 0:
                print(f"  [인프라 필터] 필수 편의시설 1km 초과 매물 {removed}건 제거")
            if s3_ok.empty:
                print("  ※ 인프라 조건을 만족하는 매물이 없습니다. 필터 완화 후 재시도 권장.")
                s3_ok = s3_df[s3_df["commute_time_min"].notna()].copy()

        # 주거비 점수 (베이즈 평활화 적용값 우선)
        price_col = "smoothed_price_per_m2" if "smoothed_price_per_m2" in s3_ok.columns else "price_per_m2"
        dep_col   = "smoothed_deposit"      if "smoothed_deposit"      in s3_ok.columns else "conv_deposit_manwon"

        sp = min_max_scale_inverse(s3_ok[price_col].fillna(s3_ok["price_per_m2"]))
        sd = min_max_scale_inverse(s3_ok[dep_col].fillna(s3_ok["conv_deposit_manwon"]))
        s3_ok["housing_score"] = (sp*0.6 + sd*0.4).round(2)
        s3_ok["commute_score"] = s3_ok["commute_time_min"].apply(
            lambda x: round(commute_score(x, allowed_commute_min), 2)
        )

        # ── 청년정책 매물별 매칭 (v6.0 — 점수 X, 표시용만) ──────
        rent_type_label = "월세" if monthly_rent_manwon > 0 else "전세"
        if user_info and is_youth_api_configured():
            print(f"\n[청년정책] 매물별 정책 매칭 중... (rent_type={rent_type_label})")
            match_cache: dict = {}
            applicable_lists: list = []
            applicable_totals: list = []
            for _, row in s3_ok.iterrows():
                gu = str(row.get("시군구_2", "")).strip()
                key = (gu, rent_type_label)
                if key not in match_cache:
                    try:
                        # 프론트에서 5개씩 페이지네이션하므로 매칭된 전체를 반환
                        pol_list, pol_total = match_policies_for_property(
                            gu, rent_type_label, user_info, top_n=100
                        )
                        match_cache[key] = (pol_list, pol_total)
                    except Exception as e:
                        print(f"  [정책 매칭 오류] {gu}: {str(e)[:80]}")
                        match_cache[key] = ([], 0)
                pol_list, pol_total = match_cache[key]
                applicable_lists.append(pol_list)
                applicable_totals.append(pol_total)
            s3_ok["applicable_policies"]       = applicable_lists
            s3_ok["applicable_policies_total"] = applicable_totals
            # 호환용 (구 코드가 참조할 수 있음, 정책 점수는 v6.0부터 항상 0)
            s3_ok["policy_score"]   = 0.0
            s3_ok["policy_matched"] = applicable_lists
            print(f"  → 매칭 완료 ({len(match_cache)}개 (구,임대유형) 조합)")
        else:
            s3_ok["applicable_policies"]       = [[] for _ in range(len(s3_ok))]
            s3_ok["applicable_policies_total"] = [0 for _ in range(len(s3_ok))]
            s3_ok["policy_score"]   = 0.0
            s3_ok["policy_matched"] = [[] for _ in range(len(s3_ok))]

        # TOPSIS 점수 적용 (v6.0: 3축 — 통근/주거비/인프라; 정책은 표시용)
        s3_ok["final_score"] = topsis_score(
            s3_ok, weight_commute, weight_housing, weight_infra, 0.0
        )
        combined = s3_ok.sort_values("final_score", ascending=False)

    else:
        # 3단계 매물 없음 → 2단계 결과(동 단위)로 fallback
        print("  ※ 3단계 매물 없음 → 2단계 동 단위 결과로 fallback")
        combined = selected_dong.copy()
        combined["display_address"]  = combined["candidate_address"]
        combined["data_source"]      = "CSV (2단계 fallback)"
        # est_commute_min → commute_time_min 으로 매핑 (출력용)
        combined["commute_time_min"] = combined["est_commute_min"] if "est_commute_min" in combined.columns else None
        combined["commute_time_am_min"] = None
        combined["commute_time_pm_min"] = None
        combined["commute_score"]    = combined["commute_score"].fillna(0)
        combined["final_score"]      = (
            combined["commute_score"].fillna(0)*weight_commute
            + combined["housing_score"].fillna(0)*weight_housing
        ).round(2)
        # ── fallback에서도 매물 좌표 노출 (영수증 모듈 호출용) ───────
        # _df_to_list가 cand_y/cand_x를 lat/lon으로 읽으므로, 동 중심 좌표를 매핑
        combined["cand_y"] = combined.get("dong_y")
        combined["cand_x"] = combined.get("dong_x")
        # ── fallback에서도 청년정책 매칭 (자치구+임대유형 기준이라 동 단위에서도 동작) ──
        rent_type_label_fb = "월세" if monthly_rent_manwon > 0 else "전세"
        if user_info and is_youth_api_configured():
            print(f"  [fallback] 청년정책 매칭 중... (rent_type={rent_type_label_fb})")
            fb_lists, fb_totals = [], []
            fb_cache: dict = {}
            for _, row in combined.iterrows():
                gu = str(row.get("시군구_2", "")).strip()
                key = (gu, rent_type_label_fb)
                if key not in fb_cache:
                    try:
                        pol_list, pol_total = match_policies_for_property(
                            gu, rent_type_label_fb, user_info, top_n=100
                        )
                        fb_cache[key] = (pol_list, pol_total)
                    except Exception as e:
                        print(f"  [정책 매칭 오류] {gu}: {str(e)[:80]}")
                        fb_cache[key] = ([], 0)
                pol_list, pol_total = fb_cache[key]
                fb_lists.append(pol_list)
                fb_totals.append(pol_total)
            combined["applicable_policies"]       = fb_lists
            combined["applicable_policies_total"] = fb_totals
        else:
            combined["applicable_policies"]       = [[] for _ in range(len(combined))]
            combined["applicable_policies_total"] = [0 for _ in range(len(combined))]

    # 중복 제거 및 최종
    if "display_address" not in combined.columns:
        combined["display_address"] = combined.get("candidate_address","")
    combined["display_address"] = combined["display_address"].fillna("")
    combined = combined.drop_duplicates(subset=["display_address"]).head(final_recommend_count)
    combined = combined.reset_index(drop=True)

    if combined.empty:
        combined["feature_tags"] = pd.Series(dtype=str)
    else:
        combined["feature_tags"] = combined.apply(
            lambda r: generate_feature_tags(r, combined), axis=1
        )

    return combined, seoul_avg_ppm


# =========================================================
# 20. 입력 처리 유틸
# =========================================================
def parse_opt_float(v):
    s = str(v).strip(); return None if s=="" else float(s)

def parse_opt_int(v, d):
    s = str(v).strip(); return d if s=="" else int(s)

def parse_transport(raw):
    v = raw.strip()
    if v in ("자가용","자차","car"):    return "car"
    if v in ("대중교통","지하철","버스","transit"): return "transit"
    raise ValueError(f"이동수단을 다시 입력해 주세요. (자가용 / 대중교통): '{v}'")

def parse_time_input(raw, default_h, default_m):
    s = raw.strip()
    if not s: return default_h, default_m
    # 1~2자리: 시간만 입력한 경우 (예: "8" → 8시00분, "18" → 18시00분)
    if len(s) <= 2:
        h, m = int(s), 0
    # 3자리: "830" → 8시30분
    elif len(s) == 3:
        h, m = int(s[0]), int(s[1:])
    # 4자리: "0830", "1800"
    elif len(s) == 4:
        h, m = int(s[:2]), int(s[2:])
    else:
        raise ValueError(f"시간 형식 오류: '{raw}'")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"시각 범위 오류 ({h}시 {m}분)")
    return h, m


# =========================================================
# 21. 실행부
# =========================================================
def main():
    print("=== 서울 주거지 추천 시스템 v5 ===\n")

    # ══════════════════════════════════════════════════════
    # 0. 직장 유무 선택
    # ══════════════════════════════════════════════════════
    print("0. 직장 상태를 선택해 주세요.")
    print("   1) 직장이 있다  (주소 직접 입력)")
    print("   2) 직장을 찾고 있다  (채용정보 검색 후 주소 자동 연결)")
    while True:
        job_status = input("   선택 (1 / 2): ").strip()
        if job_status in ("1", "2"):
            break
        print("  ※ 1 또는 2를 입력해 주세요.")

    if job_status == "1":
        # ── 기존 방식: 직접 입력 ────────────────────────
        work_address = input("\n1. 직장 주소 (필수): ").strip()
        if not work_address:
            raise ValueError("직장 주소는 필수입니다.")
        print()

    else:
        # ── 채용정보 검색 → work_address 자동 설정 ───────
        work_address = job_search_flow()
        if not work_address:
            raise ValueError(
                "직장 주소를 설정하지 못했습니다. "
                "재실행 후 '1'을 선택해 직접 입력해 주세요."
            )
        print(f"\n1. 직장 주소 (채용정보 자동 설정): {work_address}\n")

    # ══════════════════════════════════════════════════════
    # 이하 기존 입력 흐름 그대로
    # ══════════════════════════════════════════════════════
    while True:
        try:
            transport_mode = parse_transport(input("2. 이동수단 (자가용 / 대중교통): ").strip())
            break
        except ValueError as e:
            print(f"  ※ {e}")

    print("3. 예산 설정")
    while True:
        rent_type_input = input("   계약 형태 (전세 / 월세): ").strip()
        if rent_type_input in ("전세","월세"): break
        print("  ※ '전세' 또는 '월세'를 입력해 주세요.")

    budget_manwon = None
    monthly_rent_budget = 50.0  # 청년정책 점수 기준 (사용자 예산 대비 비율)
    if rent_type_input == "전세":
        v = input("   전세금 (만원, 비우면 무제한): ").strip()
        budget_manwon = parse_opt_float(v)
    else:
        dep_v  = input("   보증금 (만원, 비우면 0): ").strip()
        rent_v = input("   월세 (만원, 비우면 무제한): ").strip()
        dep_val  = parse_opt_float(dep_v) or 0.0
        rent_val = parse_opt_float(rent_v)
        if rent_val is not None:
            budget_manwon = dep_val + rent_val * 200
            monthly_rent_budget = rent_val
            print(f"   → 환산보증금 기준: {dep_val:.0f} + {rent_val:.0f}×200 = {budget_manwon:,.0f}만원 이하")

    allowed_input = input("4. 허용 통근시간 분 (비우면 90분): ").strip() or "90"
    region_filter = input("5. 희망 지역 필터 예) 강남구 (비우면 서울 전체): ").strip()
    topn_input    = input("6. 추천 개수 (비우면 10개): ").strip()

    while True:
        try:
            depart_h, depart_m = parse_time_input(
                input("7. 출근 시각 (예) 830 → 8시30분, 비우면 8시): ").strip(), 8, 0)
            break
        except ValueError as e: print(f"  ※ {e}")

    while True:
        try:
            arrive_h, arrive_m = parse_time_input(
                input("8. 퇴근 시각 (예) 1800 → 18시00분, 비우면 18시): ").strip(), 18, 0)
            break
        except ValueError as e: print(f"  ※ {e}")

    ht_input = input("9. 주택유형 (연립다세대 / 오피스텔 / 비우면 둘 다): ").strip()

    # ── 10번: 통근시간 vs 주거비 기본 비율 ──────────────────
    print("10. 통근시간 vs 주거비 기본 비율 설정")
    print("    1=통근 우선(7:3)  2=주거비 우선(3:7)  3=균형(5:5)")
    while True:
        base_input = input("    선택 (비우면 5:5 균형): ").strip()
        if not base_input or base_input == "3":
            base_commute, base_housing = 5, 5; break
        elif base_input == "1":
            base_commute, base_housing = 7, 3; break
        elif base_input == "2":
            base_commute, base_housing = 3, 7; break
        print("  ※ 1, 2, 3 중 하나를 선택해 주세요.")

    # ── 11번: 필수 편의시설 선택 ─────────────────────────
    infra_list = list(INFRA_CATEGORY_MAP.keys())
    required_infra = []
    use_infra = False
    print(f"\n11. 생활 인프라 점수를 반영하시겠습니까?")
    print(f"    선택 시 편의시설 거리 기반 점수가 최종 순위에 반영됩니다.")
    infra_yn = input("    반영 여부 (y=예 / n=아니요): ").strip().lower()
    if infra_yn == "y":
        use_infra = True
        print(f"    필수 편의시설 선택 (없으면 Enter로 건너뜀)")
        print(f"    선택 가능: {', '.join(f'{i+1}={name}' for i, name in enumerate(infra_list))}")
        infra_input = input("    번호 입력 (예) 1,3,5 또는 비우면 전체 반영): ").strip()
        if infra_input:
            for tok in infra_input.replace(" ","").split(","):
                if tok.isdigit() and 1 <= int(tok) <= len(infra_list):
                    required_infra.append(infra_list[int(tok)-1])
            if required_infra:
                print(f"    → 필수 편의시설: {', '.join(required_infra)}")
                print(f"    ※ 해당 시설이 1km 초과인 매물은 추천에서 제외됩니다.")
            else:
                print("    → 필수 편의시설 없음 (인프라 점수만 계산)")
        else:
            print("    → 인프라 점수 반영 (필수 시설 지정 없음)")
    else:
        print("    → 건너뜀 (인프라 미반영)")

    housing_filter = [h.strip() for h in ht_input.replace("/"," ").split() if h.strip()] if ht_input else None

    # ── 12번: 청년정책 맞춤 정보 입력 ────────────────────
    user_info = None
    use_policy = False
    max_policy_display = 3
    if is_youth_api_configured():
        print(f"\n12. 청년정책 혜택을 반영하시겠습니까?")
        print(f"    선택 시 주거비 절감 가능한 정책을 조회하여 최종 순위에 반영됩니다.")
        youth_input = input("    반영 여부 (y=예 / n=아니요): ").strip().lower()
        if youth_input == "y":
            use_policy = True
            user_info = collect_youth_info()
            if user_info:
                print(f"    → 청년정보 입력 완료")

                # ── 13번: 정책 표시 개수 ────────────────────
                pd_input = input("\n13. 매물당 표시할 정책 수 (비우면 3개): ").strip()
                if pd_input.isdigit() and int(pd_input) >= 1:
                    max_policy_display = int(pd_input)
                print(f"    → 정책 표시: 매물당 최대 {max_policy_display}건")
            else:
                use_policy = False
        else:
            print("    → 건너뜀 (청년정책 미반영)")
    else:
        print(f"\n12. 청년정책 기능: API 키 미설정 → 건너뜀")
        print("    ※ youth_policy_module.py의 YOUTH_API_KEY를 설정하면 사용할 수 있습니다.")

    # ── 가중치 자동 설정 (선택한 축 수에 따라 비율 배분) ──
    # 기본 2축(통근, 주거비)은 항상 포함, 추가 축은 선택 순서대로
    # 2축: 5:5 / 3축: 4:3:3 / 4축: 3:3:2:2
    # 통근 우선/주거비 우선 시 첫째 값이 더 큼
    extra_axes = []                         # (이름, 변수명) 순서 리스트
    if use_infra:  extra_axes.append("infra")
    if use_policy: extra_axes.append("policy")

    n_axes = 2 + len(extra_axes)

    if n_axes == 2:
        # 5:5 (또는 7:3 / 3:7)
        wc, wh = float(base_commute), float(base_housing)
        wi, wp = 0.0, 0.0
    elif n_axes == 3:
        # 4:3:3 — 통근/주거비 중 큰 쪽이 4, 작은 쪽이 3, 추가 축 3
        if base_commute >= base_housing:
            wc, wh = 4.0, 3.0
        else:
            wc, wh = 3.0, 4.0
        wi = 3.0 if "infra"  in extra_axes else 0.0
        wp = 3.0 if "policy" in extra_axes else 0.0
    else:
        # 4축: 3:3:2:2
        if base_commute >= base_housing:
            wc, wh = 3.0, 3.0
        else:
            wc, wh = 3.0, 3.0
        wi = 2.0 if "infra"  in extra_axes else 0.0
        wp = 2.0 if "policy" in extra_axes else 0.0

    total_w = wc + wh + wi + wp
    weight_commute = round(wc / total_w, 4)
    weight_housing = round(wh / total_w, 4)
    weight_infra   = round(wi / total_w, 4)
    weight_policy  = round(wp / total_w, 4)

    # 자동 설정 결과 표시 + 직접 변경 옵션
    parts_str = f"통근 {weight_commute*100:.0f}% : 주거비 {weight_housing*100:.0f}%"
    if weight_infra > 0:   parts_str += f" : 인프라 {weight_infra*100:.0f}%"
    if weight_policy > 0:  parts_str += f" : 정책 {weight_policy*100:.0f}%"
    print(f"\n  [가중치 자동 설정] {parts_str}")

    custom_w = input("  직접 변경하시려면 비율 입력 (예: 4:3:2:1), 그대로 진행하려면 Enter: ").strip()
    if custom_w:
        try:
            parts = custom_w.replace(" ","").split(":")
            if len(parts) == 2: parts.extend(["0","0"])
            elif len(parts) == 3: parts.append("0")
            if len(parts) == 4:
                cwc, cwh, cwi, cwp = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
                if cwc >= 0 and cwh >= 0 and cwi >= 0 and cwp >= 0 and (cwc+cwh+cwi+cwp) > 0:
                    t = cwc + cwh + cwi + cwp
                    weight_commute = round(cwc/t, 4)
                    weight_housing = round(cwh/t, 4)
                    weight_infra   = round(cwi/t, 4)
                    weight_policy  = round(cwp/t, 4)
                    parts_str = f"통근 {weight_commute*100:.0f}% : 주거비 {weight_housing*100:.0f}%"
                    if weight_infra > 0:   parts_str += f" : 인프라 {weight_infra*100:.0f}%"
                    if weight_policy > 0:  parts_str += f" : 정책 {weight_policy*100:.0f}%"
                    print(f"  → 변경 완료: {parts_str}")
                else:
                    print("  ※ 잘못된 입력 → 자동 설정 유지")
            else:
                print("  ※ 잘못된 입력 → 자동 설정 유지")
        except:
            print("  ※ 잘못된 입력 → 자동 설정 유지")

    # ── 기존 피드백 통계 표시 ──────────────────────────────
    fb_count = get_feedback_count()
    if fb_count > 0:
        print(f"\n  [이전 피드백] 누적 {fb_count}건의 피드백이 저장되어 있습니다.")

    final_df, seoul_avg_ppm = run_recommendation(
        housing_csv_path      = HOUSING_CSV_PATH,
        work_address          = work_address,
        transport_mode        = transport_mode,
        budget_manwon         = budget_manwon,
        allowed_commute_min   = int(allowed_input),
        final_recommend_count = parse_opt_int(topn_input, 10),
        depart_hour           = depart_h,
        depart_min            = depart_m,
        arrive_hour           = arrive_h,
        arrive_min            = arrive_m,
        region_filter         = region_filter if region_filter else None,
        housing_filter        = housing_filter,
        weight_commute        = weight_commute,
        weight_housing        = weight_housing,
        required_infra        = required_infra if required_infra else None,
        weight_infra          = weight_infra,
        user_info             = user_info,
        weight_policy         = weight_policy,
        max_policy_display    = max_policy_display,
        monthly_rent_manwon   = monthly_rent_budget,
    )

    print_final_results(
        final_df, transport_mode,
        depart_h, depart_m, arrive_h, arrive_m,
        seoul_avg_ppm, weight_commute, weight_housing,
        weight_infra         = weight_infra,
        allowed_commute_min  = int(allowed_input),
        user_info            = user_info,
        weight_policy        = weight_policy,
        max_policy_display   = max_policy_display,
        monthly_rent_manwon  = monthly_rent_budget,
    )

    saved = save_csv_safely(final_df, SAVE_PATH)
    print(f"\n결과 CSV 저장: {saved}")

    # ── 만족도 피드백 수집 ────────────────────────────────
    collect_feedback(
        final_df,
        weight_commute      = weight_commute,
        weight_housing      = weight_housing,
        weight_infra        = weight_infra,
        transport_mode      = transport_mode,
        budget_manwon       = budget_manwon,
        allowed_commute_min = int(allowed_input),
    )

    # 피드백 통계 요약 출력
    print_feedback_summary()


if __name__ == "__main__":
    main()
