#!/usr/bin/env python3
"""
job-watcher: polls company applicant-tracking systems (ATS) directly and emails
you the moment a new posting appears.

Detection model
---------------
* Every ATS gives each posting a stable ID (Greenhouse job id, Lever/Ashby UUID,
  Workday externalPath/req id, ...). A posting is NEW when its ID was never seen
  for that company before. Titles and dates are not used for identity.
* The first successful fetch of a company is a silent *baseline* (everything
  already open is recorded, nothing is emailed) so adding a company never floods
  you. Exception: postings whose ATS publish time is within
  `baseline_recent_hours` are still alerted.
* A posting that disappears is moved to `gone` (kept `gone_ttl_days`). If the
  same ID comes back within `flap_grace_hours` it is restored silently (ATS
  glitch / pagination shift); later than that it is alerted as "reopened".
* A NEW ID whose normalized title+location matches a recently open/closed
  posting is alerted and tagged "repost".
* Removals are ignored when a fetch looks partial (pagination short, or the
  board shrank >50% at once) and a fetch returning 0 jobs for a board that had
  jobs counts as a failure, so outages never cause false "new" floods later.
* State is only written after the email has been sent successfully; if sending
  fails, the same postings are retried on the next run (no silent loss).

Stdlib only, Python 3.11+.
Modes: run (default) | validate | test-email     (see README.md)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UTC = dt.timezone.utc
try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(os.environ.get("WATCH_TZ", "America/Vancouver"))
except Exception:  # pragma: no cover
    LOCAL_TZ = UTC

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
STATE_VERSION = 1


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


# --------------------------------------------------------------------------- HTTP
class FetchError(Exception):
    pass


def http_json(url, *, method="GET", body=None, headers=None, timeout=25, retries=4):
    """GET/POST returning parsed JSON. Retries 429/5xx/network with backoff,
    fails fast on other 4xx (bad slug)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "en-US,en;q=0.9"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    last = "unknown error"
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                snippet = raw[:80].decode("utf-8", "replace").replace("\n", " ")
                raise FetchError(f"non-JSON response ({snippet!r}) from {url}")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (408, 425, 429, 500, 502, 503, 504):
                raise FetchError(f"{last} from {url}") from None
            ra = (e.headers or {}).get("Retry-After", "")
            wait = min(float(ra), 20.0) if ra.replace(".", "", 1).isdigit() else 2.0 * (2 ** attempt)
        except FetchError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            wait = 2.0 * (2 ** attempt)
        if attempt < retries - 1:
            time.sleep(wait)
    raise FetchError(f"{last} from {url} (after {retries} attempts)")


# --------------------------------------------------------------------------- model
@dataclass
class Job:
    id: str
    title: str
    url: str
    locations: list = field(default_factory=list)
    countries: list = field(default_factory=list)  # ISO2 codes or country names
    remote: bool | None = None
    posted: dt.datetime | None = None
    posted_kind: str = ""  # published | updated | approx | ""
    posted_date_only: bool = False
    location_unknown: bool = False
    group: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def location(self) -> str:
        return "; ".join(dict.fromkeys(x.strip() for x in self.locations if x and x.strip()))

    def display_location(self, limit=4) -> str:
        """Compact: drop entries contained in a longer one, Canadian first, cap the list."""
        locs = list(dict.fromkeys(re.sub(r"\s+,", ",", x.strip()) for x in self.locations if x and x.strip()))
        low = [x.lower() for x in locs]
        keep = [x for i, x in enumerate(locs)
                if not any(j != i and low[i] in low[j] and len(low[j]) > len(low[i]) for j in range(len(locs)))]
        keep.sort(key=lambda x: 0 if re.search(r"canada|\b(BC|ON|QC|AB)\b|british columbia|ontario|quebec|alberta",
                                               x, re.I) else 1)
        more = len(keep) - limit
        return "; ".join(keep[:limit]) + (f" (+{more} more)" if more > 0 else "")


def parse_ts(v):
    """-> (datetime|None, date_only:bool). Accepts epoch s/ms, ISO 8601, 'YYYY-MM-DD',
    'YYYY-MM-DD HH:MM:SS UTC'."""
    if v in (None, "", 0):
        return None, False
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC), False
        s = str(v).strip()
        if re.fullmatch(r"\d{10,13}", s):
            return parse_ts(int(s))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return dt.datetime.fromisoformat(s).replace(tzinfo=UTC), True
        s = re.sub(r"\s*UTC$", "+00:00", s)
        s = re.sub(r"Z$", "+00:00", s)
        s = re.sub(r"^(\d{4}-\d{2}-\d{2}) ", r"\1T", s)
        d = dt.datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC), False
    except Exception:
        return None, False


def _clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


# --------------------------------------------------------------------------- adapters
# Each adapter: fetch(company_cfg) -> (list[Job], complete: bool)

def fetch_greenhouse(c):
    tok = c["id"]
    host = "boards-api.eu.greenhouse.io" if str(c.get("region", "")).lower() == "eu" else "boards-api.greenhouse.io"
    d = http_json(f"https://{host}/v1/boards/{urllib.parse.quote(tok)}/jobs")
    if not isinstance(d, dict) or "jobs" not in d:
        raise FetchError("unexpected Greenhouse payload")
    out = []
    for j in d["jobs"]:
        loc = _clean((j.get("location") or {}).get("name"))
        remote = None
        for m in j.get("metadata") or []:
            if isinstance(m, dict) and re.search(r"location type|workplace|remote", str(m.get("name")), re.I):
                if re.search(r"remote", str(m.get("value")), re.I):
                    remote = True
        if re.search(r"\bremote\b", loc, re.I):
            remote = True
        ts, kind = j.get("first_published"), "published"
        if not ts:
            ts, kind = j.get("updated_at"), "updated"
        p, donly = parse_ts(ts)
        out.append(Job(id=str(j["id"]), title=_clean(j.get("title")), url=j.get("absolute_url") or "",
                       locations=[x.strip() for x in re.split(r"\s*[|;]\s*", loc) if x.strip()] or [loc],
                       remote=remote, posted=p, posted_kind=kind if p else "", posted_date_only=donly,
                       group=str(j.get("internal_job_id") or j.get("requisition_id") or j["id"])))
    total = (d.get("meta") or {}).get("total")
    return out, (total is None or len(out) >= int(total))


