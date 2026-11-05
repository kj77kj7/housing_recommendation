"""
youth_policy_module.py
──────────────────────
청년 주거정책 모듈 (v6.0 — CSV 화이트리스트 기반)

흐름 (v6.0):
  1차) CSV 로드 → 자치구/임대유형/사용자 자격 필터
  2차) 매물별 매칭 정책 표시 (점수 산출 X, 추천 점수에도 반영 X)

CSV: webapp/data/seoul_youth_housing_policies.csv (서울 주거 정책 화이트리스트)
이전 버전(v5.x): 온통청년 API 호출 + analyze_benefit 정규식 폴백 사용 (deprecated)
"""

import os
import re
import csv
from typing import Dict, List, Tuple, Optional
from dotenv import load_dotenv
load_dotenv()

# ──────────────────────────────────────────────────────────
# 설정 — CSV 화이트리스트 경로
# ──────────────────────────────────────────────────────────
CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "webapp", "data", "seoul_youth_housing_policies.csv"
)

# ── 구버전 호환 (env에 둬도 무방, 사용 안 함) ─────────────
YOUTH_API_KEY = os.getenv("YOUTH_API_KEY", "")
YOUTH_API_URL = "https://www.youthcenter.go.kr/go/ythip/getPlcy"  # deprecated
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN   = 0.5

# ──────────────────────────────────────────────────────────
# 서울 구별 법정동코드 앞5자리
# ──────────────────────────────────────────────────────────
SEOUL_GU_ZIPCD: Dict[str, str] = {
    "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200",
    "광진구": "11215", "동대문구": "11230", "중랑구": "11260", "성북구": "11290",
    "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
    "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500",
    "구로구": "11530", "금천구": "11545", "영등포구": "11560", "동작구": "11590",
    "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710",
    "강동구": "11740",
}
SEOUL_PREFIX = "11"

# ──────────────────────────────────────────────────────────
# 상세조건 코드 → 한글 매핑
# ──────────────────────────────────────────────────────────
MARRIAGE_CODE = {
    "0055001": "미혼", "0055002": "기혼", "0055003": "제한없음",
}
INCOME_CODE = {
    "0043001": "제한없음", "0043002": "중위소득 이하", "0043003": "기초수급/차상위",
}
SCHOOL_CODE = {
    "0049001": "고졸이하", "0049002": "대학재학", "0049003": "대졸",
    "0049004": "석박사", "0049005": "제한없음",
}

# 소득 기준 상수 (보건복지부 고시 2024~2025년 기준, 만원/월)
MEDIAN_INCOME_1PERSON = 244  # 1인 가구 중위소득
NEAR_POOR_THRESHOLD   = 122  # 차상위 (중위 50%)
URBAN_WORKER_AVG      = 350  # 도시근로자 월평균 소득 (3인 가구 기준 근사)

# ── 챗봇 소득 구간 → 만원 단위 추정값 (자격 검증용) ──────
# "모름" 또는 None이면 자격 미정 (needs_check 처리)
INCOME_BAND_TO_MANWON: Dict[str, Optional[float]] = {
    "200만원 이하":  250,   # 사양: 구간 중앙값 사용
    "200~300만원":  250,
    "300~400만원":  350,
    "400~500만원":  450,
    "500만원 이상":  600,
    "모름":          0.0,   # "모름(없음)" 선택 → 소득 0으로 처리 → 모든 기준 통과
}

# 추정 산식 상수 — 학술/공공 표준 없음, 동네 간 상대 비교용
ASSUMED_RESIDENCE_MONTHS    = 12    # 거주기간 가정 (12개월)
ASSUMED_MAX_BENEFIT_RATIO   = 0.5   # 최대지원금 중 수혜 비율 가정 (50%)
MARKET_RENT_REFERENCE_MANWON = 50   # 시세 할인 기준 참조 월세 (만원)
ASSUMED_OPPORTUNITY_RATE    = 0.045 # 보증금 기회비용 연이율 (4.5% 가정)

# 전월세 전환율 테이블 (한국부동산원 2024 참고값, 연%)
# 근거: 주택임대차보호법 제7조의2, 시행령 제9조 (MIN[기준금리+2%, 10%])
# 법정 계산식: 보증금 × 전환율(연) ÷ 12 = 월 환산액
# ※ 반기 1회 갱신 권장 (한국부동산원 R-ONE stat.reb.or.kr)
_CONVERSION_RATE_TABLE: Dict[str, float] = {
    "강남구": 5.2, "서초구": 5.1, "송파구": 5.3, "강동구": 5.5,
    "마포구": 5.6, "용산구": 5.3, "성동구": 5.7, "광진구": 5.8,
    "동대문구": 6.0, "중랑구": 6.2, "성북구": 6.1, "강북구": 6.3,
    "도봉구": 6.2, "노원구": 6.1, "은평구": 6.0, "서대문구": 5.9,
    "종로구": 5.8, "중구": 5.7, "양천구": 5.8, "강서구": 5.9,
    "구로구": 6.1, "금천구": 6.2, "영등포구": 5.8, "동작구": 5.9,
    "관악구": 6.0,
}
_CONVERSION_RATE_DEFAULT = 6.0  # 폴백 (법정 상한 근사치)

# 중복 수혜 제한 키워드
DUP_LIMIT_KEYWORDS = [
    "중복 불가", "중복불가", "중복 신청 불가",
    "타 사업과", "유사 사업", "동일 사업",
    "중복 수혜 제한", "중복지원 제한",
]

# ── 취업상태 시노님 매핑 ───────────────────────────────
# 회원가입 저장값("취업자"/"미취업자"/"자영업자")을 정책 텍스트의
# 다양한 표현으로 확장 매핑
EMPLOYMENT_SYNONYMS: Dict[str, List[str]] = {
    "취업자":   ["취업자", "재직자", "근로자", "직장인",
                 "취업", "근로", "프리랜서", "재직"],
    "미취업자": ["미취업자", "미취업", "구직", "취업준비",
                 "취준", "일경험없음", "백수"],
    "자영업자": ["자영업자", "자영업", "개인사업자", "개인사업", "사업자", "창업"],
}

# ── 중복 수혜 충돌 카테고리 규칙 ─────────────────────
CATEGORY_CONFLICT_RULES: Dict[str, str] = {
    "공공임대":  "strict",     # 모든 주거 정책과 충돌
    "월세지원":  "same_type",  # 월세지원끼리 충돌
    "보증금지원": "same_type", # 보증금/대출끼리 충돌
    "기타":      "none",       # 충돌 없음
}

# 산식 신뢰도 표시
_BENEFIT_RELIABILITY: Dict[str, str] = {
    "월세보조":  "★★★ 정책 공고문 명시 금액",
    "보증금지원": "★★☆ 법정 전환율 적용 (추정)",
    "최대지원":  "★★☆ 12개월 균등 분할 가정 (추정)",
    "시세할인":  "★★☆ 평균 시세 기준 (추정)",
    "임대료할인": "★★☆ 평균 시세 기준 (추정)",
    "공공임대":  "★☆☆ 공공임대 평균 기준 (추정)",
    "전세대출":  "★☆☆ 이자 절감액 추정",
    "월세지원":  "★☆☆ 기본값 추정",
    "주거수당":  "★☆☆ 기본값 추정",
    "이자지원":  "★☆☆ 기본값 추정",
    "보증보험":  "★☆☆ 기본값 추정",
    "관리비지원": "★☆☆ 기본값 추정",
    "주거관련":  "★☆☆ 구체적 혜택 미확인",
}

