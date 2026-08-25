"""Research providers for Phase 2A / 2B.

Public Web Research is active. ZoomInfo is a stub only — no API calls.
Providers return normalized findings; Research Company UI never consumes
provider-specific raw payloads.

Public research is bounded:
  Pass 1 — official website identity + navigation discovery
  Pass 2 — highest-value first-party pages (about/products/capabilities/…)
  Pass 3 — reserved for later secondary sources (not ZoomInfo)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

USER_AGENT = (
    "NorthStarAI-Research/2A (+local; company verification; minimal context)"
)
FETCH_TIMEOUT = 12.0
NOT_VERIFIED = "Not verified in current public research."
MAX_INTERNAL_PAGES = 10  # homepage + up to 9 high-value pages

EVIDENCE_VERIFIED = "verified"
EVIDENCE_SUPPORTED = "supported_inference"
EVIDENCE_NOT_VERIFIED = "not_verified"

# Path/keywords that indicate high-value first-party pages (earlier = higher score)
HIGH_VALUE_PATH_HINTS = (
    "leadership",
    "executive",
    "management",
    "our-team",
    "investor",
    "corporate",
    "about",
    "why-",
    "why_",
    "/why",
    "company",
    "capability",
    "capabilities",
    "manufactur",
    "product",
    "products",
    "service",
    "services",
    "industry",
    "industries",
    "market",
    "markets",
    "application",
    "applications",
    "facilit",
    "location",
    "locations",
    "contact",
    "what-we",
    "who-we",
    "process",
    "quality",
    "news",
    "press",
    "media",
    "team",
    "dealer",  # dealer/contact geography — lower priority
    "resource",
    "resources",
    "brochure",
    "faq",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object) -> str:
    return str(value or "").strip()


def _fetch(url: str) -> str | None:
    """Fetch HTML text for a public URL. Returns None on failure. No auth / no bypass."""
    target = _blank(url)
    if not target.startswith("http"):
        if target.startswith("//"):
            target = "https:" + target
        else:
            return None

    def _once(u: str) -> str | None:
        try:
            with httpx.Client(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            ) as client:
                resp = client.get(u)
                if resp.status_code >= 400:
                    return None
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "text" not in ctype and ctype:
                    return None
                return resp.text
        except Exception:
            return None

    html = _once(target)
    if html:
        return html
    # Soft fallback: some manufacturer sites fail TLS on https://www but serve http
    if target.startswith("https://"):
        return _once("http://" + target[len("https://") :])
    return None


def normalize_website(raw: str) -> str:
    text = _blank(raw)
    if not text:
        return ""
    if not re.match(r"^https?://", text, flags=re.I):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}".rstrip("/")


@dataclass
class ResearchContext:
    """Minimum context sent to external research — never the full CRM."""

    company_name: str
    website: str = ""
    city: str = ""
    state: str = ""
    working_for_client_name: str = ""
    target_personas: list[str] = field(default_factory=list)
    persona_source: str = ""


@dataclass
class NormalizedFinding:
    finding_type: str
    value: str
    source_name: str
    source_url: str
    researched_at: str
    confidence: str = "medium"
    evidence_level: str = EVIDENCE_VERIFIED
    field_key: str = ""
    page_title: str = ""
    is_public_contact: bool = False
    contact_name: str = ""
    contact_title: str = ""
    provider_id: str = "public_web"


@dataclass
class ResearchedPage:
    url: str
    title: str = ""
    label: str = ""


@dataclass
class ProviderResearchResult:
    provider_id: str
    provider_name: str
    status: str  # ok | unavailable | error
    status_detail: str = ""
    findings: list[NormalizedFinding] = field(default_factory=list)
    sources_checked: list[str] = field(default_factory=list)
    pages_researched: list[ResearchedPage] = field(default_factory=list)
    future_capabilities: list[str] = field(default_factory=list)


class ResearchProvider(Protocol):
    provider_id: str
    provider_name: str

    def research_company(self, context: ResearchContext) -> ProviderResearchResult:
        ...


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _meta_content(html: str, *names: str) -> str:
    for name in names:
        patterns = [
            rf'(?is)<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'(?is)<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return _blank(m.group(1))
    return ""


def _page_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return _blank(_strip_html(m.group(1))) if m else ""


def _page_label(url: str, title: str) -> str:
    path = urlparse(url).path.lower()
    title_l = title.lower()
    for hint, label in (
        ("contact", "Contact page"),
        ("location", "Locations page"),
        ("facilit", "Facilities page"),
        ("about", "About page"),
        ("why", "About / why-us page"),
        ("company", "Company page"),
        ("capab", "Capabilities page"),
        ("manufactur", "Manufacturing page"),
        ("product", "Products page"),
        ("service", "Services page"),
        ("industr", "Industries page"),
        ("market", "Markets page"),
        ("application", "Applications page"),
        ("news", "News page"),
        ("press", "Press page"),
        ("quality", "Quality page"),
        ("process", "Process page"),
        ("dealer", "Dealer / locations page"),
        ("resource", "Resources page"),
    ):
        if hint in path or hint in title_l:
            return f"Official company {label.lower()}"
    if path in {"", "/"}:
        return "Official company website (homepage)"
    return "Official company page"


def _extract_phones(text: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(
        r"(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}", text
    ):
        phone = re.sub(r"\s+", " ", m.group(0)).strip()
        if phone not in found:
            found.append(phone)
        if len(found) >= 3:
            break
    return found


# Manufacturing PROCESSES — only when explicitly stated (not inferred from product type)
_PROCESS_PHRASES = (
    "metal stamping",
    "stamping",
    "laser cutting",
    "plasma cutting",
    "waterjet",
    "cnc machining",
    "machining",
    "mig welding",
    "tig welding",
    "robotic welding",
    "welding",
    "steel fabricators",
    "steel fabrication",
    "metal fabrication",
    "fabrication",
    "fabricators",
    "powder coating",
    "forming",
    "blanking",
    "tool and die",
    "tooling",
    "assembly",
    "press brake",
    "shearing",
    "punching",
    "bending",
    "cutting",
    "finishing",
    "painting",
    "galvaniz",
    "hand-crafted",
)

# Product / service category phrases (not manufacturing processes).
# Prefer specific multi-word categories; avoid generic singles like "components".
_PRODUCT_PHRASES = (
    "dump trailer",
    "utility trailer",
    "cargo trailer",
    "flatbed trailer",
    "equipment trailer",
    "gooseneck trailer",
    "hauling trailer",
    "deckover trailer",
    "dump trailers",
    "utility trailers",
    "equipment trailers",
    "hauling trailers",
    "deckover trailers",
    "open steel trailers",
    "chassis",
    "stampings",
    "weldments",
    "enclosures",
    "contract manufacturing",
)

# Too-generic product tokens — only keep if no specific product phrases found
_GENERIC_PRODUCT_FALLBACKS = (
    "trailers",
    "trailer",
)

_INDUSTRY_PHRASES = (
    "automotive",
    "appliance",
    "agriculture",
    "agricultural",
    "construction",
    "industrial",
    "oem",
    "transportation",
    "hvac",
    "medical",
    "consumer",
    "recreation",
    "farm",
    "landscape",
)

_MATERIAL_PHRASES = (
    "steel",
    "stainless steel",
    "aluminum",
    "carbon steel",
    "sheet metal",
    "plate",
)


def _phrase_in_text(phrase: str, lower_text: str) -> bool:
    """Literal phrase match with alphanumeric word boundaries (avoids 'steel' in 'steeling')."""
    pat = (
        r"(?<![a-z0-9])"
        + re.escape(phrase.lower())
        + r"(?![a-z0-9])"
    )
    return re.search(pat, lower_text) is not None


def _find_phrases_with_page(
    pages: list[tuple[str, str, str, str]], phrases: tuple[str, ...]
) -> list[tuple[str, str, str, str]]:
    """Return (phrase, url, label, page_title) for first page that literally contains phrase."""
    hits: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    # Prefer longer phrases first so "dump trailer" wins over nothing conflicting
    ordered = sorted(phrases, key=lambda p: (-len(p), p))
    for url, label, title, text in pages:
        lower = text.lower()
        for phrase in ordered:
            key = phrase.lower()
            if key in seen:
                continue
            if _phrase_in_text(phrase, lower):
                # Skip shorter phrase wholly contained in an already-found longer phrase
                if any(key in s and key != s for s in seen):
                    continue
                seen.add(key)
                hits.append((phrase, url, label, title))
            if len(hits) >= 12:
                return hits
    return hits


def _discover_internal_links(html: str, base_url: str) -> list[str]:
    hrefs = re.findall(r'(?is)<a[^>]+href=["\']([^"\'#]+)["\']', html)
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for href in hrefs:
        full = urljoin(base_url + "/", href.strip())
        parsed = urlparse(full)
        host = parsed.netloc.lower().replace("www.", "")
        if host and host != base_host:
            continue
        path = (parsed.path or "/").lower()
        query = (parsed.query or "").lower()
        blob = f"{path}?{query}" if query else path
        if path in {"", "/"} and not query:
            continue
        if any(
            x in blob
            for x in (".pdf", ".jpg", ".png", ".zip", "login", "cart", "wp-admin")
        ):
            continue
        score = 0
        for i, hint in enumerate(HIGH_VALUE_PATH_HINTS):
            if hint in blob or hint in path:
                score = max(score, 100 - i)
        if score <= 0:
            continue
        # Prefer clean paths; keep query only when path alone is empty-ish
        if path not in {"", "/"}:
            norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") or full
        else:
            norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
        norm = norm.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        scored.append((score, norm))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [u for _, u in scored[: MAX_INTERNAL_PAGES - 1]]


def _extract_location_mentions(
    text: str, city: str, state: str
) -> list[str]:
    found: list[str] = []
    # Prefer explicit City, ST patterns on the page (not CRM-injected guesses)
    for m in re.finditer(
        r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z]{2})\b(?:\s+\d{5})?",
        text[:8000],
    ):
        phrase = f"{m.group(1)}, {m.group(2)}"
        # Skip obvious form/noise pairs from long state lists
        if m.group(1) in {
            "State",
            "Province",
            "Country",
            "Select",
            "District",
        }:
            continue
        if phrase not in found:
            found.append(phrase)
        if len(found) >= 4:
            break

    # CRM city/state only when the contiguous "City, ST" phrase appears on-page
    # (avoids matching US state-name dropdowns like "Louisiana" to city Louisiana, MO)
    if city and state:
        pat = (
            r"(?<![a-z0-9])"
            + re.escape(city)
            + r"\s*,\s*"
            + re.escape(state)
            + r"(?![a-z0-9])"
        )
        if re.search(pat, text, flags=re.I):
            phrase = f"{city}, {state}"
            if phrase not in found:
                found.insert(0, phrase)
    return found


class PublicWebResearchProvider:
    provider_id = "public_web"
    provider_name = "Public Web Research"

    def research_company(self, context: ResearchContext) -> ProviderResearchResult:
        researched_at = _now_iso()
        findings: list[NormalizedFinding] = []
        sources_checked: list[str] = []
        pages_researched: list[ResearchedPage] = []
        company = _blank(context.company_name)
        if not company:
            return ProviderResearchResult(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                status="error",
                status_detail="Company name is required for public research.",
            )

        # Pass 1 — website identity
        website = normalize_website(context.website)
        if not website:
            website = self._discover_website(
                company, context.city, context.state, sources_checked
            )

        if website:
            findings.append(
                NormalizedFinding(
                    finding_type="website",
                    field_key="website",
                    value=website,
                    source_name="Official company website",
                    source_url=website,
                    researched_at=researched_at,
                    confidence="high",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title="",
                    provider_id=self.provider_id,
                )
            )
            page_findings, page_sources, pages_meta = self._research_website_pages(
                company,
                website,
                researched_at,
                ns_city=_blank(context.city),
                ns_state=_blank(context.state),
                personas=list(context.target_personas or []),
            )
            findings.extend(page_findings)
            sources_checked.extend(page_sources)
            pages_researched.extend(pages_meta)
        else:
            findings.append(
                NormalizedFinding(
                    finding_type="website",
                    field_key="website",
                    value=NOT_VERIFIED,
                    source_name="Public web search",
                    source_url="",
                    researched_at=researched_at,
                    confidence="low",
                    evidence_level=EVIDENCE_NOT_VERIFIED,
                    provider_id=self.provider_id,
                )
            )

        # Pass 3 — campaign-aware people discovery (runs even without a known website)
        try:
            from research_people import resolve_target_personas, run_people_discovery

            personas = list(context.target_personas or [])
            persona_source = _blank(context.persona_source)
            if not personas:
                personas, persona_source = resolve_target_personas()
            home_html = _fetch(website) if website else None
            people_findings, people_pages, _diag = run_people_discovery(
                company_name=company,
                website=website or "",
                personas=personas,
                homepage_html=home_html,
            )
            # Stamp persona source onto diagnostics finding when present
            for pf in people_findings:
                if pf.finding_type == "people_discovery" and persona_source:
                    try:
                        payload = json.loads(pf.value or "{}")
                        payload["persona_source"] = persona_source
                        pf.value = json.dumps(payload, ensure_ascii=True)
                    except Exception:
                        pass
            findings.extend(people_findings)
            pages_researched.extend(people_pages)
            sources_checked.extend([p.url for p in people_pages])
        except Exception as exc:
            # People pass must not fail the whole research run
            findings.append(
                NormalizedFinding(
                    finding_type="people_discovery",
                    field_key="diagnostics",
                    value=json.dumps(
                        {
                            "error": str(exc)[:240],
                            "personas": list(context.target_personas or []),
                            "persona_source": _blank(context.persona_source),
                            "queries": [],
                            "sources_considered": [],
                            "contacts_extracted": 0,
                            "contacts_filtered": 0,
                            "filter_reasons": [f"people_pass_error:{type(exc).__name__}"],
                        },
                        ensure_ascii=True,
                    ),
                    source_name="People discovery diagnostics",
                    source_url="",
                    researched_at=researched_at,
                    confidence="low",
                    evidence_level=EVIDENCE_SUPPORTED,
                    provider_id=self.provider_id,
                )
            )

        # Explicit NOT VERIFIED placeholders for core gaps
        present_types = {
            f.finding_type
            for f in findings
            if f.value != NOT_VERIFIED and f.evidence_level != EVIDENCE_NOT_VERIFIED
        }
        for ftype, field_key in (
            ("headquarters", "headquarters"),
            ("location", "manufacturing_locations"),
            ("overview", "overview"),
            ("product", "products"),
            ("capability", "capabilities"),
            ("industry", "industries"),
            ("material", "materials"),
            ("phone", "main_phone"),
            ("development", "recent_developments"),
        ):
            if ftype not in present_types:
                findings.append(
                    NormalizedFinding(
                        finding_type=ftype,
                        field_key=field_key,
                        value=NOT_VERIFIED,
                        source_name="Public web research",
                        source_url=website or "",
                        researched_at=researched_at,
                        confidence="low",
                        evidence_level=EVIDENCE_NOT_VERIFIED,
                        provider_id=self.provider_id,
                    )
                )

        return ProviderResearchResult(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            status="ok",
            status_detail=(
                f"Public first-party research complete — "
                f"{len(pages_researched)} page(s) inspected."
            ),
            findings=findings,
            sources_checked=sorted(set(sources_checked)),
            pages_researched=pages_researched,
        )

    def _discover_website(
        self,
        company: str,
        city: str,
        state: str,
        sources_checked: list[str],
    ) -> str:
        query = f"{company} official website"
        if city or state:
            query += f" {city} {state}".strip()
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        sources_checked.append(url)
        try:
            with httpx.Client(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    return ""
                html = resp.text
        except Exception:
            return ""

        links = re.findall(r'(?is)uddg=([^&"]+)|href="(https?://[^"]+)"', html)
        candidates: list[str] = []
        for a, b in links:
            raw = a or b
            try:
                from urllib.parse import unquote

                cand = unquote(raw)
            except Exception:
                cand = raw
            if "duckduckgo" in cand.lower() or "youtube" in cand.lower():
                continue
            if cand.startswith("http"):
                candidates.append(cand)
        name_token = re.sub(r"[^a-z0-9]", "", company.lower())[:10]
        for cand in candidates:
            host = urlparse(cand).netloc.lower().replace("www.", "")
            if name_token and name_token[:6] in host.replace(".", ""):
                return normalize_website(cand)
        for cand in candidates[:5]:
            host = urlparse(cand).netloc.lower()
            if host and not any(
                x in host for x in ("facebook", "linkedin", "wikipedia", "bloomberg")
            ):
                return normalize_website(cand)
        return ""

    def _fetch(self, client: httpx.Client, url: str) -> tuple[str, str] | None:
        try:
            resp = client.get(url)
            if resp.status_code >= 400:
                return None
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype and ctype:
                return None
            return str(resp.url), resp.text
        except Exception:
            return None

    def _research_website_pages(
        self,
        company: str,
        website: str,
        researched_at: str,
        *,
        ns_city: str,
        ns_state: str,
        personas: list[str] | None = None,
    ) -> tuple[list[NormalizedFinding], list[str], list[ResearchedPage]]:
        findings: list[NormalizedFinding] = []
        sources: list[str] = []
        pages_meta: list[ResearchedPage] = []
        # (url, label, title, text)
        page_texts: list[tuple[str, str, str, str]] = []
        target_personas = list(personas or [])

        with httpx.Client(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            verify=False,  # many manufacturer sites have expired/misconfigured certs
        ) as client:
            home = self._fetch(client, website)
            if not home and website.startswith("https://"):
                home = self._fetch(client, "http://" + website[len("https://") :])
            if not home:
                return findings, sources, pages_meta

            home_url, home_html = home
            home_title = _page_title(home_html)
            home_label = _page_label(home_url, home_title)
            sources.append(home_url)
            pages_meta.append(
                ResearchedPage(url=home_url, title=home_title, label=home_label)
            )
            page_texts.append(
                (home_url, home_label, home_title, _strip_html(home_html)[:16000])
            )

            # Homepage identity findings (once — avoid section titles like "Resources")
            meta_desc = _meta_content(
                home_html, "description", "og:description", "twitter:description"
            )
            brand = company
            if home_title and company.lower().split()[0] in home_title.lower():
                brand = home_title.split("|")[0].split("-")[0].strip() or company
            findings.append(
                NormalizedFinding(
                    finding_type="company_name",
                    field_key="company_name",
                    value=brand,
                    source_name=home_label,
                    source_url=home_url,
                    researched_at=researched_at,
                    confidence="high",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title=home_title,
                    provider_id=self.provider_id,
                )
            )
            overview_val = meta_desc
            if not overview_val:
                home_text = page_texts[0][3]
                token = company.lower().split()[0]
                # Prefer a sentence mentioning the brand, skip menu-heavy prefixes
                for chunk in re.split(r"(?<=[.!?])\s+", home_text):
                    if token in chunk.lower() and len(chunk) > 60:
                        overview_val = chunk.strip()[:420]
                        break
                if not overview_val and len(home_text) > 80:
                    overview_val = home_text[:420].strip()
            if overview_val:
                findings.append(
                    NormalizedFinding(
                        finding_type="overview",
                        field_key="overview",
                        value=overview_val,
                        source_name=home_label,
                        source_url=home_url,
                        researched_at=researched_at,
                        confidence="medium",
                        evidence_level=EVIDENCE_VERIFIED,
                        page_title=home_title,
                        provider_id=self.provider_id,
                    )
                )

            # Pass 2 — high-value internal pages (bounded)
            for link in _discover_internal_links(home_html, home_url):
                if len(page_texts) >= MAX_INTERNAL_PAGES:
                    break
                fetched = self._fetch(client, link)
                if not fetched:
                    continue
                u, html = fetched
                if any(u.rstrip("/") == p[0].rstrip("/") for p in page_texts):
                    continue
                # Skip soft-404 / not-found pages
                title = _page_title(html)
                if "not found" in title.lower() or "page not found" in _strip_html(html)[:400].lower():
                    continue
                label = _page_label(u, title)
                sources.append(u)
                pages_meta.append(ResearchedPage(url=u, title=title, label=label))
                page_texts.append((u, label, title, _strip_html(html)[:16000]))

                # One hop: also collect a few links from about/product/why pages
                if len(page_texts) < MAX_INTERNAL_PAGES and any(
                    h in urlparse(u).path.lower()
                    for h in ("about", "why", "product", "capab", "manufactur")
                ):
                    for nested in _discover_internal_links(html, u)[:3]:
                        if len(page_texts) >= MAX_INTERNAL_PAGES:
                            break
                        if any(
                            nested.rstrip("/") == p[0].rstrip("/") for p in page_texts
                        ):
                            continue
                        nested_fetch = self._fetch(client, nested)
                        if not nested_fetch:
                            continue
                        nu, nhtml = nested_fetch
                        ntitle = _page_title(nhtml)
                        if "not found" in ntitle.lower():
                            continue
                        nlabel = _page_label(nu, ntitle)
                        sources.append(nu)
                        pages_meta.append(
                            ResearchedPage(url=nu, title=ntitle, label=nlabel)
                        )
                        page_texts.append(
                            (nu, nlabel, ntitle, _strip_html(nhtml)[:16000])
                        )

        # Phone / contacts / locations from reviewed pages (not company_name/overview again)
        for url, label, title, text in page_texts:
            for phone in _extract_phones(text):
                findings.append(
                    NormalizedFinding(
                        finding_type="phone",
                        field_key="main_phone",
                        value=phone,
                        source_name=label,
                        source_url=url,
                        researched_at=researched_at,
                        confidence="medium",
                        evidence_level=EVIDENCE_VERIFIED,
                        page_title=title,
                        provider_id=self.provider_id,
                    )
                )
                break

            # People extraction on company-info pages (multiple contacts OK)
            try:
                from research_people import extract_contacts_from_text

                findings.extend(
                    extract_contacts_from_text(
                        text,
                        personas=target_personas,
                        source_url=url,
                        source_name=label,
                        page_title=title,
                        researched_at=researched_at,
                        provider_id=self.provider_id,
                        max_per_page=6,
                    )
                )
            except Exception:
                pass

            # Locations — only when city/state literally appear on page
            for loc in _extract_location_mentions(text, ns_city, ns_state):
                is_hq = bool(
                    ns_city and ns_city.lower() in loc.lower()
                )
                findings.append(
                    NormalizedFinding(
                        finding_type="headquarters" if is_hq else "location",
                        field_key="headquarters" if is_hq else "manufacturing_locations",
                        value=loc,
                        source_name=label,
                        source_url=url,
                        researched_at=researched_at,
                        confidence="medium",
                        evidence_level=EVIDENCE_VERIFIED,
                        page_title=title,
                        provider_id=self.provider_id,
                    )
                )

        # Products — explicit phrases on specific pages
        product_hits = _find_phrases_with_page(page_texts, _PRODUCT_PHRASES)
        if not product_hits:
            product_hits = _find_phrases_with_page(
                page_texts, _GENERIC_PRODUCT_FALLBACKS
            )
        for phrase, url, label, title in product_hits:
            findings.append(
                NormalizedFinding(
                    finding_type="product",
                    field_key="products",
                    value=phrase,
                    source_name=label,
                    source_url=url,
                    researched_at=researched_at,
                    confidence="medium",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title=title,
                    provider_id=self.provider_id,
                )
            )

        # Manufacturing processes — must be literally stated
        for phrase, url, label, title in _find_phrases_with_page(
            page_texts, _PROCESS_PHRASES
        ):
            findings.append(
                NormalizedFinding(
                    finding_type="capability",
                    field_key="capabilities",
                    value=phrase,
                    source_name=label,
                    source_url=url,
                    researched_at=researched_at,
                    confidence="medium",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title=title,
                    provider_id=self.provider_id,
                )
            )

        # Materials
        for phrase, url, label, title in _find_phrases_with_page(
            page_texts, _MATERIAL_PHRASES
        ):
            findings.append(
                NormalizedFinding(
                    finding_type="material",
                    field_key="materials",
                    value=phrase,
                    source_name=label,
                    source_url=url,
                    researched_at=researched_at,
                    confidence="medium",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title=title,
                    provider_id=self.provider_id,
                )
            )

        # Industries / markets
        for phrase, url, label, title in _find_phrases_with_page(
            page_texts, _INDUSTRY_PHRASES
        ):
            findings.append(
                NormalizedFinding(
                    finding_type="industry",
                    field_key="industries",
                    value=phrase,
                    source_name=label,
                    source_url=url,
                    researched_at=researched_at,
                    confidence="medium",
                    evidence_level=EVIDENCE_VERIFIED,
                    page_title=title,
                    provider_id=self.provider_id,
                )
            )

        # Supported inference example: product "trailers" does NOT imply welding/stamping.
        # Only emit supported inference when multiple verified products exist but no process.
        verified_products = [
            f.value for f in findings if f.finding_type == "product"
        ]
        verified_caps = [
            f.value for f in findings if f.finding_type == "capability"
        ]
        if verified_products and not verified_caps:
            findings.append(
                NormalizedFinding(
                    finding_type="capability",
                    field_key="capabilities",
                    value=(
                        "Manufacturing processes not explicitly stated on reviewed pages"
                    ),
                    source_name="Public web research synthesis",
                    source_url=page_texts[0][0] if page_texts else website,
                    researched_at=researched_at,
                    confidence="low",
                    evidence_level=EVIDENCE_SUPPORTED,
                    page_title="",
                    provider_id=self.provider_id,
                )
            )

        # Deduplicate
        seen: set[tuple[str, str]] = set()
        unique: list[NormalizedFinding] = []
        for f in findings:
            key = (f.finding_type, f.value.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(f)
        return unique, sources, pages_meta


class ZoomInfoStubProvider:
    """Phase 2B placeholder — no ZoomInfo API calls."""

    provider_id = "zoominfo"
    provider_name = "ZoomInfo"

    def research_company(self, context: ResearchContext) -> ProviderResearchResult:
        return ProviderResearchResult(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            status="unavailable",
            status_detail="ZoomInfo not connected",
            findings=[],
            sources_checked=[],
            pages_researched=[],
            future_capabilities=[
                "Company firmographics",
                "Employee count",
                "Revenue",
                "Decision makers",
                "Titles",
                "Business emails",
                "Direct phones",
                "Mobile phones",
            ],
        )


def get_research_providers() -> list[ResearchProvider]:
    return [PublicWebResearchProvider(), ZoomInfoStubProvider()]
