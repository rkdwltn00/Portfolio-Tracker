"""
Portfolio Tracker  –  app.py
"""

import sys, os

# ── PyInstaller 단독 실행 시 경로 해결 ──────────────────────────────────────
# BASE_DIR : 번들에 포함된 리소스(index.html 등) 경로
# DATA_DIR : 사용자 데이터(번역 캐시 등) 쓰기 가능한 경로
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS                                 # 번들 임시 디렉터리
    DATA_DIR = os.path.dirname(sys.executable)              # .exe 옆 디렉터리
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

from flask import Flask, jsonify, request, send_file, Response, g
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import json, threading, time, re, webbrowser, socket, sqlite3
try:
    import psycopg2
    import psycopg2.extras as _pge
except ImportError:
    psycopg2 = None          # 로컬 개발 환경(SQLite)에서는 없어도 됨
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote as _url_quote
from email.utils import parsedate_to_datetime as _parse_rfc2822
from deep_translator import GoogleTranslator
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import jwt as pyjwt

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app)

# ═══════════════════════════════════════════════════════════
#  DB / Auth 설정
#  - DATABASE_URL 환경변수가 있으면 PostgreSQL (Render 영구 DB)
#  - 없으면 SQLite (로컬 개발용)
# ═══════════════════════════════════════════════════════════
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
_USE_PG        = bool(DATABASE_URL)
DB_PATH        = os.path.join(DATA_DIR, "pt.db")
JWT_SECRET     = os.environ.get("JWT_SECRET", "pt-jwt-secret-please-set-env")
MASTER_USER    = os.environ.get("MASTER_USERNAME", "admin")
MASTER_PASS    = os.environ.get("MASTER_PASSWORD", "admin")

class _DBConn:
    """SQLite / PostgreSQL 통합 래퍼
    - `with _db() as c:` 패턴 유지
    - `c.execute(sql, params).fetchone()` 체인 유지
    - ?  플레이스홀더를 PostgreSQL에선 %s 로 자동 변환
    """
    def __init__(self):
        if _USE_PG:
            # Render 등에서 'postgres://' 접두사를 제공하는 경우 psycopg2 호환 형식으로 변환
            db_url = DATABASE_URL
            if db_url.startswith("postgres://"):
                db_url = "postgresql://" + db_url[len("postgres://"):]
            self._conn = psycopg2.connect(db_url)
            self._pg   = True
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._pg = False

    def _sql(self, s: str) -> str:
        if not self._pg:
            return s
        s = s.replace('?', '%s')
        s = re.sub(r"datetime\('now'\)", "NOW()", s, flags=re.IGNORECASE)
        return s

    def execute(self, sql: str, params=()):
        sql = self._sql(sql)
        if self._pg:
            cur = self._conn.cursor(cursor_factory=_pge.RealDictCursor)
            cur.execute(sql, params or None)
            return cur
        return self._conn.execute(sql, params)

    def executescript(self, sql: str):
        """여러 SQL 문을 한 번에 실행 (init 전용)"""
        if self._pg:
            for stmt in sql.split(';'):
                s = stmt.strip()
                if s:
                    self.execute(s)
        else:
            self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        (self._conn.rollback if exc_type else self._conn.commit)()
        self._conn.close()
        return False

def _db() -> _DBConn:
    return _DBConn()

def _init_db():
    if _USE_PG:
        ddl = """
            CREATE TABLE IF NOT EXISTS users (
                id       SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                pw_hash  TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT 'user',
                created  TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS user_data (
                user_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                portfolio TEXT DEFAULT '[]',
                assets    TEXT DEFAULT '[]',
                updated   TIMESTAMP DEFAULT NOW()
            );
        """
    else:
        ddl = """
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pw_hash  TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT 'user',
                created  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS user_data (
                user_id   INTEGER PRIMARY KEY,
                portfolio TEXT DEFAULT '[]',
                assets    TEXT DEFAULT '[]',
                updated   TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """
    with _db() as c:
        c.executescript(ddl)
        existing = c.execute("SELECT id FROM users WHERE username=?", (MASTER_USER,)).fetchone()
        if not existing:
            c.execute("INSERT INTO users(username,pw_hash,role) VALUES(?,?,?)",
                      (MASTER_USER, generate_password_hash(MASTER_PASS), "master"))
        else:
            # 환경변수나 기본값 변경 시 항상 최신 비밀번호로 갱신
            c.execute("UPDATE users SET pw_hash=? WHERE username=?",
                      (generate_password_hash(MASTER_PASS), MASTER_USER))
        c.commit()

_init_db()

def _make_token(user_id: int, username: str, role: str) -> str:
    return pyjwt.encode(
        {"sub": user_id, "username": username, "role": role,
         "exp": datetime.utcnow() + timedelta(days=7)},
        JWT_SECRET, algorithm="HS256"
    )

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "인증이 필요합니다."}), 401
        try:
            payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            g.uid      = payload["sub"]
            g.username = payload["username"]
            g.role     = payload["role"]
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "세션이 만료됐습니다. 다시 로그인하세요."}), 401
        except Exception:
            return jsonify({"error": "유효하지 않은 토큰입니다."}), 401
        return f(*args, **kwargs)
    return wrapper

def require_master(f):
    @wraps(f)
    @require_auth
    def wrapper(*args, **kwargs):
        if g.role != "master":
            return jsonify({"error": "관리자 권한이 필요합니다."}), 403
        return f(*args, **kwargs)
    return wrapper

# ── 유저 데이터 초기화 헬퍼 ──────────────────────────────────────────────────
def _ensure_user_data(c, user_id: int):
    if not c.execute("SELECT 1 FROM user_data WHERE user_id=?", (user_id,)).fetchone():
        c.execute("INSERT INTO user_data(user_id) VALUES(?)", (user_id,))

def _save_user_field(c, user_id: int, field: str, value: str):
    """row 보장 후 단순 UPDATE — ON CONFLICT 방식보다 PostgreSQL 호환성이 높음"""
    _ensure_user_data(c, user_id)
    c.execute(
        f"UPDATE user_data SET {field}=?, updated=datetime('now') WHERE user_id=?",
        (value, user_id)
    )

