"""
nlp_input_module.py
───────────────────
챗봇 슬롯 추출 + 상태 머신

흐름:
  사용자 자연어 → llm_module.extract_slots_from_text() (Gemini)
              → 정규식 폴백 (API 실패 시 자동)
  슬롯 완성 → get_v5_params() → run_recommendation() kwargs

필수 슬롯:
  work_address, transport_mode, rent_type, deposit_manwon, allowed_minutes
  (월세면 monthly_manwon 추가)

선택 슬롯:
  vibe (list), use_youth_policy (bool), region_filter,
  house_type, weight_preference
"""

import re
from typing import Dict, List, Optional, Any

from vibe_module import extract_vibe_from_text, get_vibe_weights, INFRA_KEYS
from llm_module import extract_slots_from_text


def _parse_commute_minutes(txt: str) -> Optional[int]:
    """다양한 통근 시간 표현 → 편도 분 단위 정수. 파싱 불가·비현실적 값은 None."""
    # 시간+분 결합 (예: 1시간 30분)
    m = re.search(r"(\d+)\s*시간\s*(\d+)\s*분", txt)
    if m:
        minutes = int(m.group(1)) * 60 + int(m.group(2))
    # N시간 반 (예: 1시간 반)
    elif re.search(r"(\d+)\s*시간\s*반", txt):
        m2 = re.search(r"(\d+)\s*시간", txt)
        minutes = int(m2.group(1)) * 60 + 30 if m2 else None
    # 한시간 반
    elif re.search(r"한\s*시간\s*반", txt):
        minutes = 90
    # N시간 (예: 1시간, 1.5시간)
    elif re.search(r"(\d+(?:\.\d+)?)\s*시간", txt):
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*시간", txt)
        minutes = int(float(m2.group(1)) * 60) if m2 else None
    # 한시간
    elif re.search(r"한\s*시간", txt):
        minutes = 60
    # N분 (예: 40분, 편도 60분)
    elif re.search(r"(\d+)\s*분", txt):
        m2 = re.search(r"(\d+)\s*분", txt)
        minutes = int(m2.group(1)) if m2 else None
    # 숫자만 입력 (예: "80" → 80분으로 처리)
    else:
        m2 = re.search(r"^\s*(\d+)\s*$", txt)
        minutes = int(m2.group(1)) if m2 else None

    if minutes is None:
        return None
    # 왕복 → 편도 환산
    if re.search(r"왕복", txt):
        minutes = minutes // 2
    # 비현실적 값 필터
    if minutes < 5 or minutes > 300:
        return None
    return minutes


def _parse_manwon(txt: str) -> Optional[int]:
    """다양한 금액 표현 → 만원 단위 정수. 파싱 불가 시 None."""
    # 억+천만 (예: 1억5천만원, 1억5천)
    m = re.search(r"(\d+)\s*억\s*(\d+)\s*천", txt)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 1000
    # 억 단독 (예: 2억, 2억원)
    m = re.search(r"(\d+)\s*억", txt)
    if m:
        return int(m.group(1)) * 10000
    # 천만원 (예: 5천만원, 5천만)
    m = re.search(r"(\d+)\s*천\s*만", txt)
    if m:
        return int(m.group(1)) * 1000
    # 천 단독 (예: 5천, 5천원) — 만원 단위 아닐 수 있으나 보증금 문맥에선 천만원
    m = re.search(r"(\d+)\s*천(?!\s*만)", txt)
    if m:
        return int(m.group(1)) * 1000
    # 만원 직접 (예: 5000만원, 5000만, 5000)
    m = re.search(r"(\d+)\s*만", txt)
    if m:
        return int(m.group(1))
    # 숫자만 (예: 5000)
    m = re.search(r"(\d{3,})", txt)
    if m:
        return int(m.group(1))
    return None

# ──────────────────────────────────────────────────────────
# 슬롯 정의
# ──────────────────────────────────────────────────────────

REQUIRED_SLOTS = [
    "work_address", "transport_mode", "rent_type",
    "deposit_manwon", "allowed_minutes",
    "house_type", "weight_preference",
]

SLOT_QUESTIONS = {
    "work_address":      "직장 주소나 지하철역을 알려주세요.\n(예: 강남구 테헤란로 521, 강남역)\n건물·회사 이름은 주소로 인식되지 않아요.",
    "transport_mode":    "출퇴근 수단은 자가용인가요, 대중교통인가요?",
    "rent_type":         "전세로 보실 건가요, 월세로 보실 건가요?",
    "deposit_manwon":    "전세금은 얼마 정도 생각하세요? (예: 1억5천만원)",
    "monthly_manwon":    "월세는 얼마까지 괜찮으세요? (예: 50만원)",
    "allowed_minutes":   "최대 통근 시간은 몇 분이에요? (예: 40분)",
    "house_type":        "주택 유형 선호가 있으신가요?\n① 오피스텔  ② 연립·다세대  ③ 상관없음",
    "weight_preference": "추천 우선순위를 선택해 주세요.\n① 통근 우선  ② 주거비 우선  ③ 균형  ④ 직접 설정",
}

# 월세일 때 보증금 질문 (전세금 문구·예시가 맞지 않아 따로 둠)
DEPOSIT_QUESTION_MONTHLY = "월세 보증금은 얼마 정도 생각하세요? (예: 1000만원)"

SLOT_DISPLAY = {
    "work_address":      "직장 주소",
    "transport_mode":    "이동수단",
    "rent_type":         "임대 유형",
    "deposit_manwon":    "보증금",
    "monthly_manwon":    "월세",
    "allowed_minutes":   "최대 통근",
    "house_type":        "주택유형",
    "weight_preference": "우선순위",
    "vibe":              "동네 분위기",
    "use_youth_policy":  "청년정책 반영",
    "region_filter":     "선호 지역",
    # 청년정책 세부 슬롯 (v6.0 — marriage 제거)
    "policy_employment": "취업상태",
    "policy_education":  "학력",
    "policy_income":     "월 소득",
    "policy_no_house":   "무주택여부",
}

# 청년정책 세부 질문 (v6.0 — use_youth_policy=True 후 순서대로 수집)
# 회원정보(user_meta)에 employment/education이 있으면 자동 채워서 질문 생략.
POLICY_SLOTS = [
    "policy_employment",
    "policy_education",
    "policy_income",
    "policy_no_house",
]

POLICY_QUESTIONS = {
    "policy_employment": (
        "취업 상태를 선택해 주세요.\n"
        "① 취업자 (회사 근무, 프리랜서 등)\n"
        "② 미취업자 (구직 중, 일 경험 없음 등)\n"
        "③ 자영업자 (사업자 등록자)"
    ),
    "policy_education": (
        "최종 학력을 선택해 주세요.\n"
        "① 고졸이하  ② 대학재학  ③ 대졸  ④ 석박사"
    ),
    "policy_income": (
        "월 소득 구간을 선택해 주세요.\n"
        "① 200만원 이하\n"
        "② 200~300만원\n"
        "③ 300~400만원\n"
        "④ 400~500만원\n"
        "⑤ 500만원 이상\n"
        "⑥ 모름(없음)"
    ),
    "policy_no_house": (
        "현재 주택을 소유하고 계신가요?\n"
        "① 무주택 (소유 없음)\n"
        "② 주택 소유\n"
        "③ 모름"
    ),
}

# ──────────────────────────────────────────────────────────
# ChatBot 클래스
# ──────────────────────────────────────────────────────────

