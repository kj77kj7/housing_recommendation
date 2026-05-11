"""
llm_module.py
─────────────
Gemini 2.5 Flash-Lite 슬롯 추출 모듈

흐름:
  extract_slots_from_text(text, current_slots)
    → call_gemini() 시도
    → 실패 시 _fallback_extraction() (정규식)

폴백 시나리오:
  A. API 키 미설정  → 즉시 폴백
  B. 네트워크/5xx  → 지수 백오프 3회 → 폴백
  C. JSON 파싱 실패 → 1회 재시도 → 폴백
  D. 일일 한도(429 daily) → 즉시 폴백
  E. 분당 한도(429 rate)  → 4초 대기 1회 재시도 → 폴백

한도: 분당 15회, 일 1,000회 (Gemini Flash-Lite 무료)
콘솔 경고: 800회 도달 시 80% 알림
"""

import os
import re
import json
import time
import datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.5-flash-lite"
PROMPTS_DIR    = Path(__file__).parent / "prompts"

# ──────────────────────────────────────────────────────────
# 일일 호출 카운터 (프로세스 메모리 내, 재시작 시 리셋)
# ──────────────────────────────────────────────────────────
_call_counter = {"date": None, "count": 0}
_DAILY_LIMIT  = 1_000
_WARN_AT      = 800


def _increment_counter() -> int:
    today = datetime.date.today().isoformat()
    if _call_counter["date"] != today:
        _call_counter["date"]  = today
        _call_counter["count"] = 0
    _call_counter["count"] += 1
    cnt = _call_counter["count"]
    if cnt == _WARN_AT:
        print(f"[LLM] 일일 한도 80% 도달 ({cnt}/{_DAILY_LIMIT})")
    return cnt


def _is_daily_limit_exceeded() -> bool:
    today = datetime.date.today().isoformat()
    if _call_counter["date"] != today:
        return False
    return _call_counter["count"] >= _DAILY_LIMIT


# ──────────────────────────────────────────────────────────
# 프롬프트 로드
# ──────────────────────────────────────────────────────────

def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ──────────────────────────────────────────────────────────
# Gemini API 호출
# ──────────────────────────────────────────────────────────

def call_gemini(system_prompt: str, user_message: str) -> Optional[str]:
    """
    Gemini API 호출. 성공 시 응답 텍스트 반환, 실패 시 None.
    재시도 로직 포함.
    """
    if not GEMINI_API_KEY:
        print("[LLM] API 키 없음, 폴백 모드")
        return None

    if _is_daily_limit_exceeded():
        print(f"[LLM] 일일 한도 초과({_DAILY_LIMIT}회), 폴백 모드")
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[LLM] google-genai 미설치, 폴백 모드")
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    delays = [1, 2, 4]
    for attempt, delay in enumerate(delays, 1):
        try:
            _increment_counter()
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{system_prompt}\n\n{user_message}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=400,
                ),
            )
            return response.text
        except Exception as e:
            err_str = str(e)
            if "429" in err_str:
                low = err_str.lower()
                # 분당/일일 구분: per-day/daily 마커가 있으면 일일, 그 외 quota 메시지는 분당으로 간주
                # Gemini는 분당 초과 시에도 "Quota exceeded" 표현을 쓰므로 'quota'만으로 일일 판정 불가
                is_daily = ("per_day" in low) or ("perday" in low) or ("daily" in low)
                if is_daily:
                    print(f"[LLM] 일일 한도 초과, 폴백")
                    return None
                # 분당 한도 — 응답에 'retry in Ns' 힌트가 있으면 그만큼 대기 (상한 10s)
                m = re.search(r"retry\s+in\s+([\d.]+)s", err_str.lower())
                wait_s = min(float(m.group(1)), 10.0) if m else 4.0
                if attempt < len(delays):
                    print(f"[LLM] 분당 한도 초과, {wait_s:.0f}s 후 재시도 (시도 {attempt}/{len(delays)})")
                    time.sleep(wait_s)
                    continue
                print(f"[LLM] 분당 한도 재시도 실패, 폴백")
                return None
            if attempt < len(delays):
                print(f"[LLM] 호출 실패 (시도 {attempt}), {delay}s 후 재시도: {err_str[:60]}")
                time.sleep(delay)
            else:
                print(f"[LLM] 호출 실패, 폴백: {err_str[:60]}")
                return None

    return None


# ──────────────────────────────────────────────────────────
# JSON 파싱
# ──────────────────────────────────────────────────────────

def _parse_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except json.JSONDecodeError:
        pass
    return None


# ──────────────────────────────────────────────────────────
# 정규식 폴백
# ──────────────────────────────────────────────────────────