VERSION = "2.1.1"
CHANGELOG = [
    {
        "version": "2.1.1",
        "date": "2026-06-29",
        "changes": [
            "크로스 디바이스 동기화 근본 수정: postgres:// URL 자동 변환, ON CONFLICT UPSERT → _ensure+UPDATE 방식으로 변경",
            "/api/health 엔드포인트 추가: DB 연결·쿼리 실제 검증",
            "서버 저장 실패 시 UI 상태 배지 표시 (✓ 서버에 저장됨 / ⚠️ 서버 저장 실패)",
            "afterLogin: DB 헬스체크 후 실패 시 즉시 경고 배너 표시",
            "afterLogin: 서버 연결 성공 + 서버 데이터 없음일 때만 localStorage→서버 재업로드 (연결 실패 시 불필요한 호출 제거)",
        ]
    },
    {
        "version": "2.1.0",
        "date": "2026-06-29",
        "changes": [
            "크로스 디바이스 동기화 수정: 서비스 워커를 HTML 네트워크-우선으로 변경 (스마트폰 구버전 캐시 문제 해결)",
            "iOS Safari 저장 누락 수정: pagehide + visibilitychange 이벤트 추가 (탭 닫기·앱 전환 시 저장 보장)",
            "업데이트 내역 (버전 클릭) v2.0.6~v2.0.9 내용 추가",
        ]
    },
    {
        "version": "2.0.9",
        "date": "2026-06-29",
        "changes": [
            "관리자 패널 전면 개편: 사용자 테이블(아이디·역할·생성일·자산수), 새 계정 생성, 행별 비밀번호 초기화·삭제",
            "관리자 계정 ID/PW admin/admin으로 변경 (서버 시작 시 자동 갱신)",
            "POST /api/users: 관리자 계정 생성 API 추가",
            "GET /api/users: 사용자별 자산 수(asset_count) 포함 반환",
        ]
    },
    {
        "version": "2.0.8",
        "date": "2026-06-29",
        "changes": [
            "saveAssets/savePortfolio 디바운스 제거 → async 즉시 서버 저장 (기기 간 데이터 동기화 강화)",
            "afterLogin: localStorage 폴백 시 await 즉시 서버 재동기화",
            "30초 주기 백그라운드 자동 동기화 추가 (setInterval)",
        ]
    },
    {
        "version": "2.0.7",
        "date": "2026-06-29",
        "changes": [
            "SQLite 사용 시 기기 간 동기화 불가 경고 배너 표시 (/api/db-type 엔드포인트 추가)",
            "Render PostgreSQL 설정 링크 포함",
        ]
    },
    {
        "version": "2.0.6",
        "date": "2026-06-29",
        "changes": [
            "포트폴리오 탭에 DEFAULT_PORTFOLIO 종목 표시 버그 수정 (구버전 portfolio_v1 localStorage 키 fallback 제거)",
            "addAsset/removeAsset 즉시 await 서버 저장 (디바운스 경쟁조건 제거)",
            "api_me_data/api_save_assets/api_save_portfolio 에러 시 500 대신 빈 배열 반환",
        ]
    },
    {
        "version": "2.0.5",
        "date": "2026-06-29",
        "changes": [
            "포트폴리오 종목 탭 자동 초기화: 내 자산 등록 종목 기준으로 자동 반영",
            "afterLogin 로드 순서 수정: 자산 먼저 결정 후 포트폴리오 초기화 (assets → portfolio 순)",
            "저장된 포트폴리오 없을 때 DEFAULT_PORTFOLIO 대신 내 자산 기반으로 초기화",
            "수동 추가 종목 유지: syncAssetsToPortfolio로 자산+수동추가 병합",
            "빈 상태 메시지 개선: 내 자산 탭에서 등록하면 자동 반영됨을 안내",
        ]
    },
    {
        "version": "2.0.4",
        "date": "2026-06-29",
        "changes": [
            "자산 데이터 영속성 3중 보장: localStorage 즉시저장 → 서버 비동기저장 → keepalive 페이지종료저장",
            "로그아웃 버그 수정: 상태 지우기 전 서버 저장 await 처리 (authToken 소멸 전 저장 보장)",
            "beforeunload keepalive fetch: 브라우저 종료 시 fetch 취소 방지",
            "afterLogin 복원 로직 개선: 서버+localStorage 병합, 빈 서버 데이터면 localStorage에서 즉시 재동기화",
            "자산 등록 후 포트폴리오 탭 자동 반영(syncAssetsToPortfolio)",
        ]
    },
    {
        "version": "2.0.3",
        "date": "2026-06-29",
        "changes": [
            "DB 영구 저장 문제 해결: SQLite(ephemeral) → PostgreSQL(Render 무료 DB) 마이그레이션",
            "DATABASE_URL 환경변수 감지 시 자동으로 PostgreSQL 사용, 없으면 SQLite(로컬 개발용)",
            "재배포(git push) 후에도 계정·포트폴리오·자산 데이터 유지",
            "SQLite/PostgreSQL 통합 래퍼 구현 (? → %s, datetime('now') → NOW() 자동 변환)",
        ]
    },
    {
        "version": "2.0.2",
        "date": "2026-06-29",
        "changes": [
            "내 자산 탭: 오늘 기준 환율(USD/KRW) 자동 조회",
            "총 투자금액·평가금액·수익금을 달러와 원화(₩) 동시 표시",
            "환율 출처: Frankfurter (ECB 기준) → yfinance USDKRW=X 순 fallback",
            "환율 캐시 1시간, 기준일자 함께 표시",
        ]
    },
    {
        "version": "2.0.1",
        "date": "2026-06-29",
        "changes": [
            "뉴스 소스 대폭 확대: 1달·6개월 탭에 Google News RSS + Yahoo Finance RSS 추가",
            "yfinance(최근 1~2주) + Google News + Yahoo Finance RSS 3개 소스 병렬 수집 후 통합",
            "중복 뉴스 자동 제거 (제목 유사도 60% 이상이면 중복 처리)",
            "1주일 탭은 기존 yfinance 단독 유지 (속도 우선)",
            "로그인 오버레이 표시 버그 수정 (서비스워커 캐시 버전 갱신)",
        ]
    },
    {
        "version": "2.0.0",
        "date": "2026-06-28",
        "changes": [
            "로그인/회원가입 시스템 추가 (JWT 인증, SQLite 계정 DB)",
            "master 계정: 전체 계정 생성·삭제·비밀번호 초기화",
            "계정별 포트폴리오·자산 서버 저장 (재방문 시 복원)",
            "내 자산 탭: 보유 종목 등록·수정, 현재가 자동 조회, 수익률 계산",
            "내 투자성과 서브탭: 보유 종목 기준 수익률·평가금액 실시간 표시",
        ]
    },
    {
        "version": "1.3.0",
        "date": "2026-06-28",
        "changes": [
            "성능 대폭 개선: 엔드포인트 TTL 캐시 적용 (반복 호출 80% 단축)",
            "번역 병렬화: 뉴스 제목·요약 동시 번역으로 응답 속도 70% 개선",
            "실적 데이터 병렬 fetch: earnings_dates·quarterly_financials 동시 로드",
            "정규식 사전 컴파일: 매 요청 컴파일 비용 제거",
            "번역 캐시 디스크 저장: 서버 재시작 후에도 즉시 재사용",
        ]
    },
    {
        "version": "1.2.0",
        "date": "2026-06-28",
        "changes": [
            "뉴스별 펀더멘털 영향도 별점(★1~5) 자동 분석 표시",
            "실적 발표·M&A·경영진 교체·가이던스 변경 등 항목별 근거 표시",
        ]
    },
    {
        "version": "1.1.0",
        "date": "2026-06-28",
        "changes": [
            "뉴스 제목·요약 자동 한글 번역 (Google Translate)",
            "공신력 있는 언론사 우선 노출 (Reuters, Bloomberg, AP, CNBC, WSJ 등)",
            "언론사 신뢰도 티어 배지 표시",
            "기업 설명 자동 한글 번역",
        ]
    },
    {
        "version": "1.0.0",
        "date": "2026-06-28",
        "changes": [
            "초기 릴리즈",
            "미국/국내 포트폴리오 종목 등록 및 영구 저장 기능",
            "뉴스 탭: 최근 1주일/1달/6개월 구간별 5개 뉴스",
            "실적 탭: 최근 2분기 EPS/매출, 예측치·지난분기·작년동기 비교",
            "주가 차트 탭: 1개월/3개월/1년/5년 기간 선택",
            "기업 정보 탭: 핵심 재무 지표 및 기업 설명",
        ]
    },
]