# ──────────────────────────────────────────────────────────
# 주거비 절감 패턴 테이블
# ──────────────────────────────────────────────────────────
BENEFIT_PATTERNS = [
    (r"월\s*(\d+)\s*만\s*원\s*(지원|보조|지급)", "월세보조", None, "월 {amount}만원 주거비 지원"),
    (r"보증금\s*(\d+)\s*만\s*원\s*(지원|보조|대출|무이자)", "보증금지원", None, "보증금 {amount}만원 지원/대출"),
    (r"최대\s*(\d+)\s*만\s*원", "최대지원", None, "최대 {amount}만원 지원"),
    (r"시세\s*(\d+)\s*%\s*(수준|이하|할인)", "시세할인", None, "시세 대비 {amount}% 수준 제공"),
    (r"(\d+)\s*%\s*(할인|감면|인하)", "임대료할인", None, "{amount}% 임대료 할인"),
    (r"공공임대|행복주택|매입임대|국민임대", "공공임대", 20, "공공임대주택 입주 자격 (시세 대비 약 60~80%)"),
    (r"전세\s*자금\s*대출|전세대출|전세\s*보증금\s*대출", "전세대출", 8, "전세자금 대출 이자 지원 (월 약 8만원 절감 추정)"),
    (r"월세\s*(지원|보조|바우처|보전)", "월세지원", 15, "월세 지원금 (월 약 15만원 추정)"),
    (r"주거\s*(안정|지원)\s*(장려|바우처|금|수당)", "주거수당", 10, "주거안정 수당/바우처 (월 약 10만원 추정)"),
    (r"이자\s*(지원|보전|감면|차액)", "이자지원", 5, "대출 이자 지원 (월 약 5만원 절감 추정)"),
    (r"보증\s*보험\s*(지원|보조)", "보증보험", 3, "보증보험료 지원 (월 약 3만원 절감 추정)"),
    (r"관리비\s*(지원|보조|감면)", "관리비지원", 5, "관리비 지원/감면 (월 약 5만원 절감 추정)"),
    (r"취업\s*(지원금|성공|장려|수당)", "취업지원", 3, "취업 지원금 (간접 가계 부담 경감)"),
    (r"교통비\s*(지원|보조)", "교통비지원", 3, "교통비 지원 (간접 주거비 부담 경감)"),
]

HOUSING_DIRECT_KEYWORDS = [
    "주거", "임대", "전세", "월세", "보증금", "주택", "임차", "공공임대",
    "행복주택", "청년주택", "매입임대", "전월세", "기숙사", "셰어하우스",
    "주거안정", "주거비", "월세보조", "전세대출", "주거수당", "관리비",
]


# ──────────────────────────────────────────────────────────
# 사용자 청년정보 입력 (1차 필터용)
# ──────────────────────────────────────────────────────────
def collect_youth_info() -> Dict[str, str]:
    """청년정책 1차 필터링에 필요한 기본 정보 수집."""
    print("\n" + "=" * 60)
    print("  청년정책 맞춤 검색을 위한 기본 정보 입력")
    print("  (비우면 전체 정책 조회)")
    print("=" * 60)
    info = {}
    info["age"] = input("\n  만 나이 (숫자, 예: 만 26세 → 26, 비우면 건너뜀): ").strip()

    print("  학력: 1=고졸이하  2=대학재학  3=대졸  4=석박사")
    edu_map = {"1": "고졸이하", "2": "대학재학", "3": "대졸", "4": "석박사"}
    info["education"] = edu_map.get(input("  선택 (비우면 전체): ").strip(), "")

    print("  취업상태: 1=재직자  2=자영업자  3=미취업자  4=프리랜서  5=일경험없음")
    emp_map = {"1": "재직자", "2": "자영업자", "3": "미취업자", "4": "프리랜서", "5": "일경험없음"}
    info["employment"] = emp_map.get(input("  선택 (비우면 전체): ").strip(), "")

    raw_inc = input("\n  월 소득 (만원, 예: 200, 비우면 건너뜀): ").strip()
    info["income_manwon"] = raw_inc if raw_inc.isdigit() else ""

    print("  혼인상태: 1=미혼  2=기혼")
    mrg_map = {"1": "미혼", "2": "기혼"}
    info["marriage"] = mrg_map.get(input("  선택 (비우면 전체): ").strip(), "")

    no_house = input("  무주택 여부 (y=무주택 / n=주택소유 / 비우면 건너뜀): ").strip().lower()
    info["no_house"] = no_house if no_house in ("y", "n") else ""

    return info


# ──────────────────────────────────────────────────────────
# CSV 화이트리스트 로더 (v6.0)
# ──────────────────────────────────────────────────────────
def _normalize_csv_row(row: dict) -> dict:
    """CSV 1행을 표준 정책 dict로 변환.
    CSV 네이티브 필드 + 구 API 필드명 호환 shim 동시 제공.
    """
    zip_codes_raw = (row.get("zip_codes") or "").strip()
    zip_codes = [z.strip() for z in zip_codes_raw.split(",") if z.strip()]
    duplicate_with = [d.strip() for d in (row.get("duplicate_with") or "").split(",") if d.strip()]

    def _to_float(v, default=0.0):
        try:
            return float(v) if v not in (None, "", "None") else default
        except (ValueError, TypeError):
            return default

    def _to_int(v, default=0):
        try:
            return int(float(v)) if v not in (None, "", "None") else default
        except (ValueError, TypeError):
            return default

    amt     = _to_float(row.get("benefit_amount_manwon"), 0.0)
    period  = _to_int(row.get("benefit_period_months"), 0)
    age_min = _to_int(row.get("target_age_min"), 0)
    age_max = _to_int(row.get("target_age_max"), 99)

    target_emp = (row.get("target_employment") or "전체").strip()
    target_edu = (row.get("target_education")  or "전체").strip()
    target_inc = (row.get("target_income")     or "전체").strip()
    target_mrg = (row.get("target_marriage")   or "전체").strip()
    target_no_house = (row.get("target_no_house") or "전체").strip()

    return {
        # ── CSV 네이티브 필드 ─────────────────────────────
        "policy_id":              row.get("policy_id", ""),
        "policy_name":            row.get("policy_name", ""),
        "source_org":             row.get("source_org", ""),
        "zip_codes":              zip_codes,
        "support_type":           row.get("support_type", ""),
        "benefit_amount_manwon":  amt,
        "benefit_period_months":  period,
        "benefit_desc":           row.get("benefit_desc", ""),
        "target_age_min":         age_min,
        "target_age_max":         age_max,
        "target_employment":      target_emp,
        "target_education":       target_edu,
        "target_income":          target_inc,
        "target_marriage":        target_mrg,
        "target_no_house":        target_no_house,
        "extra_conditions":       row.get("extra_conditions", ""),
        "duplicate_limit":        row.get("duplicate_limit", "none"),
        "duplicate_with":         duplicate_with,
        "apply_url":              row.get("apply_url", ""),
        "data_source":            row.get("data_source", ""),
        "last_updated":           row.get("last_updated", ""),

        # ── 구 온통청년 API 필드명 호환 shim ──────────────
        "plcyNm":           row.get("policy_name", ""),
        "plcyExplnCn":      row.get("benefit_desc", ""),
        "plcySprtCn":       row.get("benefit_desc", ""),
        "zipCd":            zip_codes_raw,
        "sprtTrgtMinAge":   str(age_min) if age_min else "",
        "sprtTrgtMaxAge":   str(age_max) if age_max else "",
        "sprvsnInstCdNm":   row.get("source_org", ""),
        "rgtrHghrkInstCdNm":row.get("source_org", ""),
        "lclsfNm":          "주거",
        "mclsfNm":          row.get("support_type", ""),
        "refUrlAddr1":      row.get("apply_url", ""),
        "addAplyQlfcCndCn": row.get("extra_conditions", ""),
        "ptcpPrpTrgtCn":    "",
        "etcMttrCn":        "",
        "plcyKywdNm":       row.get("support_type", ""),
        "earnCndSeCd":      "0043001" if target_inc == "전체" else "",
        "earnMaxAmt":       "0",
        "mrgSttsCd":        "0055003" if target_mrg == "전체" else "",
        "schoolCd":         "0049005" if target_edu == "전체" else "",
        "jobCd":            "",
    }


