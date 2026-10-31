#!/usr/bin/env python3
"""job-radar: a once-a-day check of target companies' own career sites for
US analog / mixed-signal / PMIC IC design roles.

How it behaves
- Uses each company's public job-search endpoint (Workday, Greenhouse, Lever).
- Reads robots.txt before touching a site and skips anything it disallows.
- Identifies itself with a plain User-Agent and pauses between requests.
- Writes its results to data/ so the daily triage step can read them:
    open_jobs.json    every matching posting that is open today
    new_jobs.json     postings first seen today (all of them on the very first run)
    closed_jobs.json  postings that were open before and disappeared today
    seen.json         memory of every posting ever matched
    health.json       per-company status of today's run
    summary.md        short human-readable report

Usage
    python radar.py                 # full run, writes data/
    python radar.py --only NVIDIA   # one company, prints results, writes nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = "job-radar/1.0 (personal job search; runs once a day)"
UA_TOKEN = "job-radar"
PAUSE = 0.7          # seconds between requests to the same host
TIMEOUT = 30
PAGE = 20            # Workday's maximum page size
DESC_LIMIT = 8000    # characters of job description kept per posting

DEFAULT_TERMS = [
    "analog", "mixed signal", "PMIC", "power management", "circuit design",
    "IC design", "data converter", "new college grad", "university graduate",
    "early career",
]

# ---------------------------------------------------------------- filters

TITLE_INCLUDE = re.compile(
    r"analog|mixed[\s-]*signal|\bams\b|\ba/ms\b|pmic|power management|power ic|"
    r"\bpower\b.*\bdesign|circuit|\bic design|\bic designer|silicon design|"
    r"converter|\badc\b|\bdac\b|\bpll\b|serdes|clock|\bldo\b|regulator|dc[\s-]?dc|"
    r"bandgap|hardware engineer|r&d engineer|design engineer|asic design|"
    r"electrical engineer",
    re.I,
)
TITLE_EXCLUDE = re.compile(
    r"\bsenior\b|\bsr\b|\bstaff\b|principal|\blead\b|director|manager|\bmgr\b|fellow|"
    r"architect|\bintern\b|internship|\bco-?op\b|technician|\bsales\b|marketing|"
    r"finance|recruit|verification|validation|\bdv\b|physical design|\brtl\b|"
    r"digital|layout|\bdft\b|\btest\b|product engineer|applications? engineer|"
    r"\bfield\b|firmware|software|\bcad\b|methodology|packag|\bprocess\b|yield|"
    r"reliability|quality|failure analysis|\bpcb\b|\bboard\b|mechanical|thermal|"
    r"manufactur|facilit|supply chain|technical writer",
    re.I,
)
STRONG_TITLE = re.compile(
    r"analog|mixed[\s-]*signal|\bams\b|pmic|power management|data converter|"
    r"\badc\b|\bpll\b|serdes|\bldo\b|regulator|dc[\s-]?dc|bandgap|circuit design",
    re.I,
)
ANALOG_TERMS = [
    ("analog", r"\banalog\b"), ("mixed-signal", r"mixed[\s-]signal"),
    ("transistor-level", r"transistor[\s-]level"), ("LDO", r"\bldos?\b"),
    ("bandgap", r"bandgap"), ("ADC", r"\badcs?\b"), ("DAC", r"\bdacs?\b"),
    ("PLL", r"\bplls?\b"), ("op-amp", r"\bop[\s-]?amps?\b|operational amplifier"),
    ("comparator", r"\bcomparators?\b"), ("Spectre", r"\bspectre\b"),
    ("Virtuoso", r"\bvirtuoso\b"), ("HSPICE", r"\bhspice\b"),
    ("power management", r"power management|\bpmics?\b"),
    ("DC-DC", r"\bdc[\s/-]?dc\b|\bbuck\b|\bboost\b"),
    ("switched-capacitor", r"switched[\s-]capacitor"),
    ("sigma-delta", r"sigma[\s-]delta|delta[\s-]sigma"), ("SerDes", r"\bserdes\b"),
    ("data converter", r"data converters?"), ("regulator", r"voltage regulators?"),
    ("tapeout", r"tape[\s-]?outs?"), ("bring-up", r"bring[\s-]?up|post[\s-]silicon"),
    ("VCO", r"\bvcos?\b"), ("LNA", r"\blnas?\b"), ("charge pump", r"charge pumps?"),
]
ANALOG_RE = [(name, re.compile(p, re.I)) for name, p in ANALOG_TERMS]

NCG_RE = re.compile(
    r"new college grad|new grad|university grad|recent(?:ly)? grad|early[\s-]career|"
    r"entry[\s-]level|0\s*(?:-|–|to)\s*[1-3]\+?\s*years|no (?:prior )?experience",
    re.I,
)
YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:or more\s+)?years?['’]?\s*(?:of\s+)?"
    r"(?:relevant\s+|industry\s+|professional\s+|related\s+|hands-on\s+|design\s+)?"
    r"(?:experience|exp\b)",
    re.I,
)
FLAG_RE = {
    "no_sponsorship": re.compile(
        r"(?:not|unable to|will not|cannot|can't|won't|does not|do not)\s+(?:\w+\s+){0,4}"
        r"sponsor|without (?:the need for )?(?:current or future )?(?:visa )?sponsorship|"
        r"no (?:visa )?sponsorship|sponsorship is not (?:available|offered)",
        re.I,
    ),
    "us_citizen_only": re.compile(
        r"u\.?s\.?\s+citizen(?:ship)?\s+(?:is\s+)?(?:required|only)|"
        r"must be (?:a\s+)?u\.?s\.?\s+citizen|requires?\s+u\.?s\.?\s+citizenship",
        re.I,
    ),
    "us_person_or_pr": re.compile(
        r"u\.?s\.?\s+person|permanent resident|green card", re.I),
    "clearance": re.compile(
        r"security clearance|secret clearance|clearance (?:is\s+)?required|ts/sci|"
        r"active clearance",
        re.I,
    ),
    "export_control": re.compile(
        r"export control|export license|\bitar\b|deemed export", re.I),
}

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "hawaii", "idaho", "illinois", "indiana",
    "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
]
US_RE = re.compile(r"\b(?:usa|united states|u\.s\.a?\.?)(?![a-z])|^\s*us\b", re.I)
NON_US_RE = re.compile(
    r"\b(?:canada|india|china|taiwan|japan|korea|germany|united kingdom|england|"
    r"scotland|wales|ireland|israel|singapore|malaysia|philippines|vietnam|france|"
    r"netherlands|spain|italy|poland|romania|turkey|türkiye|mexico|brazil|austria|"
    r"switzerland|denmark|sweden|finland|norway|belgium|czech|hungary|armenia|"
    r"egypt|thailand|australia|bengaluru|bangalore|hyderabad|noida|pune|chennai|"
    r"shanghai|beijing|shenzhen|hsinchu|taipei|tokyo|seoul|munich|dublin|"
    r"limerick|cork|edinburgh|penang|kuala lumpur|manila|ottawa|toronto|"
    r"vancouver|haifa|tel aviv|herzliya|hong kong|cairo|istanbul|yerevan)\b",
    re.I,
)


def title_passes(title: str) -> bool:
    return bool(TITLE_INCLUDE.search(title)) and not TITLE_EXCLUDE.search(title)


def is_us(locations: list[str], country: str = "") -> bool | None:
    """True / False when the location is clearly (non-)US, None when unclear."""
    if country:
        return bool(re.fullmatch(r"\s*united states(?: of america)?\s*", country, re.I))
    text = " ; ".join(loc for loc in locations if loc)
    if not text:
        return None
    if US_RE.search(text):
        return True
    # "San Diego, CA" counts; Workday's "CA-ON - Ottawa" (Canada-Ontario) does not
    for m in re.finditer(r",\s*([A-Z]{2})\b(?!\s*-\s*[A-Z]{2}\b)", text):
        if m.group(1) in US_STATES:
            return True
    low = text.lower()
    if any(re.search(rf"\b{re.escape(name)}\b", low) for name in STATE_NAMES):
        return True
    if NON_US_RE.search(text):
        return False
    return None


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(?:p|div|h\d|li|ul|ol|tr)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def snippet(text: str, m: re.Match, width: int = 90) -> str:
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return " ".join(text[a:b].split())


def build_job(c: dict, title: str, location: str, req_id: str, url: str,
              desc: str, posted: str) -> dict | None:
    """Return a job record, or None when the posting is not analog-flavoured enough."""
    text = f"{title}\n{desc}"
    hits = [name for name, pat in ANALOG_RE if pat.search(text)]
    if not STRONG_TITLE.search(title) and len(hits) < 3:
        return None
    flags = {}
    for name, pat in FLAG_RE.items():
        m = pat.search(desc)
        if m:
            flags[name] = snippet(desc, m)
    ncg = NCG_RE.search(text)
    years = sorted({int(y) for y in YEARS_RE.findall(desc) if 0 < int(y) <= 20})
    req_id = (req_id or "").strip()
    return {
        "key": f"{c['name']}|{req_id or url}",
        "company": c["name"],
        "title": title.strip(),
        "location": location.strip(),
        "req_id": req_id,
        "url": url,
        "posted": posted or "",
        "ncg_signal": snippet(text, ncg) if ncg else "",
        "years_mentioned": years,
        "flags": flags,
        "analog_hits": hits,
        "description": desc[:DESC_LIMIT],
    }


# ---------------------------------------------------------------- http

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


class Blocked(Exception):
    """robots.txt disallows the request."""


def _throttle(url: str) -> None:
    host = urllib.parse.urlsplit(url).netloc
    wait = PAUSE - (time.monotonic() - _last_hit.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def robots_for(base: str) -> urllib.robotparser.RobotFileParser:
    rp = _robots.get(base)
    if rp is not None:
        return rp
    rp = urllib.robotparser.RobotFileParser()
    try:
        _throttle(base)
        r = SESSION.get(base + "/robots.txt", timeout=TIMEOUT,
                        headers={"Accept": "text/plain,*/*"})
        if r.status_code in (401, 403) or r.status_code >= 500:
            rp.disallow_all = True
        elif r.status_code >= 400:
            rp.allow_all = True          # no robots.txt: no restrictions
        else:
            rp.parse(r.text.splitlines())
    except requests.RequestException:
        rp.disallow_all = True           # cannot read the rules: stay out
    _robots[base] = rp
    return rp


def allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return robots_for(f"{parts.scheme}://{parts.netloc}").can_fetch(UA_TOKEN, url)


def request(method: str, url: str, **kw) -> requests.Response:
    if not allowed(url):
        raise Blocked(f"robots.txt disallows {url}")
    for attempt in range(3):
        _throttle(url)
        try:
            r = SESSION.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(min(int(r.headers.get("Retry-After", 10 * (attempt + 1))), 60))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- sources

def workday_us_facet(api: str) -> tuple[str, str] | None:
    data = request("POST", api, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                       "searchText": ""}).json()

    def walk(facets, parent=""):
        for f in facets or []:
            param = f.get("facetParameter") or parent
            for v in f.get("values") or []:
                if "values" in v:
                    hit = walk([v], param)
                    if hit:
                        return hit
                elif "country" in param.lower() and re.fullmatch(
                        r"\s*united states(?: of america)?\s*", v.get("descriptor", ""), re.I):
                    return param, v.get("id")
        return None

    return walk(data.get("facets"))


def workday(c: dict) -> tuple[int, list[dict]]:
    host = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    base = f"{host}/wday/cxs/{c['tenant']}/{c['site']}"
    api = f"{base}/jobs"
    us = workday_us_facet(api)
    facets = {us[0]: [us[1]]} if us else {}
    found: dict[str, dict] = {}
    raw = 0
    for term in c.get("terms") or DEFAULT_TERMS:
        total = None
        for page in range(c.get("max_pages", 5)):
            body = {"appliedFacets": facets, "limit": PAGE, "offset": page * PAGE,
                    "searchText": term}
            data = request("POST", api, json=body).json()
            posts = data.get("jobPostings") or []
            if total is None:              # Workday reports the total on page 1 only
                total = data.get("total") or 0
            raw += len(posts)
            for p in posts:
                path = p.get("externalPath")
                if path:
                    found.setdefault(path, p)
            if not posts or (page + 1) * PAGE >= total:
                break
    jobs = []
    for path, p in found.items():
        title = p.get("title", "")
        if not title_passes(title):
            continue
        info = request("GET", base + path).json().get("jobPostingInfo") or {}
        locs = [info.get("location") or p.get("locationsText") or ""]
        locs += list(info.get("additionalLocations") or [])
        country = (info.get("country") or {}).get("descriptor", "")
        if not us and is_us(locs, country) is False:
            continue
        if us and country and is_us([], country) is False and not any(
                is_us([loc]) for loc in locs):
            continue
        req = info.get("jobReqId") or next(iter(p.get("bulletFields") or []), "")
        url = info.get("externalUrl") or f"{host}/{c['site']}{path}"
        job = build_job(c, title, "; ".join(loc for loc in locs if loc), req, url,
                        html_to_text(info.get("jobDescription", "")),
                        info.get("startDate") or p.get("postedOn", ""))
        if job:
            jobs.append(job)
    return raw, jobs


def greenhouse(c: dict) -> tuple[int, list[dict]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs?content=true"
    posts = request("GET", url).json().get("jobs") or []
    jobs = []
    for p in posts:
        title = p.get("title", "")
        loc = (p.get("location") or {}).get("name", "")
        if not title_passes(title) or is_us([loc]) is False:
            continue
        desc = html_to_text(html.unescape(p.get("content", "")))
        req = str(p.get("requisition_id") or p.get("id") or "")
        job = build_job(c, title, loc, req, p.get("absolute_url", ""), desc,
                        (p.get("updated_at") or "")[:10])
        if job:
            jobs.append(job)
    return len(posts), jobs


def lever(c: dict) -> tuple[int, list[dict]]:
    host = "https://api.eu.lever.co" if c.get("region") == "eu" else "https://api.lever.co"
    posts = request("GET", f"{host}/v0/postings/{c['site']}?mode=json").json()
    jobs = []
    for p in posts:
        title = p.get("text", "")
        cats = p.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        if not title_passes(title) or is_us(locs) is False:
            continue
        parts = [p.get("descriptionPlain", "")]
        for block in p.get("lists") or []:
            parts.append(f"{block.get('text', '')}\n{html_to_text(block.get('content', ''))}")
        parts.append(p.get("additionalPlain", ""))
        created = p.get("createdAt")
        posted = (dt.datetime.fromtimestamp(created / 1000, dt.timezone.utc).date().isoformat()
                  if created else "")
        job = build_job(c, title, "; ".join(locs), p.get("id", ""), p.get("hostedUrl", ""),
                        "\n".join(x for x in parts if x), posted)
        if job:
            jobs.append(job)
    return len(posts), jobs


FETCHERS = {"workday": workday, "greenhouse": greenhouse, "lever": lever}


# ---------------------------------------------------------------- run

def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run one company by name and print results")
    args = ap.parse_args()

    companies = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))
    if args.only:
        companies = [c for c in companies if c["name"].lower() == args.only.lower()]
        if not companies:
            print(f"no company named {args.only!r}", file=sys.stderr)
            return 2

    today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    open_jobs: dict[str, dict] = {}
    health: dict[str, dict] = {}
    for c in companies:
        t0 = time.monotonic()
        try:
            raw, jobs = FETCHERS[c["ats"]](c)
            health[c["name"]] = {"status": "ok", "ats": c["ats"], "raw_results": raw,
                                 "matched": len(jobs)}
            for j in jobs:
                open_jobs.setdefault(j["key"], j)
        except Blocked as e:
            health[c["name"]] = {"status": "blocked_by_robots", "ats": c["ats"],
                                 "detail": str(e)[:300]}
        except Exception as e:  # keep going: one broken site must not stop the run
            health[c["name"]] = {"status": "error", "ats": c["ats"],
                                 "detail": f"{type(e).__name__}: {e}"[:300]}
        health[c["name"]]["seconds"] = round(time.monotonic() - t0, 1)
        print(c["name"], json.dumps(health[c["name"]], ensure_ascii=False), flush=True)

    if args.only:
        for j in open_jobs.values():
            print(f"- {j['title']} | {j['location']} | {j['req_id']} | {j['url']}")
        return 0

    DATA.mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    baseline = not seen_path.exists()
    seen: dict[str, dict] = {} if baseline else json.loads(seen_path.read_text("utf-8"))
    ok = {name for name, h in health.items() if h["status"] == "ok"}

    new = [j for k, j in open_jobs.items() if not seen.get(k, {}).get("open")]
    closed = []
    for k, s in seen.items():
        if s.get("open") and s["company"] in ok and k not in open_jobs:
            s["open"] = False
            s["closed_on"] = today
            closed.append({"key": k, **{f: s.get(f) for f in
                                        ("company", "title", "url", "first_seen", "last_seen")}})
    for k, j in open_jobs.items():
        s = seen.setdefault(k, {"company": j["company"], "title": j["title"],
                                "url": j["url"], "first_seen": today})
        s.update(last_seen=today, open=True)
        s.pop("closed_on", None)
        j["first_seen"] = s["first_seen"]

    jobs_sorted = sorted(open_jobs.values(), key=lambda j: (j["company"], j["title"]))
    new_sorted = sorted(new, key=lambda j: (j["company"], j["title"]))
    write_json(DATA / "open_jobs.json", {"run_date": today, "jobs": jobs_sorted})
    write_json(DATA / "new_jobs.json", {"run_date": today, "baseline": baseline,
                                        "jobs": new_sorted})
    write_json(DATA / "closed_jobs.json", {"run_date": today, "jobs": closed})
    write_json(DATA / "health.json", {"run_date": today, "companies": health})
    write_json(seen_path, seen)

    lines = [f"# Job radar — {today}", ""]
    if baseline:
        lines += ["First run: every open match is listed as new (baseline).", ""]
    lines += [f"- Open matches: {len(jobs_sorted)}", f"- New today: {len(new_sorted)}",
              f"- Closed since last run: {len(closed)}", "",
              "| Company | Status | Matched | Seconds |", "|---|---|---|---|"]
    for name, h in health.items():
        lines.append(f"| {name} | {h['status']} | {h.get('matched', '')} | {h['seconds']} |")
    if new_sorted:
        lines += ["", "## New today", ""]
        lines += [f"- {j['company']} — [{j['title']}]({j['url']}) — {j['location']}"
                  for j in new_sorted]
    (DATA / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"open={len(jobs_sorted)} new={len(new_sorted)} closed={len(closed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