def fetch_lever(c):
    base = "https://api.eu.lever.co" if str(c.get("region", "")).lower() == "eu" else "https://api.lever.co"
    out, skip, page = [], 0, 100
    while True:
        d = http_json(f"{base}/v0/postings/{urllib.parse.quote(c['id'])}?mode=json&limit={page}&skip={skip}")
        if not isinstance(d, list):
            raise FetchError("unexpected Lever payload")
        for j in d:
            cat = j.get("categories") or {}
            locs = list(cat.get("allLocations") or []) or [cat.get("location") or ""]
            p, _ = parse_ts(j.get("createdAt"))
            wt = str(j.get("workplaceType") or "").lower()
            out.append(Job(id=str(j["id"]), title=_clean(j.get("text")), url=j.get("hostedUrl") or "",
                           locations=locs, countries=[j["country"]] if j.get("country") else [],
                           remote=True if wt == "remote" else (False if wt in ("onsite", "on-site") else None),
                           posted=p, posted_kind="published" if p else "", group=str(j["id"])))
        if len(d) < page:
            break
        skip += page
        if skip >= 10000:
            return out, False
    return out, True


def fetch_ashby(c):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(c['id'])}"
                  "?includeCompensation=false")
    if not isinstance(d, dict) or "jobs" not in d:
        raise FetchError("unexpected Ashby payload")
    out = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        locs, countries = [j.get("location") or ""], []

        def _addr(a):
            pa = ((a or {}).get("postalAddress") or {})
            if pa.get("addressCountry"):
                countries.append(pa["addressCountry"])
            return ", ".join(x for x in (pa.get("addressLocality"), pa.get("addressRegion"),
                                           pa.get("addressCountry")) if x)

        a0 = _addr(j.get("address"))
        if a0:
            locs.append(a0)
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location") or "")
            a = _addr(s.get("address"))
            if a:
                locs.append(a)
        p, _ = parse_ts(j.get("publishedAt"))
        remote = bool(j.get("isRemote")) or str(j.get("workplaceType") or "").lower() == "remote"
        out.append(Job(id=str(j["id"]), title=_clean(j.get("title")), url=j.get("jobUrl") or "",
                       locations=locs, countries=countries, remote=remote, posted=p,
                       posted_kind="published" if p else "", group=str(j["id"]),
                       extra={"team": j.get("team") or j.get("department") or ""}))
    return out, True


def fetch_smartrecruiters(c):
    cid = c["id"]
    out, offset, total = [], 0, None
    while True:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(cid)}/postings"
                      f"?limit=100&offset={offset}")
        content = d.get("content") or []
        if total is None:
            total = int(d.get("totalFound") or 0)
        for j in content:
            loc = j.get("location") or {}
            full = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                                     loc.get("country")) if x)
            p, _ = parse_ts(j.get("releasedDate"))
            out.append(Job(id=str(j["id"]), title=_clean(j.get("name")),
                           url=f"https://jobs.smartrecruiters.com/{cid}/{j['id']}",
                           locations=[full], countries=[str(loc.get("country") or "").upper()] if loc.get("country") else [],
                           remote=bool(loc.get("remote")), posted=p, posted_kind="published" if p else "",
                           group=str(j.get("refNumber") or j["id"])))
        offset += len(content)
        if not content or offset >= total or offset >= 10000:
            break
    return out, len(out) >= (total or 0)


def _fetch_workable_v3(slug):
    out, token = [], None
    for _ in range(50):
        body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            body["token"] = token
        d = http_json(f"https://apply.workable.com/api/v3/accounts/{urllib.parse.quote(slug)}/jobs",
                      method="POST", body=body)
        for j in d.get("results") or []:
            ls = j.get("locations") or ([j["location"]] if j.get("location") else [])
            locs = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in ls]
            p, donly = parse_ts(j.get("published"))
            sc = j.get("shortcode")
            out.append(Job(id=str(sc), title=_clean(j.get("title")), url=f"https://apply.workable.com/{slug}/j/{sc}/",
                           locations=locs, countries=[l["countryCode"] for l in ls if l.get("countryCode")],
                           remote=bool(j.get("remote")), posted=p, posted_kind="published" if p else "",
                           posted_date_only=donly, group=str(sc)))
        token = d.get("nextPage")
        if not token:
            break
    return out, True


_WORKABLE_LOCK = __import__("threading").Lock()  # Workable rate-limits per IP: one board at a time
_WORKABLE_STATE = {"blocked": False}


def fetch_workable(c):
    with _WORKABLE_LOCK:
        if _WORKABLE_STATE["blocked"]:
            raise FetchError("skipped: Workable rate-limited this run (will retry next run)")
        time.sleep(1.0)
        try:
            return _fetch_workable(c)
        except FetchError as e:
            if "HTTP 429" in str(e):
                _WORKABLE_STATE["blocked"] = True  # circuit breaker: don't burn the time budget
            raise