def _load_policies_from_csv() -> List[dict]:
    """webapp/data/seoul_youth_housing_policies.csv 로드 → 표준 정책 dict 리스트."""
    if not os.path.exists(CSV_PATH):
        print(f"  [정책 CSV 없음] {CSV_PATH}")
        return []
    policies = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            policies.append(_normalize_csv_row(row))
    return policies


def _call_youth_api(params: dict) -> List[dict]:
    """[deprecated] 온통청년 API 호출. v6.0부터 CSV 사용 — 호출 시 빈 리스트."""
    return []


def _fetch_all_policies(max_pages: int = None, page_size: int = None) -> List[dict]:
    """[v6.0] CSV 화이트리스트 로드. max_pages/page_size 파라미터는 호환용 무시."""
    return _load_policies_from_csv()


# ──────────────────────────────────────────────────────────
# 필터링 함수
# ──────────────────────────────────────────────────────────
def _extract_policy_text(p: dict) -> str:
    fields = ["plcyNm", "plcyExplnCn", "plcySprtCn", "plcyKywdNm",
              "lclsfNm", "mclsfNm", "addAplyQlfcCndCn", "etcMttrCn"]
    return " ".join(str(p.get(f, "")) for f in fields)


def _is_seoul_policy(p: dict, gu_name: str = "") -> bool:
    """매물 자치구의 zip 코드가 정책 zip_codes에 포함되는지 검증.
    gu_name이 비면 zip_codes에 서울(11로 시작) 코드가 있으면 통과.
    """
    zip_codes = p.get("zip_codes")
    if not zip_codes:
        # 구 API 호환 폴백
        zip_codes = [z.strip() for z in str(p.get("zipCd", "")).split(",") if z.strip()]
    if not zip_codes:
        # 빈 zip_codes는 전국 정책으로 간주 → 통과
        return True
    if gu_name:
        gc = SEOUL_GU_ZIPCD.get(gu_name, "")
        if gc and any(c.startswith(gc) for c in zip_codes):
            return True
        return False
    return any(c.startswith(SEOUL_PREFIX) for c in zip_codes)


def _is_housing_related(p: dict) -> bool:
    """v6.0: CSV 화이트리스트는 모두 주거 정책 → 항상 True."""
    return True


def _check_age(p: dict, user_age: str) -> bool:
    """target_age_min ≤ user_age ≤ target_age_max."""
    if not user_age:
        return True
    try:
        age = int(user_age)
    except (ValueError, TypeError):
        return True
    age_min = int(p.get("target_age_min", 0) or 0)
    age_max = int(p.get("target_age_max", 99) or 99)
    return age_min <= age <= age_max


def _syn_match(syn: str, text: str) -> bool:
    """한국어 음절 경계 매칭 (구 API 텍스트 호환용, 거의 사용 안 함)."""
    pattern = f"(?<![가-힣]){re.escape(syn)}"
    return bool(re.search(pattern, text))


def _check_employment(p: dict, user_emp: str) -> bool:
    """target_employment: 전체 / 취업자 / 미취업자 / 자영업자 등."""
    if not user_emp:
        return True
    target = str(p.get("target_employment", "전체")).strip()
    if target in ("", "전체", "제한없음"):
        return True
    # 사용자 시노님 중 하나라도 target 텍스트에 포함되면 통과
    synonyms = EMPLOYMENT_SYNONYMS.get(user_emp, [user_emp])
    return any(syn in target for syn in synonyms)


_EDU_ORDER = {"고졸이하": 1, "대학재학": 2, "대졸": 3, "석박사": 4}


def _check_education(p: dict, user_edu: str) -> bool:
    """target_education: 전체 / 고졸이하 / 대학재학 / 대졸 / 석박사."""
    if not user_edu:
        return True
    target = str(p.get("target_education", "전체")).strip()
    if target in ("", "전체", "제한없음"):
        return True
    if target == "대학재학":
        return user_edu == "대학재학"
    # 그 외는 "해당 학력 이상" 조건으로 해석
    return _EDU_ORDER.get(user_edu, 0) >= _EDU_ORDER.get(target, 0)


def _parse_income_threshold(text: str) -> Optional[float]:
    """target_income 텍스트에서 월 만원 단위 한계값 추출. 못 찾으면 None.

    지원 패턴:
      "중위 N% 이하"             → MEDIAN_INCOME_1PERSON × N%
      "도시근로자 ... N% 이하"    → URBAN_WORKER_AVG × N%
      "연 N만원 이하" / "N만원 이하" → 그대로 (이미 월소득)
      "N천만원" (연소득)         → N × 1000 / 12 (월 환산)
      "부부합산 N천만원"          → 위와 동일 (연소득)
    """
    # 중위 N%
    m = re.search(r"중위\s*(\d+)\s*%", text)
    if m:
        return MEDIAN_INCOME_1PERSON * float(m.group(1)) / 100
    # 도시근로자 N%
    m = re.search(r"도시근로자.*?(\d+)\s*%", text)
    if m:
        return URBAN_WORKER_AVG * float(m.group(1)) / 100
    # N천만원 (연소득) — 가장 큰 숫자 우선 (예: 신혼 7.5천 / 일반 5천 → 6.0 = 평균? 아니면 가장 관대?)
    # 사양: 가장 관대(큰 값)을 사용
    m_all = re.findall(r"(\d+(?:\.\d+)?)\s*천", text)
    if m_all:
        try:
            biggest = max(float(x) for x in m_all)
            return biggest * 1000 / 12
        except ValueError:
            pass
    # N만원 이하 (월소득 직접 명시)
    m = re.search(r"(\d+)\s*만원\s*이하", text)
    if m:
        return float(m.group(1))
    return None


def _check_income(p: dict, user_income_band: str) -> bool:
    """CSV target_income 텍스트와 사용자 소득 구간 비교.
    모호한 정책 텍스트나 사용자 모름 → True 반환 (needs_check로 처리됨).
    """
    target = str(p.get("target_income", "전체")).strip()
    if target in ("", "전체", "제한없음"):
        return True
    user_inc = INCOME_BAND_TO_MANWON.get(user_income_band)
    if user_inc is None:
        return True   # "모름" 등 → 미정 (needs_check)
    threshold = _parse_income_threshold(target)
    if threshold is None:
        return True   # 모호한 정책 텍스트 → 미정 (needs_check)
    return user_inc <= threshold


def _check_marriage(p: dict, user_marriage: str) -> bool:
    """target_marriage: 전체 / 미혼 / 기혼 / 신혼."""
    target = str(p.get("target_marriage", "전체")).strip()
    if target in ("", "전체", "제한없음"):
        return True
    if not user_marriage:
        return True
    return target == user_marriage


def _check_no_house(p: dict, no_house: str) -> bool:
    """target_no_house: 전체 / 필수 / 임차가구만.
    no_house: "y"=무주택 / "n"=보유 / ""=모름.
    """
    target = str(p.get("target_no_house", "전체")).strip()
    if target in ("", "전체"):
        return True
    if no_house == "":
        return True   # 모름 → 미정 (needs_check)
    if target in ("필수", "임차가구만"):
        return no_house == "y"
    return True


