"""TechnoYadro RSS candidate collector. No publishing or article archive."""
import concurrent.futures as cf
import datetime as dt
import email.utils
import json
import sys
import unittest
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

UTC = dt.timezone.utc
SOURCES = [
    ("Android Developers Blog", "https://android-developers.googleblog.com/", ["https://android-developers.googleblog.com/feeds/posts/default"]),
    ("Ars Technica", "https://arstechnica.com/", ["https://feeds.arstechnica.com/arstechnica/index"]),
    ("AppleInsider", "https://appleinsider.com/", ["https://appleinsider.com/rss/news/"]),
    ("MIT News - Computer Science", "https://news.mit.edu/topic/computer-science", ["https://news.mit.edu/rss/topic/computer-science"]),
    ("NVIDIA", "https://nvidianews.nvidia.com/", ["https://nvidianews.nvidia.com/rss"]),
    ("AMD", "https://www.amd.com/en/newsroom.html", ["https://www.amd.com/en/newsroom/rss.xml"]),
    ("Phoronix", "https://www.phoronix.com/", ["https://www.phoronix.com/rss.php"]),
    ("USB-IF", "https://www.usb.org/", ["https://www.usb.org/rss.xml"]),
    ("Microsoft", "https://blogs.microsoft.com/", ["https://blogs.microsoft.com/feed/"]),
    ("Samsung Mobile", "https://news.samsung.com/global/", ["https://news.samsung.com/global/feed"]),
    ("TechPowerUp", "https://www.techpowerup.com/", ["https://www.techpowerup.com/rss/news"]),
    ("CISA News", "https://www.cisa.gov/news-events/news", ["https://www.cisa.gov/news.xml"]),
    ("BleepingComputer", "https://www.bleepingcomputer.com/", ["https://www.bleepingcomputer.com/feed/"]),
]
MAX_BYTES = 3_000_000

def get(url):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Only HTTPS supported")
    req = urllib.request.Request(url, headers={"User-Agent": "TechnoYadroRSS/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8"})
    with urllib.request.urlopen(req, timeout=15) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Non-HTTPS redirect")
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Response too large")
        return data, response.geturl()

class Discover(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and "alternate" in (a.get("rel") or "").lower().split() and (a.get("type") or "").lower() in ("application/rss+xml", "application/atom+xml") and a.get("href"):
            url = urllib.parse.urljoin(self.base, a["href"])
            if urllib.parse.urlsplit(url).scheme == "https":
                self.links.append(url)

def local(tag):
    return tag.rsplit("}", 1)[-1].lower()

def child(node, *names):
    return next((c for c in node if local(c.tag) in names), None)

def value(node, *names):
    elem = child(node, *names)
    return "".join(elem.itertext()).strip() if elem is not None else ""

def parse_date(text):
    try:
        d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    return d.astimezone(UTC) if d and d.tzinfo else None

def normalize(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        return None
    pairs = [(k,v) for k,v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid","gclid")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", urllib.parse.urlencode(pairs), ""))

def parse(data, source, base, now):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD/entity not supported")
    root = ET.fromstring(data)
    if local(root.tag) not in ("rss","rdf","feed"):
        raise ValueError("Not RSS/Atom")
    entries = [e for e in root.iter() if local(e.tag) in ("item","entry")]
    out, unknown = [], 0
    for e in entries:
        # Atom updated is not proof of publication; deliberately do not use it.
        published = parse_date(value(e, "pubdate","published","date"))
        if published is None:
            unknown += 1
            continue
        if not now-dt.timedelta(hours=24) <= published <= now:
            continue
        link = ""
        for c in e:
            if local(c.tag) == "link" and c.get("rel","alternate") == "alternate":
                link = c.get("href") or (c.text or "")
                if link:
                    break
        title = " ".join(value(e, "title").split())[:400]
        url = normalize(urllib.parse.urljoin(base, link.strip())) if link.strip() else None
        if title and url:
            out.append({"source": source, "title":title,"url":url,"published_utc":published.isoformat()})
    return out, unknown

def collect_one(spec, now):
    name, home, candidates = spec
    errors = []
    tried = set()
    def attempt(url):
        if url in tried:
            return None
        tried.add(url)
        try:
            raw, actual = get(url)
            rows, unknown = parse(raw,name,actual,now)
            return {"source":name,"status":"ok","feed":actual,"fresh":len(rows),"undated":unknown,"items":rows}
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:140])
            return None
    for url in candidates:
        result = attempt(url)
        if result is not None:
            return result
    try:
        raw, actual = get(home)
        parser = Discover(actual)
        parser.feed(raw.decode("utf-8",errors="replace"))
        for url in parser.links[:3]:
            result = attempt(url)
            if result is not None:
                return result
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc)[:140])
    return {"source":name,"status":"failed","errors":errors,"items":[]}

def dedup(rows):
    seen = set()
    out = []
    for r in sorted(rows,key=lambda x:x["published_utc"],reverse=True):
        key = r["url"]
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

def main():
    now = dt.datetime.now(UTC)
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda spec: collect_one(spec,now), SOURCES))
    rows = dedup([r for s in reports for r in s.pop("items")])
    ok = sum(s["status"]=="ok" for s in reports)
    result = {"collected_utc":now.isoformat(),"window_hours":24,"verified_articles":False,"successful_sources":ok,"total_sources":len(SOURCES),"sources":reports,"candidates":rows}
    Path("rss-output").mkdir(exist_ok=True)
    Path("rss-output/news.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("RSS source coverage:",ok,"/",len(SOURCES),"Unique candidate URLs:",len(rows))
    for s in reports:
        print(s["source"],s["status"],s.get("fresh","-"))
    if ok < 10:
        raise SystemExit(f"Insufficient RSS coverage: {ok}/10 required")

class Tests(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime(2026,11,26,12,tzinfo=UTC)
    def test_rss_window(self):
        data=b'<rss><channel><item><title>A</title><link>https://a.example/a?utm_source=x</link><pubDate>Thu, 26 Nov 2026 10:00:00 GMT</pubDate></item><item><title>Old</title><link>https://a.example/old</link><pubDate>Tue, 24 Nov 2026 10:00:00 GMT</pubDate></item><item><title>Undated</title></item></channel></rss>'
        rows,unknown=parse(data,"test","https://a.example/",self.now)
        self.assertEqual(len(rows),1)
        self.assertEqual(unknown,1)
        self.assertEqual(rows[0]["url"],"https://a.example/a")
    def test_atom(self):
        data=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="/new"/><published>2026-11-26T11:00:00Z</published></entry><entry><title>B</title><link href="/updated"/><updated>2026-11-26T11:00:00Z</updated></entry></feed>'
        rows,unknown=parse(data,"test","https://a.example/",self.now)
        self.assertEqual(len(rows),1)
        self.assertEqual(unknown,1)
        self.assertEqual(rows[0]["url"],"https://a.example/new")
    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse(b"<html/>","test","https://a.example/",self.now)
    def test_duplicate(self):
        r={"url":"https://a.example/a","published_utc":"2026-11-26T11:00:00+00:00"}
        self.assertEqual(len(dedup([r,r])),1)
    def test_discovery(self):
        p=Discover("https://a.example/")
        p.feed('<link rel="alternate" type="application/rss+xml" href="/feed">')
        self.assertEqual(p.links,["https://a.example/feed"])
    def test_dates(self):
        self.assertIsNone(parse_date("2026-11-26"))
        self.assertIsNone(parse_date("nonsense"))

if __name__ == "__main__":
    if "--test" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        main()
