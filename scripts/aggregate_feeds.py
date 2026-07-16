#!/usr/bin/env python3
"""外部体操メディアのRSS/Atomを収集して external_feed.json を生成する。
Gymnastics AI アプリの「メディア > 外部」タブが読む。
sources.json のフィードを取得 → 最新順 → 上限300件。
"""
import json, time, hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.request
import xml.etree.ElementTree as ET

UA = "GymnasticsAI-FeedBot/1.0 (+https://daitoiwasaki.com/dscore)"
MAX_ITEMS = 300
PER_SOURCE_CAP = 25  # 1サイトが埋め尽くさないように
TIMEOUT = 20

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()

def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None

def text(el):
    return (el.text or "").strip() if el is not None else ""

def first_img_from_html(html):
    import re
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html or "")
    return m.group(1) if m else ""

OG_TIMEOUT = 6  # 記事ページ取得のタイムアウト（秒）

def og_image(url):
    """記事ページの og:image（無ければ twitter:image）を返す。
    多くのRSSは画像を含まないため、フィードで画像が取れない記事の補完に使う。
    HTMLの<head>だけ読めば十分なので先頭200KBに制限。失敗時は空文字。"""
    import re
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=OG_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return ""  # 画像/PDF等のリンクはスキップ
            html = r.read(200000).decode("utf-8", "ignore")
    except Exception:
        return ""
    for pat in (
        r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            src = m.group(1).strip()
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
    return ""

def parse_rss(root, source):
    out = []
    for item in root.iter("item"):
        title = text(item.find("title"))
        link = text(item.find("link"))
        date = parse_date(text(item.find("pubDate")) or text(item.find("dc:date", NS)))
        img = ""
        for tag in ("media:content", "media:thumbnail"):
            el = item.find(tag, NS)
            if el is not None and el.get("url"):
                img = el.get("url"); break
        if not img:
            enc = item.find("enclosure")
            if enc is not None and (enc.get("type") or "").startswith("image"):
                img = enc.get("url") or ""
        if not img:
            img = first_img_from_html(text(item.find("content:encoded", NS)) or text(item.find("description")))
        if title and link:
            out.append((title, link, date, img))
    return out

def parse_atom(root, source):
    out = []
    for e in root.findall("atom:entry", NS):
        title = text(e.find("atom:title", NS))
        link = ""
        for l in e.findall("atom:link", NS):
            if l.get("rel") in (None, "alternate"):
                link = l.get("href") or ""; break
        date = parse_date(text(e.find("atom:published", NS)) or text(e.find("atom:updated", NS)))
        img = first_img_from_html(text(e.find("atom:content", NS)) or text(e.find("atom:summary", NS)))
        if title and link:
            out.append((title, link, date, img))
    return out

def title_passes(title, keywords):
    """総合フィードから体操記事だけ通す。keywords 未指定なら全通過。
    タイトルにいずれかのキーワードを含めば採用（種目名は体操以外で出ないため安全。
    「体操」はラジオ体操等の稀なノイズを許容）。"""
    if not keywords:
        return True
    return any(k in title for k in keywords)


def main():
    sources = json.load(open("sources.json"))
    items, errors = [], []
    for src in sources:
        feed = src.get("feed")
        if not feed:
            continue
        try:
            raw = fetch(feed)
            root = ET.fromstring(raw)
            tag = root.tag.lower()
            rows = parse_atom(root, src) if tag.endswith("feed") else parse_rss(root, src)
            kw = src.get("filter")
            rows = [r for r in rows if title_passes(r[0], kw)][:PER_SOURCE_CAP]
            for title, link, date, img in rows:
                items.append({
                    "title": title[:300],
                    "url": link,
                    "source": src["name"],
                    "source_id": src["id"],
                    "lang": src.get("lang", "en"),
                    "image": img,
                    "published": date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if date else "",
                })
        except Exception as e:
            errors.append(f"{src['id']}: {type(e).__name__} {e}")
    # 重複除去（URL基準）→ 最新順 → 上限
    seen, dedup = set(), []
    for it in items:
        k = hashlib.md5(it["url"].encode()).hexdigest()
        if k in seen:
            continue
        seen.add(k); dedup.append(it)
    dedup.sort(key=lambda x: x["published"] or "0000", reverse=True)
    dedup = dedup[:MAX_ITEMS]
    # 画像が無い記事は記事ページの og:image で補完（RSSに画像を含まないソース対策）。
    og_filled = 0
    for it in dedup:
        if it["image"]:
            continue
        img = og_image(it["url"])
        if img:
            it["image"] = img
            og_filled += 1
    print(f"og_image filled={og_filled}")
    out = {
        "version": int(time.time()),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sites": [
            {"id": s["id"], "name": s["name"], "url": s["url"],
             "lang": s.get("lang", "en"), "category": s.get("category", ""),
             "has_feed": bool(s.get("feed"))}
            for s in sources
        ],
        "items": dedup,
    }
    json.dump(out, open("external_feed.json", "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"items={len(dedup)} sources={len(sources)} errors={len(errors)}")
    for e in errors:
        print("WARN", e)

if __name__ == "__main__":
    main()
