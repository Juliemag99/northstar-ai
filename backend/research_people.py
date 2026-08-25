"""Campaign-aware public people discovery for Research Company.

No LinkedIn scraping. No invented emails/phones. No paid providers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin, urlparse, unquote

from research_providers import (
    EVIDENCE_SUPPORTED,
    EVIDENCE_VERIFIED,
    NormalizedFinding,
    ResearchedPage,
    _blank,
    _fetch,
    _now_iso,
    _page_label,
    _page_title,
    _strip_html,
)

MAX_PEOPLE_QUERIES = 10
MAX_LINKEDIN_QUERIES = 4
MAX_PEOPLE_PAGES_FETCH = 8
MAX_CONTACTS_TOTAL = 25

BLOCKED_PEOPLE_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "spokeo.com", "whitepages.com",
    "fastpeoplesearch.com", "beenverified.com", "intelius.com",
    "rocketreach.co", "zoominfo.com", "apollo.io", "lusha.com", "hunter.io",
    "nuwber.com", "thatsthem.com", "peoplefinders.com", "radaris.com",
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com",
    "talent.com", "lever.co", "greenhouse.io", "workday.com",
})

LOW_QUALITY_PEOPLE_HOSTS = frozenset({
    "comparably.com", "theorg.com", "theofficialboard.com", "rocketreach.co",
    "zoominfo.com", "apollo.io",
})

JOB_PATH_MARKERS = (
    "/job/", "/jobs/", "/careers/", "/career/", "job-titles", "/salary",
)

PEOPLE_PATH_HINTS = (
    "leadership", "leadership-team", "our-team", "our-leaders", "executive",
    "executives", "management", "management-team", "board-of-directors",
    "investor", "investors", "corporate", "about/leadership",
    "about-us/leadership", "news", "press", "media", "team",
)

FALLBACK_PERSONAS = (
    "purchasing manager", "procurement manager", "buyer",
    "supply chain manager", "operations manager", "engineering manager",
)

TITLE_HINT_WORDS = (
    "manager", "director", "vp", "vice president", "president", "ceo", "cfo",
    "coo", "chief", "buyer", "purchasing", "procurement", "commodity",
    "sourcing", "supply chain", "engineer", "engineering", "operations",
    "plant", "quality", "program", "project", "tooling", "manufacturing",
)

# Buying / prospecting themes — not generic executive words
BUYING_THEME_WORDS = (
    "commodity", "sourcing", "procurement", "purchasing", "buyer", "tooling",
    "supply chain", "supplier", "materials", "stamping", "manufacturing engineer",
)

# Roles that are executive/support unless they also carry buying themes
NOT_TARGET_TITLE_MARKERS = (
    "chief financial", " cfo", "cfo,", "cfo ",
    "chief legal", "general counsel", "chief human", " human resources",
    "chro", "investor relations", "board of directors", "presiding director",
    "nominating", "audit committee", "administrative officer",
    "chief executive officer", " ceo", "ceo,", "president and ceo",
)

RELEVANCE_HIGH = "HIGH"
RELEVANCE_MEDIUM = "MEDIUM"
RELEVANCE_LOW = "LOW"
RELEVANCE_NOT_TARGET = "NOT_TARGET"

RELEVANCE_RANK = {
    RELEVANCE_HIGH: 4,
    RELEVANCE_MEDIUM: 3,
    RELEVANCE_LOW: 2,
    RELEVANCE_NOT_TARGET: 1,
}

GENERIC_EXEC_WORDS = frozenset({
    "manager", "director", "president", "vice", "executive", "chief",
    "officer", "vp", "svp", "evp", "head", "lead", "senior", "global",
})


@dataclass
class PeopleDiscoveryDiag:
    personas: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    linkedin_queries: list[str] = field(default_factory=list)
    sources_considered: list[str] = field(default_factory=list)
    linkedin_results_considered: int = 0
    linkedin_contacts_extracted: int = 0
    contacts_extracted: int = 0
    contacts_filtered: int = 0
    filter_reasons: list[str] = field(default_factory=list)
    linkedin_hints_seen: int = 0
    cross_source_merges: int = 0


def pack_contact_meta(
    persona: str = "",
    why: str = "",
    *,
    linkedin_url: str = "",
    sources: list[dict] | None = None,
    company: str = "",
    linkedin_derived: bool = False,
    relevance_tier: str = "",
) -> str:
    payload: dict = {
        "persona": _blank(persona),
        "why": _blank(why),
        "linkedin_url": _blank(linkedin_url),
        "company": _blank(company),
        "linkedin_derived": bool(linkedin_derived),
        "relevance_tier": _blank(relevance_tier),
        "sources": sources or [],
    }
    return json.dumps(payload, ensure_ascii=True)


def unpack_contact_meta(field_key: str) -> tuple[str, str]:
    data = unpack_contact_meta_full(field_key)
    return data.get("persona", ""), data.get("why", "")


def unpack_contact_meta_full(field_key: str) -> dict:
    raw = _blank(field_key)
    empty = {
        "persona": "",
        "why": "",
        "linkedin_url": "",
        "company": "",
        "linkedin_derived": False,
        "relevance_tier": "",
        "sources": [],
    }
    if not raw.startswith("{"):
        return empty
    try:
        data = json.loads(raw)
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            sources = []
        return {
            "persona": _blank(data.get("persona")),
            "why": _blank(data.get("why")),
            "linkedin_url": _blank(data.get("linkedin_url")),
            "company": _blank(data.get("company")),
            "linkedin_derived": bool(data.get("linkedin_derived")),
            "relevance_tier": _blank(data.get("relevance_tier")),
            "sources": sources,
        }
    except Exception:
        return empty


def extract_personas_from_text(text: str) -> list[str]:
    raw = _blank(text)
    if not raw:
        return []
    parts = re.split(r"[\n\r;|•·]+|(?:\s[-–—]\s)", raw)
    personas: list[str] = []
    seen: set[str] = set()
    role_words = (
        "manager", "director", "buyer", "vp", "vice", "president", "chief",
        "engineer", "engineering", "procurement", "purchasing", "commodity",
        "sourcing", "supply", "operations", "quality", "program", "project",
        "tooling", "plant", "manufacturing",
    )
    skip_starts = (
        "understand", "identify", "find", "look", "use", "when", "where",
        "what", "how", "the ", "a ", "an ", "prospect", "company",
    )
    for part in parts:
        line = _blank(part)
        if not line:
            continue
        if len(line) > 80:
            m = re.match(r"^([^.]{3,60})", line)
            line = _blank(m.group(1)) if m else line[:60]
        line = re.sub(
            r"^(target titles?|titles?|personas?)\s*[:\-–]\s*",
            "",
            line,
            flags=re.I,
        )
        line = re.sub(r"\s+", " ", line).strip(" .")
        if len(line) < 3 or len(line) > 70:
            continue
        lower = line.lower()
        if any(lower.startswith(s) for s in skip_starts):
            continue
        if not any(w in lower for w in role_words):
            continue
        # Prefer compact title-like phrases (avoid full sentences)
        if line.count(" ") > 6:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        personas.append(line)
        if len(personas) >= 12:
            break
    return personas


def resolve_target_personas(
    *,
    target_titles: str = "",
    prospecting_guidance: str = "",
) -> tuple[list[str], str]:
    from_titles = extract_personas_from_text(target_titles)
    if from_titles:
        return from_titles, "campaign_target_titles"
    from_guidance = extract_personas_from_text(prospecting_guidance)
    if from_guidance:
        return from_guidance, "prospecting_guidance"
    return list(FALLBACK_PERSONAS), "generic_fallback"


def _persona_query_forms(persona: str) -> list[str]:
    p = _blank(persona)
    if not p:
        return []
    forms = [p]
    m = re.match(r"^[A-Z]{2,8}\s+(.+)$", p)
    if m and _blank(m.group(1)):
        forms.append(_blank(m.group(1)))
    out: list[str] = []
    seen: set[str] = set()
    for f in forms:
        key = f.lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def build_linkedin_people_queries(company_name: str, personas: list[str]) -> list[str]:
    """Public search queries targeting LinkedIn profile result pages — never fetch LinkedIn."""
    company = _blank(company_name)
    if not company:
        return []
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        key = q.lower()
        if key in seen or len(queries) >= MAX_LINKEDIN_QUERIES:
            return
        seen.add(key)
        queries.append(q)

    for persona in personas:
        for form in _persona_query_forms(persona):
            _add(f'site:linkedin.com/in {company} "{form}"')
            if len(queries) >= MAX_LINKEDIN_QUERIES:
                return queries
    return queries


def build_people_queries(company_name: str, personas: list[str]) -> list[str]:
    company = _blank(company_name)
    if not company:
        return []
    queries: list[str] = []
    seen: set[str] = set()
    general_cap = max(4, MAX_PEOPLE_QUERIES - MAX_LINKEDIN_QUERIES)

    def _add(q: str) -> None:
        key = q.lower()
        if key in seen or len(queries) >= general_cap:
            return
        seen.add(key)
        queries.append(q)

    for persona in personas:
        for form in _persona_query_forms(persona):
            # Prefer lightly quoted queries — fully quoted DDG html often returns empty.
            _add(f'{company} "{form}"')
    for extra in (
        f"{company} leadership team",
        f'{company} "vice president"',
        f"{company} Corporation leadership",
    ):
        _add(extra)
        if len(queries) >= general_cap:
            break

    linkedin_qs = build_linkedin_people_queries(company_name, personas)
    for q in linkedin_qs:
        key = q.lower()
        if key not in seen and len(queries) < MAX_PEOPLE_QUERIES:
            seen.add(key)
            queries.append(q)
    return queries[:MAX_PEOPLE_QUERIES]


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _unwrap_search_url(url: str) -> str:
    """Resolve DuckDuckGo redirect wrappers to the destination URL."""
    raw = _blank(url)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlparse(raw)
        qs = parsed.query or ""
        if "uddg=" in qs or "uddg=" in raw:
            from urllib.parse import parse_qs

            vals = parse_qs(qs).get("uddg") or []
            if not vals and "uddg=" in raw:
                m = re.search(r"uddg=([^&]+)", raw)
                if m:
                    vals = [m.group(1)]
            if vals:
                return unquote(vals[0])
        # Some DDG links nest amp; entities
        cleaned = raw.replace("&amp;", "&")
        if cleaned != raw:
            return _unwrap_search_url(cleaned)
    except Exception:
        pass
    return raw


def _is_linkedin(url: str) -> bool:
    u = _blank(url).lower()
    return "linkedin.com" in u


def _is_linkedin_profile(url: str) -> bool:
    """True only for public /in/ profile URLs — not jobs, company, or posts."""
    if not _is_linkedin(url):
        return False
    path = (urlparse(url).path or "").lower()
    if "/in/" not in path:
        return False
    if any(x in path for x in ("/jobs", "/company/", "/school/", "/posts/", "/pulse/")):
        return False
    return True


def _normalize_linkedin_profile_url(url: str) -> str:
    raw = _unwrap_search_url(_blank(url))
    if not _is_linkedin_profile(raw):
        return ""
    parsed = urlparse(raw)
    # Keep scheme + host + /in/{slug} only
    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "in":
        path = f"/in/{parts[1]}"
        return f"https://www.linkedin.com{path}"
    return raw.split("?")[0].rstrip("/")


def _company_associated(text: str, company_name: str) -> bool:
    blob = _blank(text).lower()
    company = _blank(company_name).lower()
    if not blob or not company:
        return False
    if company in blob:
        return True
    token = company.split()[0]
    if len(token) >= 4 and token in blob:
        return True
    return False


def _is_job_listing(url: str) -> bool:
    u = _blank(url).lower()
    if any(m in u for m in JOB_PATH_MARKERS):
        return True
    host = _host(u)
    return any(host == b or host.endswith("." + b) for b in (
        "indeed.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com",
    ))


def _is_blocked_host(url: str) -> bool:
    h = _host(url)
    if not h:
        return True
    if _is_linkedin(url):
        return True
    if _is_job_listing(url):
        return True
    for b in BLOCKED_PEOPLE_HOSTS:
        if h == b or h.endswith("." + b):
            return True
    return False


def _duckduckgo_people_results(query: str) -> list[dict[str, str]]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    html = _fetch(url)
    results = _parse_ddg_html(html)
    if results:
        return results
    # Retry with quotes stripped if the html endpoint returns an empty shell
    soft = re.sub(r'"', "", query)
    if soft != query:
        html2 = _fetch(f"https://html.duckduckgo.com/html/?q={quote_plus(soft)}")
        return _parse_ddg_html(html2)
    return []


def _parse_ddg_html(html: str | None) -> list[dict[str, str]]:
    if not html:
        return []
    results: list[dict[str, str]] = []
    for m in re.finditer(
        r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?(?:class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)>)?',
        html,
    ):
        href = _unwrap_search_url(_blank(m.group(1)))
        title = _blank(_strip_html(m.group(2)))
        snippet = _blank(_strip_html(m.group(3) or ""))
        if href and title:
            results.append({"url": href, "title": title, "snippet": snippet})
        if len(results) >= 8:
            break
    if results:
        return results
    for m in re.finditer(r'(?is)uddg=([^&"]+).*?>(.*?)</a>', html):
        href = _unwrap_search_url(unquote(m.group(1)))
        title = _blank(_strip_html(m.group(2)))
        if href.startswith("http") and title:
            results.append({"url": href, "title": title, "snippet": ""})
        if len(results) >= 8:
            break
    return results


_NAME_TOKEN = r"[A-Z][A-Za-z'’\-]+"
_NAME_PATTERN = rf"(?P<name>{_NAME_TOKEN}(?:\s+(?:[A-Z]\.|{_NAME_TOKEN})){{1,3}})"


def _looks_like_person_name(name: str) -> bool:
    parts = [p for p in _blank(name).replace(".", " ").split() if p]
    if len(parts) < 2 or len(parts) > 4:
        return False
    blocked = {
        "about", "whirlpool", "company", "contact", "united", "states", "north",
        "america", "privacy", "policy", "terms", "service", "click", "here",
        "learn", "more", "home", "page", "appliance", "kitchen", "laundry",
        "executive", "committee", "corporation", "leadership", "directors",
        "board", "whom", "functions", "regions", "team", "information",
        "technology", "services", "employee", "senior", "global", "corporate",
        "responsibility", "reports", "careers", "governance", "material",
        "steel", "raw", "jobs", "salaries", "united", "officer", "president",
        "vice", "chief", "manager", "director", "asia", "region", "evp", "svp",
        "manufacturing",
    }
    particles = {"de", "da", "del", "van", "von", "la", "le"}
    for p in parts:
        low = p.lower().strip(",")
        if low in blocked:
            return False
        if low in particles:
            continue
        if not re.match(r"^[A-Z][A-Za-z'’\-]+$", p) and not re.match(r"^[A-Z]\.?$", p):
            return False
    # Reject all-caps org-like tokens longer than initials
    if sum(1 for p in parts if p.isupper() and len(p) > 1) >= 2:
        return False
    return True


def _title_matches_persona(title: str, personas: list[str]) -> tuple[str, int]:
    t = _blank(title).lower()
    if not t:
        return "", 0
    best = ""
    best_score = 0
    for persona in personas:
        p = _blank(persona).lower()
        if not p:
            continue
        score = 0
        if p in t or t in p:
            score = 100
        else:
            p_words = [
                w
                for w in re.split(r"\W+", p)
                if len(w) > 2 and w not in {"the", "and"}
            ]
            p_words = [w for w in p_words if w not in {"sope", "nar", "lar", "oem"}]
            # Do not score on generic exec words alone
            meaningful = [w for w in p_words if w not in GENERIC_EXEC_WORDS]
            if not meaningful:
                meaningful = p_words
            hits = sum(1 for w in meaningful if w in t)
            if hits:
                score = 40 + hits * 20
        if score > best_score:
            best_score = score
            best = persona
    if best_score < 40:
        return "", best_score
    return best, best_score


def _persona_buying_themes(personas: list[str]) -> set[str]:
    themes: set[str] = set()
    for persona in personas:
        p = _blank(persona).lower()
        for theme in BUYING_THEME_WORDS:
            if theme in p:
                themes.add(theme)
        for w in re.split(r"\W+", p):
            if w in {"commodity", "tooling", "sourcing", "procurement", "purchasing", "buyer"}:
                themes.add(w)
    return themes


def _title_buying_themes(title: str) -> set[str]:
    t = _blank(title).lower()
    return {theme for theme in BUYING_THEME_WORDS if theme in t}


def _is_not_target_executive(title: str) -> bool:
    t = f" {_blank(title).lower()} "
    if any(m in t for m in NOT_TARGET_TITLE_MARKERS):
        return True
    # Lone finance/legal/HR without buying themes
    if any(x in t for x in (" financial ", " legal ", " human resources ", " hr ")):
        if not _title_buying_themes(title):
            return True
    return False


def classify_persona_relevance(
    title: str, personas: list[str]
) -> tuple[str, str, str]:
    """Return (relevance_tier, matched_persona, why_relevant).

    HIGH — clear direct match to approved campaign persona
    MEDIUM — closely related buying function (e.g. Strategic Sourcing ↔ Commodity)
    LOW — weak buying-adjacent signal only
    NOT_TARGET — executive/finance/legal/HR/etc. without buying persona fit
    """
    title_clean = _blank(title)
    t = title_clean.lower()
    if not t:
        return RELEVANCE_NOT_TARGET, "", "No title available to evaluate campaign relevance."

    persona, score = _title_matches_persona(title_clean, personas)
    buying_in_title = _title_buying_themes(title_clean)
    campaign_themes = _persona_buying_themes(personas)
    theme_overlap = buying_in_title & campaign_themes
    # Related: sourcing ↔ commodity/procurement/purchasing
    related_pairs = {
        ("sourcing", "commodity"),
        ("sourcing", "procurement"),
        ("sourcing", "purchasing"),
        ("commodity", "sourcing"),
        ("procurement", "sourcing"),
        ("purchasing", "sourcing"),
        ("tooling", "sourcing"),
        ("sourcing", "tooling"),
    }
    related_hit = False
    for a in buying_in_title:
        for b in campaign_themes:
            if (a, b) in related_pairs or a == b:
                related_hit = True
                break

    # Direct persona match
    if persona and score >= 100:
        return (
            RELEVANCE_HIGH,
            persona,
            f"Title matches campaign persona: {persona}",
        )
    if persona and score >= 80 and buying_in_title:
        return (
            RELEVANCE_HIGH,
            persona,
            f"Title matches campaign persona: {persona}",
        )

    # Strong related functional role
    if related_hit or theme_overlap:
        matched = persona
        if not matched and campaign_themes:
            # Pick best campaign persona sharing a theme
            for p in personas:
                pl = p.lower()
                if any(th in pl for th in theme_overlap) or any(
                    th in pl for th in buying_in_title
                ):
                    matched = p
                    break
        theme = next(iter(theme_overlap or buying_in_title), "buying")
        camp = next(iter(campaign_themes), "campaign buying")
        if "sourc" in t and any("commodity" in c or "procure" in c or "purchas" in c for c in campaign_themes):
            why = "Strategic Sourcing is closely related to Commodity Management"
            if matched and "commodity" in matched.lower():
                why = f"Strategic Sourcing is closely related to {matched}"
        elif theme_overlap:
            why = f"Title includes {theme} themes aligned with campaign persona targeting"
        else:
            why = f"{title_clean.split(',')[0].strip()} is closely related to {camp} targeting"
        return RELEVANCE_MEDIUM, matched or persona, why

    # Weak buying signal without clear campaign theme overlap
    if buying_in_title and not _is_not_target_executive(title_clean):
        theme = next(iter(buying_in_title))
        return (
            RELEVANCE_LOW,
            persona,
            f"Title includes {theme}, but is only weakly tied to active campaign personas",
        )

    # Generic executives — not target
    if _is_not_target_executive(title_clean) or not buying_in_title:
        # If only generic manager/president/VP/executive language
        return (
            RELEVANCE_NOT_TARGET,
            "",
            "Executive/support role without a meaningful match to campaign buying personas",
        )

    return (
        RELEVANCE_NOT_TARGET,
        "",
        "No clear relationship to active campaign target personas",
    )


def extract_contacts_from_html(
    html: str,
    *,
    personas: list[str],
    source_url: str,
    source_name: str,
    page_title: str,
    researched_at: str,
    provider_id: str = "public_web",
    max_per_page: int = 12,
) -> list[NormalizedFinding]:
    """Prefer structured leadership markup before free-text regex."""
    findings: list[NormalizedFinding] = []
    seen: set[str] = set()
    raw = html or ""

    def _push(name: str, title: str) -> None:
        nonlocal findings
        name, title = _blank(name), _blank(title)
        if not name or not title:
            return
        if not _looks_like_person_name(name):
            return
        key = f"{name.lower()}|{title.lower()}"
        if key in seen:
            return
        # Reuse text extractor acceptance via a tiny synthetic blob
        items = extract_contacts_from_text(
            f"{name}, {title}",
            personas=personas,
            source_url=source_url,
            source_name=source_name,
            page_title=page_title,
            researched_at=researched_at,
            provider_id=provider_id,
            max_per_page=1,
        )
        for item in items:
            k = f"{item.contact_name.lower()}|{item.contact_title.lower()}"
            if k in seen:
                continue
            seen.add(k)
            findings.append(item)

    for m in re.finditer(
        r'(?is)cmp-list__item-title[^>]*>\s*([^<]{3,80})\s*</span>.*?'
        r'cmp-list__item-description[^>]*>\s*([^<]{3,160})\s*</span>',
        raw,
    ):
        _push(_strip_html(m.group(1)), _strip_html(m.group(2)))
        if len(findings) >= max_per_page:
            return findings

    for m in re.finditer(
        r'(?is)<img[^>]+(?:alt|title)=["\']([^"\']{3,60})["\'][^>]*>',
        raw,
    ):
        name = _blank(_strip_html(m.group(1)))
        if not _looks_like_person_name(name):
            continue
        # Search a nearby window for a title-like phrase
        start = max(0, m.start() - 80)
        end = min(len(raw), m.end() + 500)
        window = _strip_html(raw[start:end])
        title_m = re.search(
            r"(?i)((?:Executive\s+)?(?:Vice\s+)?President|CEO|CFO|COO|"
            r"Chief(?:\s+\w+){0,3}\s+Officer|Director|Manager)[^.\n]{0,80}",
            window,
        )
        if title_m:
            _push(name, title_m.group(0))
        if len(findings) >= max_per_page:
            return findings

    # Fallback free-text
    if len(findings) < 2:
        for item in extract_contacts_from_text(
            _strip_html(raw),
            personas=personas,
            source_url=source_url,
            source_name=source_name,
            page_title=page_title,
            researched_at=researched_at,
            provider_id=provider_id,
            max_per_page=max_per_page - len(findings),
        ):
            key = f"{item.contact_name.lower()}|{item.contact_title.lower()}"
            if key in seen:
                continue
            seen.add(key)
            findings.append(item)
    return findings[:max_per_page]


def extract_contacts_from_text(
    text: str,
    *,
    personas: list[str],
    source_url: str,
    source_name: str,
    page_title: str,
    researched_at: str,
    provider_id: str = "public_web",
    max_per_page: int = 8,
) -> list[NormalizedFinding]:
    findings: list[NormalizedFinding] = []
    seen: set[str] = set()
    blob = _blank(text)
    if not blob:
        return findings
    window = blob[:12000]
    title_alt = (
        r"(?:(?:Senior|Strategic|Global|Regional|Corporate|Assistant|Associate|"
        r"Principal|Lead|Head|Executive)\s+)?"
        r"(?:President|CEO|CFO|COO|Chief(?:\s+Executive|\s+Financial|\s+Operating)?"
        r"(?:\s+Officer)?|Owner|Director|Manager|VP|V\.P\.|"
        r"Vice\s+President|Purchasing|Buyer|Procurement|Commodity|Sourcing|"
        r"Supply\s+Chain|Engineer(?:ing)?|Operations|Plant|Quality|Program|"
        r"Project|Tooling|Manufacturing)[^.\n|;|]{0,60}"
    )

    def _accept(name: str, title: str) -> NormalizedFinding | None:
        name = _blank(name)
        title = _blank(re.sub(r"\s+", " ", title)).strip(" ,;|")
        title = re.sub(r"\s+at\s+.*$", "", title, flags=re.I).strip()
        if not _looks_like_person_name(name) or len(title) < 3:
            return None
        key = f"{name.lower()}|{title.lower()}"
        if key in seen:
            return None
        # Keep executives visible when they have a real title; classify relevance separately
        if not any(
            h in title.lower()
            for h in (
                "manager",
                "director",
                "vp",
                "vice",
                "buyer",
                "chief",
                "procure",
                "commodity",
                "sourc",
                "officer",
                "president",
                "engineer",
                "tooling",
                "purchasing",
                "operations",
                "quality",
                "program",
                "project",
                "plant",
                "supply",
            )
        ):
            return None
        tier, persona, why = classify_persona_relevance(title, personas)
        conf = (
            "high"
            if tier == RELEVANCE_HIGH
            else ("medium" if tier == RELEVANCE_MEDIUM else "low")
        )
        seen.add(key)
        sources = [
            {
                "name": source_name,
                "url": source_url,
                "kind": "linkedin"
                if _is_linkedin(source_url) or "linkedin" in source_name.lower()
                else "public",
            }
        ]
        return NormalizedFinding(
            finding_type="public_contact",
            field_key=pack_contact_meta(
                persona,
                why,
                linkedin_url=source_url if _is_linkedin_profile(source_url) else "",
                sources=sources,
                linkedin_derived=_is_linkedin_profile(source_url)
                or "linkedin" in source_name.lower(),
                relevance_tier=tier,
            ),
            value=f"{name} — {title}",
            source_name=source_name,
            source_url=source_url,
            researched_at=researched_at,
            confidence=conf,
            evidence_level=EVIDENCE_VERIFIED
            if source_url and not _is_linkedin(source_url)
            else EVIDENCE_SUPPORTED,
            page_title=page_title,
            is_public_contact=True,
            contact_name=name,
            contact_title=title,
            provider_id=provider_id,
        )

    patterns = [
        re.compile(rf"(?is){_NAME_PATTERN}\s*[,|\-–—:]\s*(?P<title>{title_alt})"),
    ]
    for pat in patterns:
        for m in pat.finditer(window):
            item = _accept(m.group("name"), m.group("title"))
            if item:
                findings.append(item)
                if len(findings) >= max_per_page:
                    return findings
    return findings


def extract_linkedin_search_contact(
    *,
    title: str,
    snippet: str,
    profile_url: str,
    company_name: str,
    personas: list[str],
    researched_at: str,
    provider_id: str = "public_web",
) -> NormalizedFinding | None:
    """Build a contact from a public search result only. Never fetches LinkedIn."""
    url = _normalize_linkedin_profile_url(profile_url)
    if not url:
        return None
    blob = f"{_blank(title)}. {_blank(snippet)}"
    if not _company_associated(blob, company_name):
        return None

    name = ""
    role = ""
    # Common SERP shapes:
    # "Name - Title at Company | LinkedIn"
    # "Name | Title | Company | LinkedIn"
    # "Name - Title - Company | LinkedIn"
    cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", _blank(title), flags=re.I).strip()
    m = re.match(
        rf"(?is)^{_NAME_PATTERN}\s*[-–—|]\s*(?P<title>.+?)\s+at\s+(?P<co>.+)$",
        cleaned,
    )
    if m:
        name = _blank(m.group("name"))
        role = _blank(m.group("title"))
    if not name:
        m = re.match(
            rf"(?is)^{_NAME_PATTERN}\s*[-–—]\s*(?P<title>[^-–—|]+?)(?:\s*[-–—|]\s*(?P<co>.+))?$",
            cleaned,
        )
        if m:
            name = _blank(m.group("name"))
            role = _blank(m.group("title"))
    if not name:
        m = re.match(
            rf"(?is)^{_NAME_PATTERN}\s*\|\s*(?P<title>[^|]+?)(?:\s*\|\s*(?P<co>.+))?$",
            cleaned,
        )
        if m:
            name = _blank(m.group("name"))
            role = _blank(m.group("title"))
    if not name or not _looks_like_person_name(name):
        return None
    role = re.sub(r"\s+", " ", role).strip(" ,;|")
    role = re.sub(r"\s+at\s+.*$", "", role, flags=re.I).strip()
    if len(role) < 3:
        # Try snippet: "Title at Company"
        sm = re.search(
            rf"(?is){re.escape(name)}\s*[-–—,]?\s*(?P<title>[^.]{{3,80}}?)\s+at\s+",
            blob,
        )
        if sm:
            role = _blank(sm.group("title"))
    if len(role) < 3:
        return None
    if not any(
        h in role.lower()
        for h in (
            "manager",
            "director",
            "vp",
            "vice",
            "buyer",
            "chief",
            "procure",
            "commodity",
            "sourc",
            "officer",
            "president",
            "engineer",
            "tooling",
            "purchasing",
            "operations",
            "quality",
            "program",
            "project",
            "plant",
            "supply",
        )
    ):
        return None

    persona, score = _title_matches_persona(role, personas)
    # LinkedIn SERP contacts must have buying-relevant titles already checked above
    tier, persona2, why = classify_persona_relevance(role, personas)
    persona = persona2 or persona
    if tier == RELEVANCE_NOT_TARGET:
        return None

    conf = (
        "high"
        if tier == RELEVANCE_HIGH
        else ("medium" if tier == RELEVANCE_MEDIUM else "low")
    )
    # LinkedIn alone never auto-high for confidence display of evidence strength
    if conf == "high":
        conf = "medium"

    sources = [
        {
            "name": "LinkedIn (public search result)",
            "url": url,
            "kind": "linkedin",
        }
    ]
    return NormalizedFinding(
        finding_type="public_contact",
        field_key=pack_contact_meta(
            persona,
            why,
            linkedin_url=url,
            sources=sources,
            company=company_name,
            linkedin_derived=True,
            relevance_tier=tier,
        ),
        value=f"{name} — {role}",
        source_name="LinkedIn (public search result)",
        source_url=url,
        researched_at=researched_at,
        confidence=conf,
        evidence_level=EVIDENCE_SUPPORTED,
        page_title=_blank(title)[:180],
        is_public_contact=True,
        contact_name=name,
        contact_title=role,
        provider_id=provider_id,
    )


def _norm_person_key(name: str) -> str:
    parts = [
        p.lower()
        for p in re.sub(r"[^A-Za-z\s'\-]", " ", _blank(name)).split()
        if p and p.lower() not in {"de", "da", "del", "van", "von", "la", "le"}
    ]
    return " ".join(parts)


def merge_person_findings(
    findings: list[NormalizedFinding],
) -> tuple[list[NormalizedFinding], int]:
    """Collapse same person across sources; preserve LinkedIn URL + source list."""
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    by_name: dict[str, NormalizedFinding] = {}
    merges = 0

    def _meta(f: NormalizedFinding) -> dict:
        return unpack_contact_meta_full(f.field_key)

    for f in findings:
        if not f.is_public_contact:
            continue
        key = _norm_person_key(f.contact_name)
        if not key:
            continue
        if key not in by_name:
            by_name[key] = f
            continue
        merges += 1
        existing = by_name[key]
        em = _meta(existing)
        nm = _meta(f)
        sources = list(em.get("sources") or [])
        for s in nm.get("sources") or []:
            if not isinstance(s, dict):
                continue
            su = _blank(s.get("url"))
            sn = _blank(s.get("name"))
            if any(
                _blank(x.get("url")) == su and _blank(x.get("name")) == sn
                for x in sources
                if isinstance(x, dict)
            ):
                continue
            sources.append(s)
        # Prefer stronger confidence / persona / verified evidence as primary
        prefer_new = (
            conf_rank.get(f.confidence, 0),
            1 if nm.get("persona") else 0,
            1 if f.evidence_level == EVIDENCE_VERIFIED else 0,
            len(_blank(f.contact_title)),
        ) > (
            conf_rank.get(existing.confidence, 0),
            1 if em.get("persona") else 0,
            1 if existing.evidence_level == EVIDENCE_VERIFIED else 0,
            len(_blank(existing.contact_title)),
        )
        primary = f if prefer_new else existing
        secondary = existing if prefer_new else f
        pm = _meta(primary)
        sm = _meta(secondary)
        linkedin_url = _blank(pm.get("linkedin_url")) or _blank(sm.get("linkedin_url"))
        persona = _blank(pm.get("persona")) or _blank(sm.get("persona"))
        why = _blank(pm.get("why")) or _blank(sm.get("why"))
        if linkedin_url and "linkedin" not in " ".join(
            _blank(x.get("name")).lower() for x in sources if isinstance(x, dict)
        ):
            sources.append(
                {
                    "name": "LinkedIn (public search result)",
                    "url": linkedin_url,
                    "kind": "linkedin",
                }
            )
        source_names = []
        for s in sources:
            if isinstance(s, dict) and _blank(s.get("name")):
                label = _blank(s.get("name"))
                if "linkedin" in label.lower():
                    label = "LinkedIn"
                if label not in source_names:
                    source_names.append(label)
        display_source = " + ".join(source_names[:4]) if source_names else primary.source_name
        # Prefer non-LinkedIn URL as primary open link when available; keep LinkedIn in meta
        primary_url = primary.source_url
        if _is_linkedin(primary_url) and secondary.source_url and not _is_linkedin(
            secondary.source_url
        ):
            primary_url = secondary.source_url
        primary.field_key = pack_contact_meta(
            persona,
            why,
            linkedin_url=linkedin_url,
            sources=sources[:8],
            company=_blank(pm.get("company")) or _blank(sm.get("company")),
            linkedin_derived=bool(pm.get("linkedin_derived") or sm.get("linkedin_derived")),
            relevance_tier=_blank(pm.get("relevance_tier"))
            or _blank(sm.get("relevance_tier")),
        )
        primary.source_name = display_source
        primary.source_url = primary_url or linkedin_url
        if not primary.contact_title and secondary.contact_title:
            primary.contact_title = secondary.contact_title
            primary.value = f"{primary.contact_name} — {primary.contact_title}"
        # Corroboration bump: LinkedIn + another source → medium max (still not auto-high)
        if linkedin_url and any(
            isinstance(s, dict) and _blank(s.get("kind")) != "linkedin"
            for s in sources
        ):
            if conf_rank.get(primary.confidence, 0) < conf_rank["medium"]:
                primary.confidence = "medium"
        by_name[key] = primary

    return list(by_name.values()), merges


def discover_people_path_links(html: str, base_url: str) -> list[str]:
    hrefs = re.findall(r'(?is)<a[^>]+href=["\']([^"\'#]+)["\']', html)
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    root = base_host.split(".")[0] if base_host else ""
    for href in hrefs:
        full = urljoin(base_url + "/", href.strip())
        parsed = urlparse(full)
        host = parsed.netloc.lower().replace("www.", "")
        if host and host != base_host:
            if not (root and root in host):
                continue
        path = (parsed.path or "/").lower()
        score = 0
        for i, hint in enumerate(PEOPLE_PATH_HINTS):
            if hint in path:
                score = max(score, 200 - i)
        if score <= 0:
            continue
        if any(
            x in path
            for x in (
                "/p.",
                "/product/",
                "sku",
                "manual",
                "dryer",
                "washer",
                "cooktop",
                "refrigerat",
            )
        ):
            score -= 150
        if score <= 0:
            continue
        norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        scored.append((score, norm))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [u for _, u in scored[:MAX_PEOPLE_PAGES_FETCH]]


def run_people_discovery(
    *,
    company_name: str,
    website: str,
    personas: list[str],
    homepage_html: str | None = None,
) -> tuple[list[NormalizedFinding], list[ResearchedPage], PeopleDiscoveryDiag]:
    researched_at = _now_iso()
    diag = PeopleDiscoveryDiag(personas=list(personas))
    findings: list[NormalizedFinding] = []
    pages_meta: list[ResearchedPage] = []
    filtered = 0
    queries = build_people_queries(company_name, personas)
    linkedin_queries = [q for q in queries if "site:linkedin.com/in" in q.lower()]
    diag.queries = list(queries)
    diag.linkedin_queries = list(linkedin_queries)
    candidate_urls: list[str] = []
    snippet_contacts: list[NormalizedFinding] = []
    linkedin_contacts: list[NormalizedFinding] = []

    if homepage_html and website:
        for u in discover_people_path_links(homepage_html, website):
            if u not in candidate_urls:
                candidate_urls.append(u)
    elif website:
        home_try = _fetch(website)
        if home_try:
            homepage_html = home_try
            for u in discover_people_path_links(home_try, website):
                if u not in candidate_urls:
                    candidate_urls.append(u)

    for query in queries:
        is_li_query = "site:linkedin.com/in" in query.lower()
        for item in _duckduckgo_people_results(query):
            url = _unwrap_search_url(_blank(item.get("url")))
            title = _blank(item.get("title"))
            snippet = _blank(item.get("snippet"))
            if not url:
                continue
            diag.sources_considered.append(url)

            # LinkedIn: use SERP evidence only — never fetch the profile page
            if _is_linkedin(url):
                diag.linkedin_hints_seen += 1
                diag.linkedin_results_considered += 1
                if not _is_linkedin_profile(url):
                    filtered += 1
                    diag.filter_reasons.append(f"linkedin_non_profile:{url[:80]}")
                    continue
                contact = extract_linkedin_search_contact(
                    title=title,
                    snippet=snippet,
                    profile_url=url,
                    company_name=company_name,
                    personas=personas,
                    researched_at=researched_at,
                )
                if contact:
                    linkedin_contacts.append(contact)
                    diag.linkedin_contacts_extracted += 1
                else:
                    filtered += 1
                    diag.filter_reasons.append(
                        f"linkedin_filtered:{title[:60] or url[:60]}"
                    )
                continue

            if _host(url) in LOW_QUALITY_PEOPLE_HOSTS or any(
                _host(url).endswith("." + h) for h in LOW_QUALITY_PEOPLE_HOSTS
            ):
                filtered += 1
                diag.filter_reasons.append(f"low_quality_host:{_host(url)}")
                extracted = extract_contacts_from_text(
                    f"{title}. {snippet}",
                    personas=personas,
                    source_url="",
                    source_name="Search result (directory hint — not treated as verified)",
                    page_title=title,
                    researched_at=researched_at,
                    max_per_page=2,
                )
                for f in extracted:
                    f.evidence_level = EVIDENCE_SUPPORTED
                    f.confidence = "low"
                snippet_contacts.extend(extracted)
                continue

            if _is_blocked_host(url) or _is_job_listing(url):
                filtered += 1
                diag.filter_reasons.append(f"blocked_host:{_host(url) or url[:48]}")
                continue

            # LinkedIn-oriented queries should not enqueue non-LinkedIn fetches beyond normal
            extracted = extract_contacts_from_text(
                f"{title}. {snippet}",
                personas=personas,
                source_url=url,
                source_name="Public search result",
                page_title=title,
                researched_at=researched_at,
                max_per_page=2,
            )
            for f in extracted:
                f.evidence_level = EVIDENCE_SUPPORTED
            snippet_contacts.extend(extracted)
            if is_li_query:
                continue
            path_l = urlparse(url).path.lower()
            if any(h in path_l for h in PEOPLE_PATH_HINTS) or "leadership" in title.lower():
                if url not in candidate_urls:
                    candidate_urls.insert(0, url)
            elif url not in candidate_urls and len(candidate_urls) < MAX_PEOPLE_PAGES_FETCH:
                candidate_urls.append(url)

    fetched = 0
    for url in candidate_urls:
        if fetched >= MAX_PEOPLE_PAGES_FETCH:
            break
        # Hard safety: never fetch LinkedIn
        if _is_linkedin(url) or _is_blocked_host(url):
            filtered += 1
            diag.filter_reasons.append(f"skip_fetch:{_host(url) or 'unknown'}")
            continue
        html = _fetch(url)
        if not html:
            filtered += 1
            diag.filter_reasons.append(f"fetch_failed:{url}")
            continue
        fetched += 1
        title = _page_title(html)
        label = _page_label(url, title) or "People / leadership page"
        pages_meta.append(ResearchedPage(url=url, title=title, label=label))
        findings.extend(
            extract_contacts_from_html(
                html,
                personas=personas,
                source_url=url,
                source_name=label,
                page_title=title,
                researched_at=researched_at,
                max_per_page=8,
            )
        )

    findings.extend(snippet_contacts)
    findings.extend(linkedin_contacts)

    # Cross-source person merge (name-based), then rank
    merged, merge_count = merge_person_findings(findings)
    diag.cross_source_merges = merge_count
    conf_rank = {"high": 3, "medium": 2, "low": 1}

    def _sort_key(f: NormalizedFinding) -> tuple:
        meta = unpack_contact_meta_full(f.field_key)
        persona = _blank(meta.get("persona"))
        _p, score = _title_matches_persona(f.contact_title, personas)
        return (
            conf_rank.get(f.confidence, 0),
            1 if f.evidence_level == EVIDENCE_VERIFIED else 0,
            score,
            1 if persona else 0,
            1 if meta.get("linkedin_url") else 0,
        )

    unique = sorted(merged, key=_sort_key, reverse=True)[:MAX_CONTACTS_TOTAL]
    final: list[NormalizedFinding] = []
    for f in unique:
        meta = unpack_contact_meta_full(f.field_key)
        persona = _blank(meta.get("persona"))
        _p, score = _title_matches_persona(f.contact_title, personas)
        if personas and score < 25 and not persona:
            if not any(h in f.contact_title.lower() for h in TITLE_HINT_WORDS):
                filtered += 1
                diag.filter_reasons.append(
                    f"low_relevance:{f.contact_name}:{f.contact_title}"
                )
                continue
        final.append(f)

    diag.contacts_extracted = len(final)
    diag.contacts_filtered = filtered
    diag.filter_reasons = list(dict.fromkeys(diag.filter_reasons))[:40]
    diag.sources_considered = list(dict.fromkeys(diag.sources_considered))[:60]

    final.append(
        NormalizedFinding(
            finding_type="people_discovery",
            field_key="diagnostics",
            value=json.dumps(
                {
                    "personas": diag.personas,
                    "queries": diag.queries,
                    "linkedin_queries": diag.linkedin_queries,
                    "sources_considered_count": len(diag.sources_considered),
                    "sources_considered": diag.sources_considered[:30],
                    "linkedin_results_considered": diag.linkedin_results_considered,
                    "linkedin_contacts_extracted": diag.linkedin_contacts_extracted,
                    "cross_source_merges": diag.cross_source_merges,
                    "contacts_extracted": diag.contacts_extracted,
                    "contacts_filtered": diag.contacts_filtered,
                    "filter_reasons": diag.filter_reasons[:20],
                    "linkedin_hints_seen": diag.linkedin_hints_seen,
                },
                ensure_ascii=True,
            ),
            source_name="People discovery diagnostics",
            source_url="",
            researched_at=researched_at,
            confidence="high",
            evidence_level=EVIDENCE_SUPPORTED,
            provider_id="public_web",
        )
    )
    return final, pages_meta, diag