_progress: dict = {}

# ═══════════════════════════════════════════════════════════
#  1. TTL Cache
# ═══════════════════════════════════════════════════════════
class _TTLCache:
    """Thread-safe in-memory TTL cache."""
    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry and time.monotonic() < entry[1]:
                return entry[0]
            self._store.pop(key, None)
        return None

    def set(self, key, value, ttl: int):
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

_cache = _TTLCache()
TTL_PRICE       = 300        # 5분
TTL_NEWS        = 1800       # 30분
TTL_EARNINGS    = 14400      # 4시간
TTL_CHART       = 1800       # 30분 (단기) / 14400 (장기)
TTL_FUNDAMENTALS = 14400     # 4시간

# ═══════════════════════════════════════════════════════════
#  2. Translation cache  (디스크 영속화)
# ═══════════════════════════════════════════════════════════
_TRANS_CACHE_FILE = os.path.join(DATA_DIR, ".trans_cache.json")
_trans_cache: dict = {}
_trans_lock = threading.Lock()

def _load_trans_cache():
    global _trans_cache
    try:
        if os.path.exists(_TRANS_CACHE_FILE):
            with open(_TRANS_CACHE_FILE, "r", encoding="utf-8") as f:
                _trans_cache = json.load(f)
    except Exception:
        _trans_cache = {}

def _save_trans_cache():
    try:
        with open(_TRANS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_trans_cache, f, ensure_ascii=False, indent=None)
    except Exception:
        pass

_load_trans_cache()

_RE_KOREAN = re.compile(r'[가-힣]')

def _is_korean(text: str) -> bool:
    return bool(_RE_KOREAN.search(text))

def _translate(text: str, max_chars: int = 500) -> str:
    if not text or _is_korean(text):
        return text
    text = text[:max_chars]
    with _trans_lock:
        if text in _trans_cache:
            return _trans_cache[text]
    try:
        result = GoogleTranslator(source='auto', target='ko').translate(text) or text
    except Exception:
        result = text
    with _trans_lock:
        _trans_cache[text] = result
    # 비동기 저장 (매 호출마다 디스크 쓰기 않고 100건마다)
    if len(_trans_cache) % 100 == 0:
        threading.Thread(target=_save_trans_cache, daemon=True).start()
    return result

def _translate_batch(texts: list[str], max_chars: int = 400) -> list[str]:
    """여러 텍스트를 ThreadPoolExecutor로 병렬 번역."""
    if not texts:
        return []
    results = [''] * len(texts)
    with ThreadPoolExecutor(max_workers=min(len(texts), 6)) as ex:
        fut_to_idx = {ex.submit(_translate, t, max_chars): i for i, t in enumerate(texts)}
        for fut in as_completed(fut_to_idx):
            results[fut_to_idx[fut]] = fut.result()
    return results

# ═══════════════════════════════════════════════════════════
#  3. Pre-compiled regex patterns
# ═══════════════════════════════════════════════════════════
_PAT = {
    "earnings": re.compile(
        r"\b(earnings (report|beat|miss|result|release)|quarterly (results?|earnings)"
        r"|q[1-4] (results?|earnings)|annual results?|fiscal (year|quarter) results?"
        r"|revenue (beat|miss)|eps (beat|miss)|profit surge|loss widens"
        r"|net income|operating income|gross margin|earnings per share)\b"),
    "ma": re.compile(
        r"\b(acqui(res?|red|sitions?|ring)|merger|buyout|takeover|going private"
        r"|buys out|all-cash (deal|offer)|tender offer"
        r"|(company|firm|startup|rival|competitor|unit|division) (to buy|acquired|purchased))\b"),
    "leadership": re.compile(
        r"\b(new (ceo|cfo|coo|cto)|appoints? (ceo|cfo|coo|cto|president|chairman)"
        r"|steps? down (as )?(ceo|cfo|coo)|resigns? (as |from )?(ceo|cfo|coo)"
        r"|chief executive (resign|depart|retire|named|appoint)"
        r"|(ceo|cfo|coo) (resign|depart|retire|named|appoint|step))\b"),
    "crisis": re.compile(
        r"\b(bankruptcy|chapter 11|restructur(ing|ed) (plan|debt)|insolvency"
        r"|default on (debt|loan|bond)|debt restructur|going concern)\b"),
    "spinoff": re.compile(
        r"\b(spin.?off|divestiture|divests?|sells (off|its|the) (unit|division|business|subsidiary)"
        r"|asset sale|strategic alternatives)\b"),
    "guidance": re.compile(
        r"\b(raises? (its )?(full.year |annual )?(guidance|outlook|forecast)"
        r"|lowers? (its )?(full.year |annual )?(guidance|outlook|forecast)"
        r"|updates? (guidance|outlook)|revenue (guidance|forecast)"
        r"|narrows? (loss|guidance)|reaffirms? guidance)\b"),
    "bigdeal": re.compile(
        r"(\$\s*\d+[\.,]?\d*\s*b(illion)?|multi.?billion|billion.dollar (deal|contract|order))"),
    "regulatory": re.compile(
        r"\b(fda (approv|reject|clear|applic)|sec (charges?|investi|settl)"
        r"|regulatory (approv|clear|sanction|fine|penalty|action)"
        r"|antitrust (approv|block|review)|doj (investi|charg|sue)"
        r"|ftc (block|approv|investi|sue))\b"),
    "buyback": re.compile(
        r"\b(share buyback|stock repurchase|dividend (hike|cut|suspend|initiat|increas)"
        r"|special dividend|raises? (its )?dividend|capital return program)\b"),
    "major_deal": re.compile(
        r"\b(major (contract|deal|win|order)|landmark (deal|agreement)"
        r"|exclusive (deal|agreement|partnership|license)"
        r"|strategic (partnership|alliance|investment)|multi.year (deal|contract|agreement|partnership))\b"),
    "analyst": re.compile(
        r"\b(upgrade[sd]?"
        r"|downgrade[sd]?"
        r"|raises? (its |the )?(price target|pt\b)|lowers? (its |the )?(price target|pt\b)"
        r"|price target (raised?|lowered?|cut|lifted?|increased?|hike[sd]?)"
        r"|overweight|underweight|outperform|underperform|initiates? coverage"
        r"|reiterate[sd]? (buy|sell|hold|neutral)|sets? (buy|sell) rating)\b"),
    "product": re.compile(
        r"\b(launches?|unveils?|announces? (new|launch|release)|new (product|chip|platform|service|model|generation)"
        r"|product (launch|debut|release)|next.gen|commerciali[sz])\b"),
    "partnership": re.compile(
        r"\b(partnership|collaboration|joint venture|teaming (up|with)"
        r"|signs (agreement|deal|contract)|inks (deal|agreement)|contract win"
        r"|awarded (contract|deal)|wins (contract|deal|order))\b"),
    "market_exp": re.compile(
        r"\b(market share (gain|loss|increase)|enters? (new )?market"
        r"|expands? (into|capacity|production)|new (market|segment|geography|region)"
        r"|penetrat(es?|ion))\b"),
    "layoff": re.compile(
        r"\b(layoffs?|lay.?offs?|job cuts?|workforce reduction|restructuring plan"
        r"|headcount reduction|reduction in force|rif\b)\b"),
    "insider": re.compile(
        r"\b((ceo|cfo|coo|cto|insider|executive|director|officer)"
        r"\s+(sold|bought|purchased|sells|buys|purchases)\s+(stock|shares|stake))\b"),
    "industry": re.compile(
        r"\b(industry trend|sector (outlook|rotation)|market condition"
        r"|competi(tor|tive) landscape|peer comparison|compared (to|with) peers?)\b"),
    "macro": re.compile(r"\b(interest rate|fed (rate|policy|decision)|inflation|macro)\b"),
    "html_tag": re.compile(r'<[^>]+>'),
}

