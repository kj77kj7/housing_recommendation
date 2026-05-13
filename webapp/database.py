"""
webapp/database.py
──────────────────
MySQL 기반 DB 초기화 + CRUD 헬퍼
SQLAlchemy 미사용 (greenlet/C++ 컴파일러 회피).
PyMySQL + 호환 어댑터로 기존 sqlite3 스타일 API (`?` 파라미터, dict 접근) 유지.

환경변수:
  DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
  또는 DATABASE_URL (mysql://user:pw@host:port/db) — Railway 등 배포 환경용

테이블:
  users                  - 회원 (이메일/비밀번호 + 카카오 OAuth)
  user_profile           - 추가 사용자 프로필
  checklist_items        - 이사 체크리스트
  search_conditions      - 저장된 검색조건
  recommendation_history - 추천 이력
"""
import os
from urllib.parse import urlparse

import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import IntegrityError as _PyMySQLIntegrityError

# 외부에서 `from webapp.database import IntegrityError` 로 잡을 수 있도록 export
IntegrityError = _PyMySQLIntegrityError


# ── 연결 파라미터 ────────────────────────────────────────
def _db_params() -> dict:
    """DATABASE_URL 우선, 없으면 개별 환경변수."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        u = urlparse(url)
        return {
            "host":     u.hostname or "localhost",
            "port":     u.port or 3306,
            "user":     u.username or "root",
            "password": u.password or "",
            "db":       (u.path or "/").lstrip("/") or "housing",
            "charset":  "utf8mb4",
        }
    return {
        "host":     os.getenv("DB_HOST", "localhost"),
        "port":     int(os.getenv("DB_PORT", "3306")),
        "user":     os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "db":       os.getenv("DB_NAME", "housing"),
        "charset":  "utf8mb4",
    }


# ── sqlite3 호환 어댑터 ─────────────────────────────────
# 기존 코드(`?` 파라미터, `conn.execute(sql, p).fetchone()` 패턴, `row["col"]` 접근)를
# 손대지 않고 MySQL 위에서 동작시키기 위한 얇은 래퍼.
class _CursorWrapper:
    def __init__(self, real_cur):
        self._c = real_cur

    def execute(self, sql, params=()):
        # SQLite 스타일 `?` → MySQL 스타일 `%s` 변환
        self._c.execute(sql.replace("?", "%s"), params)
        return self

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        self._c.close()


class _ConnWrapper:
    def __init__(self, real_conn):
        self._conn = real_conn

    def cursor(self):
        return _CursorWrapper(self._conn.cursor(DictCursor))

    def execute(self, sql, params=()):
        # sqlite3 패턴: conn.execute(...).fetchone()
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_conn():
    """기존 sqlite3.connect()와 동일한 사용감으로 MySQL 연결 반환."""
    real_conn = pymysql.connect(autocommit=False, **_db_params())
    return _ConnWrapper(real_conn)


# ── 컬럼 존재 확인 (MySQL: INFORMATION_SCHEMA) ──────────
def _column_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ? "
        "LIMIT 1",
        (table, col),
    )
    return cur.fetchone() is not None


# ── 스키마 초기화 ───────────────────────────────────────
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # ── users ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            email       VARCHAR(255) UNIQUE,
            password    VARCHAR(255),
            birth_date  VARCHAR(10),                                 -- YYYY-MM-DD
            gender      VARCHAR(2),                                  -- 'M'/'F'
            kakao_id    VARCHAR(50) UNIQUE,
            nickname    VARCHAR(100),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)
    if not _column_exists(cur, "users", "kakao_id"):
        cur.execute("ALTER TABLE users ADD COLUMN kakao_id VARCHAR(50)")
    if not _column_exists(cur, "users", "updated_at"):
        cur.execute("ALTER TABLE users ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    if not _column_exists(cur, "users", "employment"):
        cur.execute("ALTER TABLE users ADD COLUMN employment VARCHAR(20)")   # 취업상태
    if not _column_exists(cur, "users", "education"):
        cur.execute("ALTER TABLE users ADD COLUMN education VARCHAR(20)")    # 학력

    # ── user_profile ──────────────────────────────────────
    # has_own_house DEFAULT NULL — 사용자가 명시적으로 선택 안 하면 NULL 유지.
    # (이전엔 DEFAULT 0 이라 마이페이지에서 다른 항목만 편집해도 INSERT 시 자동 "무주택" 으로 박히는 버그가 있었음)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            id                INT AUTO_INCREMENT PRIMARY KEY,
            user_id           INT NOT NULL,
            employment_status VARCHAR(20),
            education_level   VARCHAR(20),
            marriage_status   VARCHAR(20),
            has_own_house     TINYINT DEFAULT NULL,
            income_range      VARCHAR(50),
            asset_range       VARCHAR(50),
            last_updated      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)
    # 기존 DB(이미 생성된 user_profile.has_own_house DEFAULT 0)에 대한 마이그레이션 —
    # MODIFY 로 DEFAULT 만 변경. 기존 행 데이터는 변경 없음 (사용자가 마이페이지에서 직접 수정 시까지 유지)
    try:
        cur.execute("ALTER TABLE user_profile MODIFY has_own_house TINYINT DEFAULT NULL")
    except Exception as e:
        # 이미 NULL 이거나 권한 문제 등 — 마이그레이션 실패해도 앱은 계속 동작
        print(f"[migration] user_profile.has_own_house DEFAULT 변경 skip: {e}")

    # ── checklist_items ───────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checklist_items (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            user_id     INT NOT NULL,
            content     TEXT,
            is_checked  TINYINT DEFAULT 0,                           -- 0=미완료, 1=완료
            is_default  TINYINT DEFAULT 0,                           -- 0=사용자, 1=기본
            category    VARCHAR(20),                                 -- 이사전/이사당일/이사후
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ── search_conditions ─────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_conditions (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            user_id         INT NOT NULL,
            workplace       VARCHAR(255),
            transport       VARCHAR(50),
            contract_type   VARCHAR(20),
            deposit         INT,
            monthly_rent    INT,
            commute_time    INT,
            preferred_area  VARCHAR(255),
            departure_time  VARCHAR(20),
            arrival_time    VARCHAR(20),
            house_type      VARCHAR(50),
            priority        VARCHAR(255),
            amenities       TEXT,
            youth_policy    TINYINT,
            age             INT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ── recommendation_history ────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recommendation_history (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            user_id       INT NOT NULL,
            workplace     VARCHAR(255),
            contract_type VARCHAR(20),
            budget        VARCHAR(100),
            commute_time  INT,
            result_count  INT,
            top_result    TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    # ── favorites (찜한 매물) ──────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id                  INT AUTO_INCREMENT PRIMARY KEY,
            user_id             INT NOT NULL,
            property_key        VARCHAR(64) NOT NULL,
            address             VARCHAR(255),
            gu                  VARCHAR(50),
            dong                VARCHAR(50),
            house_type          VARCHAR(50),
            rent_type           VARCHAR(20),
            area_m2             DECIMAL(6,2),
            deposit_manwon      INT,
            monthly_rent_manwon INT,
            commute_min         INT,
            score               INT,
            lat                 DOUBLE,
            lon                 DOUBLE,
            snapshot_json       LONGTEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_user_property (user_id, property_key),
            INDEX idx_favorites_user (user_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)

    conn.commit()
    conn.close()
    params = _db_params()
    print(f"[DB] 초기화 완료: mysql://{params['user']}@{params['host']}:{params['port']}/{params['db']}")
