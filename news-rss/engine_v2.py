"""News Engine 2.0 pilot: RSS evidence, conservative ranking, optional article extraction."""
import concurrent.futures as cf
import datetime as dt
import difflib
import html
import json
import re
import sys
import unittest
import urllib.parse as up
import urllib.request as ur
import urllib.robotparser as rp
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
import collector as base

FEED_OVERRIDES = {
    "Android Developers Blog": ("https://developer.android.com/blog/", ["https://developer.android.com/static/blog/atom.xml"]),
    "AMD": ("https://ir.amd.com/news-events/press-releases", ["https://ir.amd.com/news-events/press-releases/rss", "https://www.amd.com/en/newsroom/rss.xml"]),
    "NVIDIA": ("https://nvidianews.nvidia.com/", ["https://nvidianews.nvidia.com/rss.xml"]),
}
SOURCES = [
    (name, *FEED_OVERRIDES.get(name, (home, feeds)))
    for name, home, feeds in base.SOURCES
] + [
    ("Tom's Hardware", "https://www.tomshardware.com/", ["https://www.tomshardware.com/feeds/all"]),
    ("OpenAI", "https://openai.com/news/", ["https://openai.com/news/rss.xml"]),
    ("Google AI", "https://blog.google/innovation-and-ai/technology/ai/", ["https://blog.google/innovation-and-ai/technology/ai/rss/"]),
    ("NVIDIA Blog", "https://blogs.nvidia.com/", ["https://blogs.nvidia.com/feed/"]),
    ("Raspberry Pi", "https://www.raspberrypi.com/news/", ["https://www.raspberrypi.com/news/feed/"]),
    ("GitHub Changelog", "https://github.blog/changelog/", ["https://github.blog/changelog/feed/"]),
]
NEWSROOMS = {
    "Anthropic": "https://www.anthropic.com/news",
    "Apple Newsroom": "https://www.apple.com/newsroom/archive/",
    "Intel Newsroom": "https://www.intel.com/content/www/us/en/newsroom/home.html",
}
OFFICIAL = {
    "AMD", "NVIDIA", "Microsoft", "Samsung Mobile", "CISA News", "USB-IF",
    "Android Developers Blog", "OpenAI", "Google AI", "NVIDIA Blog",
    "Raspberry Pi", "GitHub Changelog", *NEWSROOMS,
}
TOPICS = {
    "hardware": r"\b(cpu|gpu|ryzen|radeon|geforce|intel|nvidia|amd|ssd|nvme|ddr[456]|motherboard|processor|graphics|memory|chipset|pcie|ram|raspberry pi|iphone|ipad|macbook)\b",
    "security": r"\b(cve-\d{4}-\d+|vulnerability|vulnerabilities|ransomware|malware|security|exploit|patch)\b",
    "software": r"\b(windows|linux|kernel|driver|firmware|uefi|bios|mesa|android|macos|ios|github|update)\b",
    "engineering": r"\b(usb|pcie|pci express|semiconductor|transistor|power delivery|cooling|architecture|chip|silicon)\b",
    "ai": r"\b(ai|llm|inference|machine learning|accelerator|cuda|rocm|chatgpt|gpt[- ]?\d+|codex|claude|gemini|copilot)\b",
}
RUMOR = re.compile(r"\b(rumou?rs?|leaks?|leaked|reportedly|unconfirmed|alleged|speculation|purported|report claims)\b",re.I)
AD = re.compile(r"\b(sponsored|advertorial|affiliate|deal[s]?|discount|coupon|black friday|cyber monday|giveaway|price index|lowest prices?|all-time low|for just\s+\$|gift guide|best deals)\b|\bsave\s+\d+\s*%|\b\d+\s*%\s+off\b|\b(?:for just|at)\s+\$\d",re.I)
STOP = set("a an the and for of to on in with is are as at by from new its has have it this that be will can says report".split())
USER_AGENT = "TechnoYadroRSS/2.0"
MAX_EXCERPT = 600
MAX_ARTICLES = 8
# Reject oversized bodies instead of silently truncating editorial evidence.
MAX_EVIDENCE_CHARS = 50000
MAX_BYTES = 1_500_000

class Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style","noscript","template"): self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ("script","style","noscript","template"): self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)
    def result(self): return " ".join(" ".join(self.parts).split())

def excerpt(s):
    p=Plain()
    p.feed(s[:15000])
    return p.result()[:MAX_EXCERPT]

def rss_body(entry):
    """Read an explicit content field, never upgrade a summary to full text."""
    for c in entry:
        is_rss=c.tag=="{http://purl.org/rss/1.0/modules/content/}encoded"
        is_atom=c.tag=="{http://www.w3.org/2005/Atom}content"
        if not (is_rss or is_atom) or c.get("src"): continue
        typ=c.get("type","text" if is_atom else "html")
        if typ not in ("text","html","xhtml"): continue
        if typ=="xhtml":
            import xml.etree.ElementTree as ET
            raw="".join(ET.tostring(x,encoding="unicode") for x in c)
        else:
            if len(c): continue
            raw=c.text or ""
        if len(raw)>MAX_BYTES: return None
        if typ=="text":
            body=" ".join(raw.split())
        else:
            parser=Plain()
            parser.feed(raw)
            body=parser.result()
        if 250<=len(body)<=MAX_EVIDENCE_CHARS:
            return body
    return None

def enriched_parse(data,source,base_url,now,approved=None,parse=None):
    rows,unknown=(parse or base.parse)(data,source,base_url,now)
    approved=approved or set()
    import xml.etree.ElementTree as ET
    notes={}
    for e in ET.fromstring(data).iter():
        if base.local(e.tag) not in ("entry","item"): continue
        for c in e:
            if base.local(c.tag)=="link" and c.get("rel","alternate")=="alternate":
                link=(c.get("href") or c.text or "").strip()
                if link:
                    url=base.normalize(up.urljoin(base_url,link))
                    if url:
                        note={"rss_excerpt":excerpt(base.value(e,"description","summary"))}
                        # Both feed and article must be explicitly approved for
                        # automated access AND temporary text sharing.
                        if official_url(source,base_url,approved) and official_url(source,url,approved):
                            body=rss_body(e)
                            if body:
                                note["_rss_body"]=body
                                note["_rss_feed"]=base_url
                        notes[url]=note
                    break
    for r in rows:
        r.update(notes.get(r["url"],{"rss_excerpt":""}))
    return rows,unknown

