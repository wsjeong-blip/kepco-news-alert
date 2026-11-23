"""한국전력/한전 뉴스 알림 → 텔레그램
- 대상: 지정 언론사(방송·통신·주요 일간지) + 보안 전문지
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
}

# 언론사 자체 RSS (전체기사 피드를 받아 키워드로 거름 → 네이버/구글보다 빠름)
DIRECT_FEEDS = {
    "boannews.com": "http://www.boannews.com/media/news_rss.xml",
    "dailysecu.com": "https://www.dailysecu.com/rss/allArticle.xml",
}

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
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def item(id_, title, link, dom, date):
    return {"id": id_ or link, "title": title, "link": link, "outlet": dom, "date": date}


def fetch_direct():
    items = []
    for dom, url in DIRECT_FEEDS.items():
        try:
            for it in ET.fromstring(http_get(url)).iter("item"):
                link = (it.findtext("link") or "").strip()
                items.append(item(link, clean(it.findtext("title")), link, dom,
                                  parse_date(it.findtext("pubDate"))))
        except Exception as e:
            print(f"[warn] feed {dom}: {e}", file=sys.stderr)
    return items


def fetch_naver():
    cid, secret = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        return []
    items = []
    for kw in ("한국전력", "한전", "KEPCO", *TOPIC_KEYWORDS):
        # NAVER API HUB (2026.7.31부터 신규 키는 여기서만 발급)
        url = ("https://naverapihub.apigw.ntruss.com/search/v1/news"
               "?display=100&sort=date&format=json&query=" + urllib.parse.quote(kw))
        data = json.loads(http_get(url, {"X-NCP-APIGW-API-KEY-ID": cid,
                                         "X-NCP-APIGW-API-KEY": secret}))
        for it in data.get("items", []):
            orig = it.get("originallink") or ""
            dom = outlet_of(orig)
            if dom:
                items.append(item(orig, clean(it.get("title")), orig or it.get("link"),
                                  dom, parse_date(it.get("pubDate"))))
    return items


def fetch_google():
    sites = " OR ".join(f"site:{d}" for d in OUTLETS)
    kws = " OR ".join(["한국전력", "한전", "KEPCO", *TOPIC_KEYWORDS])
    q = urllib.parse.quote(f"({kws}) ({sites}) when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    items = []
    for it in ET.fromstring(http_get(url)).iter("item"):
        src = it.find("source")
        dom = outlet_of(src.get("url") if src is not None else "")
        if not dom:
            continue
        title = clean(it.findtext("title"))
        name = clean(src.text) if src is not None else ""
        if name and title.endswith(" - " + name):
            title = title[: -len(name) - 3]
        items.append(item(it.findtext("guid"), title, it.findtext("link"), dom,
                          parse_date(it.findtext("pubDate"))))
    return items


def send_telegram(it):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    name, cat = OUTLETS[it["outlet"]]
    when = it["date"].astimezone(KST).strftime("%m/%d %H:%M") if it["date"] else ""
    icon = {"both": "🚨 한전+", "company": "⚡", "topic": "📎 참고"}[classify(it["title"])]
    text = (f"{icon} [{cat}] {html.escape(name)}\n<b>{html.escape(it['title'])}</b>\n{when}\n"
            f"<a href=\"{html.escape(it['link'])}\">기사 보기</a>")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "false"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = [] if first_run else json.load(open(SEEN_FILE, encoding="utf-8"))
    seen_set = set(seen)

    items = []
    for fetch in (fetch_direct, fetch_naver, fetch_google):  # 앞쪽 출처 우선
        try:
            items += fetch()
        except Exception as e:
            print(f"[warn] {fetch.__name__}: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    new = []
    for it in items:
        if not it["link"] or not is_target(it["title"]):
            continue
        if it["date"] and now - it["date"] > MAX_AGE:
            continue
        keys = {it["id"], "t:" + norm_title(it["title"])}
        if keys & seen_set:
            continue
        seen_set |= keys
        seen += list(keys)
        new.append(it)

    sent = 0
    if not first_run:  # 첫 실행은 기록만 (알림 폭탄 방지)
        for it in sorted(new, key=lambda x: x["date"] or now):
            try:
                send_telegram(it)
                sent += 1
            except Exception as e:
                print(f"[warn] telegram: {e}", file=sys.stderr)

    json.dump(seen[-MAX_SEEN:], open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"fetched={len(items)} new={len(new)} sent={sent} first_run={first_run}")


if __name__ == "__main__":
    main()