def conversion_rate_lookup(gu_name: str = "") -> float:
    """자치구별 전월세 전환율 반환 (연%, 한국부동산원 통계 기반).

    근거: 주택임대차보호법 제7조의2, 시행령 제9조
    """
    return _CONVERSION_RATE_TABLE.get(gu_name, _CONVERSION_RATE_DEFAULT)


def detect_dup_limit(p: dict) -> bool:
    """정책 텍스트에서 중복 수혜 제한 키워드 탐지."""
    text = _extract_policy_text(p)
    return any(kw in text for kw in DUP_LIMIT_KEYWORDS)


def _auto_match_labels(p: dict, user_info: Dict[str, str]) -> str:
    """자동 매칭 확인된 조건 목록을 문자열로 반환 (후보 목록 표시용)."""
    labels = []
    if user_info.get("age") and (p.get("sprtTrgtMinAge") or p.get("sprtTrgtMaxAge")):
        labels.append("나이")
    earn_max = str(p.get("earnMaxAmt", "0"))
    earn_cd  = str(p.get("earnCndSeCd", ""))
    has_income_field = earn_max not in ("0", "", "None") or (earn_cd and earn_cd != "0043001")
    if user_info.get("income_manwon") and has_income_field:
        labels.append("소득")
    if user_info.get("marriage") and p.get("mrgSttsCd") and p.get("mrgSttsCd") != "0055003":
        labels.append("혼인")
    if user_info.get("education") and p.get("schoolCd") and p.get("schoolCd") != "0049005":
        labels.append("학력")
    if not labels:
        return ""
    return "✅ " + " · ".join(labels) + " 자동매칭"


# ──────────────────────────────────────────────────────────
# 충돌 라벨링 + 선택 검증
# ──────────────────────────────────────────────────────────
def _classify_conflict(policy: dict, benefit_type: str) -> dict:
    """정책의 중복 수혜 충돌 카테고리 분류.

    Returns: {
        "level": "strict" | "same_type" | "none",
        "label": str,
        "warning_text": str,
    }
    """
    level = CATEGORY_CONFLICT_RULES.get(benefit_type, "none")

    # 텍스트에 중복 불가 키워드가 명시돼 있으면 same_type 격상
    if level == "none" and detect_dup_limit(policy):
        level = "same_type"

    warning_map = {
        "strict":    "* 입주 시 다른 주거 정책 동시 신청 불가",
        "same_type": f"* 동일 유형({benefit_type}) 정책끼리 중복 불가",
        "none":      "",
    }
    return {
        "level":        level,
        "label":        benefit_type,
        "warning_text": warning_map.get(level, ""),
    }


def validate_policy_selection(selected_policies: List[dict]) -> dict:
    """사용자가 선택한 정책들의 충돌 검증.

    Returns: {"valid": bool, "error": str | None}
    """
    if not selected_policies:
        return {"valid": True, "error": None}

    # strict 정책이 있고 다른 정책도 있으면 충돌
    strict_policies = [
        p for p in selected_policies
        if p.get("_conflict_label", {}).get("level") == "strict"
    ]
    if strict_policies and len(selected_policies) > 1:
        names = [p.get("plcyNm", "") for p in strict_policies]
        return {
            "valid": False,
            "error": f"{', '.join(names)}은(는) 다른 주거 정책과 동시 신청이 불가합니다.",
        }

    # 같은 same_type 카테고리 정책이 2개 이상이면 충돌
    by_label: Dict[str, list] = {}
    for p in selected_policies:
        cl = p.get("_conflict_label", {})
        if cl.get("level") == "same_type":
            by_label.setdefault(cl.get("label", ""), []).append(p)

    for label, policies in by_label.items():
        if len(policies) >= 2:
            names = [p.get("plcyNm", "") for p in policies]
            return {
                "valid": False,
                "error": f"{label} 정책 ({', '.join(names)}) 중 하나만 선택 가능합니다.",
            }

    return {"valid": True, "error": None}


# ──────────────────────────────────────────────────────────
# 1차 필터링 (챗봇용) — 소득/혼인/무주택은 검증 안 함
# ──────────────────────────────────────────────────────────
def fetch_candidates_basic(
    user_info: Dict[str, str],
    gu_name: str,
) -> List[dict]:
    """
    1차 필터링: 나이/취업/학력/지역/주거관련/절감액>0 만으로 후보 추출.
    소득/혼인/무주택은 검증하지 않음 (사용자가 카드 선택 후 따로 받음).

    Returns: 절감액 내림차순 정렬된 후보 정책 리스트.
             각 정책에 _monthly_saving, _benefit_type, _benefit_desc,
             _is_housing, _conflict_label 메타데이터 포함.
    """
    global _POLICY_CACHE

    # 테스트 모드
    source = MOCK_POLICIES if _TEST_MODE else None
    if source is None:
        if not _POLICY_CACHE:
            _POLICY_CACHE = _fetch_all_policies(max_pages=10, page_size=100)
        source = _POLICY_CACHE

    # 단계별 카운터 (디버그용)
    counts = {"total": len(source), "seoul": 0, "age": 0, "emp": 0,
              "edu": 0, "housing": 0, "saving": 0}

    candidates = []
    for p in source:
        if not _is_seoul_policy(p, gu_name):
            continue
        counts["seoul"] += 1
        if not _check_age(p, user_info.get("age", "")):
            continue
        counts["age"] += 1
        if not _check_employment(p, user_info.get("employment", "")):
            continue
        counts["emp"] += 1
        if not _check_education(p, user_info.get("education", "")):
            continue
        counts["edu"] += 1
        if not _is_housing_related(p):
            continue
        counts["housing"] += 1

        benefit = analyze_benefit(p, gu_name)
        if benefit["monthly_saving"] <= 0:
            continue
        counts["saving"] += 1

        # 메타데이터 부착 (dict 원본 복사 후 수정)
        p = dict(p)
        p["_monthly_saving"]  = benefit["monthly_saving"]
        p["_benefit_type"]    = benefit["benefit_type"]
        p["_benefit_desc"]    = benefit["benefit_desc"]
        p["_is_housing"]      = benefit["is_housing"]
        p["_conflict_label"]  = _classify_conflict(p, benefit["benefit_type"])
        p["_auto_match"]      = _auto_match_labels(p, user_info)
        candidates.append(p)

    print(f"[정책 1차 필터링 funnel] gu={gu_name}, "
          f"emp={user_info.get('employment','')}, edu={user_info.get('education','')}, "
          f"age={user_info.get('age','')}")
    print(f"  total={counts['total']} → 서울={counts['seoul']} → 나이={counts['age']} → "
          f"취업={counts['emp']} → 학력={counts['edu']} → 주거={counts['housing']} → "
          f"절감>0={counts['saving']}")

    # 절감액 순 정렬 + 정책명 중복 제거
    candidates.sort(key=lambda x: x["_monthly_saving"], reverse=True)
    seen: set = set()
    deduped = []
    for p in candidates:
        nm = p.get("plcyNm", "")
        if nm and nm not in seen:
            seen.add(nm)
            deduped.append(p)

    return deduped