def _fetch_workable(c):
    slug = c["id"]
    try:
        d = http_json(f"https://apply.workable.com/api/v1/widget/accounts/{urllib.parse.quote(slug)}", retries=3)
    except FetchError as e:
        if "HTTP 404" in str(e) or "HTTP 429" in str(e):
            raise
        return _fetch_workable_v3(slug)
    if not isinstance(d, dict) or "jobs" not in d:
        return _fetch_workable_v3(slug)
    out = []
    for j in d["jobs"]:
        locs, countries = [], []
        for l in j.get("locations") or []:
            if l.get("hidden"):
                continue
            locs.append(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x))
            if l.get("countryCode"):
                countries.append(l["countryCode"])
        if not locs:
            locs = [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
        p, donly = parse_ts(j.get("published_on") or j.get("created_at"))
        sc = j.get("shortcode") or j.get("code")
        out.append(Job(id=str(sc), title=_clean(j.get("title")),
                       url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{sc}/",
                       locations=locs, countries=countries, remote=bool(j.get("telecommuting")),
                       posted=p, posted_kind="published" if p else "", posted_date_only=donly, group=str(sc)))
    return out, True


def fetch_bamboohr(c):
    sub = c["id"]
    d = http_json(f"https://{sub}.bamboohr.com/careers/list")
    res = d.get("result") if isinstance(d, dict) else None
    if res is None:
        raise FetchError("unexpected BambooHR payload")
    out = []
    for j in res:
        a = j.get("atsLocation") or {}
        l = j.get("location") or {}
        loc = ", ".join(x for x in (a.get("city") or l.get("city"), a.get("province") or a.get("state") or l.get("state"),
                                    a.get("country")) if x)
        remote = bool(j.get("isRemote")) or str(j.get("locationType") or "") in ("1", "remote", "Remote")
        out.append(Job(id=str(j["id"]), title=_clean(j.get("jobOpeningName")),
                       url=f"https://{sub}.bamboohr.com/careers/{j['id']}", locations=[loc],
                       countries=[a["country"]] if a.get("country") else [], remote=remote,
                       location_unknown=not loc, group=str(j["id"])))
    return out, True


def fetch_recruitee(c):
    sub = c["id"]
    d = http_json(f"https://{sub}.recruitee.com/api/offers/")
    out = []
    for j in (d or {}).get("offers", []):
        if j.get("status") and j["status"] != "published":
            continue
        locs = [l.get("name") and ", ".join(x for x in (l.get("city"), l.get("state"), l.get("country")) if x)
                for l in j.get("locations") or []] or [j.get("location") or ""]
        countries = [l.get("country_code") for l in j.get("locations") or [] if l.get("country_code")] or \
                    ([j["country_code"]] if j.get("country_code") else [])
        p, _ = parse_ts(j.get("published_at") or j.get("created_at"))
        out.append(Job(id=str(j["id"]), title=_clean(j.get("title")), url=j.get("careers_url") or "",
                       locations=[x for x in locs if x], countries=countries, remote=bool(j.get("remote")),
                       posted=p, posted_kind="published" if p else "", group=str(j["id"])))
    return out, True


def fetch_breezy(c):
    sub = c["id"]
    d = http_json(f"https://{sub}.breezy.hr/json")
    if not isinstance(d, list):
        raise FetchError("unexpected Breezy payload")
    out = []
    for j in d:
        ls = j.get("locations") or [j.get("location") or {}]
        locs, countries, remote = [], [], False
        for l in ls:
            locs.append(l.get("name") or "")
            cc = (l.get("country") or {}).get("id")
            if cc:
                countries.append(cc)
            remote = remote or bool(l.get("is_remote"))
        p, _ = parse_ts(j.get("published_date"))
        out.append(Job(id=str(j["id"]), title=_clean(j.get("name")), url=j.get("url") or "",
                       locations=locs, countries=countries, remote=remote, posted=p,
                       posted_kind="published" if p else "", group=str(j["id"])))
    return out, True


def fetch_rippling(c):
    slug = c["id"]
    jobs, page, pages = {}, 0, 1
    while page < pages and page < 100:
        d = http_json(f"https://ats.rippling.com/api/v2/board/{urllib.parse.quote(slug)}/jobs?page={page}")
        if not isinstance(d, dict) or "items" not in d:
            raise FetchError("unexpected Rippling payload")
        pages = int(d.get("totalPages") or 1)
        for it in d["items"]:  # one item per (job, location): merge by id
            j = jobs.get(it["id"])
            if not j:
                j = jobs[it["id"]] = Job(id=str(it["id"]), title=_clean(re.sub(r"\s*\[\d+\]\s*$", "", it.get("name") or "")),
                                         url=it.get("url") or "", group=str(it["id"]))
            for l in it.get("locations") or []:
                j.locations.append(l.get("name") or "")
                if l.get("countryCode"):
                    j.countries.append(l["countryCode"])
                if str(l.get("workplaceType") or "").upper() == "REMOTE":
                    j.remote = True
        page += 1
    return list(jobs.values()), page >= pages


def fetch_jazzhr(c):
    """JazzHR has no keyless JSON feed; parse the public applytojob.com list page."""
    sub = c["id"]
    url = f"https://{sub}.applytojob.com/apply"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                page = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise FetchError(f"HTTP {e.code} from {url}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 2:
                raise FetchError(f"{e} from {url}") from None
        time.sleep(3 * (attempt + 1))
    out = []
    for m in re.finditer(r'<a href="(https://[\w-]+\.applytojob\.com/apply/([A-Za-z0-9]+)/[^"]*)"[^>]*>\s*(.*?)\s*</a>'
                         r'(.*?)(?=<li class="list-group-item"|</ul>\s*</div>|$)', page, re.S):
        link, code, title, rest = m.groups()
        loc = re.search(r"fa-map-marker'?\"?></i>\s*([^<]+)", rest)
        out.append(Job(id=code, title=_clean(html.unescape(re.sub("<[^>]+>", "", title))), url=link,
                       locations=[_clean(html.unescape(loc.group(1)))] if loc else [],
                       location_unknown=not loc, group=code))
    if not out and "applytojob" not in page:
        raise FetchError("JazzHR account not found (page redirects away from applytojob.com)")
    return out, True


def fetch_pinpoint(c):
    host = re.sub(r"^https?://", "", c["id"]).strip("/")
    d = http_json(f"https://{host}/postings.json")
    if not isinstance(d, dict) or "data" not in d:
        raise FetchError("unexpected Pinpoint payload")
    out = []
    for j in d["data"]:
        l = j.get("location") or {}
        loc = ", ".join(x for x in (l.get("city"), l.get("name")) if x)
        out.append(Job(id=str(j["id"]), title=_clean(j.get("title")), url=j.get("url") or f"https://{host}{j.get('path', '')}",
                       locations=[loc], remote=str(j.get("workplace_type") or "").lower() == "remote",
                       location_unknown=not loc, group=str((j.get("job") or {}).get("id") or j["id"])))
    return out, True


def fetch_teamtailor(c):
    import xml.etree.ElementTree as ET
    sub = c["id"]
    host = sub if "." in sub else f"{sub}.teamtailor.com"
    url = f"https://{host}/jobs.rss"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            root = ET.fromstring(r.read())
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
        raise FetchError(f"{e} from {url}") from None
    out = []
    for it in root.iter("item"):
        locs = []
        for el in it.iter():
            if el.tag.endswith("}location"):
                parts = [(x.text or "").strip() for x in el if (x.text or "").strip() and not x.tag.endswith("}address")]
                locs.append(", ".join(dict.fromkeys(parts)))
        p, _ = parse_ts(email_date(it.findtext("pubDate")))
        guid = it.findtext("guid") or it.findtext("link")
        out.append(Job(id=str(guid), title=_clean(it.findtext("title")), url=it.findtext("link") or "",
                       locations=[x for x in locs if x], location_unknown=not any(locs),
                       remote=(it.findtext("remoteStatus") or "none").lower() in ("fully", "remote", "temporary"),
                       posted=p, posted_kind="published" if p else "", group=str(guid)))
    return out, True


def fetch_jibe(c, *, settings=None):
    """Jibe (Google-hosted career sites in front of iCIMS etc.), e.g. careers.amd.com, careers.rivian.com.
    Country is filtered server-side (`jibe_country`, default settings.workday_country / Canada)."""
    host = re.sub(r"^https?://", "", c["id"]).strip("/")
    country = c.get("jibe_country", (settings or {}).get("workday_country", "Canada"))
    tmpl = c.get("url_template", "https://{host}/careers-home/jobs/{slug}")
    out, page, total = [], 1, None
    while True:
        q = {"page": page, "limit": 100}
        if country:
            q["country"] = country
        d = http_json(f"https://{host}/api/jobs?{urllib.parse.urlencode(q)}")
        if not isinstance(d, dict) or "jobs" not in d:
            raise FetchError("unexpected Jibe payload")
        if total is None:
            total = int(d.get("totalCount") or 0)
        for x in d["jobs"]:
            j = x.get("data") or {}
            p, _ = parse_ts(str(j.get("posted_date") or "").replace("+0000", "+00:00"))
            slug = j.get("slug") or j.get("req_id")
            out.append(Job(id=str(j.get("req_id") or slug), title=_clean(j.get("title")),
                           url=tmpl.format(host=host, slug=slug),
                           locations=[j.get("full_location") or j.get("location_name") or ""],
                           countries=[j["country_code"]] if j.get("country_code") else [],
                           posted=p, posted_kind="published" if p else "", group=str(j.get("req_id")),
                           extra={"facet_country": country}))
        if not d["jobs"] or len(out) >= total or page >= 50:
            break
        page += 1
    return out, len(out) >= (total or 0)


def fetch_oracle(c):
    """Oracle Recruiting Cloud (HCM) candidate-experience sites. id = 'host/SITE_NUMBER'."""
    raw = re.sub(r"^https?://", "", c["id"]).strip("/")
    host, _, site = raw.partition("/")
    site = site.split("/")[-1] or "CX_1"
    out, offset, total, limit = [], 0, None, 100
    while True:
        finder = f"findReqs;siteNumber={site},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
        d = http_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                      f"&expand=requisitionList.secondaryLocations&finder={urllib.parse.quote(finder, safe=';=,')}")
        items = d.get("items") or []
        if not items:
            raise FetchError("unexpected Oracle payload")
        it = items[0]
        if total is None:
            total = int(it.get("TotalJobsCount") or 0)
        reqs = it.get("requisitionList") or []
        for r in reqs:
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
            ctry = [r["PrimaryLocationCountry"]] if r.get("PrimaryLocationCountry") else []
            ctry += [s.get("CountryCode") for s in r.get("secondaryLocations") or [] if s.get("CountryCode")]
            p, donly = parse_ts(r.get("PostedDate"))
            out.append(Job(id=str(r["Id"]), title=_clean(r.get("Title")),
                           url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                           locations=locs, countries=ctry,
                           remote=bool(re.search(r"remote", str(r.get("WorkplaceType") or ""), re.I)) or None,
                           posted=p, posted_kind="published" if p else "", posted_date_only=donly, group=str(r["Id"])))
        offset += len(reqs)
        if not reqs or offset >= total or offset >= 5000:
            break
    return out, len(out) >= 0.97 * (total or 0)  # total can drift while paging


def fetch_jobvite(c):
    slug = c["id"]
    out, seen = [], set()
    for p in range(0, 30):
        url = f"https://jobs.jobvite.com/{slug}/jobs" + (f"?p={p}" if p else "")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                page = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise FetchError(f"HTTP {e.code} from {url}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"{e} from {url}") from None
        added = 0
        for m in re.finditer(r'<td class="jv-job-list-name">\s*<a href="(/[^"]+/job/([A-Za-z0-9]+))"[^>]*>(.*?)</a>\s*</td>'
                             r'\s*<td class="jv-job-list-location">(.*?)</td>', page, re.S):
            path, jid, title, loc = m.groups()
            if jid in seen:
                continue
            seen.add(jid)
            added += 1
            loc = _clean(html.unescape(re.sub("<[^>]+>", " ", loc)))
            out.append(Job(id=jid, title=_clean(html.unescape(re.sub("<[^>]+>", "", title))),
                           url=f"https://jobs.jobvite.com{path}", locations=[loc] if loc else [],
                           location_unknown=not loc, remote=bool(re.search(r"remote", loc, re.I)) or None, group=jid))
        if not added or f"?p={p + 1}" not in page:
            break
    if not out:
        raise FetchError("no jobs parsed from Jobvite page")
    return out, True


def email_date(s):
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).isoformat()
    except Exception:
        return None


# ---- Workday ---------------------------------------------------------------
_LOCALE_RX = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def _workday_parts(c):
    """config id: 'host/site' (locale segment tolerated) e.g. nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"""
    raw = re.sub(r"^https?://", "", c["id"]).strip("/")
    host, _, rest = raw.partition("/")
    segs = [s for s in rest.split("/") if s and not _LOCALE_RX.match(s)]
    if not segs:
        raise FetchError("workday id must be 'host/site'")
    site = segs[0]
    tenant = c.get("tenant") or host.split(".")[0]
    return host, tenant, site


def _wd_posted(s, ref):
    s = (s or "").lower()
    if not s:
        return None
    if "today" in s:
        return ref
    if "yesterday" in s:
        return ref - dt.timedelta(days=1)
    m = re.search(r"(\d+)(\+?)\s*day", s)
    if m and not m.group(2):
        return ref - dt.timedelta(days=int(m.group(1)))
    return None


def _wd_find_country_facet(facets, country):
    """Search (possibly nested) facets for a value whose descriptor is the country."""
    want = country.strip().lower()
    stack = [(f, None) for f in facets or []]
    while stack:
        f, parent_param = stack.pop()
        param = f.get("facetParameter") or parent_param
        for v in f.get("values") or []:
            if "values" in v:  # nested group
                stack.append((v, param))
                continue
            if str(v.get("descriptor", "")).strip().lower() == want and param and v.get("id"):
                # prefer country-level facets over site-level ones
                return param, v["id"]
    return None


_CA_SITE_RX = re.compile(
    r"canada|\bCAN\b|\b(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE)\b|ontario|qu[eé]bec|british columbia|alberta|manitoba|"
    r"saskatchewan|nova scotia|new brunswick|toronto|markham|montr[eé]al|vancouver|burnaby|coquitlam|surrey|calgary|"
    r"edmonton|ottawa|kanata|waterloo|kitchener|mississauga|oakville|peterborough|longueuil|laval|winnipeg|halifax|"
    r"kelowna|saskatoon|regina|guelph|brampton|vaughan|oshawa|sudbury|kingston, on|gatineau|sherbrooke", re.I)
_NON_CA_SITE_RX = re.compile(r"\bUSA?\b|united states|\b(WA|NY|CA|TX|OR|IL|MA|PA|OH|GA|FL)\b(?!\s*\d)", 0)


def _wd_canadian_sites(facets):
    """Fallback when a Workday board has no country facet: {param: [ids]} for Canadian site values."""
    out = {}
    stack = [(f, None) for f in facets or []]
    while stack:
        f, parent = stack.pop()
        param = f.get("facetParameter") or parent
        for v in f.get("values") or []:
            if "values" in v:
                stack.append((v, param))
                continue
            d = str(v.get("descriptor") or "")
            if param and v.get("id") and "location" in param.lower() and _CA_SITE_RX.search(d) \
                    and not _NON_CA_SITE_RX.search(d.replace(" CA ", " CAN ")):
                out.setdefault(param, []).append(v["id"])
    # prefer the most specific single parameter (site list) to avoid AND-ing two facet groups
    if not out:
        return {}
    param = max(out, key=lambda p: len(out[p]))
    return {param: out[param]}


def fetch_workday(c, *, settings=None):
    host, tenant, site = _workday_parts(c)
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    hdr = {"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"}
    country = c.get("workday_country", (settings or {}).get("workday_country", "Canada"))
    facets = dict(c.get("workday_facets") or {})
    page, cap = 20, int(c.get("max_jobs", 2000))

    def post(offset, f):
        return http_json(api, method="POST", headers=hdr,
                         body={"appliedFacets": f, "limit": page, "offset": offset, "searchText": c.get("search", "")})

    first = post(0, facets)
    if country and not facets:
        hit = _wd_find_country_facet(first.get("facets"), country)
        if hit:
            facets = {hit[0]: [hit[1]]}
        elif country.lower() == "canada":
            # no country facet: select every site-level location that is clearly Canadian
            sites = _wd_canadian_sites(first.get("facets"))
            if sites:
                facets = sites
        if facets:
            first = post(0, facets)
        else:
            log(f"  [workday] {c['name']}: no '{country}' facet found, scanning whole board")
    total = int(first.get("total") or 0)
    pages = [first]
    if total > page:
        offsets = list(range(page, min(total, cap), page))
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            pages += list(ex.map(lambda o: post(o, facets), offsets))
    ref = now_utc().replace(hour=12, minute=0, second=0, microsecond=0)
    out, seen = [], set()
    for d in pages:
        for p in d.get("jobPostings") or []:
            ep = p.get("externalPath")
            if not ep or ep in seen:
                continue
            seen.add(ep)
            lt = _clean(p.get("locationsText"))
            unknown = bool(re.fullmatch(r"\d+ locations?", lt, re.I)) or not lt
            posted = _wd_posted(p.get("postedOn"), ref)
            req = (p.get("bulletFields") or [""])[0]
            out.append(Job(id=ep, title=_clean(p.get("title")), url=f"https://{host}/en-US/{site}{ep}",
                           locations=[] if unknown else [lt], location_unknown=unknown,
                           remote=True if re.search(r"remote", lt + ep, re.I) else None,
                           posted=posted, posted_kind="approx" if posted else "", posted_date_only=True,
                           group=str(req or ep), extra={"wd": (host, tenant, site, ep),
                                                        "facet_country": country if facets else ""}))
    complete = total == 0 or len(out) >= 0.97 * min(total, cap)
    if total > cap:
        log(f"  [workday] {c['name']}: {total} jobs > cap {cap}; removals disabled for this board")
        complete = False
    return out, complete


def enrich_workday(job: Job):
    """Fetch detail for a NEW Workday job: exact locations + start (posted) date."""
    host, tenant, site, ep = job.extra["wd"]
    d = http_json(f"https://{host}/wday/cxs/{tenant}/{site}{ep}", retries=2,
                  headers={"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"})
    info = d.get("jobPostingInfo") or {}
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    locs = [_clean(x) for x in locs if x]
    if locs:
        job.locations, job.location_unknown = locs, False
    ctry = (info.get("country") or {}).get("descriptor")
    if ctry:
        job.countries = [ctry]
    if re.search(r"remote", str(info.get("remoteType") or "") + " ".join(locs), re.I):
        job.remote = True
    p, donly = parse_ts(info.get("startDate"))
    if p:
        job.posted, job.posted_kind, job.posted_date_only = p, "published", donly
    if info.get("externalUrl"):
        job.url = info["externalUrl"]


ADAPTERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable, "bamboohr": fetch_bamboohr,
    "recruitee": fetch_recruitee, "breezy": fetch_breezy, "workday": fetch_workday,
    "rippling": fetch_rippling, "jazzhr": fetch_jazzhr, "pinpoint": fetch_pinpoint, "teamtailor": fetch_teamtailor,
    "jibe": fetch_jibe, "jobvite": fetch_jobvite, "oracle": fetch_oracle,
}
NEEDS_SETTINGS = {fetch_workday, fetch_jibe}
ENRICHERS = {"workday": enrich_workday}


