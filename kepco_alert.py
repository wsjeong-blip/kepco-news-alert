"""KEPCO 뉴스 + 공개 유출·랜섬웨어 위협정보 모니터링 → 텔레그램.

- 기존 지정 언론사 뉴스, ransomware.live RSS 유지
- RansomLook / ThreatCluster 공개 랜섬웨어 피해 주장 RSS 추가
- 공개 보도 내 해킹포럼·정보판매 관련 KEPCO 키워드 탐지 (포럼 직접 수집 아님)
- 선택: HIBP 인증 도메인 유출 계정 집계(API 키와 도메인 인증 필요)
- 유출 원본·계정 주소·비밀번호는 다운로드하거나 저장/발송하지 않음

필수 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, HIBP_API_KEY, HIBP_DOMAIN
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
SEEN_FILE = "seen.json"
MAX_SEEN = 20000  # 기사 ID와 제목의 복수 키를 보관하므로 충분한 이력 유지
MAX_AGE = timedelta(hours=6)  # 뉴스 기사만 적용
INTEL_MAX_AGE = timedelta(days=14)  # 공개 유출정보는 수집 지연을 고려


# ── 감시 대상 언론사 (도메인: (표시명, 분류)) ── 필요하면 여기만 고치면 됨
OUTLETS = {
    # 방송·통신
    "kbs.co.kr": ("KBS", "방송"),
    "imbc.com": ("MBC", "방송"),
    "sbs.co.kr": ("SBS", "방송"),
    "ytn.co.kr": ("YTN", "방송"),
    "jtbc.co.kr": ("JTBC", "방송"),
    "mbn.co.kr": ("MBN", "방송"),
    "ichannela.com": ("채널A", "방송"),
    "tvchosun.com": ("TV조선", "방송"),
    "yonhapnewstv.co.kr": ("연합뉴스TV", "방송"),
    "yna.co.kr": ("연합뉴스", "통신"),
    "newsis.com": ("뉴시스", "통신"),
    "news1.kr": ("뉴스1", "통신"),
    # 주요 일간지
    "chosun.com": ("조선일보", "신문"),
    "joongang.co.kr": ("중앙일보", "신문"),
    "donga.com": ("동아일보", "신문"),
    "hani.co.kr": ("한겨레", "신문"),
    "khan.co.kr": ("경향신문", "신문"),
    "hankookilbo.com": ("한국일보", "신문"),
    "mk.co.kr": ("매일경제", "신문"),
    "hankyung.com": ("한국경제", "신문"),
    # 보안 전문지
    "boannews.com": ("보안뉴스", "보안"),
    "dailysecu.com": ("데일리시큐", "보안"),
    "datanet.co.kr": ("데이터넷", "보안"),
    # 다크웹 (랜섬웨어 조직 유출 사이트 모음)
    "ransomware.live": ("랜섬웨어 유출", "유출정보"),
    "ransomlook.io": ("RansomLook", "유출정보"),
    "threatcluster.io": ("ThreatCluster", "유출정보"),
    "public-intel.google": ("공개 유출·포럼 보도", "공개위협보도"),
    "hibp": ("HIBP 도메인 계정", "유출계정"),
}

# 언론사 자체 RSS (전체기사 피드를 받아 키워드로 거름 → 네이버/구글보다 빠름)
DIRECT_FEEDS = {
    "boannews.com": "https://www.boannews.com/rss/allArticle.xml",
    "dailysecu.com": "https://www.dailysecu.com/rss/allArticle.xml",
}

# 랜섬웨어 유출 사이트 감시 (ransomware.live 공개 RSS, 다크웹 직접 접속 없음)
RANSOMWARE_FEED = "https://www.ransomware.live/rss.xml"
RANSOMWARE_WATCH = ["KEPCO", "Korea Electric Power", "kepco.co.kr", "한국전력", "한전"]
RANSOMWARE_COUNTRY = "KR"   # ransomware.live에서 국내 피해기관 참고 알림

# 공개 수집원만 사용합니다. 직접 다크웹·폐쇄 포럼에 접속하지 않습니다.
PUBLIC_LEAK_FEEDS = {
    "ransomlook.io": "https://www.ransomlook.io/rss.xml",
    "threatcluster.io": "https://threatcluster.io/dark-web/feed.xml",
}
PUBLIC_FEED_ROOTS = {
    "ransomlook.io": "https://www.ransomlook.io/recent",
    "threatcluster.io": "https://threatcluster.io/dark-web",
}
# 일반 검색 결과와 별개로, 기관 + 유출/판매/해킹포럼 관련 공개 보도를 탐지
PUBLIC_INTEL_QUERIES = (
    '"KEPCO" "BreachForums"',
    '"KEPCO" "data leak"',
    '"kepco.co.kr" "breach"',
    '"한국전력" "다크웹"',
    '"한국전력" "개인정보 유출"',
)
LEAK_TERMS = re.compile(
    r"breachforums|dark\s?web|leak(?:ed)?|breach|database|data\s?dump|"
    r"stealer|for\s+sale|해킹|유출|다크웹|정보판매|판매글|계정탈취",
    re.IGNORECASE,
)
# 신규 소스 도입 첫 성공 실행 시 과거 게시물은 기록만 하고 다음 신규 게시부터 알림
BOOTSTRAP_SOURCES = {"ransomlook.io", "threatcluster.io", "public-intel.google", "hibp"}
SOURCE_READY = set()
EXTRA_MARKERS = set()


# ── 키워드 / 계열사 제외 ──
AFFILIATES = [
    "한국전력기술", "한국전력거래소", "한국전력정보",
    "한전KPS", "한전케이피에스", "한전KDN", "한전케이디엔",
    "한전기술", "한전원자력연료", "한전MCS", "한전엠씨에스",
    "한전산업개발", "한전ENS", "한전FMS", "한전CSC", "한전전력연구원",
]
COMPANY_KEYWORDS = ["한국전력공사", "한국전력", "한전", "KEPCO"]  # 계열사 제외 적용
TOPIC_KEYWORDS = ["개인정보", "다크웹"]                          # 참고용 (단독으로도 알림)

COMPANY_RE = re.compile("|".join(map(re.escape, COMPANY_KEYWORDS)), re.IGNORECASE)
TOPIC_RE = re.compile("|".join(map(re.escape, TOPIC_KEYWORDS)), re.IGNORECASE)
AFFIL_RE = re.compile(
    "|".join(sorted(map(re.escape, AFFILIATES), key=len, reverse=True))
    + r"|KEPCO[\s\-]?(?:KPS|KDN|E&C|NF|ENS|MCS)",
    re.IGNORECASE)


def classify(title):
    """'both' | 'company' | 'topic' | None"""
    company = bool(COMPANY_RE.search(AFFIL_RE.sub(" ", title)))
    topic = bool(TOPIC_RE.search(title))
    if company and topic:
        return "both"
    return "company" if company else "topic" if topic else None


def is_target(title):
    return classify(title) is not None


def outlet_of(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for dom in OUTLETS:
        if host == dom or host.endswith("." + dom):
            return dom
    return None


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def norm_title(title):
    return re.sub(r"[\W_]+", "", title).casefold()


def item_keys(it):
    """과거 seen.json과 호환: GUID, URL, 원래 제목, 소문자 제목.

    텔레그램은 전송 API의 idempotency key가 없어, 원격 저장 실패 시
    exactly-once를 보장하지는 못한다.
    """
    keys = {str(it["id"])}
    if it["outlet"] == "hibp":  # 계정 수가 같아도 API 결과가 바뀌면 재탐지
        return keys
    title = it["title"]
    legacy_title = re.sub(r"[\W_]+", "", title)
    if len(legacy_title) >= 8:
        keys.update({"t:" + legacy_title, "t:" + legacy_title.casefold()})
    link = it.get("link") or ""
    if link.startswith(("https://", "http://")):
        u = urllib.parse.urlsplit(link)
        params = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
        params = [(k, v) for k, v in params
                  if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
        canonical = urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"),
                                            urllib.parse.urlencode(params), ""))
        keys.add("u:" + canonical)
    return keys


def safe_feed_link(link, dom):
    """유출 원본이나 onion URL이 아니라 공개 인텔 제공자의 링크만 전송."""
    fallback = PUBLIC_FEED_ROOTS[dom]
    try:
        u = urllib.parse.urlparse(link or "")
        host = (u.hostname or "").lower()
        if u.scheme == "https" and (host == dom or host.endswith("." + dom)):
            return link
    except ValueError:
        pass
    return fallback


def atomic_write_seen(keys):
    """중간에 프로세스가 종료되어도 seen.json이 훼손되지 않도록 교체."""
    markers = sorted({k for k in keys if k.startswith(("bootstrap:", "hibp:poll:"))}
                     | EXTRA_MARKERS)
    history = [k for k in keys if not k.startswith(("bootstrap:", "hibp:poll:"))]
    # 과거 일자 HIBP 조회 마커는 보관할 필요 없음
    today = datetime.now(KST).date().isoformat()
    markers = [m for m in markers if not m.startswith("hibp:poll:") or m.endswith(":" + today)]
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history[-MAX_SEEN:] + markers, f, ensure_ascii=False)
    os.replace(tmp, SEEN_FILE)


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:  # 데일리시큐 형식: 2026-11-23 10:27:27 (시간대 없음 = KST)
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
    return d if d.tzinfo else d.replace(tzinfo=KST)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


SOURCE_LOG = []  # 요청 단위 로그: (출처, 결과, 받은 기사 수, 비고)


def progress(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def log_source(name, ok, n=0, note=""):
    SOURCE_LOG.append((name, "성공" if ok else "실패", n, note))
    progress(f"{name}: {'성공' if ok else '실패'} {n}건 {note}")


def item(id_, title, link, dom, date, via, kind=None):
    return {"id": id_ or link, "title": title, "link": link, "outlet": dom,
            "date": date, "via": via, "kind": kind}  # kind: 미리 정한 알림 종류 (다크웹용)


def fetch_direct():
    items = []
    for dom, url in DIRECT_FEEDS.items():
        name = f"RSS {OUTLETS[dom][0]}"
        try:
            got = []
            for it in ET.fromstring(http_get(url)).iter("item"):
                link = (it.findtext("link") or "").strip()
                got.append(item(link, clean(it.findtext("title")), link, dom,
                                parse_date(it.findtext("pubDate")), "RSS"))
            items += got
            log_source(name, True, len(got), "전체기사 피드")
        except Exception as e:
            log_source(name, False, 0, str(e)[:120])
    return items


def fetch_ransomware():
    """랜섬웨어 조직이 유출 사이트에 새 피해 기관을 올리면 감지"""
    watch_re = re.compile("|".join(map(re.escape, RANSOMWARE_WATCH)), re.IGNORECASE)
    items = []
    try:
        raw = list(ET.fromstring(http_get(RANSOMWARE_FEED)).iter("item"))
        for it in raw:
            title = clean(it.findtext("title"))
            m = re.search(r"(.+?) has just published a new victim\s*:\s*(.+)", title)
            group, victim = (m.group(1).replace("🏴‍☠️", "").strip(), m.group(2).strip()) if m \
                else ("?", title)
            desc = clean(it.findtext("description"))
            country = (it.findtext("category") or "").strip().upper()
            if watch_re.search(AFFIL_RE.sub(" ", victim)) or watch_re.search(AFFIL_RE.sub(" ", desc)):
                kind = "both"            # 한전 관련 피해 → 🚨
            elif RANSOMWARE_COUNTRY and country == RANSOMWARE_COUNTRY:
                kind = "topic"           # 국내 피해 기관 → 📎 참고
            else:
                continue
            items.append(item(it.findtext("guid") or it.findtext("link"),
                              f"랜섬웨어 '{group}' 유출 사이트에 '{victim}' 게시 ({country})",
                              (it.findtext("link") or "").strip(), "ransomware.live",
                              parse_date(it.findtext("pubDate")), "다크웹", kind))
        log_source("랜섬웨어 유출 사이트", True, len(items),
                   f"전체 {len(raw)}건 중 감시 대상 {len(items)}건")
    except Exception as e:
        log_source("랜섬웨어 유출 사이트", False, 0, str(e)[:120])
    return items


def fetch_public_leaks():
    """공개 피해 주장 RSS. 게시글 자체를 확인하는 것이 아니며 유출 확정 아님."""
    watch_re = re.compile("|".join(map(re.escape, RANSOMWARE_WATCH)), re.IGNORECASE)
    results = []
    for dom, url in PUBLIC_LEAK_FEEDS.items():
        try:
            raw = list(ET.fromstring(http_get(url)).iter("item"))
            matches = []
            for entry in raw:
                title = clean(entry.findtext("title"))
                desc = clean(entry.findtext("description"))
                text = title + " " + desc
                # 계열사 명칭을 제거해 모회사 피해로 오인하는 것을 줄임
                if not watch_re.search(AFFIL_RE.sub(" ", text)):
                    continue
                raw_link = (entry.findtext("link") or "").strip()
                link = safe_feed_link(raw_link, dom)
                ident = (entry.findtext("guid") or raw_link or title).strip()
                matches.append(item(f"{dom}:{ident}", f"유출 게시 주장: {title}",
                                    link, dom, parse_date(entry.findtext("pubDate")),
                                    "공개 유출정보 RSS", "both"))
            results += matches
            SOURCE_READY.add(dom)
            log_source(OUTLETS[dom][0] + " RSS", True, len(matches),
                       f"전체 {len(raw)}건 중 기관 연관 {len(matches)}건 (주장·미확인)")
        except Exception as exc:
            log_source(OUTLETS[dom][0] + " RSS", False, 0, str(exc)[:120])
    return results


def fetch_public_intel():
    """해킹포럼·유출정보를 다룬 공개 보도 검색. 실제 포럼 직접 수집 아님."""
    results = []
    any_success = False
    for query in PUBLIC_INTEL_QUERIES:
        try:
            url = ("https://news.google.com/rss/search?q=" +
                   urllib.parse.quote(query + " when:7d") + "&hl=ko&gl=KR&ceid=KR:ko")
            raw = list(ET.fromstring(http_get(url)).iter("item"))
            matches = []
            for entry in raw:
                title = clean(entry.findtext("title"))
                desc = clean(entry.findtext("description"))
                # 검색엔진이 본문 일치로 반환한 무관 결과를 배제
                company = bool(COMPANY_RE.search(AFFIL_RE.sub(" ", title + " " + desc)) or
                               re.search(r"kepco\.co\.kr|Korea Electric Power", title + " " + desc,
                                         re.IGNORECASE))
                if not company or not LEAK_TERMS.search(title + " " + desc):
                    continue
                link = (entry.findtext("link") or "").strip()
                title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
                ident = (entry.findtext("guid") or link or title).strip()
                matches.append(item(f"google-intel:{ident}", title, link,
                                    "public-intel.google", parse_date(entry.findtext("pubDate")),
                                    "구글 공개 유출정보 검색", "both"))
            results += matches
            any_success = True
            log_source("공개 유출보도 '" + query + "'", True, len(matches),
                       f"전체 {len(raw)}건 중 기관·유출 키워드 동시 일치 {len(matches)}건")
        except Exception as exc:
            log_source("공개 유출보도 '" + query + "'", False, 0, str(exc)[:120])
    if any_success:
        SOURCE_READY.add("public-intel.google")
    return results


def fetch_hibp(seen_set):
    """도메인 소유권 인증을 받은 HIBP 계정 유출 결과를 하루 1회 집계.

    개별 이메일 주소, 비밀번호, 유출 데이터는 저장하거나 전송하지 않는다.
    """
    api_key = (os.getenv("HIBP_API_KEY") or "").strip()
    domain = (os.getenv("HIBP_DOMAIN") or "").strip().lower()
    if not api_key or not domain:
        SOURCE_LOG.append(("HIBP 인증 도메인", "건너뜀", 0, "API 키·인증된 도메인 미설정"))
        return []
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain):
        log_source("HIBP 인증 도메인", False, 0, "HIBP_DOMAIN 도메인 형식 오류")
        return []
    marker = f"hibp:poll:{domain}:{datetime.now(KST).date().isoformat()}"
    if marker in seen_set:
        SOURCE_LOG.append(("HIBP 인증 도메인", "건너뜀", 0, "당일 조회 완료(하루 1회)"))
        SOURCE_READY.add("hibp")
        return []
    try:
        url = "https://haveibeenpwned.com/api/v3/breachedDomain/" + urllib.parse.quote(domain)
        data = json.loads(http_get(url, {"hibp-api-key": api_key, "User-Agent": "KEPCO-Security-Watch/1.0"}))
        if not isinstance(data, dict):
            raise ValueError("예상과 다른 HIBP 응답")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            data = {}  # 유출 계정 없음 (정상 응답)
        else:
            log_source("HIBP 인증 도메인", False, 0,
                       f"HTTP {exc.code}: 키·도메인 인증/구독 및 제한 확인")
            return []
    except Exception as exc:
        log_source("HIBP 인증 도메인", False, 0, str(exc)[:120])
        return []
    # 계정명 자체를 이력에 남기지 않고 집계의 해시만 계산
    pairs = sorted({(alias.lower(), str(breach)) for alias, breaches in data.items()
                    if isinstance(breaches, list) for breach in breaches})
    digest = hashlib.sha256(json.dumps(pairs, ensure_ascii=False).encode()).hexdigest()[:20]
    EXTRA_MARKERS.add(marker)
    SOURCE_READY.add("hibp")
    log_source("HIBP 인증 도메인", True, len(data),
               f"{domain}: 유출 계정 {len(data)}개 (상세정보 미저장, 하루 1회)")
    if not pairs:
        return []
    title = f"{domain} 도메인 유출 계정 현황: 계정 {len(data)}개, 유출 기록 {len(pairs)}건"
    return [item("hibp:" + domain + ":" + digest, title,
                 "https://haveibeenpwned.com/DomainSearch", "hibp",
                 datetime.now(timezone.utc), "HIBP 인증 도메인", "both")]


def fetch_naver():
    cid = (os.getenv("NAVER_CLIENT_ID") or "").strip()
    secret = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        SOURCE_LOG.append(("네이버 API", "건너뜀", 0, "키 미등록"))
        return []
    items = []
    for kw in ("한국전력", "한전", "KEPCO", *TOPIC_KEYWORDS):
        name = f"네이버 '{kw}'"
        try:
            # NAVER API HUB (2026.7.31부터 신규 키는 여기서만 발급)
            url = ("https://naverapihub.apigw.ntruss.com/search/v1/news"
                   "?display=100&sort=date&format=json&query=" + urllib.parse.quote(kw))
            data = json.loads(http_get(url, {"X-NCP-APIGW-API-KEY-ID": cid,
                                             "X-NCP-APIGW-API-KEY": secret}))
            raw = data.get("items", [])
            kept = 0
            for it in raw:
                orig = it.get("originallink") or ""
                dom = outlet_of(orig)
                if dom:
                    kept += 1
                    items.append(item(orig, clean(it.get("title")), orig or it.get("link"),
                                      dom, parse_date(it.get("pubDate")), "네이버"))
            log_source(name, True, kept, f"전체 {len(raw)}건 중 대상 언론사 {kept}건")
        except Exception as e:
            log_source(name, False, 0, str(e)[:120])
    return items


def fetch_google():
    # 키워드별로 따로 검색한 뒤 대상 언론사만 남김
    # (언론사 site: 조건을 한 번에 묶으면 키워드가 무시된 결과가 섞여 나옴)
    items = []
    for kw in ("한국전력", "한전", "KEPCO", *TOPIC_KEYWORDS):
        name = f"구글 '{kw}'"
        try:
            q = urllib.parse.quote(f'"{kw}" when:1d')
            url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
            raw = list(ET.fromstring(http_get(url)).iter("item"))
            kept = 0
            for it in raw:
                src = it.find("source")
                dom = outlet_of(src.get("url") if src is not None else "")
                if not dom:
                    continue
                title = clean(it.findtext("title"))
                sname = clean(src.text) if src is not None else ""
                if sname and title.endswith(" - " + sname):
                    title = title[: -len(sname) - 3]
                kept += 1
                items.append(item(it.findtext("guid"), title, it.findtext("link"), dom,
                                  parse_date(it.findtext("pubDate")), "구글"))
            log_source(name, True, kept, f"전체 {len(raw)}건 중 대상 언론사 {kept}건")
        except Exception as e:
            log_source(name, False, 0, str(e)[:120])
    return items


def send_text(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()  # 붙여넣을 때 딸려온 줄바꿈·공백 제거
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "false"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(2):
        try:
            urllib.request.urlopen(url, body, timeout=15)
            return
        except urllib.error.HTTPError as e:  # 텔레그램이 알려주는 실제 사유까지 남김
            # 429(요청 제한)는 기다려도 소용없는 경우가 많아 바로 실패 처리
            raise Exception(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt == 0:
                progress("텔레그램 응답 없음 → 3초 뒤 1회 재시도")
                time.sleep(3)
                continue
            raise Exception(f"연결 실패: {e}")


def send_telegram(it):
    name, cat = OUTLETS[it["outlet"]]
    when = it["date"].astimezone(KST).strftime("%m/%d %H:%M") if it["date"] else ""
    kind = it.get("kind") or classify(it["title"])
    icon = {"both": "🚨 한전+", "company": "⚡", "topic": "📎 참고"}[kind]
    label = "출처 확인" if cat in ("유출정보", "유출계정", "공개위협보도") else "기사 보기"
    send_text(f"{icon} [{cat}] {html.escape(name)}\n<b>{html.escape(it['title'])}</b>\n{when}\n"
              f"<a href=\"{html.escape(it['link'], quote=True)}\">{label}</a>")


def tg_call(method, params=None):
    """텔레그램 API 호출 결과를 (성공여부, 내용)으로 반환 (진단용)"""
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(url, data, timeout=15) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return False, str(e)[:200]


def send_test_message():
    """3단계 진단: ① 토큰(getMe) ② 방 확인(getChat) ③ 메시지 전송"""
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    out = []
    ok, r = tg_call("getMe")
    out.append(f"① 토큰 확인: " + (f"성공 — 봇 @{r['result']['username']}" if ok else f"실패 — {r}"))
    ok, r = tg_call("getChat", {"chat_id": chat})
    out.append(f"② 알림방 확인: " + (f"성공 — '{r['result'].get('title', '')}' ({r['result']['type']})"
                                  if ok else f"실패 — {r}"))
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    try:
        send_text(f"✅ <b>테스트 메시지</b>\n한전 뉴스 알림 봇이 이 방에 정상 연결됐습니다.\n{now}")
        out.append("③ 메시지 전송: 성공 (텔레그램 방을 확인하세요)")
    except Exception as e:
        out.append(f"③ 메시지 전송: 실패 — {str(e)[:200]}")
    return out


def write_report(items, matched, new, sent, first_run, tg_errors, test_result=None):
    stats = {d: {"fetched": 0, "matched": 0, "new": 0, "sent": 0, "via": set()} for d in OUTLETS}
    for it in items:
        stats[it["outlet"]]["fetched"] += 1
        stats[it["outlet"]]["via"].add(it["via"])
    for it in matched:
        stats[it["outlet"]]["matched"] += 1
    for it in new:
        stats[it["outlet"]]["new"] += 1
        if it.get("sent"):
            stats[it["outlet"]]["sent"] += 1

    lines = []
    if test_result:
        lines += ["## 텔레그램 진단", ""] + [f"- {l}" for l in test_result] + [""]
    lines += ["## 1. 요청 결과 (출처별)", "", "| 출처 | 결과 | 수집·일치 건수 | 비고 |", "|---|---|---|---|"]
    lines += [f"| {n} | {r} | {c} | {note} |" for n, r, c, note in SOURCE_LOG]
    lines += ["", "## 2. 출처별 결과", "",
              "| 분류 | 언론사 | 수집 | 키워드 일치 | 신규 | 전송 | 경로 |",
              "|---|---|---|---|---|---|---|"]
    for d, s in sorted(stats.items(), key=lambda x: (OUTLETS[x[0]][1], OUTLETS[x[0]][0])):
        name, cat = OUTLETS[d]
        lines.append(f"| {cat} | {name} | {s['fetched']} | {s['matched']} | {s['new']} | "
                     f"{s['sent']} | {', '.join(sorted(s['via'])) or '-'} |")
    lines += ["", f"**합계** 수집 {len(items)} · 키워드 일치 {len(matched)} · 신규 {len(new)} · "
                  f"전송 {sent} · 첫 실행 {'예(기록만)' if first_run else '아니오'}"]
    if tg_errors:
        lines += ["", "**텔레그램 전송 오류**", ""] + [f"- {e}" for e in tg_errors[:5]]
    if new:
        lines += ["", "## 3. 신규 감지 항목", ""]
        lines += [f"- [{OUTLETS[it['outlet']][0]}] {it['title']} ({it['via']})" for it in new[:50]]

    report = "\n".join(lines)
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:  # Actions 실행 화면 Summary에 표로 표시
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")


def main():
    progress("시작")
    test_result = None
    if os.getenv("TEST_MESSAGE", "").lower() == "true":
        progress("텔레그램 진단 중")
        test_result = send_test_message()
        for line in test_result:
            progress(line)

    first_run = not os.path.exists(SEEN_FILE)
    if first_run:
        seen = []
    else:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
        if not isinstance(seen, list):
            raise ValueError("seen.json은 문자열 목록이어야 합니다")
    seen_set = set(seen)

    # 공개 RSS 및 검색 결과만 수집. 통신 실패한 소스는 실패로 표시하고 계속 진행.
    gathered = (fetch_direct() + fetch_ransomware() + fetch_public_leaks() +
                fetch_naver() + fetch_google() + fetch_public_intel() + fetch_hibp(seen_set))
    items = []
    ids = set()
    for it in gathered:
        if it["id"] not in ids:
            ids.add(it["id"])
            items.append(it)

    now = datetime.now(timezone.utc)
    matched, new = [], []
    baselined = 0
    for it in items:
        if not it["link"] or not (it["kind"] or is_target(it["title"])):
            continue
        age = INTEL_MAX_AGE if it["outlet"] in BOOTSTRAP_SOURCES else MAX_AGE
        if it["date"] and now - it["date"] > age:
            continue
        matched.append(it)
        keys = item_keys(it)
        if keys & seen_set:
            continue
        seen_set.update(keys)  # 이번 실행 내부 중복 제거
        it["keys"] = keys
        # 새로 추가한 공개 유출정보 피드: 첫 성공 조회는 과거자료 학습만
        if it["outlet"] in BOOTSTRAP_SOURCES and "bootstrap:" + it["outlet"] not in seen:
            seen.extend(sorted(keys))
            baselined += 1
            continue
        new.append(it)

    # 실패한 피드는 마커를 만들지 않아 다음 회차에 다시 초기화할 수 있게 함
    for src in BOOTSTRAP_SOURCES & SOURCE_READY:
        marker = "bootstrap:" + src
        if marker not in seen_set:
            seen.append(marker)
            seen_set.add(marker)

    sent, tg_errors = 0, []
    for it in sorted(new, key=lambda x: x["date"] or now):
        if not first_run:
            try:
                progress(f"전송: {it['title'][:40]}")
                send_telegram(it)
                it["sent"] = True
                sent += 1
                time.sleep(1)
            except Exception as e:
                tg_errors.append(str(e)[:200])
                if "429" in str(e) or "연결 실패" in str(e):
                    tg_errors.append("텔레그램 제한으로 나머지 전송을 다음 실행으로 넘김")
                    break
                continue  # 실패한 기사는 기록하지 않아 다음 실행에서 재시도
        seen.extend(sorted(it["keys"]))

    atomic_write_seen(seen)
    progress(f"완료 (신규 {len(new)} / 전송 {sent} / 신규 소스 초기기록 {baselined})")
    write_report(items, matched, new, sent, first_run, tg_errors, test_result)
    if baselined:
        progress(f"초기 등록된 공개 유출정보 {baselined}건은 이전 게시물이므로 알림 생략")


if __name__ == "__main__":
    main()
