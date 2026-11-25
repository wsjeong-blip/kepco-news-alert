"""한국전력/한전 뉴스 알림 → 텔레그램
- 제목에 A(한전 관련) + B(개인정보·유출·보안 관련) 단어가 함께 있을 때만 알림
- 출처: 보안뉴스·데일리시큐 RSS, 네이버 뉴스, 구글 뉴스(모든 언론사), 랜섬웨어 유출 사이트
- 한전 계열사 기사 제외

환경변수
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (필수)
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  (선택: NAVER API HUB 키. 없으면 네이버는 건너뜀)
"""
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
MAX_SEEN = 3000
MAX_AGE = timedelta(hours=6)

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
    "ransomware.live": ("랜섬웨어 유출", "다크웹"),
    "darkwebinformer.com": ("Dark Web Informer", "다크웹"),
    # 위 목록 밖 언론사 (네이버·구글에서 A+B 조합으로 잡힌 기사)
    "_other": ("기타 언론", "기타"),
}

# 비슷한 제목(같은 사건)은 한 번만 알림: 글자쌍 유사도 기준 (0~1, 높을수록 엄격)
SIMILAR_TITLE = 0.5

# 한 번 실행에 개별 알림 최대 건수 (넘는 건 한 메시지로 묶어서 보냄)
MAX_PER_RUN = 5

# 언론사 자체 RSS (전체기사 피드를 받아 키워드로 거름 → 네이버/구글보다 빠름)
DIRECT_FEEDS = {
    "boannews.com": "https://www.boannews.com/rss/allArticle.xml",
    "dailysecu.com": "https://www.dailysecu.com/rss/allArticle.xml",
}

# 랜섬웨어 유출 사이트 감시 (ransomware.live 공개 RSS, 다크웹 직접 접속 없음)
RANSOMWARE_FEED = "https://www.ransomware.live/rss.xml"
RANSOMWARE_WATCH = ["KEPCO", "Korea Electric Power", "kepco.co.kr", "한국전력", "한전"]

# 해킹 포럼 유출·판매 게시글 정리 블로그 (Dark Web Informer 공개 RSS, 다크웹 직접 접속 없음)
# 제목·요약에 RANSOMWARE_WATCH 단어가 있으면 🚨 (게시글 자체가 유출 관련이라 B 단어 불필요)
FORUM_FEED = "https://darkwebinformer.com/rss/"
RANSOMWARE_COUNTRY = ""     # "KR"로 바꾸면 한전이 아니어도 국내 피해 기관을 참고 알림

# ── 키워드 / 계열사 제외 ──
AFFILIATES = [
    "한국전력기술", "한국전력거래소", "한국전력정보",
    "한전KPS", "한전케이피에스", "한전KDN", "한전케이디엔",
    "한전기술", "한전원자력연료", "한전MCS", "한전엠씨에스",
    "한전산업개발", "한전ENS", "한전FMS", "한전CSC", "한전전력연구원",
]
# ── 알림 조건: 제목에 A와 B가 모두 있어야 함 ──
# A: 한전 관련 (영문은 대소문자 구분 없음 → KEPCO, kepco, Kepco 모두 일치)
A_KEYWORDS = ["한국전력공사", "한국전력", "한전", "KEPCO", "켑코", "Korea Electric Power",
              "kepco.co.kr", "한전ON", "한전온", "SRM", "조달"]
# B-강: 보안 사고 관련 → 🚨
B_STRONG = ["개인정보", "개인 정보", "고객정보", "고객 정보", "주민번호", "주민등록번호", "계좌번호",
            "다크웹", "darkweb", "dark web", "유출", "해킹", "해커", "침해", "랜섬웨어", "랜섬",
            "탈취", "털렸", "털린", "사이버", "악성코드", "피싱", "스미싱", "도용", "노출",
            "디도스", "DDoS", "크리덴셜", "보안사고", "보안 사고",
            "breach", "leak", "hack", "ransomware", "phishing", "cyber"]
# B-약: 범위가 넓은 단어 → ⚡ (전기요금 '피해', 요금 '정보' 같은 기사도 걸릴 수 있음)
B_BROAD = ["정보", "피해", "보안", "공격"]