# --------------------------------------------------------------------------- filters
class Filters:
    def __init__(self, f: dict):
        def rx(lst, flags=re.I):
            lst = [p for p in (lst or []) if p]
            return re.compile("|".join(f"(?:{p})" for p in lst), flags) if lst else None

        self.t_inc = rx(f.get("title_include"))
        self.t_exc = rx(f.get("title_exclude"))
        self.l_inc = rx(f.get("location_include"))
        self.l_inc_cs = rx(f.get("location_include_cs"), 0)
        self.l_exc = rx(f.get("location_exclude"))
        self.l_exc_cs = rx(f.get("location_exclude_cs"), 0)
        self.target_countries = {x.upper() for x in f.get("country_codes", ["CA"])} | \
                                {x.lower() for x in f.get("country_names", ["Canada"])}
        self.remote_unspecified = bool(f.get("include_remote_unspecified", True))
        self.unknown = bool(f.get("include_unknown_location", True))

    def title_ok(self, title, company):
        mode = company.get("titles", "filtered")
        if mode == "all":
            return True
        t = unicodedata.normalize("NFKC", title)
        if self.t_exc and self.t_exc.search(t):
            return False
        return bool(self.t_inc.search(t)) if self.t_inc else True

    def location_ok(self, job: Job, company):
        """-> (ok, note)"""
        if company.get("all_locations") or job.extra.get("facet_country"):
            return True, ""  # board already restricted server-side (Workday country facet)
        blob = " | ".join(job.locations + [str(x) for x in job.countries])
        if (self.l_inc and self.l_inc.search(blob)) or (self.l_inc_cs and self.l_inc_cs.search(blob)):
            return True, ""
        if any(str(x).upper() in self.target_countries or str(x).lower() in self.target_countries
               for x in job.countries):
            return True, ""
        names_other = (self.l_exc and self.l_exc.search(blob)) or (self.l_exc_cs and self.l_exc_cs.search(blob))
        remoteish = job.remote or re.search(r"\b(remote|anywhere|worldwide|global|distributed)\b", blob, re.I)
        if remoteish and self.remote_unspecified and not names_other and not job.countries:
            return True, "remote, region not stated"
        if (job.location_unknown or not blob.strip()) and self.unknown:
            return True, "location not listed"
        return False, ""