# ──────────────────────────────────────────────────────────
# 혜택 분석 (v6.0 — CSV 명시 필드 직접 사용)
# ──────────────────────────────────────────────────────────
def analyze_benefit(p: dict, gu_name: str = "") -> dict:
    """CSV 화이트리스트의 명시 필드를 그대로 사용 (정규식 폴백 제거).

    benefit_amount_manwon: CSV 명시 절감액 (만원/월). 0이면 표시용 정책.
    support_type:          월세지원/전세대출/공공임대/이자지원/보증/감면 등
    benefit_desc:          CSV 명시 설명문
    """
    saving = float(p.get("benefit_amount_manwon", 0) or 0)
    return {
        "is_housing":     True,            # CSV는 모두 주거 정책
        "benefit_type":   p.get("support_type", ""),
        "monthly_saving": saving,
        "benefit_desc":   p.get("benefit_desc", ""),
        "saving_pct":     round(saving / MARKET_RENT_REFERENCE_MANWON * 100, 1) if saving > 0 else 0.0,
    }


# ──────────────────────────────────────────────────────────
# 상세조건 표시 + 사용자 확인
# ──────────────────────────────────────────────────────────
def _format_detail_conditions(p: dict) -> List[str]:
    """정책의 상세 조건을 사람이 읽을 수 있는 문장 리스트로 반환."""
    conditions = []

    # 나이
    min_a = p.get("sprtTrgtMinAge", "")
    max_a = p.get("sprtTrgtMaxAge", "")
    if min_a or max_a:
        conditions.append(f"나이: 만 {min_a}~{max_a}세")

    # 혼인상태
    mrg = MARRIAGE_CODE.get(str(p.get("mrgSttsCd", "")), "")
    if mrg and mrg != "제한없음":
        conditions.append(f"혼인상태: {mrg}")

    # 소득조건
    earn_cd = INCOME_CODE.get(str(p.get("earnCndSeCd", "")), "")
    earn_min = p.get("earnMinAmt", "0")
    earn_max = p.get("earnMaxAmt", "0")
    if earn_cd and earn_cd != "제한없음":
        conditions.append(f"소득조건: {earn_cd}")
    elif str(earn_max) != "0" and earn_max:
        conditions.append(f"소득: 최대 {earn_max}만원 이하")

    # 학력
    school = SCHOOL_CODE.get(str(p.get("schoolCd", "")), "")
    if school and school != "제한없음":
        conditions.append(f"학력: {school}")

    # 추가 자격 조건 (텍스트)
    add_cond = str(p.get("addAplyQlfcCndCn", "")).strip()
    if add_cond and add_cond != "-":
        # 너무 길면 축약
        if len(add_cond) > 100:
            add_cond = add_cond[:100] + "..."
        conditions.append(f"추가조건: {add_cond}")

    # 참여 대상
    target = str(p.get("ptcpPrpTrgtCn", "")).strip()
    if target and target != "-":
        if len(target) > 100:
            target = target[:100] + "..."
        conditions.append(f"참여대상: {target}")

    # 선착순 여부
    if str(p.get("sprtArvlSeqYn", "")) == "Y":
        cnt = p.get("sprtSclCnt", "")
        cnt_str = f" ({cnt}명)" if cnt and str(cnt) != "0" else ""
        conditions.append(f"선착순 모집{cnt_str}")

    # 사업기간
    bg = str(p.get("bizPrdBgngYmd", ""))
    ed = str(p.get("bizPrdEndYmd", ""))
    if bg and ed:
        bg_fmt = f"{bg[:4]}.{bg[4:6]}.{bg[6:]}" if len(bg) == 8 else bg
        ed_fmt = f"{ed[:4]}.{ed[4:6]}.{ed[6:]}" if len(ed) == 8 else ed
        conditions.append(f"사업기간: {bg_fmt} ~ {ed_fmt}")

    return conditions


def verify_policy_with_user(p: dict, idx: int) -> bool:
    """
    선택한 정책의 상세 조건을 표시하고 사용자에게 충족 여부를 확인.

    Returns: True(충족) / False(미충족)
    """
    name = p.get("plcyNm", "정책명 없음")
    conditions = _format_detail_conditions(p)

    print(f"\n    ── 정책 [{idx}] 상세 조건 확인 ──")
    print(f"    정책명: {name}")

    if not conditions:
        print(f"    상세 조건: 별도 제한 없음")
        return True

    print(f"    상세 조건:")
    for c in conditions:
        print(f"      · {c}")

    # 신청 URL
    ref = p.get("refUrlAddr1", "") or p.get("aplyUrlAddr", "")
    if ref:
        print(f"    원문 확인: {ref}")

    while True:
        answer = input(f"    → 위 조건에 해당하십니까? (y=예 / n=아니요 / s=잘 모르겠음): ").strip().lower()
        if answer == "y":
            return True
        elif answer == "n":
            return False
        elif answer == "s":
            print(f"    → '잘 모르겠음' → 일단 포함하되 신뢰도 낮음으로 표시합니다.")
            p["_uncertain"] = True
            return True
        print("    ※ y, n, s 중 하나를 입력해 주세요.")