class ChatBot:
    """
    슬롯 수집 상태 머신.
    process(user_text) → (bot_reply: str, is_done: bool, v5_params: dict|None)
    """

    MAX_TURNS = 25

    def __init__(self):
        self.slots: Dict[str, Any] = {}
        self.turn = 0
        self.vibe_unrecognized: Optional[str] = None
        self._asked_vibe    = False
        self._asked_policy  = False
        self._done          = False
        # 초기값 work_address — JS 초기 인사말이 직장 주소를 묻기 때문에
        # 첫 사용자 입력도 컨텍스트 폴백이 작동하도록
        self._last_asked_slot: Optional[str] = "work_address"
        self.user_meta: Dict[str, str] = {}  # 로그인 유저 정보 (birth_date, gender, age, nickname)
        # ── 청년정책 자격 수집 안내 메시지 1회 표시 플래그 (v6.0) ──
        self._policy_intro_shown: bool = False
        # ── 카드 플로우 잔재 (v6.0 deprecated, /api/chat 응답 호환용) ──
        self._selection_error: Optional[str] = None
        self.policy_cards: Optional[List[Dict]] = None
        self.has_more_policy_cards: bool = False
        # ── 슬롯 입력 순서 추적 (이전으로/수정하기 기능용) ──
        self._slot_fill_order: List[str] = []
        # ── 정책 슬롯 출처 추적 (조건부 DB 저장용) ──────────
        # _db_loaded_slots: 세션 시작 시 DB에서 로드되어 채워진 슬롯들
        #                   → DB 저장 트리거 안 함 (이미 DB에 있는 값)
        # _session_only_slots: "수정하기"로 변경된 슬롯들
        #                      → DB 저장 트리거 안 함 (세션 한정 변경)
        # 두 set에 모두 없는 슬롯만이 "신규 자연흐름 입력" 으로 간주되어 DB 저장됨.
        self._db_loaded_slots: set = set()
        self._session_only_slots: set = set()
        # ── 추천 결과 캐시 + 동시성 제어 ─────────────────────
        # _recommendation_status: "idle" / "running" / "complete" / "error"
        # _recommendation_result: { results, seoul_avg, workplace_lat, workplace_lon, ... }
        # _recommendation_lock 은 main.py 의 /api/recommend 에서 asyncio.Lock 으로 생성·할당.
        # 이렇게 ChatBot 인스턴스 단위로 락을 두면 동일 session_id 에서 두 번 호출돼도
        # 한 번만 실제 작업이 실행되고 나머지 호출은 캐시 결과를 받게 됨.
        self._recommendation_status: str = "idle"
        self._recommendation_result: Optional[Dict[str, Any]] = None
        self._recommendation_error: Optional[str] = None
        self._recommendation_lock = None  # main.py 에서 asyncio.Lock 으로 lazy 초기화
        # ── 수정 중 취소(active edit cancel) 백업 ────────────
        # _edit_slot 호출 시 원본 값을 저장 → __CANCEL_EDIT_ACTIVE__ 로 복원
        # _edit_backup_slot: 수정 중인 슬롯 이름 (없으면 None)
        # _edit_backup_value: 슬롯의 원본 값
        # _edit_backup_extra: 연동 슬롯(예: rent_type 변경 시 deposit/monthly) 원본 dict
        # _edit_backup_done: 수정 전 _done 상태 (True 면 요약 화면이었음)
        self._edit_backup_slot: Optional[str] = None
        self._edit_backup_value: Any = None
        self._edit_backup_extra: Dict[str, Any] = {}
        self._edit_backup_done: bool = False
        # ── 직장 주소 실제 조회 검증 (main.py 가 카카오 지오코딩 함수로 주입) ──
        # (address) -> (ok: bool, error_msg: str). None 이면 형식 검사만 수행.
        self.address_validator = None

    # ── 질문 문구 (임대유형에 따라 달라지는 질문 처리) ──────────

    def _slot_question(self, slot: str) -> str:
        if slot == "deposit_manwon" and self.slots.get("rent_type") == "월세":
            return DEPOSIT_QUESTION_MONTHLY
        return SLOT_QUESTIONS[slot]

    # ── 필수 슬롯 확인 ──────────────────────────────────────

    def _missing_required(self) -> List[str]:
        return [s for s in self._required_order() if self.slots.get(s) is None]

    def _required_order(self) -> List[str]:
        """필수 슬롯 질문 순서 (rent_type='월세'면 monthly_manwon 포함)."""
        order = ["work_address", "transport_mode", "rent_type", "deposit_manwon"]
        if self.slots.get("rent_type") == "월세":
            order.append("monthly_manwon")
        order += ["allowed_minutes", "house_type", "weight_preference"]
        return order

    def _question_order(self) -> List[str]:
        """전체 질문 순서 (필수 + vibe/policy까지 — PREV용)."""
        order = list(self._required_order())
        if self._asked_vibe:
            order.append("vibe")
        if self._asked_policy:
            order.append("use_youth_policy")
        if self.slots.get("use_youth_policy"):
            order += POLICY_SLOTS
        return order

    def _is_slots_complete(self) -> bool:
        return len(self._missing_required()) == 0

    # ── 슬롯 머지 (None이 아닌 새 값만 채움) ────────────────

    def _merge_slots(self, new_slots: Dict):
        for k, v in new_slots.items():
            if v is not None and self.slots.get(k) is None:
                self.slots[k] = v
                # 필수 슬롯 입력 순서 추적 (이전으로/수정하기 기능용)
                if k in REQUIRED_SLOTS or k == "monthly_manwon":
                    if k not in self._slot_fill_order:
                        self._slot_fill_order.append(k)
        if "vibe_unrecognized" in new_slots and new_slots["vibe_unrecognized"]:
            self.vibe_unrecognized = new_slots["vibe_unrecognized"]

    # ── 다음 질문 결정 ──────────────────────────────────────

    def _next_question(self) -> Optional[str]:
        missing = self._missing_required()
        if missing:
            self._last_asked_slot = missing[0]
            return self._slot_question(missing[0])

        # 직접설정 선택 후 비율 입력 (통근/주거비/인프라 — 합이 10이 아니어도 비율로 환산)
        if self.slots.get("weight_preference") == "직접설정" and self.slots.get("weight_custom") is None:
            self._last_asked_slot = "weight_custom"
            return (
                "통근·주거비·인프라 비중을 숫자로 입력해 주세요.\n"
                "(예: 통근 6 주거비 3 인프라 1, 또는 6:3:1 — 합이 10이 아니어도 비율로 계산해요)\n"
                "※ 인프라는 동네 분위기를 선택했을 때만 반영됩니다."
            )

        # vibe 확인
        # 앞선 답변에서 이미 분위기가 추출됐으면 질문만 건너뛰고 아래 청년정책 질문으로 진행
        # (예전엔 여기서 None 을 반환해 '질문 끝'으로 처리 → 청년정책 질문이 통째로 생략됐다)
        if not self._asked_vibe:
            self._asked_vibe = True
            if self.slots.get("vibe") is None:
                self._last_asked_slot = "vibe"
                if self.vibe_unrecognized:
                    return (
                        f"'{self.vibe_unrecognized}'은 통근시간으로 반영하실 수 있어요.\n"
                        "동네 분위기 선호가 있으신가요?\n"
                        "① 조용함 ② 번화함 ③ 청년활기 ④ 가족친화 ⑤ 자연친화 "
                        "⑥ 편의 우선 ⑦ 운동·건강 ⑧ 카페·문화 ⑨ 상관없음"
                    )
                return (
                    "동네 분위기 선호가 있으신가요?\n"
                    "① 조용함 ② 번화함 ③ 청년활기 ④ 가족친화 ⑤ 자연친화 "
                    "⑥ 편의 우선 ⑦ 운동·건강 ⑧ 카페·문화 ⑨ 상관없음"
                )

        # 청년정책 확인 (예/아니요)
        if not self._asked_policy:
            self._asked_policy = True
            if self.slots.get("use_youth_policy") is None:
                self._last_asked_slot = "use_youth_policy"
                return "현재 조건에 맞는 청년정책도 같이 확인하시겠어요? (예 / 아니요)"

        # 청년정책 자격 정보 수집 (v6.0 — 카드 단계 제거)
        # 회원정보(employment/education) + user_profile(income/no_house) 로 자동 채움.
        # 자동 채운 슬롯은 _db_loaded_slots에 표시하여 DB 재저장 트리거 방지.
        if self.slots.get("use_youth_policy"):
            # user_meta → 슬롯 자동 채우기 (1회성)
            if self.slots.get("policy_employment") is None:
                emp = (self.user_meta.get("employment", "") or "").strip()
                if emp:
                    self.slots["policy_employment"] = emp
                    self._db_loaded_slots.add("policy_employment")
            if self.slots.get("policy_education") is None:
                edu = (self.user_meta.get("education", "") or "").strip()
                if edu:
                    self.slots["policy_education"] = edu
                    self._db_loaded_slots.add("policy_education")
            if self.slots.get("policy_income") is None:
                inc = (self.user_meta.get("policy_income", "") or "").strip()
                if inc:
                    self.slots["policy_income"] = inc
                    self._db_loaded_slots.add("policy_income")
            if self.slots.get("policy_no_house") is None:
                nh = (self.user_meta.get("policy_no_house", "") or "").strip()
                if nh:
                    self.slots["policy_no_house"] = nh
                    self._db_loaded_slots.add("policy_no_house")

            # 청년정책 안내 메시지 (최초 1회, 첫 미답 슬롯과 함께 출력)
            missing_policy = [s for s in POLICY_SLOTS if self.slots.get(s) is None]
            if missing_policy and not getattr(self, "_policy_intro_shown", False):
                self._policy_intro_shown = True
                first = missing_policy[0]
                self._last_asked_slot = first
                intro = (
                    f"정확한 정책 안내를 위해 {len(missing_policy)}가지만 더 여쭤볼게요. "
                    "모르시면 '모름'을 선택하셔도 됩니다.\n"
                    "(건너뛰면 일부 정책은 신청 전 추가 확인이 필요할 수 있어요.)\n\n"
                )
                return intro + POLICY_QUESTIONS[first]

            # 슬롯 순서대로 질문
            for slot in POLICY_SLOTS:
                if self.slots.get(slot) is None:
                    self._last_asked_slot = slot
                    return POLICY_QUESTIONS[slot]

        return None  # 모든 슬롯 완료

    # ── vibe 번호 선택 파싱 ─────────────────────────────────

    _VIBE_MAP = {
        "1": "조용함", "①": "조용함",
        "2": "번화함", "②": "번화함",
        "3": "청년활기", "③": "청년활기",
        "4": "가족친화", "④": "가족친화",
        "5": "자연친화", "⑤": "자연친화",
        "6": "편의 우선", "⑥": "편의 우선",
        "7": "운동·건강", "⑦": "운동·건강",
        "8": "카페·문화", "⑧": "카페·문화",
        "9": "상관없음", "⑨": "상관없음",
    }

    # "상관없음"은 슬롯이 채워졌음을 나타내는 값 — get_v5_params 에서 필터 없음(None)으로 변환.
    # (예전엔 None 으로 저장해 '미입력'으로 취급 → 같은 질문 무한 반복)
    HOUSE_ANY = "상관없음"
    _HOUSE_MAP = {
        "①": "오피스텔",
        "②": "연립다세대",
        "③": HOUSE_ANY,
    }
    # 단순 숫자 "1"~"4"는 주소 등 다른 입력에서 오작동하므로 원문자만 사용

    _WEIGHT_MAP = {
        "①": "통근우선",
        "②": "주거비우선",
        "③": "균형",
        "④": "직접설정",
    }
    # 단순 숫자 "1"~"4"는 주소 등 다른 입력에서 오작동하므로 원문자만 사용

    @classmethod
    def _parse_vibe_choice(cls, text: str) -> Optional[List[str]]:
        chosen = []
        for k, v in cls._VIBE_MAP.items():
            if k in text:
                chosen.append(v)
        if not chosen:
            for n in re.findall(r"\b([1-9])\b", text):
                if n in cls._VIBE_MAP:
                    chosen.append(cls._VIBE_MAP[n])
        # 텍스트로 분위기 이름을 직접 입력한 경우
        if not chosen:
            for v in dict.fromkeys(cls._VIBE_MAP.values()):
                if v in text:
                    chosen.append(v)
        return list(dict.fromkeys(chosen)) or None

    @classmethod
    def _parse_house_choice(cls, text: str) -> Optional[str]:
        # 원문자 우선 매칭
        for k, v in cls._HOUSE_MAP.items():
            if k in text:
                return v
        # 단어 경계 숫자 폴백 (주소·문장 속 숫자 오작동 방지)
        if re.search(r"\b1\b", text): return "오피스텔"
        if re.search(r"\b2\b", text): return "연립다세대"
        if re.search(r"\b3\b", text): return cls.HOUSE_ANY
        # 자연어 키워드 매칭 (LLM 추출 후 가드 통과용)
        if re.search(r"오피스텔", text): return "오피스텔"
        if re.search(r"연립|다세대|빌라", text): return "연립다세대"
        if re.search(r"상관\s*없|아무거나|둘\s*다", text): return cls.HOUSE_ANY
        return None

    @classmethod
    def _parse_weight_choice(cls, text: str) -> Optional[str]:
        # 원문자 우선 매칭
        for k, v in cls._WEIGHT_MAP.items():
            if k in text:
                return v
        # 단어 경계 숫자 폴백 (주소·문장 속 숫자 오작동 방지)
        if re.search(r"\b1\b", text): return "통근우선"
        if re.search(r"\b2\b", text): return "주거비우선"
        if re.search(r"\b3\b", text): return "균형"
        if re.search(r"\b4\b", text): return "직접설정"
        # 텍스트 키워드 매칭
        if re.search(r"통근|출퇴근", text): return "통근우선"
        if re.search(r"주거비|비용|저렴", text): return "주거비우선"
        if re.search(r"균형|반반", text): return "균형"
        if re.search(r"직접\s*설정", text): return "직접설정"
        if re.search(r"알아서|위임|맡겨", text): return "위임"
        return None

    @staticmethod
    def _clean_work_address(text: str) -> str:
        """직장 주소 정제 — 콤마 이후 부속 정보(층/호/방향 등) 제거."""
        if not text:
            return text
        s = text.strip()
        # 콤마 이후는 모두 제거 (", 1층", ", 지하1층", ", 옥상 등")
        s = re.sub(r"\s*,.*$", "", s).strip()
        # 공백 + 층/호/방향 (콤마 없이 띄어쓴 경우)
        s = re.sub(r"\s+(지하\s*\d+층?|[Bb]\s*\d+층?|\d+층|\d+호)(\s.*)?$", "", s).strip()
        s = re.sub(r"\s+(우측|좌측|앞|뒤|옆)$", "", s).strip()
        s = re.sub(r"\s+", " ", s).strip()
        return s

    # 서울로 통근하기 어려운 지역 (수도권 밖)
    _FAR_REGION_RE = re.compile(
        r"(부산|대구|광주광역|광주시|대전|울산|세종|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)"
    )

    @staticmethod
    def _restore_region_prefix(addr: str, user_text: str) -> str:
        """LLM 이 뺀 '○○구'·'서울' 접두어를 사용자 원문에서 복원."""
        m = re.search(r"((?:서울(?:특별시|시)?\s*)?[가-힣]+구)\s", user_text + " ")
        if m and m.group(1).split()[-1] not in addr:
            return f"{m.group(1)} {addr}".strip()
        return addr

    @classmethod
    def _validate_work_address(cls, text: str):
        """(valid: bool, error_msg: str). 형식 검사 — 건물명·무의미 문자열·수도권 밖 주소 거부.

        역명은 카카오 키워드 검색으로 좌표 조회 가능하므로 허용 (출구 무관, 역 중심 좌표 사용).
        실제 존재 여부는 address_validator(카카오 조회)가 추가로 확인한다.
        """
        stripped = text.strip()
        if re.search(r"(코엑스|타워|몰|센터|빌딩|광장|마트|쇼핑|터미널)$", stripped.replace(" ", "")):
            return False, (
                f"'{stripped}'은 건물·시설 이름이에요. 실제 도로명 주소를 입력해 주세요.\n"
                "(예: 강남구 영동대로 513)"
            )
        # 완성형 한글 2자 이상 + 주소/역 형태여야 함 ('ㅁㄴㅇㄹ asdf' 같은 입력 거부)
        hangul = re.findall(r"[가-힣]", stripped)
        looks_like_address = re.search(r"[가-힣]+(로|길|동|가|역|구|시)(\s|\d|$)", stripped)
        if len(hangul) < 2 or not looks_like_address:
            return False, (
                "주소를 이해하지 못했어요 🤔\n"
                "도로명 주소나 지하철역 이름으로 알려주세요.\n(예: 강남구 테헤란로 521, 강남역)"
            )
        if cls._FAR_REGION_RE.search(stripped) and not re.search(r"서울|경기|인천", stripped):
            return False, (
                "서울로 출퇴근하실 수 있는 수도권 직장만 추천할 수 있어요.\n"
                "서울·경기·인천 지역의 직장 주소를 알려주세요."
            )
        return True, ""

    @staticmethod
    def _parse_weight_custom(text: str) -> Optional[List[int]]:
        """'통근 6 주거비 3 인프라 1' 또는 '6:3:1' 형식 → [commute, housing, infra]
        2개만 입력 시 [commute, housing] 으로 인프라는 0 처리."""
        nums = re.findall(r"\d+", text)
        if len(nums) >= 3:
            a, b, c = int(nums[0]), int(nums[1]), int(nums[2])
            if a + b + c > 0:
                return [a, b, c]
        if len(nums) >= 2:
            a, b = int(nums[0]), int(nums[1])
            if a + b > 0:
                return [a, b, 0]
        return None

    @staticmethod
    def _parse_policy_choice(text: str) -> Optional[bool]:
        if re.search(r"^(예|네|응|ㅇ|y|yes|반영|확인|좋아|알려줘|할게|해줘)", text.strip().lower()):
            return True
        if re.search(r"^(아니|no|n|됐|필요없|괜찮|넘어|skip|별로)", text.strip().lower()):
            return False
        return None

    # ── 청년정책 세부 슬롯 파서 ────────────────────────────

    _EMPLOYMENT_MAP = {
        "1": "재직자",   "①": "재직자",
        "2": "자영업자", "②": "자영업자",
        "3": "미취업자", "③": "미취업자",
        "4": "프리랜서", "④": "프리랜서",
        "5": "일경험없음", "⑤": "일경험없음",
    }

    _MARRIAGE_MAP = {
        "1": "미혼", "①": "미혼",
        "2": "기혼", "②": "기혼",
    }

    _EDUCATION_MAP = {
        "1": "고졸이하", "①": "고졸이하",
        "2": "대학재학", "②": "대학재학",
        "3": "대졸",     "③": "대졸",
        "4": "석박사",   "④": "석박사",
    }

    @classmethod
    def _parse_employment_choice(cls, text: str) -> Optional[str]:
        """취업상태 파싱 → 3분류 정규화 (취업자/미취업자/자영업자)."""
        # 1. 3분류 직접 입력
        if re.search(r"자영업자|자영업|개인사업|사업자|창업", text):
            return "자영업자"
        if re.search(r"미취업자|미취업|구직|취업준비|취준|백수|일경험없|경험없", text):
            return "미취업자"
        if re.search(r"취업자|재직자|근로자|직장인|재직|근무|회사|프리랜서|프리", text):
            return "취업자"
        # 2. 번호 선택 (구 5분류 → 3분류 정규화)
        _old_to_new = {
            "재직자": "취업자", "프리랜서": "취업자", "일경험없음": "미취업자",
            "미취업자": "미취업자", "자영업자": "자영업자",
        }
        for k, v in cls._EMPLOYMENT_MAP.items():
            if k in text:
                return _old_to_new.get(v, v)
        return None

    @classmethod
    def _parse_marriage_choice(cls, text: str) -> Optional[str]:
        for k, v in cls._MARRIAGE_MAP.items():
            if k in text:
                return v
        if re.search(r"미혼|싱글|혼자|비혼|결혼안|결혼 안", text):
            return "미혼"
        if re.search(r"기혼|결혼|배우자|유부", text):
            return "기혼"
        return None

    @classmethod
    def _parse_education_choice(cls, text: str) -> Optional[str]:
        for k, v in cls._EDUCATION_MAP.items():
            if k in text:
                return v
        if re.search(r"석사|박사", text):
            return "석박사"
        if re.search(r"대졸|대학교\s*졸|4년제\s*졸", text):
            return "대졸"
        if re.search(r"재학|대학교\s*재|다니|학생", text):
            return "대학재학"
        if re.search(r"고졸|고등학교|고등\s*졸", text):
            return "고졸이하"
        return None

    # 소득 구간 선택지 매핑 (v6.0 — INCOME_BAND_TO_MANWON 키와 일치)
    _INCOME_BAND_MAP = {
        "①": "200만원 이하",
        "②": "200~300만원",
        "③": "300~400만원",
        "④": "400~500만원",
        "⑤": "500만원 이상",
        "⑥": "모름",
    }
    # 단순 숫자 "1"~"6"은 주소·금액 텍스트에서 오작동하므로 원문자만 사용

    @classmethod
    def _parse_income_band_choice(cls, text: str) -> Optional[str]:
        """소득 구간 6선택지 파싱."""
        t = text.strip()
        # 1) 원문자 매칭
        for k, v in cls._INCOME_BAND_MAP.items():
            if k in t:
                return v
        # 단어 경계 숫자 폴백
        if re.search(r"\b6\b", t): return "모름"
        if re.search(r"\b5\b", t): return "500만원 이상"
        if re.search(r"\b4\b", t): return "400~500만원"
        if re.search(r"\b3\b", t): return "300~400만원"
        if re.search(r"\b2\b", t): return "200~300만원"
        if re.search(r"\b1\b", t): return "200만원 이하"
        # 2) 텍스트 직접 매칭
        if re.search(r"모름|몰라|소득\s*없|없음", t):
            return "모름"
        if re.search(r"500\s*만원\s*이상|500이상|5백\s*이상", t):
            return "500만원 이상"
        if re.search(r"400.*500|4백.*5백", t):
            return "400~500만원"
        if re.search(r"300.*400|3백.*4백", t):
            return "300~400만원"
        if re.search(r"200.*300|2백.*3백", t):
            return "200~300만원"
        if re.search(r"200\s*만원\s*이하|200이하|2백\s*이하", t):
            return "200만원 이하"
        # 3) 만원 단위 직접 입력 → 가장 가까운 구간 ('월 250 정도요' 처럼 '만' 생략도 허용)
        m = re.search(r"(\d+)\s*만", t) or re.search(r"(\d{2,4})", t)
        if m:
            amt = int(m.group(1))
            if   amt <= 200: return "200만원 이하"
            elif amt <= 300: return "200~300만원"
            elif amt <= 400: return "300~400만원"
            elif amt <= 500: return "400~500만원"
            else:            return "500만원 이상"
        return None

    @staticmethod
    def _parse_no_house_choice(text: str) -> Optional[str]:
        """무주택 여부 파싱.
        ① / 1 / "무주택"   → "y"
        ② / 2 / "소유"     → "n"
        ③ / 3 / "모름"     → ""  (빈 문자열로 슬롯 채움 → backend에서 needs_check)
        """
        t = text.strip()
        if re.search(r"[③3]|모름|몰라", t):
            return ""
        if re.search(r"[①1]|무주택|없어|소유\s*안|안\s*소유", t):
            return "y"
        if re.search(r"[②2]|소유|있어|주택\s*있|집\s*있", t):
            return "n"
        return None

    # ── 잡담/인사 감지 ─────────────────────────────────────

    # 인사말 패턴
    _GREET_RE = re.compile(
        r"^(안녕|하이|hello|hi|헬로|ㅎㅇ|ㅎㅎ|방가|반가|반갑|반겨|좋은\s*(아침|오전|오후|저녁|밤|하루)|잘\s*부탁|처음\s*뵙)",
        re.I,
    )
    # 감사 패턴
    _THANKS_RE = re.compile(r"^(감사|고마워|고맙|수고|ㄳ|땡큐|thanks|thank)", re.I)
    # 도움 요청/모르겠다 패턴
    _HELP_RE = re.compile(
        r"^(뭘|뭐|어떻게|어떡|모르|모르겠|어디서|어떤\s*정보|도움|도와|설명|알려줘|알려주세요|어디\s*적)",
        re.I,
    )
    # 처음부터 / 다시 패턴
    _RESTART_RE = re.compile(r"^(처음|다시|새로\s*시작|리셋|초기화)", re.I)
    # 잘 지내요/요즘 어때 등 안부 패턴
    _CHITCHAT_RE = re.compile(
        r"^(잘\s*지내|잘\s*있|날씨|심심|배고|피곤|힘들|바빠|뭐\s*해|뭐해|어때|요즘)",
        re.I,
    )
    # 자기소개 요청
    _WHO_RE = re.compile(r"(너는?|넌|당신은?)\s*(누구|뭐야|정체)|누구(세요|야|니)|이름이\s*뭐", re.I)
    # 집/이사 관련 의지 표현 (슬롯 질문으로 자연 연결)
    _HOUSING_INTENT_RE = re.compile(
        r"(집\s*(구하|찾|보)|이사|방\s*(구하|찾|보)|주거|살\s*곳|거주|전세|월세)",
        re.I,
    )

    def _detect_small_talk(self, text: str) -> Optional[str]:
        """
        인사/잡담 감지 → 자연스러운 응답 문자열 반환.
        잡담이 아니면 None.
        슬롯이 절반 이상 채워진 상태라면 잡담 감지 비활성화 (흐름 방해 방지).
        """
        t = text.strip()
        # 자기소개 질문은 대화 중에도 답함 (무시하고 질문만 반복하면 고장난 것처럼 보임)
        if self._WHO_RE.search(t):
            return "저는 서울 공공데이터로 청년 주거지를 추천해 드리는 집찾봇이에요 🏠"
        # 슬롯이 3개 이상 채워진 중간 대화라면 잡담 처리 건너뜀
        filled_count = sum(1 for v in self.slots.values() if v is not None)
        if filled_count >= 3:
            return None
        # 집 찾기 의도가 있으면 잡담 아님
        if self._HOUSING_INTENT_RE.search(t) and len(t) > 3:
            return None

        if self._GREET_RE.search(t):
            greets = [
                "안녕하세요! 😊 반갑습니다.",
                "안녕하세요! 🏠 잘 오셨어요.",
                "반갑습니다! 😊 집 찾는 걸 도와드릴게요.",
            ]
            import random
            return random.choice(greets)

        if self._THANKS_RE.search(t):
            return "천만에요 😊 계속 진행해 볼까요?"

        if self._RESTART_RE.search(t):
            return "처음부터 다시 시작하려면 페이지를 새로고침(F5)해 주세요! 🔄"

        if self._HELP_RE.search(t):
            return (
                "몇 가지 질문에 답해주시면 서울 내 최적 주거지를 추천해드려요 🏠\n"
                "직장 주소, 예산, 통근 시간 등을 순서대로 여쭤볼게요!"
            )

        if self._CHITCHAT_RE.search(t):
            return "저는 항상 열심히 집을 찾고 있어요 🏠😄 주거지 추천을 도와드릴게요!"

        return None

    # ── 청년정책 카드 플로우 헬퍼 ─────────────────────────

    def _extract_work_gu(self) -> str:
        """work_address에서 구 이름 추출."""
        addr = self.slots.get("work_address", "")
        m = re.search(r"([가-힣]+구)", addr)
        return m.group(1) if m else ""

    def _compute_required_conditions(self, policies: list) -> list:
        """[v6.0 deprecated] 카드 플로우 잔재. 항상 빈 리스트."""
        return []

    def _build_card_message(self) -> str:
        """[v6.0 deprecated] 카드 플로우 잔재."""
        return ""

    def _build_card_message_LEGACY(self) -> str:
        """카드 표시 메시지 생성 + self.policy_cards / has_more_policy_cards 세팅."""
        candidates = self.slots.get("candidate_policies", [])
        offset     = self.slots.get("show_more_offset", 0)
        page_size  = 5
        start = offset * page_size
        end   = start + page_size
        page  = candidates[start:end]

        # API 응답용 카드 데이터 세팅
        self.has_more_policy_cards = len(candidates) > end
        if page:
            self.policy_cards = []
            for i, p in enumerate(page, start + 1):
                cl = p.get("_conflict_label", {})
                self.policy_cards.append({
                    "index":            i,
                    "name":             p.get("plcyNm", ""),
                    "description":      p.get("_benefit_desc", ""),
                    "monthly_saving":   p.get("_monthly_saving", 0),
                    "benefit_type":     p.get("_benefit_type", ""),
                    "url":              p.get("refUrlAddr1", "") or p.get("aplyUrlAddr", ""),
                    "auto_match_label": p.get("_auto_match", ""),
                    "conflict_warning": cl.get("warning_text", ""),
                    "conflict_level":   cl.get("level", "none"),
                })
        else:
            self.policy_cards = []

        if not page:
            return (
                "아쉽게도 현재 조건에 맞는 청년정책을 찾지 못했어요.\n"
                "회원가입 시 입력한 취업상태·학력 정보가 맞지 않을 수 있어요.\n\n"
                "👉 '조건 다시 입력'을 누르면 챗봇이 다시 물어볼게요.\n"
                "👉 '정책 없이 진행'을 누르면 청년정책 없이 추천을 진행해요."
            )

        lines = ["📋 내 조건에 맞는 청년정책이에요!\n"]
        for i, p in enumerate(page, start + 1):
            name    = p.get("plcyNm", "")
            desc    = p.get("_benefit_desc", "")
            saving  = p.get("_monthly_saving", 0)
            cl      = p.get("_conflict_label", {})
            warning = cl.get("warning_text", "")
            url     = p.get("refUrlAddr1", "") or p.get("aplyUrlAddr", "")
            auto_m  = p.get("_auto_match", "")
            lines.append(f"[{i}] {name}")
            if auto_m:
                lines.append(f"  {auto_m}")  # auto_m 자체에 이미 ✅ 포함
            lines.append(f"  혜택: {desc}")
            lines.append(f"  💰 월 약 {saving:.0f}만원 절감 추정")
            if warning:
                lines.append(f"  ⚠️ {warning}")
            if url:
                lines.append(f"  🔗 {url}")
            lines.append("")

        if self.has_more_policy_cards:
            lines.append(f"(외 {len(candidates) - end}건 더 있어요. '더보기' 입력 시 추가 표시)")
        lines.append("원하시는 정책 번호를 선택해 주세요. (예: 1 또는 1,3)")
        lines.append("정책이 없으면 '없음'을 입력해 주세요.")
        return "\n".join(lines)

    # ── 이전으로 / 수정하기 헬퍼 ──────────────────────────

    def _undo_last_slot(self):
        """__PREV__ 명령 — 현재 질문의 직전 질문으로 되돌리기.

        `_slot_fill_order` LIFO 방식은 LLM이 한 턴에 여러 슬롯을 추출하거나
        EDIT으로 fill_order가 흐트러진 경우 엉뚱한 슬롯을 pop하므로,
        대화 흐름의 직전 질문(질문 순서 인덱스 -1)을 기준으로 되돌린다.
        """
        target = self._prev_question_slot()
        if target is None:
            return "이전에 입력한 내용이 없어요.", False, None

        self.slots.pop(target, None)
        if target in self._slot_fill_order:
            self._slot_fill_order.remove(target)
        # rent_type 취소 시 deposit_manwon / monthly_manwon도 연동 취소
        if target == "rent_type":
            for linked in ["deposit_manwon", "monthly_manwon"]:
                self.slots.pop(linked, None)
                if linked in self._slot_fill_order:
                    self._slot_fill_order.remove(linked)

        # 되돌아간 단계 이후의 흐름을 다시 살리기 위해 단계 플래그 리셋
        # 필수 슬롯/직접설정으로 돌아가면 vibe·policy 모두 재질문
        if target in self._required_order() or target == "weight_custom":
            self._asked_vibe = False
            self._asked_policy = False
            self._policy_intro_shown = False
        elif target == "vibe":
            self._asked_policy = False
            self._policy_intro_shown = False
        elif target == "use_youth_policy":
            self._policy_intro_shown = False

        self._done = False
        self._last_asked_slot = target
        # 직전 질문 텍스트 복원
        if target in SLOT_QUESTIONS:
            q = self._slot_question(target)
        elif target == "vibe":
            q = (
                "동네 분위기 선호가 있으신가요?\n"
                "① 조용함 ② 번화함 ③ 청년활기 ④ 가족친화 ⑤ 자연친화 "
                "⑥ 편의 우선 ⑦ 운동·건강 ⑧ 카페·문화 ⑨ 상관없음"
            )
        elif target == "use_youth_policy":
            q = "현재 조건에 맞는 청년정책도 같이 확인하시겠어요? (예 / 아니요)"
        elif target in POLICY_QUESTIONS:
            q = POLICY_QUESTIONS[target]
        else:
            q = "다시 답해주세요."
        return f"다시 입력해 주세요 🔄\n\n{q}", False, None

    def _prev_question_slot(self) -> Optional[str]:
        """현재 질문(_last_asked_slot 또는 다음 미답 슬롯)의 직전 질문 슬롯을 반환."""
        order = self._question_order()
        # 기준점: 봇이 가장 최근에 던진 질문 슬롯
        anchor = self._last_asked_slot
        if anchor not in order:
            # vibe/policy 단계 또는 done 상태 — 기준은 가장 마지막 필수 질문
            missing = [s for s in order if self.slots.get(s) is None]
            anchor = missing[0] if missing else order[-1]
        idx = order.index(anchor)
        # 현재 질문이 이미 채워진 상태(예: done 후 PREV)면 그 자체를 직전 질문으로 본다
        if self.slots.get(anchor) is not None:
            return anchor
        if idx == 0:
            return None
        return order[idx - 1]

    def _edit_slot(self, slot: str):
        """__EDIT:slot__ 명령 — 특정 슬롯을 초기화하고 해당 질문 재출력."""
        valid = (
            list(REQUIRED_SLOTS)
            + ["monthly_manwon", "vibe", "use_youth_policy"]
            + POLICY_SLOTS
        )
        if slot not in valid:
            return "수정할 항목을 찾을 수 없어요.", self._done, None

        # ── 수정 중 취소 대비 백업 ──────────────────────────
        # 사용자가 잘못 누른 경우 __CANCEL_EDIT_ACTIVE__ 로 원본 복원 가능하게.
        self._edit_backup_slot  = slot
        self._edit_backup_value = self.slots.get(slot)
        self._edit_backup_done  = self._done
        self._edit_backup_extra = {}
        if slot == "rent_type":
            for linked in ["deposit_manwon", "monthly_manwon"]:
                self._edit_backup_extra[linked] = self.slots.get(linked)
        if slot == "weight_preference":
            self._edit_backup_extra["weight_custom"] = self.slots.get("weight_custom")
        if slot == "use_youth_policy":
            for linked in POLICY_SLOTS:
                self._edit_backup_extra[linked] = self.slots.get(linked)
            self._edit_backup_extra["_asked_policy"] = self._asked_policy
            self._edit_backup_extra["_policy_intro_shown"] = self._policy_intro_shown
        if slot == "vibe":
            self._edit_backup_extra["_asked_vibe"] = self._asked_vibe
            self._edit_backup_extra["vibe_unrecognized"] = self.vibe_unrecognized

        self._done = False
        self.slots.pop(slot, None)
        if slot in self._slot_fill_order:
            self._slot_fill_order.remove(slot)
        # 정책 슬롯을 "수정하기"로 변경 → 세션 한정 (DB 저장 X)
        # _db_loaded_slots 에서 제거하고 _session_only_slots 로 옮김
        if slot in POLICY_SLOTS:
            self._db_loaded_slots.discard(slot)
            self._session_only_slots.add(slot)
        # rent_type 수정 시 deposit_manwon / monthly_manwon도 연동 초기화
        if slot == "rent_type":
            for linked in ["deposit_manwon", "monthly_manwon"]:
                self.slots.pop(linked, None)
                if linked in self._slot_fill_order:
                    self._slot_fill_order.remove(linked)
        # weight_preference 수정 시 이전 직접설정 비율(weight_custom) 도 함께 초기화
        if slot == "weight_preference":
            self.slots.pop("weight_custom", None)
        # use_youth_policy 수정 시 정책 세부 슬롯도 모두 초기화하고 안내·intro 플래그 리셋
        if slot == "use_youth_policy":
            for linked in POLICY_SLOTS:
                self.slots.pop(linked, None)
            self._asked_policy = False
            self._policy_intro_shown = False
        # vibe 수정 시 _asked_vibe 플래그 리셋
        if slot == "vibe":
            self._asked_vibe = False
            self.vibe_unrecognized = None
        self._last_asked_slot = slot

        # 슬롯별 질문 텍스트 결정
        if slot in SLOT_QUESTIONS:
            q = self._slot_question(slot)
        elif slot == "vibe":
            q = (
                "동네 분위기 선호가 있으신가요?\n"
                "① 조용함 ② 번화함 ③ 청년활기 ④ 가족친화 ⑤ 자연친화 "
                "⑥ 편의 우선 ⑦ 운동·건강 ⑧ 카페·문화 ⑨ 상관없음"
            )
        elif slot == "use_youth_policy":
            q = "현재 조건에 맞는 청년정책도 같이 확인하시겠어요? (예 / 아니요)"
        elif slot in POLICY_QUESTIONS:
            q = POLICY_QUESTIONS[slot]
        else:
            q = "다시 답해주세요."

        return (f"수정할게요 ✏️\n\n{q}", False, None)

    def _cancel_active_edit(self):
        """__CANCEL_EDIT_ACTIVE__ 명령 — 진행 중인 슬롯 수정을 취소하고 원본 복원.

        _edit_slot 에서 백업한 값을 되돌리고 요약 화면으로 복귀.
        백업이 없으면 (수정 진행 중이 아니면) 안내 메시지만.
        """
        if not self._edit_backup_slot:
            return "취소할 수정 작업이 없어요.", self._done, None

        slot = self._edit_backup_slot
        # 슬롯 본 값 복원
        if self._edit_backup_value is not None:
            self.slots[slot] = self._edit_backup_value
        else:
            self.slots.pop(slot, None)
        # 연동 슬롯 복원
        for k, v in self._edit_backup_extra.items():
            if k == "_asked_policy":
                self._asked_policy = v
            elif k == "_policy_intro_shown":
                self._policy_intro_shown = v
            elif k == "_asked_vibe":
                self._asked_vibe = v
            elif k == "vibe_unrecognized":
                self.vibe_unrecognized = v
            else:
                # 일반 슬롯
                if v is not None:
                    self.slots[k] = v
                else:
                    self.slots.pop(k, None)
        # _slot_fill_order 에 슬롯이 빠져 있으면 복원
        if self._edit_backup_value is not None and slot not in self._slot_fill_order \
                and (slot in REQUIRED_SLOTS or slot == "monthly_manwon"):
            self._slot_fill_order.append(slot)
        # 정책 슬롯 수정 취소면 _session_only 표시도 제거 (원본은 원래 DB-loaded 상태일 수 있음)
        if slot in POLICY_SLOTS:
            self._session_only_slots.discard(slot)
            # 원본 값이 있으면 DB-loaded 로 간주
            if self._edit_backup_value is not None:
                self._db_loaded_slots.add(slot)

        # 상태 복원
        was_done = self._edit_backup_done
        self._done = was_done
        self._last_asked_slot = None

        # 백업 클리어
        self._edit_backup_slot  = None
        self._edit_backup_value = None
        self._edit_backup_extra = {}
        self._edit_backup_done  = False

        # was_done 이면 요약 화면 + v5 params 다시 반환 (수정 전 상태로 복귀)
        if was_done:
            return self._build_summary(), True, self.get_v5_params()
        # was_done 이 아니면 (드문 케이스) 다음 질문으로
        next_q = self._next_question()
        if next_q is not None:
            return next_q, False, None
        return "취소했어요.", False, None

    # ── 메인 process() ─────────────────────────────────────

    def process(self, user_text: str):
        # ── 특수 명령 처리 (완료 전/후 모두 허용) ────────────
        _t = user_text.strip()
        if _t == "__PREV__":
            return self._undo_last_slot()
        if _t == "__CANCEL_EDIT_ACTIVE__":
            return self._cancel_active_edit()
        if _t.startswith("__EDIT:") and _t.endswith("__"):
            return self._edit_slot(_t[7:-2])

        if self._done:
            return (
                "조건 입력이 모두 끝났어요 ✅\n"
                "아래 '추천 시작'을 누르거나, '수정하기'로 조건을 바꿀 수 있어요.\n"
                "처음부터 다시 하려면 위의 '새 추천 시작'을 눌러 주세요."
            ), True, None

        self.turn += 1
        if self.turn > self.MAX_TURNS:
            return "대화 한도를 초과했습니다. 새로 시작해 주세요.", False, None

        # ── 잡담/인사 감지 ─────────────────────────────────
        small_talk = self._detect_small_talk(user_text)
        if small_talk:
            # 다음에 물어볼 슬롯 질문과 자연스럽게 연결
            missing = self._missing_required()
            if missing:
                next_q = self._slot_question(missing[0])
                self._last_asked_slot = missing[0]
                return f"{small_talk}\n\n{next_q}", False, None
            next_q = self._next_question()
            return (f"{small_talk}\n\n{next_q}" if next_q else small_talk), False, None

        # LLM 슬롯 추출
        new_slots = extract_slots_from_text(user_text, self.slots)

        # ── LLM 추측 차단 가드 ─────────────────────────────
        # 봇이 해당 슬롯을 직접 묻지 않은 상태에서 LLM이 슬롯을 채운 경우,
        # 사용자 발화에 명시적 키워드가 있을 때만 LLM 추출 유지.
        # (원문자 ①②③ 는 house_type/weight_preference/vibe 에서 중복되므로 confirm 용으로 못 씀)
        if new_slots.get("house_type") is not None and self._last_asked_slot != "house_type":
            if not re.search(r"오피스텔|연립|다세대|빌라|단독|다가구", user_text):
                new_slots.pop("house_type", None)
        if new_slots.get("weight_preference") is not None and self._last_asked_slot != "weight_preference":
            if not re.search(r"통근\s*우선|주거비\s*우선|균형|직접\s*설정|반반|저렴|위임|알아서|맡겨", user_text):
                new_slots.pop("weight_preference", None)
        if new_slots.get("weight_custom") is not None and self._last_asked_slot != "weight_custom":
            # weight_custom 은 비율 입력 단계에서만 유효
            new_slots.pop("weight_custom", None)
        if new_slots.get("vibe") is not None and self._last_asked_slot != "vibe":
            if not re.search(r"조용|번화|청년활기|가족친화|자연친화|편의|운동|건강|카페|문화", user_text):
                new_slots.pop("vibe", None)
        if new_slots.get("use_youth_policy") is not None and self._last_asked_slot != "use_youth_policy":
            if not re.search(r"청년정책|정책\s*반영|정책\s*확인", user_text):
                new_slots.pop("use_youth_policy", None)
        # 월세 금액은 직접 물었을 때 또는 '월세/월 N' 이 명시됐을 때만 (보증금 답이 월세로 복제되는 문제)
        if new_slots.get("monthly_manwon") is not None and self._last_asked_slot != "monthly_manwon":
            if not re.search(r"월세|월\s*\d|매달|한\s*달", user_text):
                new_slots.pop("monthly_manwon", None)
        # 보증금도 마찬가지 — 월세 질문에 답한 금액이 보증금으로 들어가지 않게
        if new_slots.get("deposit_manwon") is not None and self._last_asked_slot not in (None, "deposit_manwon"):
            if not re.search(r"보증금|전세|억|천\s*만|예산", user_text):
                new_slots.pop("deposit_manwon", None)

        # ── monthly_manwon / deposit_manwon LLM 결과 검증 및 보정 ────────────
        # LLM(Gemini)이 숫자/단위를 잘못 해석하는 경우 잦음:
        #   - "200만원" → 20 (단위 무시)
        #   - "2억"   → 200000000000... (만원 단위가 아닌 원 단위로 환각, 0 과다)
        # 직접 파싱(_parse_manwon)이 성공하면 그 값으로 덮어씀.
        if self._last_asked_slot == "monthly_manwon":
            direct = _parse_manwon(user_text.strip())
            if direct is not None:
                new_slots["monthly_manwon"] = direct
        if self._last_asked_slot == "deposit_manwon":
            direct = _parse_manwon(user_text.strip())
            if direct is not None:
                new_slots["deposit_manwon"] = direct

        # ── 슬롯-특화 답변에 대해 파서 결과로 LLM override ─────
        # LLM이 enum 값을 잘못 매핑하는 경우(특히 "직접설정")가 있어,
        # 봇이 그 슬롯을 물어보고 있을 때엔 파서가 검출한 값이 LLM 결과보다 우선.
        # (이전엔 new_slots[x] is None 일 때만 폴백 → LLM이 잘못된 값 채우면 못 고침)
        if self._last_asked_slot == "weight_preference":
            parsed = self._parse_weight_choice(user_text)
            if parsed:
                new_slots["weight_preference"] = parsed
        if self._last_asked_slot == "house_type":
            parsed = self._parse_house_choice(user_text)
            if parsed is not None:
                new_slots["house_type"] = parsed

        # ── 컨텍스트 인식 폴백 ──────────────────────────────
        # 봇이 직전에 특정 슬롯을 물어봤는데 LLM이 그 슬롯을 못 잡은 경우,
        # 사용자 발화를 해당 슬롯 값으로 직접 사용
        _addr_err = None
        if self._last_asked_slot and new_slots.get(self._last_asked_slot) is None:
            asked = self._last_asked_slot
            txt   = user_text.strip()
            if asked == "work_address" and txt:
                # 콤마/층 등 부속 정보 정제 후 검증
                cleaned = self._clean_work_address(txt) or txt
                valid, err = self._validate_work_address(cleaned)
                if valid and self.address_validator:
                    valid, err = self.address_validator(cleaned)
                if valid:
                    new_slots["work_address"] = cleaned
                else:
                    _addr_err = err
            elif asked == "weight_custom":
                parsed = self._parse_weight_custom(txt)
                if parsed:
                    new_slots["weight_custom"] = parsed
            elif asked == "allowed_minutes":
                parsed = _parse_commute_minutes(txt)
                if parsed is not None:
                    new_slots["allowed_minutes"] = parsed
            elif asked == "deposit_manwon":
                new_slots["deposit_manwon"] = _parse_manwon(txt)
            elif asked == "monthly_manwon":
                m = re.search(r"(\d+)", txt)
                if m:
                    new_slots["monthly_manwon"] = int(m.group(1))
            # ── 청년정책 세부 슬롯 (v6.0 — marriage 제거) ──
            elif asked == "policy_employment":
                parsed = self._parse_employment_choice(txt)
                if parsed:
                    new_slots["policy_employment"] = parsed
            elif asked == "policy_education":
                parsed = self._parse_education_choice(txt)
                if parsed:
                    new_slots["policy_education"] = parsed
            elif asked == "policy_income":
                # 소득 구간 선택지 (200만원 이하 / 200~300 / ... / 모름)
                parsed = self._parse_income_band_choice(txt)
                if parsed is not None:
                    new_slots["policy_income"] = parsed
            elif asked == "policy_no_house":
                parsed = self._parse_no_house_choice(txt)
                if parsed is not None:
                    new_slots["policy_no_house"] = parsed

        # LLM이 추출한 work_address도 정제 후 검증
        if new_slots.get("work_address") and self.slots.get("work_address") is None:
            cleaned = self._clean_work_address(new_slots["work_address"]) or new_slots["work_address"]
            # LLM 이 '마포구 월드컵북로 396' 에서 구 이름을 빼고 돌려주는 경우 사용자 입력의 구를 복원
            cleaned = self._restore_region_prefix(cleaned, user_text)
            new_slots["work_address"] = cleaned
            valid, err = self._validate_work_address(cleaned)
            if valid and self.address_validator:
                valid, err = self.address_validator(cleaned)
            if not valid:
                new_slots.pop("work_address")
                if not _addr_err:
                    _addr_err = err
                    self._last_asked_slot = "work_address"

        # 번호 선택 폴백 (LLM이 못 잡은 경우) —
        # 봇이 그 슬롯을 직접 묻고 있는 동안에만 적용.
        # (원문자 ①②③ 가 여러 슬롯에서 중복되므로 _last_asked_slot 으로 정확히 매칭해야
        #  엉뚱한 슬롯이 자동으로 채워지는 문제를 막을 수 있음.)
        if self._last_asked_slot == "house_type" and new_slots.get("house_type") is None:
            parsed = self._parse_house_choice(user_text)
            if parsed is not None:
                new_slots["house_type"] = parsed

        if self._last_asked_slot == "weight_preference" and new_slots.get("weight_preference") is None:
            parsed = self._parse_weight_choice(user_text)
            if parsed:
                new_slots["weight_preference"] = parsed

        if self._last_asked_slot == "vibe" and self.slots.get("vibe") is None and new_slots.get("vibe") is None:
            parsed = self._parse_vibe_choice(user_text)
            if parsed:
                if "상관없음" in parsed:
                    new_slots["vibe"] = []
                else:
                    new_slots["vibe"] = [v for v in parsed if v != "상관없음"]
            else:
                vibe_result = extract_vibe_from_text(user_text)
                if vibe_result["matched"]:
                    new_slots["vibe"] = vibe_result["matched"]

        if self._last_asked_slot == "use_youth_policy" and self.slots.get("use_youth_policy") is None and new_slots.get("use_youth_policy") is None:
            pol = self._parse_policy_choice(user_text)
            if pol is not None:
                new_slots["use_youth_policy"] = pol

        self._merge_slots(new_slots)

        # 주소 검증 실패 시 다른 슬롯은 저장하되 work_address만 다시 묻기
        # (사용자 발화에서 LLM이 추출한 transport_mode·house_type 등을 보존해 UX 개선)
        if _addr_err:
            return _addr_err, False, None

        # 다음 질문 (v6.0 — 카드 단계 제거됨)
        next_q = self._next_question()
        if next_q is not None:
            return next_q, False, None

        if not self._is_slots_complete():
            miss = self._missing_required()
            return self._slot_question(miss[0]), False, None

        self._done = True
        # 수정 작업이 정상 완료됐으면 백업 클리어 (취소 시점 지남)
        self._edit_backup_slot  = None
        self._edit_backup_value = None
        self._edit_backup_extra = {}
        self._edit_backup_done  = False
        params = self.get_v5_params()
        return self._build_summary(), True, params

    # ── 요약 메시지 ─────────────────────────────────────────

    def _build_summary(self) -> str:
        s = self.slots
        rt  = s.get("rent_type", "전세")
        dep = s.get("deposit_manwon", 0)
        lines = ["조건이 모두 입력됐어요! 아래 버튼으로 추천을 시작하거나 조건을 수정할 수 있어요.\n", "📋 입력 조건 요약"]
        lines.append(f"- 직장: {s.get('work_address','')}")
        lines.append(f"- 이동수단: {'자가용' if s.get('transport_mode')=='car' else '대중교통'}")
        if rt == "월세":
            mon = s.get("monthly_manwon", 0)
            lines.append(f"- 예산: 월세 보증금 {dep:,}만원 / 월 {mon}만원")
        else:
            lines.append(f"- 예산: 전세 {dep:,}만원")
        lines.append(f"- 최대 통근: {s.get('allowed_minutes', 60)}분")
        ht = s.get("house_type")
        lines.append(f"- 주택유형: {ht if ht else '상관없음'}")
        wp = s.get("weight_preference", "균형")
        if wp == "직접설정":
            wc = s.get("weight_custom") or [3, 4, 3]
            if len(wc) >= 3:
                lines.append(f"- 우선순위: 직접설정 (통근 {wc[0]} : 주거비 {wc[1]} : 인프라 {wc[2]})")
            else:
                lines.append(f"- 우선순위: 직접설정 (통근 {wc[0]} : 주거비 {wc[1]})")
        else:
            lines.append(f"- 우선순위: {wp}")
        vibe = s.get("vibe")
        if vibe:
            lines.append(f"- 동네 분위기: {', '.join(vibe)}")
        if s.get("use_youth_policy"):
            # v6.0: 카드 선택 단계 제거 → 자격 정보만 요약
            emp = s.get("policy_employment", "")
            edu = s.get("policy_education", "")
            inc = s.get("policy_income", "")
            nh_raw = s.get("policy_no_house", None)
            nh  = ("무주택" if nh_raw == "y"
                   else "주택소유" if nh_raw == "n"
                   else "주택소유 모름" if nh_raw == ""
                   else "")
            age = self.user_meta.get("age", "")
            inc_str = ("소득 없음" if inc == "모름"
                       else f"소득 {inc}" if inc else "")
            detail = "  |  ".join(filter(None, [
                f"나이 {age}세" if age else "",
                emp, edu, inc_str, nh,
            ]))
            lines.append(f"- 청년정책: 반영  ({detail})" if detail else "- 청년정책: 반영")
        return "\n".join(lines)

    # ── v5 파라미터 변환 ────────────────────────────────────

    def get_v5_params(self) -> Dict:
        s = self.slots
        rt  = s.get("rent_type", "전세")
        dep = s.get("deposit_manwon", 0)
        mon = s.get("monthly_manwon", 0)

        budget_manwon      = dep if rt == "전세" else dep + mon * 200
        monthly_rent_manwon = 0.0 if rt == "전세" else float(mon)

        vibe       = [v for v in (s.get("vibe") or []) if v]
        use_policy = bool(s.get("use_youth_policy"))

        # ── 가중치 프리셋 (10 기준 — 통근 : 주거비 : 인프라) ──
        # 통근우선: 6/3/1, 주거비우선: 3/6/1, 균형: 3/4/3, 직접설정: 사용자 입력
        # vibe 미선택 시 인프라 비중을 0으로 둔 뒤 통근·주거비 비율을 유지하며 재정규화.
        _WEIGHT_PRESETS = {
            "통근우선":  (6, 3, 1),
            "주거비우선": (3, 6, 1),
            "균형":      (3, 4, 3),
        }
        weight_policy = 0.0   # v6.0: 정책은 추천 점수 미반영 (표시용 매칭만)

        wp_pref = s.get("weight_preference", "균형")
        if wp_pref == "직접설정":
            wc = s.get("weight_custom") or [3, 4, 3]
            if len(wc) >= 3:
                ratio_c, ratio_h, ratio_i = wc[0], wc[1], wc[2]
            else:
                ratio_c, ratio_h, ratio_i = wc[0], wc[1], 0
        else:
            ratio_c, ratio_h, ratio_i = _WEIGHT_PRESETS.get(wp_pref, _WEIGHT_PRESETS["균형"])

        # vibe 미선택 → 인프라 점수 의미 없음 → 인프라 비중 제거
        if not vibe:
            ratio_i = 0

        total_ratio = ratio_c + ratio_h + ratio_i
        if total_ratio <= 0:
            # 안전망 — 모두 0이면 균형으로 폴백
            ratio_c, ratio_h, ratio_i = 3, 4, (3 if vibe else 0)
            total_ratio = ratio_c + ratio_h + ratio_i

        weight_commute = round(ratio_c / total_ratio, 4)
        weight_housing = round(ratio_h / total_ratio, 4)
        weight_infra   = round(ratio_i / total_ratio, 4)

        required_infra = None
        if vibe:
            weights = get_vibe_weights(vibe)
            required_infra = [k for k in INFRA_KEYS if weights.get(k, 1.0) >= 1.3] or None

        ht = s.get("house_type")
        housing_filter = [ht] if ht and ht != self.HOUSE_ANY else None

        user_info = None
        if use_policy:
            # user_meta(회원정보) 우선, 없으면 챗봇 수집 슬롯 사용
            # v6.0: income_band(구간) + no_house(y/n/"") + marriage 제거
            age_val = self.user_meta.get("age", "") or ""
            user_info = {
                "age":          str(age_val),
                "employment":   s.get("policy_employment", ""),
                "education":    s.get("policy_education", ""),
                "income_band":  s.get("policy_income", ""),    # "200만원 이하" 등
                "no_house":     s.get("policy_no_house", ""),  # "y"/"n"/""
            }

        return dict(
            work_address        = s.get("work_address", ""),
            transport_mode      = s.get("transport_mode", "transit"),
            budget_manwon       = budget_manwon,
            allowed_commute_min = s.get("allowed_minutes", 60),
            final_recommend_count = 5,
            depart_hour=8, depart_min=30,
            arrive_hour=9, arrive_min=0,
            region_filter       = s.get("region_filter"),
            housing_filter      = housing_filter,
            weight_commute      = weight_commute,
            weight_housing      = weight_housing,
            required_infra      = required_infra,
            weight_infra        = weight_infra,
            user_info           = user_info,
            weight_policy       = weight_policy,   # v6.0: Stage C에서 0 고정 예정
            max_policy_display  = 5,                # 5건까지 표시
            monthly_rent_manwon = monthly_rent_manwon,
            chatbot_mode        = True,
            vibe_list           = vibe or None,
            selected_policies   = None,             # v6.0: 카드 선택 제거
        )

    # ── 슬롯 상태 (우측 패널용) ─────────────────────────────

    def slot_status(self) -> List[Dict]:
        rows = []
        keys = list(REQUIRED_SLOTS) + ["monthly_manwon", "vibe", "use_youth_policy"]
        for k in keys:
            if k == "monthly_manwon" and self.slots.get("rent_type") != "월세":
                continue
            v = self.slots.get(k)
            display_val = v
            if isinstance(v, list):
                display_val = ", ".join(v) if v else "상관없음"
            elif isinstance(v, bool):
                display_val = "예" if v else "아니요"
            rows.append({
                "key":    k,
                "label":  SLOT_DISPLAY.get(k, k),
                "value":  display_val,
                "filled": v is not None,
            })

        # 청년정책 세부 슬롯 (v6.0 — 4개: employment/education/income/no_house)
        if self.slots.get("use_youth_policy"):
            for k in POLICY_SLOTS:
                v = self.slots.get(k)
                display_val = v
                if k == "policy_no_house":
                    display_val = ("무주택" if v == "y"
                                   else "주택소유" if v == "n"
                                   else "모름" if v == ""
                                   else None)
                elif v == "":
                    display_val = "모름"
                rows.append({
                    "key":    k,
                    "label":  SLOT_DISPLAY.get(k, k),
                    "value":  display_val,
                    "filled": v is not None,
                })
        return rows