# --------------------------------------------------------------------------- state
def norm_title(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b(r|req|jr|job)[-_ ]?\d+\b", " ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(job: Job):
    loc = norm_title(job.location) if not job.location_unknown else ""
    return hashlib.sha1(f"{norm_title(job.title)}|{loc}".encode()).hexdigest()[:12]


def company_key(c):
    return f"{re.sub(r'[^a-z0-9]+', '-', c['name'].lower()).strip('-')}@{c['ats']}:{c['id'].lower()}"


def load_state(path: Path):
    if path.exists():
        s = json.loads(path.read_text() or "{}")
        if s.get("v") != STATE_VERSION:
            raise SystemExit(f"state version mismatch in {path}")
        s.setdefault("companies", {})
        return s
    return {"v": STATE_VERSION, "companies": {}, "heartbeat": ""}


def save_state(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def iso_week(t: dt.datetime):
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


# --------------------------------------------------------------------------- core
@dataclass
class Alert:
    company: str
    job: Job
    tag: str  # new | repost | reopened | baseline-recent
    note: str
    detected: dt.datetime
    order: int


def load_config(path: Path):
    cfg = tomllib.loads(path.read_text())
    seen, cos = set(), []
    for i, c in enumerate(cfg.get("companies", [])):
        if c.get("enabled", True) is False:
            continue
        for k in ("name", "ats", "id"):
            if not c.get(k):
                raise SystemExit(f"company #{i} missing '{k}': {c}")
        c["ats"] = c["ats"].lower()
        if c["ats"] not in ADAPTERS:
            raise SystemExit(f"{c['name']}: unknown ats '{c['ats']}' (supported: {', '.join(ADAPTERS)})")
        c["_key"], c["_order"] = company_key(c), i
        if c["_key"] in seen:
            raise SystemExit(f"duplicate company entry: {c['name']}")
        seen.add(c["_key"])
        cos.append(c)
    cfg["companies"] = cos
    cfg.setdefault("settings", {})
    cfg.setdefault("filters", {})
    return cfg


def fetch_all(companies, settings):
    results = {}
    workers = int(settings.get("max_workers", 12))
    budget = float(settings.get("fetch_budget_seconds", 300))

    def one(c):
        t0 = time.monotonic()
        fn = ADAPTERS[c["ats"]]
        jobs, complete = fn(c, settings=settings) if fn in NEEDS_SETTINGS else fn(c)
        uniq = {}
        for j in jobs:
            if j.id and j.title:
                uniq.setdefault(j.id, j)
        return list(uniq.values()), complete, time.monotonic() - t0

    ex = cf.ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(one, c): c for c in companies}
    done, pending = cf.wait(futs, timeout=budget)
    for f in done:
        c = futs[f]
        try:
            results[c["_key"]] = f.result()
        except Exception as e:  # noqa: BLE001
            results[c["_key"]] = e
    for f in pending:
        results[futs[f]["_key"]] = FetchError(f"timed out after {budget:.0f}s")
    ex.shutdown(wait=False, cancel_futures=True)
    return results


def process(cfg, state, results, now: dt.datetime):
    """Pure-ish diff step. Mutates state; returns (alerts, baselined, warnings)."""
    st = cfg["settings"]
    flt = Filters(cfg["filters"])
    gone_ttl = dt.timedelta(days=float(st.get("gone_ttl_days", 45)))
    flap = dt.timedelta(hours=float(st.get("flap_grace_hours", 72)))
    recent = dt.timedelta(hours=float(st.get("baseline_recent_hours", 24)))
    repost_window = dt.timedelta(days=float(st.get("repost_window_days", 30)))
    fail_threshold = int(st.get("fail_warn_after_runs", 6))
    notify_reposts = bool(st.get("notify_reposts", True))
    ts = int(now.timestamp())
    alerts, baselined, warnings, to_enrich = [], [], [], []

    for c in cfg["companies"]:
        key, res = c["_key"], results.get(c["_key"])
        cs = state["companies"].setdefault(key, {"name": c["name"]})
        cs["name"] = c["name"]
        if res is None:
            continue
        if not isinstance(res, Exception):
            jobs, complete, _secs = res
            if not jobs and len(cs.get("jobs") or {}) >= 3:
                res = FetchError(f"board returned 0 jobs (had {len(cs['jobs'])}); treating as outage")
        if isinstance(res, Exception):
            cs["fails"] = int(cs.get("fails", 0)) + 1
            cs["err"] = str(res)[:300]
            first_time = "since" not in cs
            if not cs.get("warned") and (first_time or cs["fails"] >= fail_threshold):
                warnings.append(f"{c['name']} ({c['ats']}:{c['id']}): {'never fetched' if first_time else str(cs['fails']) + ' failed runs in a row'} — {cs['err']}")
                cs["warned"] = True
            continue

        if cs.get("warned"):
            warnings.append(f"{c['name']}: back online ({len(jobs)} open roles)")
        for k in ("fails", "err", "warned"):
            cs.pop(k, None)

        cur = {j.id: j for j in jobs}
        if "since" not in cs:  # ---------- baseline
            cs["since"] = now.isoformat(timespec="seconds")
            cs["jobs"] = {jid: [ts, fingerprint(j)] for jid, j in cur.items()}
            cs["gone"] = {}
            baselined.append((c["name"], len(cur)))
            for j in cur.values():
                if j.posted and j.posted_kind == "published" and not j.posted_date_only and now - j.posted <= recent:
                    to_enrich.append((c, j, "baseline-recent"))
            continue

        known, gone = cs.setdefault("jobs", {}), cs.setdefault("gone", {})
        # repost = new ID whose title+location matches a posting CLOSED within the window
        fps_recent = {v[1] for v in gone.values() if ts - v[2] <= repost_window.total_seconds()}
        for jid, j in cur.items():
            if jid in known:
                continue
            fp = fingerprint(j)
            if jid in gone:
                first_seen, _fp, gone_at = gone.pop(jid)
                known[jid] = [first_seen, fp]
                if ts - gone_at <= flap.total_seconds():
                    continue  # flapped: silently restored
                to_enrich.append((c, j, "reopened"))
                continue
            known[jid] = [ts, fp]
            tag = "repost" if fp in fps_recent else "new"
            if tag == "repost" and not notify_reposts:
                continue
            to_enrich.append((c, j, tag))

        removed = [jid for jid in known if jid not in cur]
        shrink = len(known) >= 10 and len(cur) < 0.5 * len(known)
        if removed and complete and not shrink:
            for jid in removed:
                first_seen, fp = known.pop(jid)
                gone[jid] = [first_seen, fp, ts]
        elif removed:
            log(f"  {c['name']}: {len(removed)} missing but fetch looks partial/shrunk; keeping them")
        for jid in [k for k, v in gone.items() if ts - v[2] > gone_ttl.total_seconds()]:
            del gone[jid]

    # enrich (only new candidates, cheap) then filter
    def _enrich(item):
        c, j, _ = item
        fn = ENRICHERS.get(c["ats"])
        if fn:
            try:
                fn(j)
            except Exception as e:  # noqa: BLE001
                log(f"  enrich failed for {c['name']} {j.id}: {e}")
        return item

    if to_enrich:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            to_enrich = list(ex.map(_enrich, to_enrich))
    for c, j, tag in to_enrich:
        if tag == "baseline-recent" and not (j.posted and now - j.posted <= recent):
            continue
        if not flt.title_ok(j.title, c):
            continue
        ok, note = flt.location_ok(j, c)
        if not ok:
            continue
        alerts.append(Alert(c["name"], j, tag, note, now, c["_order"]))

    wk = iso_week(now)
    if state.get("heartbeat") != wk:  # guarantees >=1 commit/week (keeps GH schedule alive)
        state["heartbeat"] = wk
    return alerts, baselined, warnings


# --------------------------------------------------------------------------- email
def _fmt_local(t: dt.datetime | None, date_only=False):
    if not t:
        return ""
    if date_only:
        return t.strftime("%b %d")
    return t.astimezone(LOCAL_TZ).strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _ago(t, now, date_only):
    if not t:
        return ""
    if date_only:
        days = (now.date() - t.date()).days
        return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days}d ago")
    s = (now - t).total_seconds()
    if s < 0:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{s / 3600:.1f} h ago"
    return f"{int(s // 86400)}d ago"