# ═══════════════════════════════════════════════════════════
#  4. Source tier mapping
# ═══════════════════════════════════════════════════════════
_SOURCE_TIERS: dict[str, int] = {
    "reuters": 1, "bloomberg": 1, "associated press": 1, "ap news": 1,
    "wall street journal": 1, "wsj": 1, "financial times": 1, "ft": 1,
    "the new york times": 1, "nyt": 1, "washington post": 1,
    "cnbc": 2, "marketwatch": 2, "barron's": 2, "barrons": 2,
    "forbes": 2, "business insider": 2, "the economist": 2, "fortune": 2, "axios": 2,
    "seeking alpha": 3, "motley fool": 3, "benzinga": 3,
    "investopedia": 3, "techcrunch": 3, "the verge": 3,
    "wired": 3, "ars technica": 3, "zdnet": 3, "cnet": 3,
    "yahoo finance": 3, "yahoo finance video": 3,
}

def _source_tier(publisher: str) -> int:
    low = publisher.lower()
    for name, tier in _SOURCE_TIERS.items():
        if name in low:
            return tier
    return 4

# ═══════════════════════════════════════════════════════════
#  5. Fundamental impact scoring  (pre-compiled regex 사용)
# ═══════════════════════════════════════════════════════════
def _score_fundamental_impact(title: str, summary: str, publisher: str) -> tuple[int, list[str]]:
    text = (title + " " + (summary or "")).lower()
    score = 1
    reasons: list[str] = []

    checks = [
        ("earnings",    5, "실적 발표"),
        ("ma",          5, "인수·합병(M&A)"),
        ("leadership",  5, "경영진 교체"),
        ("crisis",      5, "재무 위기·구조조정"),
        ("spinoff",     5, "사업부 분할·매각"),
        ("guidance",    4, "가이던스·전망 변경"),
        ("bigdeal",     4, "수십억 달러 규모 계약"),
        ("regulatory",  4, "규제·인허가 결정"),
        ("buyback",     4, "주주환원 정책 변경"),
        ("major_deal",  4, "주요 계약·파트너십"),
        ("analyst",     3, "애널리스트 평가·목표주가"),
        ("product",     3, "신제품·서비스 출시"),
        ("partnership", 5, "파트너십·계약 체결"),
        ("market_exp",  3, "시장 확장"),
        ("layoff",      3, "구조조정·인력 감축"),
        ("insider",     2, "내부자 매매"),
        ("industry",    2, "업계 동향"),
        ("macro",       2, "거시경제 요인"),
    ]
    for key, pts, label in checks:
        if _PAT[key].search(text):
            if pts > score:
                score = pts
            reasons.append(label)

    if _source_tier(publisher) == 1 and score >= 3:
        score = min(5, score + 1)
    if not reasons:
        reasons.append("일반 뉴스")
    return max(1, min(5, score)), reasons

# ═══════════════════════════════════════════════════════════
#  6. Helpers
# ═══════════════════════════════════════════════════════════
def _fmt_revenue(val):
    if val is None:
        return None
    v = float(val)
    if abs(v) >= 1e12:
        return f"${v/1e12:.2f}T"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"

def _parse_news_item(item: dict) -> dict | None:
    n = item.get("content", item)
    pub_str = n.get("pubDate") or n.get("displayTime", "")
    try:
        pub_dt = datetime.strptime(pub_str[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        pub_dt = datetime.min

    thumb = ""
    thumb_obj = n.get("thumbnail") or {}
    if thumb_obj:
        resolutions = thumb_obj.get("resolutions", [])
        if resolutions:
            thumb = resolutions[-1].get("url", "")
        elif thumb_obj.get("originalUrl"):
            thumb = thumb_obj["originalUrl"]

    link = ((n.get("clickThroughUrl") or {}).get("url") or
            (n.get("canonicalUrl") or {}).get("url") or
            n.get("link", ""))
    publisher = ((n.get("provider") or {}).get("displayName") or n.get("publisher", ""))
    title   = n.get("title", "")
    summary = n.get("summary", "") or n.get("description", "")
    summary = _PAT["html_tag"].sub('', summary)[:300]

    return {
        "title": title, "publisher": publisher, "link": link,
        "pub_dt": pub_dt, "thumbnail": thumb, "summary": summary,
        "tier": _source_tier(publisher),
    }

def _rss_pub_dt(date_str: str) -> datetime:
    """RFC 2822 날짜 문자열 → datetime (실패 시 datetime.min)."""
    try:
        return _parse_rfc2822(date_str).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _fetch_rss(url: str, days: int, default_pub: str = "") -> list[dict]:
    """RSS 피드를 가져와 기간 내 뉴스 아이템 반환. 실패 시 []."""
    cutoff = datetime.now() - timedelta(days=days)
    try:
        import requests as _req
        r = _req.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PTBot/2.0)"},
                     timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for item in root.findall(".//item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link")  or "").strip()
            pub_str = item.findtext("pubDate") or ""
            desc    = item.findtext("description") or ""
            source  = (item.findtext("source") or default_pub).strip()

            # HTML 태그 제거
            desc = _PAT["html_tag"].sub("", desc)[:300].strip()

            pub_dt = _rss_pub_dt(pub_str)
            if pub_dt == datetime.min or pub_dt < cutoff:
                continue

            # Google News 형식: "기사 제목 - 언론사"
            if not source and " - " in title:
                t, s = title.rsplit(" - ", 1)
                title, source = t.strip(), s.strip()

            if not title:
                continue

            out.append({
                "title":     title,
                "publisher": source or default_pub,
                "link":      link,
                "pub_dt":    pub_dt,
                "thumbnail": "",
                "summary":   desc,
                "tier":      _source_tier(source or default_pub),
            })
        return out
    except Exception:
        return []


def _dedup_news(items: list[dict]) -> list[dict]:
    """제목 단어 자카드 유사도 > 60% 이면 중복으로 처리."""
    seen: list[set] = []
    out  = []
    for it in items:
        words = set(re.sub(r"[^\w\s]", "", it["title"].lower()).split())
        if not any(len(words & s) / max(len(words | s), 1) > 0.6 for s in seen):
            seen.append(words)
            out.append(it)
    return out


def resolve_ticker(query: str):
    candidates = [query.upper(), query.upper() + ".KS", query.upper() + ".KQ"]
    for sym in candidates:
        try:
            t = yf.Ticker(sym)
            info = t.info
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if price:
                return sym, info
        except Exception:
            pass
    return None, None

# ═══════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))

@app.route("/manifest.json")
def manifest():
    return send_file(os.path.join(BASE_DIR, "manifest.json"),
                     mimetype="application/manifest+json")