# ──────────────────────────────────────────────────────────
# 메인 흐름: 1차 필터 → 사용자 선택 → 2차 확인 → 점수
# ──────────────────────────────────────────────────────────
def policy_selection_flow(all_policies: List[dict],
                           user_info: Dict[str, str],
                           gu_name: str = "",
                           max_display: int = 3,
                           user_budget_monthly: float = 50.0,
                           auto_confirm: bool = False) -> Tuple[float, List[dict]]:
    """
    1차) 기본 필터(서울/나이/취업/소득/혼인/학력/무주택) + 주거 혜택 분석
    2차) 후보 목록 표시 → 사용자 선택
    3차) 상세 조건 확인
    4차) 확인된 정책만 점수 반영 (사용자 예산 대비 비율)

    Returns: (점수 0~100, 확인된 정책 리스트)
    """
    # ── 1차 필터링 ──
    candidates = []
    for p in all_policies:
        if not _is_seoul_policy(p, gu_name):
            continue
        if not _check_age(p, user_info.get("age", "")):
            continue
        if not _check_employment(p, user_info.get("employment", "")):
            continue
        if not _check_income(p, user_info.get("income_manwon", "")):
            continue
        if not _check_marriage(p, user_info.get("marriage", "")):
            continue
        if not _check_education(p, user_info.get("education", "")):
            continue
        if not _check_no_house(p, user_info.get("no_house", "")):
            continue

        benefit = analyze_benefit(p, gu_name)
        if benefit["monthly_saving"] > 0:
            p["_monthly_saving"] = benefit["monthly_saving"]
            p["_benefit_type"] = benefit["benefit_type"]
            p["_benefit_desc"] = benefit["benefit_desc"]
            p["_saving_pct"] = benefit["saving_pct"]
            p["_is_housing"] = benefit["is_housing"]
            p["_uncertain"] = False
            candidates.append(p)

    # 절감액 순 정렬 + 중복 제거
    candidates.sort(key=lambda x: x["_monthly_saving"], reverse=True)
    seen = set()
    deduped = []
    for p in candidates:
        nm = p.get("plcyNm", "")
        if nm and nm not in seen:
            seen.add(nm)
            deduped.append(p)
    candidates = deduped

    if not candidates:
        print(f"\n  [{gu_name}] 주거비 절감에 도움되는 정책이 없습니다.")
        return 0.0, []

    # ── auto_confirm 모드: 챗봇/Streamlit에서 자동 선택 ──
    if auto_confirm:
        top_n = min(max_display, len(candidates))
        verified = candidates[:top_n]
        for p in verified:
            p.setdefault("_uncertain", False)
        print(f"\n  [{gu_name}] 청년정책 {top_n}건 자동 매칭")
        for i, p in enumerate(verified, 1):
            print(f"  [{i}] {p.get('plcyNm','')}  (월 약 {p['_monthly_saving']:.0f}만원 절감)")

        top_savings = [p["_monthly_saving"] for p in verified[:3]]
        total = sum(top_savings)
        ref = max(user_budget_monthly, 1.0)
        score = min(100.0, round(total / ref * 100, 2))
        uncertain_count = sum(1 for p in verified[:3] if p.get("_uncertain"))
        if uncertain_count > 0:
            penalty = 0.7 + 0.1 * (3 - uncertain_count)
            score = round(score * penalty, 2)

        print(f"\n  ✅ 자동 반영: {len(verified)}건 / 정책 점수: {score:.0f}점")
        dup_policies = [p for p in verified if detect_dup_limit(p)]
        if dup_policies:
            print(f"\n  ⚠ 중복 수혜 제한 가능성:")
            for p in dup_policies:
                print(f"    - {p.get('plcyNm', '')}")
        return score, verified

    # ── 2차: 후보 목록 표시 → 사용자 선택 ──
    print(f"\n  [{gu_name}] 활용 가능한 청년정책 {len(candidates)}건 (1차 필터 통과)")
    print(f"  ────────────────────────────────────────")

    display_count = min(len(candidates), max(max_display + 2, 8))  # 선택지를 넉넉히 표시
    for i, p in enumerate(candidates[:display_count], 1):
        name = p.get("plcyNm", "")
        saving = p["_monthly_saving"]
        desc = p["_benefit_desc"]
        icon = "🏠" if p["_is_housing"] else "💼"
        org = p.get("sprvsnInstCdNm", "")
        min_a = p.get("sprtTrgtMinAge", "")
        max_a = p.get("sprtTrgtMaxAge", "")
        age_str = f" (만 {min_a}~{max_a}세)" if min_a or max_a else ""
        match_label = _auto_match_labels(p, user_info)

        print(f"  {icon} [{i:>2}] {name}{age_str}")
        if match_label:
            print(f"        {match_label}")
        print(f"        혜택: {desc}  |  월 약 {saving:.0f}만원 절감")
        if org:
            print(f"        주관: {org}")

    if len(candidates) > display_count:
        print(f"  ... 외 {len(candidates) - display_count}건")

    print(f"\n  확인할 정책 번호를 선택하세요 (쉼표로 복수 선택 가능)")
    print(f"  예) 1,2,3  또는  전체 확인하려면 'a'  또는  건너뛰려면 Enter")
    sel_input = input(f"  선택: ").strip()

    if not sel_input:
        print(f"  → 건너뜀 (정책 점수 미반영)")
        return 0.0, []

    # 선택 번호 파싱
    if sel_input.lower() == "a":
        selected_indices = list(range(min(display_count, len(candidates))))
    else:
        selected_indices = []
        for tok in sel_input.replace(" ", "").split(","):
            if tok.isdigit():
                idx = int(tok) - 1
                if 0 <= idx < len(candidates):
                    selected_indices.append(idx)

    if not selected_indices:
        print(f"  → 유효한 선택 없음 (정책 점수 미반영)")
        return 0.0, []

    # ── 3차: 선택한 정책별 상세 조건 확인 ──
    print(f"\n  선택한 {len(selected_indices)}건의 정책에 대해 상세 조건을 확인합니다.")
    verified = []

    for idx in selected_indices:
        p = candidates[idx]
        if verify_policy_with_user(p, idx + 1):
            verified.append(p)
            status = "✅ 조건 충족" + (" (불확실)" if p.get("_uncertain") else "")
            print(f"    → {status}")
        else:
            print(f"    → ❌ 조건 미충족 (점수 미반영)")

    if not verified:
        print(f"\n  조건을 충족하는 정책이 없습니다. (정책 점수 미반영)")
        return 0.0, []

    # ── 4차: 확인된 정책만으로 점수 계산 (Phase 6-4: 사용자 예산 대비 비율) ──
    top_savings = [p["_monthly_saving"] for p in verified[:3]]
    total = sum(top_savings)
    ref = max(user_budget_monthly, 1.0)
    score = min(100.0, round(total / ref * 100, 2))

    # 불확실 정책이 포함된 경우 점수 감소
    uncertain_count = sum(1 for p in verified[:3] if p.get("_uncertain"))
    if uncertain_count > 0:
        penalty = 0.7 + 0.1 * (3 - uncertain_count)
        score = round(score * penalty, 2)

    print(f"\n  ✅ 조건 확인 완료: {len(verified)}건 반영")
    print(f"     월 절감 추정: 약 {total:.0f}만원")
    print(f"     정책 점수: {score:.0f}점 (예산 {ref:.0f}만원 대비 {total/ref*100:.0f}%)")

    # ── 중복 수혜 경고 (Phase 6-5) ──
    dup_policies = [p for p in verified if detect_dup_limit(p)]
    if dup_policies:
        print(f"\n  ⚠ 중복 수혜 제한 가능성 있는 정책:")
        for p in dup_policies:
            print(f"    - {p.get('plcyNm', '')}")
        print(f"  → 중복 수혜 가능 여부는 각 정책 신청 URL에서 확인 필요")

    return score, verified


# ──────────────────────────────────────────────────────────
# 구 단위 정책 조회 (run_recommendation에서 호출)
# ──────────────────────────────────────────────────────────
# 전역 캐시: 전체 정책은 1회만 조회
_POLICY_CACHE: List[dict] = []

# ──────────────────────────────────────────────────────────
# 테스트 모드: test_v5_auto.py에서 True로 설정하면 API 호출 없이
# MOCK_POLICIES로 동작 (Phase 6-4~6-6 코드 경로 검증용)
# ──────────────────────────────────────────────────────────
_TEST_MODE: bool = False

MOCK_POLICIES: List[dict] = [
    {
        "plcyNm": "[테스트] 서울시 청년 월세 지원",
        "plcySprtCn": "월 20만원 주거비 지원",
        "plcyExplnCn": "만 19~39세 청년 무주택자 대상 월 20만원 주거비 지원.",
        "lclsfNm": "주거",
        "sprtTrgtMinAge": "19",
        "sprtTrgtMaxAge": "39",
        "zipCd": "11440",
        "sprvsnInstCdNm": "서울특별시",
        "earnCndSeCd": "0043001",
        "mrgSttsCd": "0055003",
        "schoolCd": "0049005",
        "refUrlAddr1": "https://youth.seoul.go.kr",
    },
    {
        "plcyNm": "[테스트] 마포구 청년 보증금 지원",
        "plcySprtCn": "보증금 500만원 무이자 대출 지원",
        "plcyExplnCn": "마포구 1인 청년가구 임차보증금 500만원 무이자 대출.",
        "lclsfNm": "주거",
        "sprtTrgtMinAge": "18",
        "sprtTrgtMaxAge": "34",
        "zipCd": "11440",
        "sprvsnInstCdNm": "마포구",
        "earnCndSeCd": "0043001",
        "earnMaxAmt": "250",
        "mrgSttsCd": "0055001",
        "schoolCd": "0049005",
        "refUrlAddr1": "https://www.mapo.go.kr",
    },
    {
        "plcyNm": "[테스트] 행복주택 입주 지원",
        "plcySprtCn": "공공임대 시세 60% 수준 제공. 타 사업과 중복 불가.",
        "plcyExplnCn": "청년 대상 행복주택 입주 지원. 유사 사업과 중복 신청 불가.",
        "lclsfNm": "주거",
        "sprtTrgtMinAge": "19",
        "sprtTrgtMaxAge": "39",
        "zipCd": "11440",
        "sprvsnInstCdNm": "한국토지주택공사",
        "earnCndSeCd": "0043001",
        "mrgSttsCd": "0055003",
        "schoolCd": "0049005",
        "refUrlAddr1": "https://apply.lh.or.kr",
    },
]