def group_alerts(alerts):
    groups = {}
    for a in sorted(alerts, key=lambda a: (a.order, norm_title(a.job.title))):
        k = (a.company, norm_title(a.job.title))
        groups.setdefault(k, []).append(a)
    return list(groups.values())


TAG_LABEL = {"new": "", "repost": "reposted", "reopened": "reopened", "baseline-recent": "posted before tracking began"}


def build_email(alerts, baselined, warnings, now):
    groups = group_alerts(alerts)
    n = len(groups)
    if n:
        a0 = groups[0][0]
        subject = f"🆕 {a0.company}: {a0.job.title}" + (f" (+{n - 1} more)" if n > 1 else "")
    elif baselined:
        subject = f"Job watcher: now tracking {len(baselined)} new board(s)"
    else:
        subject = f"Job watcher: {len(warnings)} board status change(s)"
    det = _fmt_local(now)
    text, rows = [], []
    for g in groups:
        a = g[0]
        locs = "; ".join(dict.fromkeys(x.job.display_location() or x.note or "—" for x in g))
        posted = a.job.posted
        p_txt = f"{_fmt_local(posted, a.job.posted_date_only)} ({_ago(posted, now, a.job.posted_date_only)})" if posted else "not published by ATS"
        if a.job.posted_kind in ("updated", "approx"):
            p_txt += f" [{a.job.posted_kind}]"
        tag = TAG_LABEL.get(a.tag, a.tag)
        links = [x.job.url for x in g]
        text.append(f"• {a.company} — {a.job.title}" + (f"  [{tag}]" if tag else "") +
                    f"\n  {locs}" + (f" ({a.note})" if a.note and a.job.location else "") +
                    f"\n  Posted: {p_txt}\n  " + "\n  ".join(links))
        link_html = " · ".join(f'<a href="{html.escape(u)}">{"Apply" if i == 0 else f"#{i + 1}"}</a>'
                               for i, u in enumerate(links))
        badge = f' <span style="background:#fde68a;border-radius:4px;padding:1px 6px;font-size:11px">{html.escape(tag)}</span>' if tag else ""
        rows.append(
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee"><b>{html.escape(a.company)}</b></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee"><a href="{html.escape(links[0])}" style="color:#1d4ed8;font-weight:600">{html.escape(a.job.title)}</a>{badge}'
            f'<div style="color:#555;font-size:12px">{html.escape(locs)}{" · " + html.escape(a.note) if a.note and a.job.location else ""}</div></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;font-size:12px;color:#333;white-space:nowrap">{html.escape(p_txt)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;white-space:nowrap">{link_html}</td></tr>')
    extra_html, extra_txt = "", []
    if warnings:
        extra_txt.append("Board status:\n" + "\n".join(f"  ! {w}" for w in warnings))
        extra_html += "<h4 style='margin:18px 0 6px'>Board status</h4><ul>" + "".join(
            f"<li>{html.escape(w)}</li>" for w in warnings) + "</ul>"
    if baselined:
        tot = sum(k for _, k in baselined)
        extra_txt.append(f"Now tracking ({tot} roles already open, not alerted):\n" +
                         "\n".join(f"  - {nm}: {k}" for nm, k in baselined))
        extra_html += (f"<h4 style='margin:18px 0 6px'>Now tracking</h4><p style='font-size:12px;color:#555'>"
                       f"Already-open roles are recorded silently; only postings that appear from now on are emailed.</p><p style='font-size:13px'>"
                       + ", ".join(f"{html.escape(nm)} ({k})" for nm, k in baselined) + "</p>")
    body_txt = (f"{n} new posting(s) detected {det} (Pacific).\n\n" if n else "") + "\n\n".join(text) + \
               ("\n\n" if text and extra_txt else "") + "\n\n".join(extra_txt)
    table = (f'<table style="border-collapse:collapse;width:100%;font-size:14px">'
             f'<tr style="text-align:left;background:#f6f6f6"><th style="padding:8px">Company</th><th style="padding:8px">Role</th>'
             f'<th style="padding:8px">Posted (per ATS)</th><th style="padding:8px">Link</th></tr>{"".join(rows)}</table>') if rows else ""
    body_html = (f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:900px">'
                 + (f'<p style="margin:0 0 10px">{n} new posting(s) detected <b>{html.escape(det)}</b> Pacific.</p>' if n else "")
                 + table + extra_html + "</div>")
    return subject, body_txt, body_html