# 검색어 (네이버·구글): A 단어로 검색 후 제목에서 A+B 조합만 남김
SEARCH_A = ["한국전력", "한전", "KEPCO", "켑코", "SRM", "조달"]


def _re(words):
    return re.compile("|".join(sorted(map(re.escape, words), key=len, reverse=True)),
                      re.IGNORECASE)


A_RE, B_STRONG_RE, B_BROAD_RE = _re(A_KEYWORDS), _re(B_STRONG), _re(B_BROAD)
AFFIL_RE = re.compile(
    "|".join(sorted(map(re.escape, AFFILIATES), key=len, reverse=True))
    + r"|KEPCO[\s\-]?(?:KPS|KDN|E&C|NF|ENS|MCS)",
    re.IGNORECASE)


def classify(title):
    """'strong'(A+B강) | 'broad'(A+B약) | None"""
    if not A_RE.search(AFFIL_RE.sub(" ", title)):  # 계열사 이름은 지우고 A 확인
        return None
    if B_STRONG_RE.search(title):
        return "strong"
    if B_BROAD_RE.search(title):
        return "broad"
    return None


def is_target(title):
    return classify(title) is not None


def outlet_of(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for dom in OUTLETS:
        if host == dom or host.endswith("." + dom):
            return dom
    return None


# 감시 목록 밖 언론사 이름 표시용 (네이버는 언론사 이름을 주지 않아 도메인으로 추정)
OTHER_NAMES = {
    "mt.co.kr": "머니투데이", "edaily.co.kr": "이데일리", "asiae.co.kr": "아시아경제",
    "etnews.com": "전자신문", "zdnet.co.kr": "지디넷코리아", "fnnews.com": "파이낸셜뉴스",
    "heraldcorp.com": "헤럴드경제", "sedaily.com": "서울경제", "seoul.co.kr": "서울신문",
    "kmib.co.kr": "국민일보", "segye.com": "세계일보", "munhwa.com": "문화일보",
    "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스", "newspim.com": "뉴스핌",
    "dt.co.kr": "디지털타임스", "inews24.com": "아이뉴스24", "bloter.net": "블로터",
    "electimes.com": "전기신문", "energy-news.co.kr": "에너지신문", "ekn.kr": "에너지경제",
    "ddaily.co.kr": "디지털데일리", "byline.network": "바이라인네트워크",
}


def host_name(url):
    h = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for dom, nm in OTHER_NAMES.items():
        if h == dom or h.endswith("." + dom):
            return nm
    for pre in ("www.", "news.", "m.", "n."):
        if h.startswith(pre):
            h = h[len(pre):]
    return h or "기타 언론"


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def norm_title(title):
    return re.sub(r"[\W_]+", "", title)


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


def item(id_, title, link, dom, date, via, kind=None, src_name=None):
    return {"id": id_ or link, "title": title, "link": link, "outlet": dom,
            "date": date, "via": via, "kind": kind, "src_name": src_name}  # kind: 미리 정한 알림 종류 (다크웹용)


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
            if watch_re.search(victim) or watch_re.search(desc):
                kind = "strong"          # 한전 관련 피해 → 🚨
            elif RANSOMWARE_COUNTRY and country == RANSOMWARE_COUNTRY:
                kind = "broad"           # 국내 피해 기관 → ⚡ (RANSOMWARE_COUNTRY 설정 시)
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


def fetch_forum():
    """해킹 포럼 유출·판매 게시글 (Dark Web Informer)에서 한전 관련 감지"""
    watch_re = re.compile("|".join(map(re.escape, RANSOMWARE_WATCH)), re.IGNORECASE)
    items = []
    try:
        raw = list(ET.fromstring(http_get(FORUM_FEED)).iter("item"))
        for it in raw:
            title = clean(it.findtext("title"))
            desc = clean(it.findtext("description"))
            text = AFFIL_RE.sub(" ", f"{title} {desc}")  # 계열사 이름은 제외
            if not watch_re.search(text):
                continue
            link = (it.findtext("link") or "").strip()
            items.append(item(it.findtext("guid") or link, f"[포럼 유출] {title}", link,
                              "darkwebinformer.com", parse_date(it.findtext("pubDate")),
                              "다크웹", "strong"))
        log_source("포럼 유출 (Dark Web Informer)", True, len(items),
                   f"전체 {len(raw)}건 중 한전 관련 {len(items)}건")
    except Exception as e:
        log_source("포럼 유출 (Dark Web Informer)", False, 0, str(e)[:120])
    return items


def fetch_naver():
    """네이버 뉴스 (NAVER API HUB). 모든 언론사 대상, A+B 조합은 main에서 판별"""
    cid = (os.getenv("NAVER_CLIENT_ID") or "").strip()
    secret = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        SOURCE_LOG.append(("네이버 API", "건너뜀", 0, "키 미등록"))
        return []
    items = []
    for kw in SEARCH_A:
        name = f"네이버 '{kw}'"
        try:
            url = ("https://naverapihub.apigw.ntruss.com/search/v1/news"
                   "?display=100&sort=date&format=json&query=" + urllib.parse.quote(kw))
            data = json.loads(http_get(url, {"X-NCP-APIGW-API-KEY-ID": cid,
                                             "X-NCP-APIGW-API-KEY": secret}))
            raw = data.get("items", [])
            combo = 0
            for it in raw:
                orig = it.get("originallink") or it.get("link") or ""
                title = clean(it.get("title"))
                dom = outlet_of(orig) or "_other"
                sname = OUTLETS[dom][0] if dom != "_other" else host_name(orig)
                combo += is_target(title)
                items.append(item(orig, title, orig, dom, parse_date(it.get("pubDate")),
                                  "네이버", None, sname))
            log_source(name, True, len(raw), f"전체 {len(raw)}건 중 A+B 조합 {combo}건")
        except Exception as e:
            log_source(name, False, 0, str(e)[:120])
    return items


def fetch_google():
    """구글 뉴스. A 단어 + B 단어(OR)로 검색, 모든 언론사 대상"""
    b_or = " OR ".join(["개인정보", "유출", "해킹", "다크웹", "침해", "랜섬웨어", "사이버",
                        "정보", "피해", "보안"])
    items = []
    for kw in SEARCH_A:
        name = f"구글 '{kw}'"
        try:
            q = urllib.parse.quote(f'"{kw}" ({b_or}) when:1d')
            url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
            raw = list(ET.fromstring(http_get(url)).iter("item"))
            combo = 0
            for it in raw:
                src = it.find("source")
                dom = outlet_of(src.get("url") if src is not None else "") or "_other"
                title = clean(it.findtext("title"))
                sname = clean(src.text) if src is not None else ""
                if sname and title.endswith(" - " + sname):
                    title = title[: -len(sname) - 3]
                combo += is_target(title)
                items.append(item(it.findtext("guid"), title, it.findtext("link"), dom,
                                  parse_date(it.findtext("pubDate")), "구글", None,
                                  sname or OUTLETS[dom][0]))
            log_source(name, True, len(raw), f"전체 {len(raw)}건 중 A+B 조합 {combo}건")
        except Exception as e:
            log_source(name, False, 0, str(e)[:120])
    return items


def bigrams(t):
    t = norm_title(t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def is_similar(title, seen_titles):
    """이미 보낸 제목 중 비슷한 것이 있으면 True (같은 사건 반복 방지)"""
    b = bigrams(title)
    if not b:
        return False
    for o in seen_titles:
        if o and len(b & o) / len(b | o) >= SIMILAR_TITLE:
            return True
    return False


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
    name = it.get("src_name") or name
    when = it["date"].astimezone(KST).strftime("%m/%d %H:%M") if it["date"] else ""
    kind = it.get("kind") or classify(it["title"])
    icon = {"strong": "🚨 한전+보안", "broad": "⚡ 한전+참고"}[kind]
    send_text(f"{icon} [{cat}] {html.escape(name)}\n<b>{html.escape(it['title'])}</b>\n{when}\n"
              f"<a href=\"{html.escape(it['link'])}\">기사 보기</a>")


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

    lines = [f"**요약** 신규 {len(new)}건 · 전송 {sent}건 · "
             f"{datetime.now(KST).strftime('%m/%d %H:%M')} 실행", ""]
    if test_result:
        lines += ["## 텔레그램 진단", ""] + [f"- {l}" for l in test_result] + [""]
    lines += ["## 1. 요청 결과 (출처별)", "", "| 출처 | 결과 | 받은 기사 | 비고 |", "|---|---|---|---|"]
    lines += [f"| {n} | {r} | {c} | {note} |" for n, r, c, note in SOURCE_LOG]
    lines += ["", "## 2. 언론사별 결과", "",
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
    lines += ["", "## 3. 신규 기사", ""]
    if not new:
        lines.append("이번 실행에서 새 기사 없음")
    if new:
        lines += [f"- [{it.get('src_name') or OUTLETS[it['outlet']][0]}] {it['title']} ({it['via']})"
                  for it in new[:50]]

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
    seen = [] if first_run else json.load(open(SEEN_FILE, encoding="utf-8"))
    seen_set = set(seen)

    items, ids = [], set()
    for it in fetch_direct() + fetch_ransomware() + fetch_forum() + fetch_naver() + fetch_google():  # 앞쪽 출처 우선
        if it["id"] not in ids:  # 여러 키워드 검색에 같은 기사가 겹치면 하나로
            ids.add(it["id"])
            items.append(it)

    now = datetime.now(timezone.utc)
    seen_titles = [bigrams(k[2:]) for k in seen if k.startswith("t:")]
    matched, new, similar = [], [], 0
    for it in items:
        if not it["link"] or not (it["kind"] or is_target(it["title"])):
            continue
        if it["date"] and now - it["date"] > MAX_AGE:
            continue
        matched.append(it)
        keys = {it["id"], "t:" + norm_title(it["title"])}
        if keys & seen_set:
            continue
        if is_similar(it["title"], seen_titles):  # 같은 사건의 비슷한 제목
            similar += 1
            continue
        seen_set |= keys  # 이번 실행 안에서 중복 방지
        seen_titles.append(bigrams(it["title"]))
        it["keys"] = keys
        new.append(it)

    sent, tg_errors = 0, []
    ordered = sorted(new, key=lambda x: x["date"] or now)
    singles, extra = ordered[:MAX_PER_RUN], ordered[MAX_PER_RUN:]
    blocked = False
    for it in singles:
        if not first_run:  # 첫 실행은 기록만 (알림 폭탄 방지)
            try:
                progress(f"전송: {it['title'][:40]}")
                send_telegram(it)
                it["sent"] = True
                sent += 1
                time.sleep(1)  # 연속 전송 속도 제한
            except Exception as e:
                tg_errors.append(str(e)[:200])
                if "429" in str(e) or "연결 실패" in str(e):
                    # 텔레그램이 막고 있으면 남은 기사는 이번엔 보내지 않고 다음 실행으로 넘김
                    tg_errors.append("텔레그램 제한으로 이번 실행의 나머지 전송을 중단함")
                    blocked = True
                    break
                continue  # 실패한 기사는 기록하지 않음 → 다음 실행 때 다시 시도
        seen += list(it["keys"])
    if extra and not blocked:
        if not first_run:  # 많을 때는 나머지를 한 메시지로 묶음
            lines = [f"📚 <b>관련 기사 {len(extra)}건 더</b>"]
            for it in extra[:20]:
                nm = it.get("src_name") or OUTLETS[it["outlet"]][0]
                lines.append(f"• <a href=\"{html.escape(it['link'])}\">"
                             f"{html.escape(it['title'][:60])}</a> ({html.escape(nm)})")
            if len(extra) > 20:
                lines.append(f"…외 {len(extra) - 20}건 (GitHub Summary 참고)")
            try:
                send_text("\n".join(lines))
                for it in extra:
                    it["sent"] = True
                sent += len(extra)
            except Exception as e:
                tg_errors.append(str(e)[:200])
                extra = []  # 실패 시 기록하지 않음
        for it in extra:
            seen += list(it["keys"])

    json.dump(seen[-MAX_SEEN:], open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    progress(f"완료 (신규 {len(new)} / 전송 {sent} / 비슷한 제목 제외 {similar})")
    write_report(items, matched, new, sent, first_run, tg_errors, test_result)


if __name__ == "__main__":
    main()