# ──────────────────────────────────────────────────────────
# 매물별 정책 매칭 (v6.0 — 점수 산출 X, 표시용)
# ──────────────────────────────────────────────────────────

# ── 임대유형 → 통과 가능한 support_type 화이트리스트 ────
_RENT_TYPE_SUPPORT_FILTER: Dict[str, List[str]] = {
    "월세": ["월세지원", "월세대출", "보증", "보증료", "감면",
            "이자지원", "공공임대", "대출"],
    "전세": ["전세대출", "보증", "보증료", "감면",
            "이자지원", "공공임대", "대출"],
}


# 정책 이름·조건에 다른 임대유형 전용 표현이 있으면 제외
# (support_type 이 '보증'/'대출' 처럼 공통이어도 실제로는 전세 전용인 정책이 있음 — 예: 버팀목)
_RENT_TYPE_EXCLUSIVE_KEYWORDS: Dict[str, List[str]] = {
    "전세": ["전세", "버팀목"],
    "월세": ["월세"],
}


def _is_other_rent_type_only(p: dict, rent_type: str) -> bool:
    """정책이 rent_type 과 다른 임대유형 전용이면 True."""
    other = "전세" if rent_type == "월세" else "월세"
    text = " ".join(str(p.get(k, "")) for k in ("policy_name", "benefit_desc", "extra_conditions"))
    mentions_other = any(kw in text for kw in _RENT_TYPE_EXCLUSIVE_KEYWORDS[other])
    mentions_self = any(kw in text for kw in _RENT_TYPE_EXCLUSIVE_KEYWORDS[rent_type])
    return mentions_other and not mentions_self


def _parse_manwon_amount(text: str) -> Optional[float]:
    """'8천만원', '1억', '3억원', '60만원' → 만원 단위 숫자."""
    t = text.replace(",", "").replace(" ", "")
    m = re.match(r"(\d+(?:\.\d+)?)억(?:(\d+)천)?", t)
    if m:
        return float(m.group(1)) * 10000 + (int(m.group(2)) * 1000 if m.group(2) else 0)
    m = re.match(r"(\d+)천", t)
    if m:
        return int(m.group(1)) * 1000.0
    m = re.match(r"(\d+(?:\.\d+)?)만", t)
    if m:
        return float(m.group(1))
    return None


def _price_caps(p: dict) -> Dict[str, float]:
    """정책 조건 문구의 매물 가격 상한 추출 — {'monthly': 60, 'deposit': 8000} (만원)."""
    text = " ".join(str(p.get(k, "")) for k in ("extra_conditions", "benefit_desc"))
    caps: Dict[str, float] = {}
    m = re.search(r"월세\s*([\d.,]+\s*만)\s*원?\s*이하", text)
    if m:
        v = _parse_manwon_amount(m.group(1))
        if v is not None:
            caps["monthly"] = v
    m = re.search(r"보증금\s*([\d.,]+\s*(?:억(?:\s*\d+\s*천)?|천\s*만?|만))\s*원?\s*이하", text)
    if m:
        v = _parse_manwon_amount(m.group(1))
        if v is not None:
            caps["deposit"] = v
    return caps


def filter_policies_by_property(policies: List[dict],
                                deposit_manwon=None,
                                monthly_rent_manwon=None) -> List[dict]:
    """매물 가격이 정책 조건(월세·보증금 상한)을 넘으면 제외.

    match_policies_for_property 는 (자치구, 임대유형) 단위로 캐시되므로,
    매물마다 다른 가격 조건은 이 함수로 한 번 더 거른다. 가격을 모르면(None) 통과.
    """
    def _num(v):
        try:
            f = float(v)
            return None if f != f else f   # NaN → None
        except (TypeError, ValueError):
            return None

    dep = _num(deposit_manwon)
    mon = _num(monthly_rent_manwon)
    out = []
    for pol in policies:
        caps = pol.get("_price_caps")
        if caps is None:
            caps = _price_caps(pol)
        if mon is not None and "monthly" in caps and mon > caps["monthly"]:
            continue
        if dep is not None and "deposit" in caps and dep > caps["deposit"]:
            continue
        out.append(pol)
    return out


def _build_policy_tags(p: dict) -> List[str]:
    """정책 자격 조건을 사람이 읽을 수 있는 짧은 태그로 변환 (상위 2~3개)."""
    tags = []
    age_min = p.get("target_age_min", 0)
    age_max = p.get("target_age_max", 99)
    if age_min and age_max and age_max < 99:
        tags.append(f"만 {age_min}~{age_max}세")
    elif age_min:
        tags.append(f"만 {age_min}세 이상")
    inc = (p.get("target_income") or "").strip()
    if inc and inc not in ("", "전체", "제한없음"):
        if len(inc) > 18:
            inc = inc[:18] + "…"
        tags.append(inc)
    nh = (p.get("target_no_house") or "").strip()
    if nh in ("필수", "임차가구만"):
        tags.append("무주택 필수" if nh == "필수" else "임차가구")
    emp = (p.get("target_employment") or "").strip()
    if emp and emp not in ("", "전체", "제한없음"):
        tags.append(emp)
    return tags[:3]


def _evaluate_policy_eligibility(p: dict, user_info: Dict[str, str]) -> str:
    """정책 자격을 평가해 'eligible' / 'needs_check' / 'ineligible' 반환.

    - eligible:    모든 자격 명확히 충족
    - needs_check: 자격 미정(사용자 모름 또는 정책 텍스트 모호)
    - ineligible:  명확한 미달 (나이/취업/학력 또는 명시 소득/무주택 미달)
    """
    age = user_info.get("age", "")
    emp = user_info.get("employment", "")
    edu = user_info.get("education", "")
    income_band = user_info.get("income_band", "")
    no_house    = user_info.get("no_house", "")

    # 명확 미달 — 나이/취업/학력
    if not _check_age(p, age):           return "ineligible"
    if not _check_employment(p, emp):    return "ineligible"
    if not _check_education(p, edu):     return "ineligible"

    # 소득/무주택 — 자격 미정 가능
    target_inc = (p.get("target_income") or "").strip()
    target_nh  = (p.get("target_no_house") or "").strip()
    income_uncertain  = (
        target_inc not in ("", "전체", "제한없음")
        and (INCOME_BAND_TO_MANWON.get(income_band) is None
             or _parse_income_threshold(target_inc) is None)
    )
    no_house_uncertain = (
        target_nh in ("필수", "임차가구만") and no_house == ""
    )
    if not _check_income(p, income_band):       return "ineligible"
    if not _check_no_house(p, no_house):        return "ineligible"

    if income_uncertain or no_house_uncertain:
        return "needs_check"
    return "eligible"