def send_email(subject, text, html_body, cfg):
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("ALERT_TO", "").strip() or cfg.get("email", {}).get("to", "")
    if not (user and pw and to):
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD / ALERT_TO not set")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Job Watcher <{user}>", to
    msg["Message-ID"] = make_msgid(domain="job-watcher.local")
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx, last = ssl.create_default_context(), None
    for attempt in range(3):
        try:
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                    s.login(user, pw)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.starttls(context=ctx)
                    s.login(user, pw)
                    s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------- modes
def append_history(path: Path, alerts):
    with path.open("a", encoding="utf-8") as f:
        for a in alerts:
            f.write(json.dumps({"detected": a.detected.isoformat(timespec="seconds"), "company": a.company,
                                "title": a.job.title, "url": a.job.url, "location": a.job.location,
                                "posted": a.job.posted.isoformat() if a.job.posted else None,
                                "posted_kind": a.job.posted_kind, "tag": a.tag}, ensure_ascii=False) + "\n")


def run(cfg, state_path: Path, dry_run=False):
    state = load_state(state_path)
    t0 = time.monotonic()
    results = fetch_all(cfg["companies"], cfg["settings"])
    now = now_utc()
    for c in cfg["companies"]:
        r = results.get(c["_key"])
        log(f"  {'FAIL' if isinstance(r, Exception) else ' ok '} {c['name']:<28} "
            + (str(r)[:120] if isinstance(r, Exception) else f"{len(r[0]):>5} jobs  {r[2]:.1f}s" + ("" if r[1] else "  (partial)")))
    alerts, baselined, warnings = process(cfg, state, results, now)
    log(f"fetched {len(results)} boards in {time.monotonic() - t0:.1f}s; "
        f"{len(alerts)} alert(s), {len(baselined)} baselined, {len(warnings)} warning(s)")
    for a in alerts:
        log(f"  NEW [{a.tag}] {a.company}: {a.job.title} | {a.job.location} | {a.job.url}")
    if alerts or baselined or warnings:
        subject, txt, htm = build_email(alerts, baselined, warnings, now)
        if dry_run:
            log("--- dry run: email not sent ---\n" + subject + "\n" + txt)
        else:
            send_email(subject, txt, htm, cfg)  # raises -> state not saved -> retried next run
            log(f"email sent: {subject}")
    if not dry_run:
        save_state(state_path, state)
        if alerts:
            append_history(state_path.with_name("history.jsonl"), alerts)
    return alerts