@app.route("/service-worker.js")
def service_worker():
    resp = send_file(os.path.join(BASE_DIR, "service-worker.js"),
                     mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/icons/<path:filename>")
def icons(filename):
    return send_file(os.path.join(BASE_DIR, "icons", filename))

@app.route("/api/version")
def api_version():
    return jsonify({"version": VERSION, "changelog": CHANGELOG})

@app.route("/api/db-type")
def api_db_type():
    return jsonify({"type": "postgresql" if _USE_PG else "sqlite"})

@app.route("/api/health")
def api_health():
    """DB 연결·읽기·쓰기를 실제로 검증하는 헬스체크"""
    try:
        with _db() as c:
            # 실제 쿼리로 연결 확인
            c.execute("SELECT COUNT(*) FROM users")
            c.commit()
        return jsonify({"ok": True, "db": "postgresql" if _USE_PG else "sqlite"})
    except Exception as e:
        app.logger.error(f"health check failed: {e}")
        return jsonify({"ok": False, "error": str(e), "db": "postgresql" if _USE_PG else "sqlite"}), 503

# ── 회원가입 ──────────────────────────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "아이디와 비밀번호를 입력하세요."}), 400
    if len(username) < 3:
        return jsonify({"error": "아이디는 3자 이상이어야 합니다."}), 400
    if len(password) < 6:
        return jsonify({"error": "비밀번호는 6자 이상이어야 합니다."}), 400
    try:
        with _db() as c:
            c.execute("INSERT INTO users(username,pw_hash) VALUES(?,?)",
                      (username, generate_password_hash(password)))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            _ensure_user_data(c, uid)
            c.commit()
        return jsonify({"message": "계정이 생성됐습니다."})
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err:
            return jsonify({"error": "이미 사용 중인 아이디입니다."}), 409
        app.logger.error(f"Register error: {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

# ── 로그인 ────────────────────────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["pw_hash"], password):
        return jsonify({"error": "아이디 또는 비밀번호가 올바르지 않습니다."}), 401
    token = _make_token(row["id"], row["username"], row["role"])
    return jsonify({"token": token, "username": row["username"], "role": row["role"]})

# ── 내 정보 ───────────────────────────────────────────────────────────────────
@app.route("/api/auth/me")
@require_auth
def api_me():
    return jsonify({"user_id": g.uid, "username": g.username, "role": g.role})

# ── 유저 목록 (master) ────────────────────────────────────────────────────────
@app.route("/api/users")
@require_master
def api_users():
    with _db() as c:
        rows = c.execute("SELECT id,username,role,created FROM users ORDER BY id").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            ud = c.execute("SELECT assets FROM user_data WHERE user_id=?", (row["id"],)).fetchone()
            try:
                d["asset_count"] = len(json.loads(ud["assets"] or "[]")) if ud else 0
            except Exception:
                d["asset_count"] = 0
            result.append(d)
    return jsonify(result)

# ── 유저 생성 (master) ────────────────────────────────────────────────────────
@app.route("/api/users", methods=["POST"])
@require_master
def api_create_user():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "아이디와 비밀번호를 입력하세요."}), 400
    if len(username) < 3:
        return jsonify({"error": "아이디는 3자 이상이어야 합니다."}), 400
    try:
        with _db() as c:
            c.execute("INSERT INTO users(username,pw_hash) VALUES(?,?)",
                      (username, generate_password_hash(password)))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            _ensure_user_data(c, uid)
            c.commit()
        return jsonify({"message": f"'{username}' 계정이 생성됐습니다."})
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err:
            return jsonify({"error": "이미 사용 중인 아이디입니다."}), 409
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500

# ── 유저 삭제 (master) ────────────────────────────────────────────────────────
@app.route("/api/users/<int:uid>", methods=["DELETE"])
@require_master
def api_delete_user(uid):
    if uid == g.uid:
        return jsonify({"error": "자신의 계정은 삭제할 수 없습니다."}), 400
    with _db() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "존재하지 않는 계정입니다."}), 404
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.commit()
    return jsonify({"message": f"'{row['username']}' 계정이 삭제됐습니다."})

# ── 유저 비밀번호 변경 (master) ───────────────────────────────────────────────
@app.route("/api/users/<int:uid>/password", methods=["PUT"])
@require_master
def api_reset_password(uid):
    d = request.json or {}
    new_pw = (d.get("password") or "").strip()
    if not new_pw:
        return jsonify({"error": "비밀번호를 입력하세요."}), 400
    with _db() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "존재하지 않는 계정입니다."}), 404
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(new_pw), uid))
        c.commit()
    return jsonify({"message": f"'{row['username']}' 비밀번호가 변경됐습니다."})

# ── 내 데이터 로드 ────────────────────────────────────────────────────────────
@app.route("/api/me/data")
@require_auth
def api_me_data():
    try:
        with _db() as c:
            _ensure_user_data(c, g.uid)
            c.commit()
            row = c.execute("SELECT portfolio,assets FROM user_data WHERE user_id=?", (g.uid,)).fetchone()
        if not row:
            return jsonify({"portfolio": [], "assets": []})
        return jsonify({
            "portfolio": json.loads(row["portfolio"] or "[]"),
            "assets":    json.loads(row["assets"]    or "[]"),
        })
    except Exception as e:
        app.logger.error(f"api_me_data uid={g.uid}: {e}")
        return jsonify({"portfolio": [], "assets": []})

# ── 포트폴리오 저장 ───────────────────────────────────────────────────────────
@app.route("/api/me/portfolio", methods=["PUT"])
@require_auth
def api_save_portfolio():
    data = request.json or {}
    portfolio = data.get("portfolio", [])
    try:
        with _db() as c:
            _save_user_field(c, g.uid, "portfolio", json.dumps(portfolio, ensure_ascii=False))
            c.commit()
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.error(f"api_save_portfolio uid={g.uid}: {e}")
        return jsonify({"error": str(e), "ok": False}), 500

# ── 자산 저장 ─────────────────────────────────────────────────────────────────
@app.route("/api/me/assets", methods=["PUT"])
@require_auth
def api_save_assets():
    data = request.json or {}
    assets = data.get("assets", [])
    try:
        with _db() as c:
            _save_user_field(c, g.uid, "assets", json.dumps(assets, ensure_ascii=False))
            c.commit()
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.error(f"api_save_assets uid={g.uid}: {e}")
        return jsonify({"error": str(e), "ok": False}), 500

# ── USD/KRW 환율 ──────────────────────────────────────────────────────────────
@app.route("/api/exchange-rate")
def api_exchange_rate():
    cache_key = "exchange_rate:USDKRW"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    # 1차: Frankfurter (ECB 기준, 무료·키 불필요)
    try:
        import requests as _req
        r = _req.get("https://api.frankfurter.app/latest?from=USD&to=KRW",
                     timeout=6)
        d = r.json()
        result = {
            "rate": round(float(d["rates"]["KRW"]), 2),
            "date": d.get("date", datetime.now().strftime("%Y-%m-%d")),
            "source": "Frankfurter (ECB)",
        }
        _cache.set(cache_key, result, 3600)   # 1시간 캐시
        return jsonify(result)
    except Exception:
        pass

    # 2차 fallback: yfinance USDKRW=X
    try:
        fi = yf.Ticker("USDKRW=X").fast_info
        rate = float(fi.last_price)
        result = {
            "rate": round(rate, 2),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source": "Yahoo Finance",
        }
        _cache.set(cache_key, result, 3600)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/<ticker>")