def _fallback_extraction(text: str) -> Dict:
    from vibe_module import extract_vibe_from_text

    slots: Dict = {}

    if re.search(r"자가용|내 차|자차|자동차|차로", text):
        slots["transport_mode"] = "car"
    elif re.search(r"대중교통|지하철|버스|전철", text):
        slots["transport_mode"] = "transit"

    if "전세" in text:
        slots["rent_type"] = "전세"
    elif "월세" in text:
        slots["rent_type"] = "월세"

    # 보증금/전세금 파싱
    m = re.search(r"(\d+)\s*억\s*(\d+)\s*천", text)
    if m:
        slots["deposit_manwon"] = int(m.group(1)) * 10000 + int(m.group(2)) * 1000
    else:
        m = re.search(r"(\d+)\s*억", text)
        if m:
            slots["deposit_manwon"] = int(m.group(1)) * 10000
        else:
            m = re.search(r"(\d+)\s*천\s*만", text)
            if m:
                slots["deposit_manwon"] = int(m.group(1)) * 1000
            else:
                m = re.search(r"(\d+)\s*천(?!\s*만)", text)
                if m:
                    slots["deposit_manwon"] = int(m.group(1)) * 1000
                else:
                    m = re.search(r"(\d+)\s*만원?", text)
                    if m:
                        slots["deposit_manwon"] = int(m.group(1))

    m = re.search(r"월세\s*(\d+)\s*만?", text)
    if m:
        slots["monthly_manwon"] = int(m.group(1))

    _cm = None
    _m = re.search(r"(\d+)\s*시간\s*(\d+)\s*분", text)
    if _m:
        _cm = int(_m.group(1)) * 60 + int(_m.group(2))
    elif re.search(r"한\s*시간\s*반", text):
        _cm = 90
    elif re.search(r"(\d+)\s*시간\s*반", text):
        _m2 = re.search(r"(\d+)\s*시간", text)
        _cm = int(_m2.group(1)) * 60 + 30 if _m2 else None
    elif re.search(r"(\d+(?:\.\d+)?)\s*시간", text):
        _m2 = re.search(r"(\d+(?:\.\d+)?)\s*시간", text)
        _cm = int(float(_m2.group(1)) * 60) if _m2 else None
    elif re.search(r"한\s*시간", text):
        _cm = 60
    else:
        _m = re.search(r"(\d+)\s*분", text)
        if _m:
            _cm = int(_m.group(1))
    if _cm is not None:
        if re.search(r"왕복", text):
            _cm = _cm // 2
        if 5 <= _cm <= 300:
            slots["allowed_minutes"] = _cm

    gu = re.search(
        r"(강남|강동|강북|강서|관악|광진|구로|금천|노원|도봉|동대문|동작|마포|서대문|서초|성동|성북|송파|양천|영등포|용산|은평|종로|중구|중랑)구",
        text,
    )
    if gu:
        slots["region_filter"] = gu.group(0)

    # weight_preference
    if re.search(r"통근\s*우선|출퇴근\s*우선", text):
        slots["weight_preference"] = "통근우선"
    elif re.search(r"주거비\s*우선|비용\s*우선|저렴", text):
        slots["weight_preference"] = "주거비우선"
    elif re.search(r"균형|반반|고르게", text):
        slots["weight_preference"] = "균형"
    elif re.search(r"알아서|위임|맡겨", text):
        slots["weight_preference"] = "위임"

    if re.search(r"오피스텔", text):
        slots["house_type"] = "오피스텔"
    elif re.search(r"연립|다세대|빌라", text):
        slots["house_type"] = "연립다세대"

    if re.search(r"청년정책|청년 지원|정책 반영", text):
        slots["use_youth_policy"] = True
    elif re.search(r"정책 없|필요 없|정책 빼", text):
        slots["use_youth_policy"] = False

    vibe_result = extract_vibe_from_text(text)
    if vibe_result["matched"]:
        slots["vibe"] = vibe_result["matched"]
    if vibe_result["unrecognized"]:
        slots["vibe_unrecognized"] = vibe_result["unrecognized"]

    return slots


# ──────────────────────────────────────────────────────────
# 메인 진입점
# ──────────────────────────────────────────────────────────

def extract_slots_from_text(text: str, current_slots: Dict) -> Dict:
    """
    사용자 발화 → 새로운 슬롯 dict 반환.
    LLM 실패 시 정규식 폴백 자동 적용.
    """
    system_prompt = _load_prompt("slot_extraction.txt")
    if not system_prompt:
        return _fallback_extraction(text)

    context = (
        f"현재 슬롯: {json.dumps(current_slots, ensure_ascii=False)}\n"
        f"사용자 발화: {text}"
    )

    # 1차 시도
    raw = call_gemini(system_prompt, context)
    result = _parse_json(raw)

    # JSON 파싱 실패 시 1회 재시도
    if result is None and raw is not None:
        print("[LLM] JSON 파싱 실패, 1회 재시도")
        raw2 = call_gemini(system_prompt, context)
        result = _parse_json(raw2)

    if result is None:
        print("[LLM] 폴백 모드 진입")
        return _fallback_extraction(text)

    # null 값 제거
    return {k: v for k, v in result.items() if v is not None}
