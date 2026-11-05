"""
webapp/session_store.py
───────────────────────
챗봇 세션 + 추천 진행상태·결과를 DB(chat_sessions 테이블)에 저장.

예전엔 서버 메모리(dict)에만 있어서
  - 재배포·재시작하면 진행 중이던 대화·추천 결과가 모두 사라졌고
  - 새 탭·새 창에서는 진행 중인 추천을 볼 수 없었다.
이제 DB 에 사용자(user_id)별로 저장하고, 메모리는 최근 세션만 캐시한다 (LRU).

추천 진행률은 몇 초마다 갱신되므로 메모리에는 즉시, DB 에는 일정 간격으로만 기록한다.
"""
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from webapp import database as _db

MEMORY_CACHE_SIZE = 300          # 메모리에 유지할 최근 세션 수
PROGRESS_DB_INTERVAL = 2.0       # 진행률 DB 기록 최소 간격 (초)
STALE_RUNNING_SEC = 30 * 60      # 이 시간 넘게 'running' 이면 중단된 것으로 간주

REC_FIELDS = ("rec_status", "rec_params_key", "rec_progress", "rec_result", "rec_error", "rec_started_ts")


class SessionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._bots: "OrderedDict[str, Tuple[Any, Optional[int]]]" = OrderedDict()   # sid → (ChatBot, user_id)
        self._recs: Dict[str, Dict[str, Any]] = {}                                  # sid → 추천 상태
        self._last_db_write: Dict[str, float] = {}

    # ── DB 헬퍼 ──────────────────────────────────────────
    @staticmethod
    def _upsert(sid: str, fields: Dict[str, Any]) -> None:
        fields = dict(fields, updated_ts=time.time())
        cols = list(fields)
        conn = _db.get_conn()
        try:
            cur = conn.execute(
                f"UPDATE chat_sessions SET {', '.join(c + '=?' for c in cols)} WHERE session_id=?",
                [fields[c] for c in cols] + [sid],
            )
            if cur.rowcount == 0:
                try:
                    conn.execute(
                        f"INSERT INTO chat_sessions (session_id, {', '.join(cols)}) "
                        f"VALUES (?, {', '.join('?' for _ in cols)})",
                        [sid] + [fields[c] for c in cols],
                    )
                except _db.IntegrityError:
                    pass   # 이미 있는 행 (MySQL 은 값이 같으면 UPDATE rowcount=0)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _fetch(sid: str) -> Optional[dict]:
        conn = _db.get_conn()
        try:
            row = conn.execute("SELECT * FROM chat_sessions WHERE session_id=?", (sid,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    @staticmethod
    def _rec_from_row(row: dict) -> Dict[str, Any]:
        def _j(v):
            try:
                return json.loads(v) if v else None
            except (TypeError, ValueError):
                return None
        return {
            "status":     row.get("rec_status") or "idle",
            "params_key": row.get("rec_params_key"),
            "progress":   _j(row.get("rec_progress")),
            "result":     _j(row.get("rec_result")),
            "error":      _j(row.get("rec_error")),
            "started_ts": row.get("rec_started_ts"),
            "user_id":    row.get("user_id"),
        }

    # ── 챗봇 상태 ─────────────────────────────────────────
    def load_bot(self, sid: str, bot_factory) -> Tuple[Optional[Any], Optional[int]]:
        """(ChatBot, 소유자 user_id). 없으면 (None, None). bot_factory(state) → ChatBot."""
        with self._lock:
            if sid in self._bots:
                self._bots.move_to_end(sid)
                return self._bots[sid]
        row = self._fetch(sid)
        if not row or not row.get("state_json"):
            return None, None
        try:
            bot = bot_factory(json.loads(row["state_json"]))
        except Exception as e:
            print(f"[session] 상태 복원 실패 sid={sid[:8]}: {e}")
            return None, None
        with self._lock:
            self._remember(sid, bot, row.get("user_id"))
            self._recs.setdefault(sid, self._rec_from_row(row))
        return bot, row.get("user_id")

    def save_bot(self, sid: str, bot, user_id: Optional[int]) -> None:
        with self._lock:
            self._remember(sid, bot, user_id)
        try:
            self._upsert(sid, {"user_id": user_id, "state_json": json.dumps(bot.to_state(), ensure_ascii=False)})
        except Exception as e:
            print(f"[session] 저장 실패 sid={sid[:8]}: {e}")

    def _remember(self, sid, bot, user_id):
        self._bots[sid] = (bot, user_id)
        self._bots.move_to_end(sid)
        while len(self._bots) > MEMORY_CACHE_SIZE:
            old, _ = self._bots.popitem(last=False)
            # 진행 중인 추천 상태는 메모리에 유지 (작업 스레드가 갱신 중)
            if (self._recs.get(old) or {}).get("status") != "running":
                self._recs.pop(old, None)

    # ── 추천 상태 ─────────────────────────────────────────
    def get_rec(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._recs.get(sid)
        if rec is None:
            row = self._fetch(sid)
            rec = self._rec_from_row(row) if row else {"status": "idle"}
            with self._lock:
                self._recs[sid] = rec
        return dict(rec)

    def set_rec(self, sid: str, user_id: Optional[int] = None, force_db: bool = True, **fields) -> None:
        """fields: status / params_key / progress / result / error / started_ts"""
        with self._lock:
            rec = self._recs.setdefault(sid, {"status": "idle"})
            rec.update(fields)
            if user_id is not None:
                rec["user_id"] = user_id
            now = time.time()
            write = force_db or now - self._last_db_write.get(sid, 0) >= PROGRESS_DB_INTERVAL
            if write:
                self._last_db_write[sid] = now
                snapshot = dict(rec)
        if not write:
            return
        db_fields = {
            "rec_status":     snapshot.get("status"),
            "rec_params_key": snapshot.get("params_key"),
            "rec_progress":   json.dumps(snapshot.get("progress"), ensure_ascii=False) if snapshot.get("progress") else None,
            "rec_result":     json.dumps(snapshot.get("result"), ensure_ascii=False, default=str) if snapshot.get("result") else None,
            "rec_error":      json.dumps(snapshot.get("error"), ensure_ascii=False) if snapshot.get("error") else None,
            "rec_started_ts": snapshot.get("started_ts"),
        }
        if snapshot.get("user_id") is not None:
            db_fields["user_id"] = snapshot["user_id"]
        try:
            self._upsert(sid, db_fields)
        except Exception as e:
            print(f"[session] 추천 상태 저장 실패 sid={sid[:8]}: {e}")

    def latest_for_user(self, user_id: int) -> Optional[str]:
        """사용자의 가장 최근 추천 세션 id (진행 중 또는 완료)."""
        conn = _db.get_conn()
        try:
            row = conn.execute(
                "SELECT session_id FROM chat_sessions WHERE user_id=? AND rec_status IN ('running','complete') "
                "ORDER BY updated_ts DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        return row["session_id"] if row else None

    def recover_interrupted(self) -> int:
        """서버 시작 시: 이전 프로세스에서 '진행 중'이던 추천은 중단된 것 → 오류로 표시."""
        err = json.dumps({
            "error": "server restarted",
            "error_code": "interrupted",
            "user_message": "서버가 재시작되어 추천이 중단됐어요. '다시 추천'을 눌러 주세요.",
        }, ensure_ascii=False)
        conn = _db.get_conn()
        try:
            cur = conn.execute(
                "UPDATE chat_sessions SET rec_status='error', rec_error=? WHERE rec_status='running'", (err,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


store = SessionStore()