def tokens(s):
    return set(re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*",s.lower()))-STOP

def classify(title):
    return [k for k,pat in TOPICS.items() if re.search(pat,title,re.I)]

def rank(row, now):
    title=row["title"]
    topics=classify(title)
    age=max(0,(now-base.parse_date(row["published_utc"])).total_seconds()/3600)
    flags=[]
    if RUMOR.search(title): flags.append("rumor")
    if AD.search(title): flags.append("commercial")
    if not topics: flags.append("off_topic")
    score={"source":30 if row["source"] in OFFICIAL else 18,
           "technical":min(25,10*len(topics)),
           "freshness":round(max(0,20*(1-age/24))),
           "relevance":15 if topics else 0,
           "practical":10 if re.search(r"\b(fix|patch|driver|release|vulnerability|update|benchmark|support)\b",title,re.I) else 0}
    return dict(row,topics=topics,flags=flags,score_components=score,score=sum(score.values()),
                verification="UNVERIFIED",publication_allowed=False)

def similar(a,b):
    x,y=tokens(a),tokens(b)
    if min(len(x),len(y))<4: return False
    return len(x&y)/len(x|y)>=0.65 or (len(x&y)>=4 and difflib.SequenceMatcher(None,a.lower(),b.lower()).ratio()>=0.86)

def cluster(rows):
    groups=[]
    for row in sorted(rows,key=lambda r:(r["score"],r["published_utc"]),reverse=True):
        group=next((g for g in groups if similar(g[0]["title"],row["title"])),None)
        if group is None: groups.append([row])
        else: group.append(row)
    return groups

# No article domains are enabled by default. Enable only after checking publisher
# terms and permission. RSS summaries remain available without article fetch.
def host_of(url):
    try:
        p=up.urlsplit(url)
        if p.scheme!="https" or p.username or p.password or p.port not in (None,443):
            return None
        return p.hostname.lower() if p.hostname else None
    except ValueError: return None

KNOWN_HOSTS = {host_of(home) for _, home, _ in SOURCES}
KNOWN_HOSTS.update(host_of(url) for url in NEWSROOMS.values())
KNOWN_HOSTS.update({"arstechnica.com","www.arstechnica.com","techpowerup.com","www.techpowerup.com","tomshardware.com","www.tomshardware.com","bleepingcomputer.com","www.bleepingcomputer.com","amd.com","www.amd.com","nvidia.com","www.nvidia.com","cisa.gov","www.cisa.gov"})
KNOWN_HOSTS.discard(None)
# Explicit publisher identity mapping; approval alone cannot turn a media outlet
# or a cross-domain link into an official primary source.
OFFICIAL_HOSTS = {name: {host_of(home), *(host_of(u) for u in feeds)}
                  for name, home, feeds in SOURCES if name in OFFICIAL}
OFFICIAL_HOSTS.update({name: {host_of(url)} for name, url in NEWSROOMS.items()})
OFFICIAL_HOSTS['AMD'].update({'amd.com','www.amd.com'})
OFFICIAL_HOSTS['NVIDIA'].update({'nvidia.com','www.nvidia.com','blogs.nvidia.com'})
OFFICIAL_HOSTS['CISA News'].update({'cisa.gov','www.cisa.gov'})

def publisher_url(source, url):
    return host_of(url) in OFFICIAL_HOSTS.get(source, set())


def official_url(source, url, approved):
    return host_of(url) in approved and publisher_url(source, url)


def official_candidates(rows):
    """Expose first-party URLs for live review without copying article bodies."""
    candidates = (row for row in rows if publisher_url(row["source"], row["url"]))
    fields = ("source", "title", "url", "published_utc", "topics", "score")
    return [{field: row[field] for field in fields}
            for row in sorted(candidates, key=lambda row: (row["score"], row["published_utc"]), reverse=True)]

def safe_url(url,hosts):
    return host_of(url) in hosts

class GuardRedirect(ur.HTTPRedirectHandler):
    def __init__(self,hosts): self.hosts=hosts
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not safe_url(newurl,self.hosts): raise ValueError("Redirect outside approved hosts")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch_html(url,hosts,kind):
    if not safe_url(url,hosts): raise ValueError("Unapproved URL")
    opener=ur.build_opener(GuardRedirect(hosts))
    req=ur.Request(url,headers={"User-Agent":USER_AGENT,"Accept":"text/html,text/plain"})
    with opener.open(req,timeout=10) as r:
        if not safe_url(r.geturl(),hosts): raise ValueError("Unapproved final URL")
        typ=r.headers.get_content_type()
        if kind=="article" and typ not in ("text/html","application/xhtml+xml"): raise ValueError("Not HTML")
        if kind=="robots" and typ!="text/plain": raise ValueError("No readable robots.txt")
        directives=r.headers.get("X-Robots-Tag","").lower()
        if any(x in directives for x in ("noai","noarchive","nosnippet","noindex","nofollow")): raise ValueError("Restricted by X-Robots-Tag")
        raw=r.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES: raise ValueError("Response too large")
        return raw.decode(r.headers.get_content_charset() or "utf-8",errors="replace")

class Article(Plain):
    def __init__(self):
        super().__init__()
        self.region=0
        self.restricted=False
    def handle_starttag(self,tag,attrs):
        super().handle_starttag(tag,attrs)
        if tag in ("article","main"): self.region+=1
        a=dict(attrs)
        if tag=="meta":
            name=(a.get("name") or "").lower()
            content=(a.get("content") or "").lower()
            if name in ("robots","googlebot","noai") and any(x in content or x==name for x in ("noai","noarchive","nosnippet","noindex","nofollow")): self.restricted=True
            if (a.get("itemprop") or "").lower()=="isaccessibleforfree" and content=="false": self.restricted=True
    def handle_endtag(self,tag):
        super().handle_endtag(tag)
        if tag in ("article","main"): self.region=max(0,self.region-1)
    def handle_data(self,data):
        if self.region and not self.hidden: self.parts.append(data)

def extract(row,approved):
    url=row["url"]
    host=host_of(url)
    if host not in approved: return {"status":"not_approved"}
    if not official_url(row["source"],url,approved): return {"status":"not_official"}
    hosts={host}
    try:
        robots=fetch_html("https://"+host+"/robots.txt",hosts,"robots")
        robot=rp.RobotFileParser()
        robot.parse(robots.splitlines())
        if not robot.can_fetch(USER_AGENT,url): return {"status":"robots_denied"}
        page=fetch_html(url,hosts,"article")
        if re.search(r'"isAccessibleForFree"\s*:\s*(false|"false")',page,re.I):
            return {"status":"restricted"}
        parser=Article()
        parser.feed(page)
        body=parser.result()
        if parser.restricted or re.search(r"\b(subscribe to continue|sign in to continue|subscriber.only|members.only)\b",body,re.I):
            return {"status":"restricted"}
        if len(body)<250: return {"status":"insufficient_text"}
        # Approval must cover automated access and temporary storage/sharing in
        # the existing one-day artifact of this public repository.
        # Extraction is not proof of completeness, authorship or factual accuracy.
        if len(body)>MAX_EVIDENCE_CHARS: return {"status":"oversized_text"}
        return {"status":"official_text_for_review","text":body,
                "excerpt":body[:MAX_EXCERPT],"characters_read":len(body),
                "source_url":url,"format":"html_article_or_main",
                "completeness_verified":False}
    except Exception as exc:
        return {"status":"unavailable","reason":type(exc).__name__,"http_status":getattr(exc,"code",None)}

# These newsrooms expose publication days without reliable publication times.
# Keep them out of the rolling 24-hour shortlist and report their dates as shown.
NEWSROOM_CARD_PATTERNS = {
    "Anthropic": re.compile(
        r'<a class="[^"]*?_title-link" href="(?P<link>[^"]+)"[^>]*>'
        r'[^<]*(?:(?!</a>).)*?<h3[^>]*>(?P<title>.*?)</h3>.*?</a>'
        r'(?:(?!<div class="[^"]*?_details").)*?<time[^>]*>(?P<date>[^<]+)</time>',
        re.S,
    ),
    "Apple Newsroom": re.compile(
        r'<a href="(?P<link>/newsroom/\d{4}/\d{2}/[^"]+)" class="result__item row-anchor"[^>]*>'
        r'(?:(?!</a>).)*?<h3 class="item__headline">(?P<title>.*?)</h3>\s*'
        r'<p class="item__date">(?P<date>[^<]+)</p>',
        re.S,
    ),
    "Intel Newsroom": re.compile(
        r'<a class="cmp-teaser__link" href="(?P<link>/content/www/us/en/newsroom/[^"]+)"[^>]*>'
        r'(?:(?!</a>).)*?<h2 class="cmp-teaser__title">(?P<title>.*?)</h2>\s*'
        r'<div class="cmp-teaser__description">\s*<p>(?P<date>[^<]+)</p>',
        re.S,
    ),
}


def parse_newsroom(source, page, url):
    articles = {}
    for match in NEWSROOM_CARD_PATTERNS[source].finditer(page):
        link, title, day = match.group("link", "title", "date")
        link = base.normalize(up.urljoin(url, html.unescape(link)))
        if not link or not publisher_url(source, link):
            continue
        parsed_day = re.fullmatch(r"([A-Za-z]{3})[A-Za-z]* (\d{1,2}), (\d{4})", day.strip())
        if not parsed_day:
            continue
        try:
            published = dt.datetime.strptime(" ".join(parsed_day.groups()), "%b %d %Y").date()
        except ValueError:
            continue
        articles[link] = {
            "source": source, "title": excerpt(title), "url": link,
            "published_date": published.isoformat(),
        }
    return list(articles.values())


def collect_newsroom(spec, now):
    source, url = spec
    try:
        raw, actual = base.get(url)
        if not publisher_url(source, actual):
            raise ValueError("Newsroom redirected outside its official host")
        articles = parse_newsroom(source, raw.decode("utf-8", errors="replace"), actual)
        if not articles:
            raise ValueError("No dated newsroom entries found")
    except (OSError, ValueError) as exc:
        return {"source": source, "status": "failed", "errors": [str(exc)[:140]], "items": []}

    # Calendar dates have no timezone or time of day. Show today and yesterday
    # for editorial review without assigning a timestamp or marking them fresh.
    dates = {now.date().isoformat(), (now.date() - dt.timedelta(days=1)).isoformat()}
    rows = [row for row in articles if row["title"] and row["published_date"] in dates
            and not (RUMOR.search(row["title"]) or AD.search(row["title"]))]
    return {"source": source, "status": "ok", "index": actual, "dated": len(rows), "items": rows}


def run(approved):
    now=dt.datetime.now(base.UTC)
    # Use the proven RSS collector without editing its source file.
    original=base.parse
    # Avoid recursion: wrapper calls the saved parser; base.collect_one uses global parse.
    def wrapped(data,source,base_url,when):
        return enriched_parse(data,source,base_url,when,approved,original)
    base.parse=wrapped
    try:
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            feeds = [pool.submit(base.collect_one, spec, now) for spec in SOURCES]
            newsrooms = [pool.submit(collect_newsroom, spec, now) for spec in NEWSROOMS.items()]
            reports = [job.result() for job in feeds]
            newsroom_reports = [job.result() for job in newsrooms]
    finally: base.parse=original
    rows=[rank(r,now) for r in base.dedup([r for s in reports for r in s.pop("items")])]
    eligible=[r for r in rows if not r["flags"]]
    groups=cluster(eligible)
    shortlist=[]
    budget=MAX_ARTICLES
    for g in groups[:15]:
        lead={k:v for k,v in g[0].items() if k not in ("_rss_body","_rss_feed")}
        lead["related_candidates"]=[{"source":r["source"],"url":r["url"],"title":r["title"],"rss_excerpt":r["rss_excerpt"]} for r in g[1:4]]
        lead["distinct_reporting_sources"]=len(set(r["source"] for r in g))
        # Similar titles across outlets do not establish independent confirmation.
        lead["independent_confirmation"]=False
        # Prefer an approved primary source within the cluster, even when
        # a secondary report ranks higher. Keep its own provenance.
        primary=next((r for r in g if official_url(r["source"],r["url"],approved)),None)
        if budget and primary:
            if primary.get("_rss_body"):
                body=primary["_rss_body"]
                lead["article"]={"status":"official_text_for_review","text":body,
                    "excerpt":body[:MAX_EXCERPT],"characters_read":len(body),
                    "format":"rss_explicit_content","feed_url":primary["_rss_feed"],
                    "completeness_verified":False}
            else:
                lead["article"]=extract(primary,approved)
            lead["article"]["source"]=primary["source"]
            lead["article"]["source_url"]=primary["url"]
            lead["article"]["published_utc"]=primary["published_utc"]
            budget-=1
        else: lead["article"]={"status":"budget_exhausted" if primary else "no_approved_primary"}
        shortlist.append(lead)
    dated = [row for report in newsroom_reports for row in report.pop("items")]
    rss_ok = sum(s["status"] == "ok" for s in reports)
    reports.extend(newsroom_reports)
    ok = sum(s["status"] == "ok" for s in reports)
    result={"schema_version":2,"collected_utc":now.isoformat(),"window_hours":24,
        "successful_sources":ok,"total_sources":len(reports),"sources":reports,
        "unique_candidates":len(rows),"filtered_candidates":len(rows)-len(eligible),
        "clusters":len(groups),"approved_article_hosts":sorted(approved),
        "official_texts_for_review":sum(r["article"]["status"]=="official_text_for_review" for r in shortlist),
        "verified_articles":False,"publication_allowed":False,"shortlist":shortlist,
        "official_candidates":official_candidates(eligible),
        "official_dated_candidates":sorted(dated, key=lambda row: row["published_date"], reverse=True)}
    Path("rss-output").mkdir(exist_ok=True)
    Path("rss-output/news-v2.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Coverage:",ok,"/",len(reports),"Candidates:",len(rows),"Clusters:",len(groups),"Shortlist:",len(shortlist))
    if rss_ok<10: raise SystemExit(f"Insufficient RSS coverage: {rss_ok}/10 required")

class Tests(unittest.TestCase):
    def setUp(self):
        self.now=dt.datetime(2026,11,26,12,tzinfo=base.UTC)
        self.row={"source":"AMD","title":"AMD releases Ryzen CPU security patch","url":"https://www.amd.com/test","published_utc":self.now.isoformat(),"rss_excerpt":""}
    def test_rank(self):
        r=rank(self.row,self.now)
        self.assertGreaterEqual(r["score"],80)
        self.assertLessEqual(r["score"],100)
        self.assertFalse(r["publication_allowed"])
    def test_flags(self):
        self.assertIn("rumor",rank(dict(self.row,title="Leaked Ryzen CPU rumor"),self.now)["flags"])
        self.assertIn("commercial",rank(dict(self.row,title="GPU Black Friday deal"),self.now)["flags"])
    def test_cluster(self):
        a=rank(self.row,self.now)
        b=dict(a,source="Ars Technica",url="https://arstechnica.com/test")
        self.assertEqual(len(cluster([a,b])),1)
        self.assertEqual(len(cluster([a,dict(b,title="Windows kernel update changes network driver")])),2)
    def test_excerpt(self):
        self.assertEqual(excerpt("<p>CPU</p><script>ignore</script><b>patch</b>"),"CPU patch")
        self.assertEqual(len(excerpt("x"*700)),600)
    def test_feed_excerpt(self):
        raw=b'<rss><channel><item><title>CPU patch</title><link>https://example.com/a</link><pubDate>Thu, 26 Nov 2026 11:00:00 GMT</pubDate><description>&lt;p&gt;Fix&lt;/p&gt;</description></item></channel></rss>'
        rows,_=enriched_parse(raw,"test","https://example.com/",self.now)
        self.assertEqual(rows[0]["rss_excerpt"],"Fix")
    def test_rss_full_content_requires_both_approvals(self):
        raw=('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
             '<title>CPU patch</title><link>https://www.amd.com/test</link>'
             '<pubDate>Thu, 26 Nov 2026 11:00:00 GMT</pubDate><description>Summary</description>'
             '<content:encoded><![CDATA[<p>'+"x"*1000+'</p>]]></content:encoded></item></channel></rss>').encode()
        for hosts in (set(),{"www.amd.com"},{"amd.com"}):
            rows,_=enriched_parse(raw,"AMD","https://amd.com/feed",self.now,hosts)
            self.assertNotIn("_rss_body",rows[0])
        rows,_=enriched_parse(raw,"AMD","https://amd.com/feed",self.now,{"amd.com","www.amd.com"})
        self.assertEqual(rows[0]["_rss_body"],"x"*1000)
        self.assertEqual(rows[0]["rss_excerpt"],"Summary")
    def test_rss_summary_not_full(self):
        import xml.etree.ElementTree as ET
        self.assertIsNone(rss_body(ET.fromstring("<item><description>"+"x"*1000+"</description></item>")))
    def test_commercial_price_titles(self):
        for title in ("CPU Price Index 2026","Get an RTX GPU for just $1,141","AMD Ryzen CPU at an all-time low"):
            self.assertIn("commercial",rank(dict(self.row,title=title),self.now)["flags"])
    def test_additional_headline_filters(self):
        for title in ("Save 21% on this SSD", "SSD 30% off today"):
            self.assertIn("commercial",rank(dict(self.row,title=title),self.now)["flags"])
        self.assertIn("rumor",rank(dict(self.row,title="GPU shipments stopped, report claims"),self.now)["flags"])
    def test_url(self):
        hosts={"www.amd.com"}
        for u in ("http://www.amd.com/a","https://www.amd.com.evil.test/a","https://www.amd.com:123/a","https://user@www.amd.com/a"):
            self.assertFalse(safe_url(u,hosts))
        self.assertTrue(safe_url(self.row["url"],hosts))
    def test_android_blog_migration_requires_approval(self):
        url="https://developer.android.com/blog/example"
        self.assertIn("developer.android.com",KNOWN_HOSTS)
        self.assertTrue(official_url("Android Developers Blog",url,{"developer.android.com"}))
        self.assertFalse(official_url("Android Developers Blog",url,set()))
        self.assertFalse(official_url("AMD",url,{"developer.android.com"}))
        self.assertFalse(official_url("Android Developers Blog","https://developer.android.com.evil.test/blog/example",{"developer.android.com"}))
    def test_official_links_without_article_permission(self):
        official = rank(dict(self.row, source="NVIDIA", url="https://blogs.nvidia.com/blog/example/"), self.now)
        mirror = dict(official, source="Ars Technica")
        candidates = official_candidates([mirror, dict(official, _rss_body="not for export")])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], official["url"])
        self.assertNotIn("_rss_body", candidates[0])
        self.assertFalse(official_url("NVIDIA", official["url"], set()))

    def test_newsroom_dates_remain_separate_from_publication_times(self):
        cards = {
            "Anthropic": (
                '<a class="card_title-link" href="/news/example"><span><h3>Claude &amp; Codex</h3>'
                '</span></a><div class="card_date"><time>Nov 26, 2026</time></div>'
            ),
            "Apple Newsroom": (
                '<a href="/newsroom/2026/11/example/" class="result__item row-anchor">'
                '<h3 class="item__headline">iPhone release</h3>'
                '<p class="item__date">November 26, 2026</p></a>'
            ),
            "Intel Newsroom": (
                '<a class="cmp-teaser__link" href="/content/www/us/en/newsroom/news/example.html">'
                '<h2 class="cmp-teaser__title">Intel release</h2>'
                '<div class="cmp-teaser__description"><p>November 26, 2026</p></div></a>'
            ),
        }
        for source, card in cards.items():
            with self.subTest(source=source):
                url = NEWSROOMS[source]
                with patch.object(base, "get", return_value=(card.encode(), url)):
                    report = collect_newsroom((source, url), self.now)
                    self.assertEqual(report["status"], "ok")
                    self.assertEqual(report["dated"], 1)
                    self.assertEqual(report["items"][0]["published_date"], "2026-11-26")
                    self.assertNotIn("published_utc", report["items"][0])
                    stale = collect_newsroom((source, url), self.now + dt.timedelta(days=2))
                    self.assertEqual(stale["items"], [])

    def test_no_permission(self):
        with patch(__name__+".fetch_html") as f:
            self.assertEqual(extract(self.row,set())["status"],"not_approved")
            f.assert_not_called()
    def test_robots_denial(self):
        with patch(__name__+".fetch_html",return_value="User-agent: *\nDisallow: /") as f:
            self.assertEqual(extract(self.row,{"www.amd.com"})["status"],"robots_denied")
            self.assertEqual(f.call_count,1)
    def test_restrictions(self):
        with patch(__name__+".fetch_html",side_effect=["User-agent: *\nAllow: /",'<meta name="robots" content="nosnippet"><article>'+"x"*700+"</article>"]):
            self.assertEqual(extract(self.row,{"www.amd.com"})["status"],"restricted")
    def test_full_article_for_review(self):
        with patch(__name__+".fetch_html",side_effect=["User-agent: *\nAllow: /","<article>"+"x"*700+"</article>"]):
            r=extract(self.row,{"www.amd.com"})
            self.assertEqual(r["status"],"official_text_for_review")
            self.assertEqual(r["text"],"x"*700)
            self.assertEqual(len(r["excerpt"]),600)
            self.assertFalse(r["completeness_verified"])
    def test_nonofficial_approved_host(self):
        row=dict(self.row,source="Ars Technica",url="https://arstechnica.com/test")
        with patch(__name__+".fetch_html") as f:
            self.assertEqual(extract(row,{"arstechnica.com"})["status"],"not_official")
            f.assert_not_called()
    def test_cross_source_host(self):
        row=dict(self.row,url="https://nvidianews.nvidia.com/test")
        with patch(__name__+".fetch_html") as f:
            self.assertEqual(extract(row,{"nvidianews.nvidia.com"})["status"],"not_official")
            f.assert_not_called()
    def test_oversized_article(self):
        with patch(__name__+".fetch_html",side_effect=["User-agent: *\nAllow: /","<article>"+"x"*(MAX_EVIDENCE_CHARS+1)+"</article>"]):
            self.assertEqual(extract(self.row,{"www.amd.com"})["status"],"oversized_text")

if __name__=="__main__":
    if "--test" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        import os
        approved={h.strip().lower() for h in os.getenv("APPROVED_ARTICLE_HOSTS","").split(",") if h.strip()}
        if not approved<=KNOWN_HOSTS: raise SystemExit("Unknown approved host")
        run(approved)