def validate(cfg):
    flt = Filters(cfg["filters"])
    results = fetch_all(cfg["companies"], cfg["settings"])
    bad = 0
    print(f"{'company':<30}{'ats':<16}{'jobs':>6}{'match':>7}  newest / error")
    for c in cfg["companies"]:
        r = results.get(c["_key"])
        if isinstance(r, Exception) or r is None:
            bad += 1
            print(f"{c['name']:<30}{c['ats']:<16}{'ERR':>6}{'':>7}  {str(r)[:110]}")
            continue
        jobs, complete, secs = r
        match = [j for j in jobs if flt.title_ok(j.title, c) and flt.location_ok(j, c)[0]]
        newest = max((j for j in jobs if j.posted), key=lambda j: j.posted, default=None)
        print(f"{c['name']:<30}{c['ats']:<16}{len(jobs):>6}{len(match):>7}  "
              + (f"{newest.posted.date()} {newest.title[:60]}" if newest else "(no dates)")
              + ("" if complete else "  [partial]") + ("  [EMPTY]" if not jobs else ""))
    print(f"\n{len(cfg['companies']) - bad}/{len(cfg['companies'])} boards OK")
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--mode", choices=["run", "validate", "test-email"], default="run")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--state", default=str(ROOT / "state" / "seen.json"))
    ap.add_argument("--dry-run", action="store_true", help="fetch + diff + print email, but don't send or save")
    ap.add_argument("--only", help="comma-separated company names to restrict to (debugging)")
    a = ap.parse_args(argv)
    cfg = load_config(Path(a.config))
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        cfg["companies"] = [c for c in cfg["companies"] if c["name"].lower() in want]
    if a.mode == "validate":
        return 1 if validate(cfg) else 0
    if a.mode == "test-email":
        j = Job(id="test", title="Test Posting — Software Engineer", url="https://example.com/job",
                locations=["Vancouver, BC, Canada"], posted=now_utc(), posted_kind="published")
        s, t, h = build_email([Alert("Job Watcher", j, "new", "", now_utc(), 0)], [], [], now_utc())
        send_email("[test] " + s, t, h, cfg)
        log("test email sent")
        return 0
    try:
        run(cfg, Path(a.state), dry_run=a.dry_run)
    except smtplib.SMTPAuthenticationError as e:
        log(f"SMTP auth failed (check app password): {e}")
        return 2
    except RuntimeError as e:
        log(f"error: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