def api_debug(ticker):
    """각 yfinance 호출을 단계별로 테스트해 결과/오류를 JSON으로 반환"""
    result = {}
    t = yf.Ticker(ticker.upper())

    # 1. 기본 info
    try:
        info = t.info
        result["info"] = {
            "ok": True,
            "name": info.get("longName") or info.get("shortName"),
            "price": info.get("regularMarketPrice") or info.get("currentPrice"),
        }
    except Exception as e:
        result["info"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 2. earnings_dates
    try:
        ed = t.earnings_dates
        if ed is None or ed.empty:
            result["earnings_dates"] = {"ok": True, "rows": 0, "note": "비어있음"}
        else:
            reported = ed[ed["Reported EPS"].notna()]
            result["earnings_dates"] = {"ok": True, "total_rows": len(ed), "reported_rows": len(reported)}
    except Exception as e:
        result["earnings_dates"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 3. quarterly_financials
    try:
        qf = t.quarterly_financials
        if qf is None or qf.empty:
            result["quarterly_financials"] = {"ok": True, "rows": 0, "note": "비어있음"}
        else:
            result["quarterly_financials"] = {"ok": True, "index": list(qf.index), "cols": len(qf.columns)}
    except Exception as e:
        result["quarterly_financials"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 4. 번역 테스트
    try:
        translated = _translate("earnings report", 50)
        result["translation"] = {"ok": True, "result": translated}
    except Exception as e:
        result["translation"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    print(f"[debug/{ticker}] {result}")
    return jsonify(result)

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"success": False, "message": "검색어를 입력하세요."})
    sym, info = resolve_ticker(query)
    if sym:
        price = info.get("regularMarketPrice") or info.get("currentPrice", 0)
        prev  = info.get("regularMarketPreviousClose") or info.get("previousClose", 0)
        return jsonify({
            "success": True, "ticker": sym,
            "name": info.get("longName") or info.get("shortName") or sym,
            "price": price,
            "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
            "currency": info.get("currency", "USD"),
        })
    return jsonify({"success": False, "message": f"'{query}'을(를) 찾을 수 없습니다."})

@app.route("/api/stock/price")
def api_price():
    ticker = request.args.get("ticker", "")
    cache_key = f"price:{ticker}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)
    try:
        info  = yf.Ticker(ticker).info
        price = info.get("regularMarketPrice") or info.get("currentPrice", 0)
        prev  = info.get("regularMarketPreviousClose") or info.get("previousClose", 0)
        change = price - prev if prev else 0
        result = {
            "price": price,
            "change": round(change, 4),
            "change_pct": round((change / prev * 100) if prev else 0, 2),
            "currency": info.get("currency", "USD"),
            "name": info.get("longName") or info.get("shortName") or ticker,
        }
        _cache.set(cache_key, result, TTL_PRICE)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stock/news")
def api_news():
    ticker = (request.args.get("ticker", "") or "").upper()
    period = request.args.get("period", "1w")

    # ── 0. 기간별 결과 캐시 (가장 빠른 경로) ─────────────────────────────────
    cache_key = f"news:{ticker}:{period}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    period_days = {"1w": 7, "1m": 30, "6m": 180}
    days = period_days.get(period, 7)

    # ═════════════════════════════════════════════════════════════════════════
    #  [1주일] yfinance raw 캐시만 사용 (빠름)
    # ═════════════════════════════════════════════════════════════════════════
    if period == "1w":
        raw_key = f"news_raw:{ticker}"
        scored  = _cache.get(raw_key)
        if scored is None:
            try:
                tk = yf.Ticker(ticker)
                try:
                    raw = tk.get_news(count=50) or []
                except Exception:
                    raw = tk.news or []
                scored = []
                for item in raw:
                    p = _parse_news_item(item)
                    if not p or not p["title"]:
                        continue
                    p["stars"], p["star_reasons"] = _score_fundamental_impact(
                        p["title"], p["summary"], p["publisher"])
                    scored.append(p)
                _cache.set(raw_key, scored, TTL_NEWS)
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        cutoff   = datetime.now() - timedelta(days=7)
        filtered = [p for p in scored
                    if p["pub_dt"] != datetime.min and p["pub_dt"] >= cutoff]
        if len(filtered) < 3:
            filtered = list(scored)   # yfinance 뉴스가 7일 미만이면 전체 반환

    # ═════════════════════════════════════════════════════════════════════════
    #  [1달·6개월] yfinance + Google News RSS + Yahoo Finance RSS 병렬 수집
    # ═════════════════════════════════════════════════════════════════════════
    else:
        # 회사명 (가격 캐시에 있으면 활용해 검색 품질 향상)
        price_c  = _cache.get(f"price:{ticker}")
        company  = (price_c or {}).get("name", ticker)

        # Google News: ticker + 회사명 복합 쿼리
        q_enc    = _url_quote(f"{ticker} {company} stock")
        goog_url = (f"https://news.google.com/rss/search"
                    f"?q={q_enc}&hl=en-US&gl=US&ceid=US:en")
        yhoo_url = (f"https://feeds.finance.yahoo.com/rss/2.0/headline"
                    f"?s={ticker}&region=US&lang=en-US")

        # yfinance raw (이미 캐시됐으면 재사용)
        yf_scored = _cache.get(f"news_raw:{ticker}") or []

        # Google News / Yahoo Finance RSS 병렬 fetch
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_g = ex.submit(_fetch_rss, goog_url, days)
            fut_y = ex.submit(_fetch_rss, yhoo_url, days, "Yahoo Finance")
        google_items = fut_g.result()
        yahoo_items  = fut_y.result()

        # RSS 아이템 별점 계산
        for p in google_items + yahoo_items:
            p["stars"], p["star_reasons"] = _score_fundamental_impact(
                p["title"], p["summary"], p["publisher"])

        # yfinance 기간 필터 (날짜 실패 항목은 통과)
        cutoff    = datetime.now() - timedelta(days=days)
        yf_part   = [p for p in yf_scored
                     if p["pub_dt"] == datetime.min or p["pub_dt"] >= cutoff]

        # 합치기 → 중복 제거
        all_items = yf_part + google_items + yahoo_items
        filtered  = _dedup_news(all_items)
        if not filtered:
            filtered = yf_scored[:5]   # 모든 소스 실패 시 yfinance 원본 사용

    # ── 중요도↓ · 티어↑ · 날짜↓ 정렬 → 상위 5개 ────────────────────────────
    filtered.sort(key=lambda x: (
        -x["stars"], x["tier"],
        -(x["pub_dt"].timestamp() if x["pub_dt"] != datetime.min else 0)
    ))
    top = filtered[:5]

    # ── 제목·요약 병렬 번역 ───────────────────────────────────────────────────
    texts        = [p["title"] for p in top] + [p["summary"] for p in top]
    translated   = _translate_batch(texts, 400)
    titles_ko    = translated[:len(top)]
    summaries_ko = translated[len(top):]

    result = [{
        "title":          titles_ko[i],
        "title_original": p["title"],
        "publisher":      p["publisher"],
        "tier":           p["tier"],
        "link":           p["link"],
        "date":           (p["pub_dt"].strftime("%Y-%m-%d %H:%M")
                          if p["pub_dt"] != datetime.min else ""),
        "thumbnail":      p["thumbnail"],
        "summary":        summaries_ko[i],
        "stars":          p["stars"],
        "star_reasons":   p["star_reasons"],
    } for i, p in enumerate(top)]

    # 1m·6m 는 캐시 TTL 단축 (외부 API 결과는 시간이 지나면 바뀜)
    ttl = TTL_NEWS if period == "1w" else TTL_NEWS // 2
    _cache.set(cache_key, result, ttl)
    return jsonify(result)

@app.route("/api/stock/earnings")
def api_earnings():
    ticker = request.args.get("ticker", "")
    cache_key = f"earnings:{ticker}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)
    try:
        t = yf.Ticker(ticker)

        # ── 병렬 fetch: earnings_dates + quarterly_financials ──────────────────
        def _fetch_eps():
            rows = []
            try:
                print(f"[earnings:{ticker}] earnings_dates 조회 시작")
                ed = t.earnings_dates
                if ed is None or ed.empty:
                    print(f"[earnings:{ticker}] earnings_dates 비어있음")
                    return rows
                reported = ed[ed["Reported EPS"].notna()].copy()
                print(f"[earnings:{ticker}] earnings_dates 보고된 행 수: {len(reported)}")
                for dt, row in reported.iterrows():
                    actual   = row.get("Reported EPS")
                    estimate = row.get("EPS Estimate")
                    surprise = row.get("Surprise(%)")
                    rows.append({
                        "date":        dt,
                        "epsActual":   float(actual)   if pd.notna(actual)   else 0.0,
                        "epsEstimate": float(estimate)  if pd.notna(estimate)  else 0.0,
                        "surprisePct": float(surprise)  if pd.notna(surprise)  else 0.0,
                    })
            except Exception as e:
                print(f"[earnings:{ticker}] _fetch_eps 오류: {type(e).__name__}: {e}")
            return rows

        def _fetch_rev():
            rev_list = []
            try:
                print(f"[earnings:{ticker}] quarterly_financials 조회 시작")
                qf = t.quarterly_financials
                if qf is None or qf.empty:
                    print(f"[earnings:{ticker}] quarterly_financials 비어있음")
                    return rev_list
                print(f"[earnings:{ticker}] quarterly_financials 인덱스: {list(qf.index)}")
                for label in ["Total Revenue", "Revenue"]:
                    if label in qf.index:
                        for col, val in qf.loc[label].items():
                            if pd.notna(val):
                                rev_list.append((col, float(val)))
                        break
                rev_list.sort(key=lambda x: x[0], reverse=True)
                print(f"[earnings:{ticker}] 매출 행 수: {len(rev_list)}")
            except Exception as e:
                print(f"[earnings:{ticker}] _fetch_rev 오류: {type(e).__name__}: {e}")
            return rev_list

        def _fetch_rev_estimate():
            rev_estimate_map: dict = {}
            try:
                re_df = t.revenue_estimate
                if re_df is None or re_df.empty:
                    return rev_estimate_map
                from yfinance.data import YfData as _YfData
                _tr = _YfData(session=None).get_raw_json(
                    f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                    params={"modules": "earningsTrend"}
                )
                for item in _tr["quoteSummary"]["result"][0]["earningsTrend"]["trend"]:
                    end = item.get("endDate")
                    if isinstance(end, dict):
                        end = end.get("fmt", "")
                    period_key = str(end)[:10]
                    rev_est = item.get("revenueEstimate", {})
                    avg_obj = rev_est.get("avg", {})
                    avg_raw = avg_obj.get("raw") if isinstance(avg_obj, dict) else avg_obj
                    if avg_raw:
                        rev_estimate_map[period_key] = {
                            "estimate":    float(avg_raw),
                            "estimateFmt": _fmt_revenue(avg_raw),
                        }
            except Exception as e:
                print(f"[earnings:{ticker}] _fetch_rev_estimate 오류: {type(e).__name__}: {e}")
            return rev_estimate_map

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_eps  = ex.submit(_fetch_eps)
            f_rev  = ex.submit(_fetch_rev)
            f_rest = ex.submit(_fetch_rev_estimate)
            eps_rows        = f_eps.result()
            rev_list        = f_rev.result()
            rev_estimate_map = f_rest.result()

        # ── 결합 ──────────────────────────────────────────────────────────────
        rows = []
        for i, ep in enumerate(eps_rows[:8]):
            rev_val    = rev_list[i][1] if i < len(rev_list) else 0.0
            dt         = ep["date"]
            period_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
            rev_est    = rev_estimate_map.get(period_str)
            rev_surp   = None
            if rev_est and rev_val:
                rev_surp = round((rev_val - rev_est["estimate"]) / abs(rev_est["estimate"]) * 100, 2)
            rows.append({
                "period":             period_str,
                "epsActual":          ep["epsActual"],
                "epsEstimate":        round(ep["epsEstimate"], 4),
                "epsSurprisePct":     round(ep["surprisePct"], 2),
                "revenueActual":      rev_val,
                "revenueActualFmt":   _fmt_revenue(rev_val),
                "revenueEstimate":    rev_est["estimate"] if rev_est else None,
                "revenueEstimateFmt": rev_est["estimateFmt"] if rev_est else None,
                "revSurprisePct":     rev_surp,
            })

        for i in range(min(2, len(rows))):
            if i + 1 < len(rows):
                pe, pr = rows[i+1]["epsActual"], rows[i+1]["revenueActual"]
                rows[i]["epsQoQPct"] = round((rows[i]["epsActual"] - pe) / abs(pe) * 100, 2) if pe else None
                rows[i]["revQoQPct"] = round((rows[i]["revenueActual"] - pr) / abs(pr) * 100, 2) if pr else None
            if i + 4 < len(rows):
                ye, yr = rows[i+4]["epsActual"], rows[i+4]["revenueActual"]
                rows[i]["epsYoYPct"] = round((rows[i]["epsActual"] - ye) / abs(ye) * 100, 2) if ye else None
                rows[i]["revYoYPct"] = round((rows[i]["revenueActual"] - yr) / abs(yr) * 100, 2) if yr else None

        result = rows[:2]
        _cache.set(cache_key, result, TTL_EARNINGS)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stock/chart")
def api_chart():
    ticker = request.args.get("ticker", "")
    period = request.args.get("period", "1y")
    cache_key = f"chart:{ticker}:{period}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)
    ttl = TTL_CHART if period in ("1mo", "3mo") else TTL_FUNDAMENTALS
    try:
        hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        data = [
            {
                "date":   dt.strftime("%Y-%m-%d"),
                "open":   round(float(row["Open"]), 4),
                "high":   round(float(row["High"]), 4),
                "low":    round(float(row["Low"]), 4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for dt, row in hist.iterrows()
        ]
        _cache.set(cache_key, data, ttl)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stock/fundamentals")
def api_fundamentals():
    ticker = request.args.get("ticker", "")
    cache_key = f"fund:{ticker}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)
    try:
        info = yf.Ticker(ticker).info

        def safe(key, digits=2):
            v = info.get(key)
            return round(float(v), digits) if v is not None else None

        mkt = info.get("marketCap")
        raw_desc   = (info.get("longBusinessSummary") or "")[:600]
        raw_sector = info.get("sector", "")
        raw_ind    = info.get("industry", "")

        # 기업 설명·섹터·산업 병렬 번역
        desc_ko, sector_ko, ind_ko = _translate_batch(
            [raw_desc, raw_sector, raw_ind], 600)

        def safe_pe(key):
            """P/E는 음수·극단값(-500 이상 or 500 이하) 필터링"""
            v = info.get(key)
            if v is None:
                return None
            v = float(v)
            if abs(v) > 500:   # 비정상 수치 (EPS가 0에 가까울 때 발생)
                return None
            return round(v, 2)

        # 배당수익률: yfinance dividendYield는 이미 % 단위 (0.09 = 0.09%)
        # trailingAnnualDividendYield는 비율 단위 (0.000853 = 0.085%)
        # 더 정확한 trailing 값을 우선 사용, 없으면 dividendYield 그대로 사용
        trailing_dy = info.get("trailingAnnualDividendYield")
        fwd_dy      = info.get("dividendYield")
        if trailing_dy and trailing_dy > 0:
            div_yield = round(trailing_dy * 100, 4)   # 비율 → %
        elif fwd_dy and fwd_dy > 0:
            div_yield = round(float(fwd_dy), 4)        # 이미 % 단위
        else:
            div_yield = None

        # 연간 배당금(달러)
        div_rate = info.get("trailingAnnualDividendRate") or info.get("dividendRate")

        result = {
            "marketCap":     (_fmt_revenue(mkt).replace("$", "") + " USD") if mkt else None,
            "pe":            safe_pe("trailingPE"),
            "forwardPE":     safe_pe("forwardPE"),
            "eps":           safe("trailingEps"),
            "dividendYield": div_yield,
            "dividendRate":  round(float(div_rate), 2) if div_rate else None,
            "beta":          safe("beta"),
            "high52w":       safe("fiftyTwoWeekHigh"),
            "low52w":        safe("fiftyTwoWeekLow"),
            "avgVolume":     info.get("averageVolume"),
            "sector":        sector_ko or None,
            "industry":      ind_ko or None,
            "employees":     info.get("fullTimeEmployees"),
            "description":   desc_ko,
            "currency":      info.get("currency", "USD"),
            "website":       info.get("website", ""),
        }
        _cache.set(cache_key, result, TTL_FUNDAMENTALS)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/update", methods=["POST"])
def api_update():
    data    = request.json or {}
    tickers = data.get("tickers", [])
    sid     = data.get("session_id", "default")

    def worker():
        # 업데이트 시 캐시 무효화 후 병렬 재fetch
        for ticker in tickers:
            for prefix in ("price", "news:1w", "news:1m", "news:6m",
                           "earnings", "chart:1mo", "chart:3mo", "chart:1y", "chart:5y", "fund"):
                _cache.delete(f"{prefix}:{ticker}" if ":" not in prefix else f"{prefix}:{ticker}".replace(f":{ticker}", f":{ticker}"))
            # 올바른 키 형태로 삭제
            _cache.delete(f"price:{ticker}")
            _cache.delete(f"earnings:{ticker}")
            _cache.delete(f"fund:{ticker}")
            _cache.delete(f"news_raw:{ticker}")
            for p in ("1w", "1m", "6m"):
                _cache.delete(f"news:{ticker}:{p}")
            for p in ("1mo", "3mo", "1y", "5y"):
                _cache.delete(f"chart:{ticker}:{p}")

        total = len(tickers)
        for idx, ticker in enumerate(tickers):
            _progress[sid] = {
                "progress": int(idx / total * 90),
                "message":  f"{ticker} 업데이트 중... ({idx+1}/{total})",
                "done":     False,
            }
            # price + news(1w) + earnings 병렬 fetch → 캐시 채우기
            t = yf.Ticker(ticker)
            def _pre_price(t=t, tk=ticker):
                try:
                    info  = t.info
                    price = info.get("regularMarketPrice") or info.get("currentPrice", 0)
                    prev  = info.get("regularMarketPreviousClose") or info.get("previousClose", 0)
                    change = price - prev if prev else 0
                    _cache.set(f"price:{tk}", {
                        "price": price, "change": round(change, 4),
                        "change_pct": round((change/prev*100) if prev else 0, 2),
                        "currency": info.get("currency","USD"),
                        "name": info.get("longName") or info.get("shortName") or tk,
                    }, TTL_PRICE)
                except Exception:
                    pass
            def _pre_news(t=t, tk=ticker):
                try:
                    try:
                        raw = t.get_news(count=50) or []
                    except Exception:
                        raw = t.news or []
                    scored = []
                    for item in raw:
                        p = _parse_news_item(item)
                        if not p or not p["title"]:
                            continue
                        p["stars"], p["star_reasons"] = _score_fundamental_impact(
                            p["title"], p["summary"], p["publisher"])
                        scored.append(p)
                    _cache.set(f"news_raw:{tk}", scored, TTL_NEWS)

                    # 1w 결과도 즉시 캐시 (가장 자주 쓰이는 기간)
                    cutoff = datetime.now() - timedelta(days=7)
                    filtered = [p for p in scored
                                if p["pub_dt"] != datetime.min and p["pub_dt"] >= cutoff] or list(scored)
                    filtered.sort(key=lambda x: (-x["stars"], x["tier"],
                                                  -(x["pub_dt"].timestamp() if x["pub_dt"] != datetime.min else 0)))
                    top = filtered[:5]
                    texts = [p["title"] for p in top] + [p["summary"] for p in top]
                    translated = _translate_batch(texts, 400)
                    titles_ko = translated[:len(top)]
                    sums_ko   = translated[len(top):]
                    result = [{
                        "title": titles_ko[i], "title_original": p["title"],
                        "publisher": p["publisher"], "tier": p["tier"],
                        "link": p["link"],
                        "date": p["pub_dt"].strftime("%Y-%m-%d %H:%M") if p["pub_dt"] != datetime.min else "",
                        "thumbnail": p["thumbnail"], "summary": sums_ko[i],
                        "stars": p["stars"], "star_reasons": p["star_reasons"],
                    } for i, p in enumerate(top)]
                    _cache.set(f"news:{tk}:1w", result, TTL_NEWS)
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=2) as ex:
                ex.submit(_pre_price)
                ex.submit(_pre_news)

        # 번역 캐시 디스크 저장
        _save_trans_cache()
        _progress[sid] = {"progress": 100, "message": "업데이트 완료!", "done": True}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"session_id": sid})

@app.route("/api/update/progress")
def api_progress():
    sid = request.args.get("session_id", "default")
    def generate():
        while True:
            d = _progress.get(sid, {"progress": 0, "message": "대기 중...", "done": False})
            yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
            if d.get("done"):
                _progress.pop(sid, None)
                break
            time.sleep(0.3)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _find_free_port(start: int = 5000) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start

if __name__ == "__main__":
    port = _find_free_port(5000)
    url  = f"http://localhost:{port}"
    print(f"Portfolio Tracker v{VERSION}")
    print(f"접속 주소: {url}")
    print("종료하려면 이 창을 닫으세요.")
    # 브라우저를 1초 뒤 자동으로 열기
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(debug=False, host="127.0.0.1", port=port, threaded=True)