def match_policies_for_property(
    gu_name: str,
    rent_type: str,                        # "월세" / "전세"
    user_info: Dict[str, str],
    top_n: int = 5,
) -> Tuple[List[dict], int]:
    """매물(자치구+임대유형)에 적용 가능한 정책 매칭.

    필터:
      1. 자치구 zip_codes 매칭
      2. 회원정보(나이/취업/학력) — 미달이면 제외
      3. 임대유형 — support_type 화이트리스트
      4. 자격 평가 — eligible / needs_check / ineligible (ineligible은 제외)

    정렬:
      1차: eligible 우선 (eligible > needs_check)
      2차: 자치구 한정(is_gu_specific=True) 우선
      3차: benefit_amount_manwon 내림차순

    Returns: (상위 top_n 정책 리스트, 전체 매칭 건수)
    """
    global _POLICY_CACHE
    if not _POLICY_CACHE:
        _POLICY_CACHE = _load_policies_from_csv()
    if not _POLICY_CACHE:
        return [], 0

    rent_filter = _RENT_TYPE_SUPPORT_FILTER.get(rent_type, [])

    matched = []
    for p in _POLICY_CACHE:
        # 1. 자치구
        if not _is_seoul_policy(p, gu_name):
            continue
        # 2. 회원정보 — 명확 미달 제외 (소득/무주택은 후속 단계)
        if not _check_age(p, user_info.get("age", "")):              continue
        if not _check_employment(p, user_info.get("employment", "")):continue
        if not _check_education(p, user_info.get("education", "")):  continue
        # 3. 임대유형 — support_type 화이트리스트 + 다른 임대유형 전용 정책 제외
        st = p.get("support_type", "")
        if rent_filter and st not in rent_filter:
            continue
        if rent_type in _RENT_TYPE_EXCLUSIVE_KEYWORDS and _is_other_rent_type_only(p, rent_type):
            continue
        # 4. 자격 평가
        status = _evaluate_policy_eligibility(p, user_info)
        if status == "ineligible":
            continue

        # 자치구 한정 정책 표시 (zip_codes 5개 이하)
        is_gu_specific = len(p.get("zip_codes", [])) <= 5

        matched.append({
            "policy_id":             p.get("policy_id", ""),
            "policy_name":           p.get("policy_name", ""),
            "support_type":          st,
            "benefit_desc":          p.get("benefit_desc", ""),
            "benefit_amount_manwon": float(p.get("benefit_amount_manwon", 0) or 0),
            "benefit_period_months": int(p.get("benefit_period_months", 0) or 0),
            "eligibility_status":    status,
            "extra_conditions":      p.get("extra_conditions", ""),
            "tags":                  _build_policy_tags(p),
            "apply_url":             p.get("apply_url", ""),
            "is_gu_specific":        is_gu_specific,
            "source_org":            p.get("source_org", ""),
        })

    # 정렬: eligible 우선 → 자치구 한정 우선 → 절감액 큰 순
    def _sort_key(item):
        status_rank = 0 if item["eligibility_status"] == "eligible" else 1
        gu_rank     = 0 if item["is_gu_specific"] else 1
        return (status_rank, gu_rank, -item["benefit_amount_manwon"])
    matched.sort(key=_sort_key)

    total_count = len(matched)
    return matched[:top_n], total_count


def fetch_policies_for_gu(gu_name: str,
                           user_info: Dict[str, str],
                           max_display: int = 3,
                           user_budget_monthly: float = 50.0,
                           auto_confirm: bool = False) -> Tuple[float, List[dict]]:
    """
    추천된 주거지 구에 대한 청년정책 조회 + 선택 + 확인 + 점수.
    전체 정책은 전역 캐시에서 재사용.
    _TEST_MODE=True 이면 API 호출 없이 MOCK_POLICIES 사용.
    auto_confirm=True 이면 input() 없이 자동 선택 (챗봇/Streamlit 모드).
    """
    global _POLICY_CACHE

    if _TEST_MODE:
        print(f"\n  [테스트 모드] mock 정책 {len(MOCK_POLICIES)}건 사용 (API 호출 없음)")
        return policy_selection_flow(
            MOCK_POLICIES, user_info, gu_name, max_display, user_budget_monthly,
            auto_confirm=auto_confirm,
        )

    if not _POLICY_CACHE:
        print(f"\n  [온통청년 API] 전체 정책 조회 중...")
        _POLICY_CACHE = _fetch_all_policies(max_pages=5, page_size=50)
        if _POLICY_CACHE:
            print(f"  → 전체 {len(_POLICY_CACHE)}건 조회 완료")
        else:
            print(f"  → 정책 데이터 없음")
            return 0.0, []

    return policy_selection_flow(
        _POLICY_CACHE, user_info, gu_name, max_display, user_budget_monthly,
        auto_confirm=auto_confirm,
    )


def reset_policy_cache():
    """정책 캐시 초기화 (새 세션 시작 시)"""
    global _POLICY_CACHE
    _POLICY_CACHE = []


# ──────────────────────────────────────────────────────────
# 출력 함수 (최종 결과에서 호출)
# ──────────────────────────────────────────────────────────
def print_policy_section(gu_name: str, score: float,
                          matched_policies: List[dict],
                          max_display: int = 3,
                          conv_deposit: float = None,
                          user_info: Dict = None,
                          user_budget_monthly: float = 50.0) -> None:
    """추천 결과 내 [청년정책 혜택] 섹션 출력 — 이미 검증된 정책만."""
    print("  [청년정책 혜택]")
    if not matched_policies:
        print("    · 조건 확인된 정책 없음")
        return

    top = matched_policies[:max_display]
    top_savings = [p["_monthly_saving"] for p in top]
    total_saving = sum(top_savings)
    ref_budget = max(user_budget_monthly, 1.0)

    print(f"    · 정책 점수: {score:.0f}점  (예산 {ref_budget:.0f}만원 대비 절감 비율)")
    print(f"    · 적용 정책 {len(top)}건:")

    for idx, p in enumerate(top, 1):
        name = p.get("plcyNm", "정책명 없음")
        saving = p["_monthly_saving"]
        desc = p["_benefit_desc"]
        btype = p.get("_benefit_type", "")
        icon = "🏠" if p.get("_is_housing") else "💼"
        uncertain = " ⚠불확실" if p.get("_uncertain") else ""
        reliability = _BENEFIT_RELIABILITY.get(btype, "")

        min_a = p.get("sprtTrgtMinAge", "")
        max_a = p.get("sprtTrgtMaxAge", "")
        age_str = f" (만 {min_a}~{max_a}세)" if min_a or max_a else ""

        match_label = _auto_match_labels(p, user_info) if user_info else ""

        print(f"    {icon} [{idx}] {name}{age_str}{uncertain}")
        if match_label:
            print(f"         {match_label}")
        print(f"         혜택: {desc}")
        print(f"         월 절감: 약 {saving:.0f}만원")
        if reliability:
            print(f"         산출 근거: {reliability}")

        ref = p.get("refUrlAddr1", "") or p.get("aplyUrlAddr", "")
        if ref:
            print(f"         신청 URL: {ref}")

    if total_saving > 0:
        print(f"    ──────────────────────────────")
        print(f"    · 예상 총 월 절감액: 약 {total_saving:.0f}만원")
        if conv_deposit and conv_deposit > 0:
            conv_rate = conversion_rate_lookup(gu_name) / 100
            monthly_equiv = conv_deposit * conv_rate / 12
            if monthly_equiv > 0:
                pct = total_saving / monthly_equiv * 100
                print(f"    · 이 매물 월 환산 주거비({monthly_equiv:.0f}만원) 대비 약 {pct:.0f}% 절감 추정")

    # 중복 수혜 경고
    dup_policies = [p for p in top if detect_dup_limit(p)]
    if dup_policies:
        print(f"    ⚠ 중복 수혜 제한 가능성:")
        for p in dup_policies:
            print(f"      - {p.get('plcyNm', '')}")

    remaining = len(matched_policies) - max_display
    if remaining > 0:
        print(f"    · ...외 {remaining}건의 추가 확인 정책이 있습니다.")

    print(f"    ──────────────────────────────")
    print(f"    ※ 절감액은 동네 간 상대 비교용 추정값입니다.")
    print(f"       정확한 자격·혜택은 각 정책 신청 URL에서 직접 확인하세요.")


def is_youth_api_configured() -> bool:
    """[v6.0] CSV 화이트리스트 파일 존재 여부 (구 버전: API 키 검사)."""
    return os.path.exists(CSV_PATH)
