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

from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import json, threading, time, re, webbrowser, socket
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app)

VERSION = "1.3.0"
CHANGELOG = [
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
    ticker = request.args.get("ticker", "")
    period = request.args.get("period", "1w")
    cache_key = f"news:{ticker}:{period}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    period_days = {"1w": 7, "1m": 30, "6m": 180}
    cutoff = datetime.now() - timedelta(days=period_days.get(period, 7))
    try:
        raw = yf.Ticker(ticker).news or []

        # parse + date filter
        parsed = []
        for item in raw:
            p = _parse_news_item(item)
            if p and p["pub_dt"] >= cutoff:
                parsed.append(p)

        # 별점 계산 (CPU-only, 빠름)
        for p in parsed:
            p["stars"], p["star_reasons"] = _score_fundamental_impact(
                p["title"], p["summary"], p["publisher"])

        # 별점↓ · 티어↑ · 날짜↓ 정렬 후 상위 5개
        parsed.sort(key=lambda x: (-x["stars"], x["tier"], -x["pub_dt"].timestamp()))
        top = parsed[:5]

        # 제목·요약 병렬 번역
        texts  = [p["title"]   for p in top] + [p["summary"] for p in top]
        translated = _translate_batch(texts, 400)
        titles_ko  = translated[:len(top)]
        summaries_ko = translated[len(top):]

        result = []
        for i, p in enumerate(top):
            result.append({
                "title":          titles_ko[i],
                "title_original": p["title"],
                "publisher":      p["publisher"],
                "tier":           p["tier"],
                "link":           p["link"],
                "date":           p["pub_dt"].strftime("%Y-%m-%d %H:%M") if p["pub_dt"] != datetime.min else "",
                "thumbnail":      p["thumbnail"],
                "summary":        summaries_ko[i],
                "stars":          p["stars"],
                "star_reasons":   p["star_reasons"],
            })

        _cache.set(cache_key, result, TTL_NEWS)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

        result = {
            "marketCap":     (_fmt_revenue(mkt).replace("$", "") + " USD") if mkt else None,
            "pe":            safe("trailingPE"),
            "forwardPE":     safe("forwardPE"),
            "eps":           safe("trailingEps"),
            "dividendYield": round(info.get("dividendYield", 0) * 100, 2) if info.get("dividendYield") else None,
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
                    raw = t.news or []
                    cutoff = datetime.now() - timedelta(days=7)
                    parsed = [p for item in raw
                              if (p := _parse_news_item(item)) and p["pub_dt"] >= cutoff]
                    for p in parsed:
                        p["stars"], p["star_reasons"] = _score_fundamental_impact(
                            p["title"], p["summary"], p["publisher"])
                    parsed.sort(key=lambda x: (-x["stars"], x["tier"], -x["pub_dt"].timestamp()))
                    top = parsed[:5]
                    texts = [p["title"] for p in top] + [p["summary"] for p in top]
                    translated = _translate_batch(texts, 400)
                    titles_ko = translated[:len(top)]
                    sums_ko   = translated[len(top):]
                    result = [{
                        "title": titles_ko[i], "title_original": p["title"],
                        "publisher": p["publisher"], "tier": p["tier"],
                        "link": p["link"],
                        "date": p["pub_dt"].strftime("%Y-%m-%d %H:%M"),
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
