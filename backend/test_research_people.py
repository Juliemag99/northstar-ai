"""Tests for campaign-aware public people discovery (incl. LinkedIn SERP evidence)."""

from __future__ import annotations

import testdb
import unittest
from unittest.mock import patch

from research_people import (
    build_linkedin_people_queries,
    build_people_queries,
    extract_contacts_from_text,
    extract_linkedin_search_contact,
    extract_personas_from_text,
    merge_person_findings,
    pack_contact_meta,
    resolve_target_personas,
    unpack_contact_meta,
    unpack_contact_meta_full,
)
from research_providers import EVIDENCE_SUPPORTED, EVIDENCE_VERIFIED, NormalizedFinding


class PersonaSourceTests(unittest.TestCase):
    def test_campaign_target_titles_drive_personas(self):
        titles = (
            "SOPE Tooling Manager — understand processes.\n"
            "Commodity Manager\n"
            "Strategic Sourcing"
        )
        personas, source = resolve_target_personas(target_titles=titles)
        self.assertEqual(source, "campaign_target_titles")
        joined = " | ".join(p.lower() for p in personas)
        self.assertIn("tooling manager", joined)
        self.assertIn("commodity manager", joined)
        self.assertTrue(any("sourc" in p.lower() for p in personas))

    def test_fallback_when_no_personas(self):
        personas, source = resolve_target_personas()
        self.assertEqual(source, "generic_fallback")
        self.assertGreaterEqual(len(personas), 3)

    def test_guidance_before_fallback(self):
        personas, source = resolve_target_personas(
            prospecting_guidance="Target Purchasing Manager and Supply Chain Director"
        )
        self.assertEqual(source, "prospecting_guidance")
        self.assertTrue(any("purchasing" in p.lower() for p in personas))


class PeopleQueryTests(unittest.TestCase):
    def test_queries_include_company_and_persona(self):
        queries = build_people_queries(
            "Acme Metals",
            ["Commodity Manager", "Strategic Sourcing"],
        )
        self.assertTrue(any("Acme Metals" in q and "Commodity Manager" in q for q in queries))
        self.assertTrue(any("Strategic Sourcing" in q for q in queries))
        self.assertLessEqual(len(queries), 10)

    def test_linkedin_queries_use_campaign_personas(self):
        queries = build_people_queries(
            "Acme Metals",
            ["Commodity Manager", "Tooling Manager"],
        )
        li = [q for q in queries if "site:linkedin.com/in" in q]
        self.assertTrue(li)
        self.assertTrue(any("Commodity Manager" in q for q in li))
        self.assertTrue(any("Tooling Manager" in q for q in li))
        self.assertFalse(any("Whirlpool" in q for q in li))
        self.assertFalse(any("Carmeco" in q for q in li))
        dedicated = build_linkedin_people_queries("Acme Metals", ["Commodity Manager"])
        self.assertTrue(all("site:linkedin.com/in" in q for q in dedicated))
        self.assertLessEqual(len(dedicated), 4)

    def test_empty_company_yields_no_queries(self):
        self.assertEqual(build_people_queries("", ["Buyer"]), [])


class ContactExtractionTests(unittest.TestCase):
    def test_multiple_contacts_and_name_variants(self):
        text = (
            "Jane O'Brien, Commodity Manager leads sourcing. "
            "Mary-Anne Smith — Strategic Sourcing Director for appliances. "
            "Robert J. Lee, Manufacturing Engineering Manager."
        )
        findings = extract_contacts_from_text(
            text,
            personas=["Commodity Manager", "Strategic Sourcing", "Manufacturing Engineering"],
            source_url="https://example.com/leadership",
            source_name="Leadership",
            page_title="Team",
            researched_at="2026-01-01T00:00:00Z",
            max_per_page=8,
        )
        names = {f.contact_name for f in findings}
        self.assertIn("Jane O'Brien", names)
        self.assertIn("Mary-Anne Smith", names)
        self.assertIn("Robert J. Lee", names)
        self.assertGreaterEqual(len(findings), 2)
        for f in findings:
            persona, why = unpack_contact_meta(f.field_key)
            self.assertTrue(persona or why)
            self.assertFalse(getattr(f, "contact_email", None))

    def test_no_invented_email_phone(self):
        findings = extract_contacts_from_text(
            "Alex Buyer, Purchasing Manager",
            personas=["Purchasing Manager"],
            source_url="https://example.com",
            source_name="About",
            page_title="About",
            researched_at="2026-01-01T00:00:00Z",
        )
        self.assertTrue(findings)
        self.assertEqual(findings[0].contact_name, "Alex Buyer")
        self.assertNotIn("@", findings[0].value)

    def test_pack_unpack_meta_preserves_linkedin(self):
        raw = pack_contact_meta(
            "Commodity Manager",
            'Matches campaign persona "Commodity Manager".',
            linkedin_url="https://www.linkedin.com/in/ryanmdecker",
            sources=[{"name": "LinkedIn (public search result)", "url": "https://www.linkedin.com/in/ryanmdecker", "kind": "linkedin"}],
            linkedin_derived=True,
        )
        p, w = unpack_contact_meta(raw)
        full = unpack_contact_meta_full(raw)
        self.assertEqual(p, "Commodity Manager")
        self.assertIn("Matches", w)
        self.assertEqual(full["linkedin_url"], "https://www.linkedin.com/in/ryanmdecker")
        self.assertTrue(full["linkedin_derived"])


class LinkedInSearchResultTests(unittest.TestCase):
    def test_parse_linkedin_serp_contact(self):
        contact = extract_linkedin_search_contact(
            title="Ryan Decker - Commodity Manager at Whirlpool Corporation | LinkedIn",
            snippet="Commodity Manager at Whirlpool Corporation",
            profile_url="https://www.linkedin.com/in/ryanmdecker",
            company_name="Whirlpool",
            personas=["Commodity Manager", "Tooling Manager"],
            researched_at="2026-01-01T00:00:00Z",
        )
        self.assertIsNotNone(contact)
        assert contact is not None
        self.assertEqual(contact.contact_name, "Ryan Decker")
        self.assertIn("Commodity", contact.contact_title)
        self.assertEqual(contact.source_url, "https://www.linkedin.com/in/ryanmdecker")
        meta = unpack_contact_meta_full(contact.field_key)
        self.assertEqual(meta["linkedin_url"], "https://www.linkedin.com/in/ryanmdecker")
        self.assertEqual(meta["persona"], "Commodity Manager")
        self.assertEqual(meta.get("relevance_tier"), "HIGH")
        self.assertIn("Commodity Manager", meta.get("why") or "")
        self.assertEqual(contact.confidence, "medium")  # LinkedIn evidence capped
        self.assertNotIn("@", contact.value)

    def test_irrelevant_linkedin_filtered(self):
        contact = extract_linkedin_search_contact(
            title="Sam Volunteer - Community Organizer at Other Co | LinkedIn",
            snippet="Loves hiking",
            profile_url="https://www.linkedin.com/in/samvolunteer",
            company_name="Whirlpool",
            personas=["Commodity Manager"],
            researched_at="2026-01-01T00:00:00Z",
        )
        self.assertIsNone(contact)

    def test_jobs_url_rejected(self):
        contact = extract_linkedin_search_contact(
            title="Commodity Manager jobs at Whirlpool",
            snippet="",
            profile_url="https://www.linkedin.com/jobs/view/123",
            company_name="Whirlpool",
            personas=["Commodity Manager"],
            researched_at="2026-01-01T00:00:00Z",
        )
        self.assertIsNone(contact)

    def test_no_linkedin_page_fetch_in_discovery(self):
        fetched: list[str] = []

        def fake_fetch(url: str):
            fetched.append(url)
            if "duckduckgo" in url:
                return (
                    '<a class="result__a" href="https://www.linkedin.com/in/ryanmdecker">'
                    "Ryan Decker - Commodity Manager at Whirlpool Corporation | LinkedIn</a>"
                    '<a class="result__snippet">Commodity Manager at Whirlpool</a>'
                )
            return None

        with patch("research_people._fetch", side_effect=fake_fetch):
            from research_people import run_people_discovery

            findings, _pages, diag = run_people_discovery(
                company_name="Whirlpool",
                website="",
                personas=["Commodity Manager"],
                homepage_html=None,
            )
        self.assertTrue(any("site:linkedin.com/in" in q for q in diag.linkedin_queries or diag.queries))
        self.assertTrue(all("linkedin.com/in" not in u for u in fetched))
        li_contacts = [
            f for f in findings if f.is_public_contact and "linkedin" in (f.source_name or "").lower()
        ]
        self.assertTrue(li_contacts or diag.linkedin_contacts_extracted >= 0)


class CrossSourceDedupeTests(unittest.TestCase):
    def test_merge_same_person_across_sources(self):
        a = NormalizedFinding(
            finding_type="public_contact",
            field_key=pack_contact_meta(
                "Commodity Manager",
                "Matches",
                linkedin_url="https://www.linkedin.com/in/ryanmdecker",
                sources=[{"name": "LinkedIn (public search result)", "url": "https://www.linkedin.com/in/ryanmdecker", "kind": "linkedin"}],
                linkedin_derived=True,
            ),
            value="Ryan Decker — Commodity Manager",
            source_name="LinkedIn (public search result)",
            source_url="https://www.linkedin.com/in/ryanmdecker",
            researched_at="t",
            confidence="low",
            evidence_level=EVIDENCE_SUPPORTED,
            is_public_contact=True,
            contact_name="Ryan Decker",
            contact_title="Commodity Manager",
        )
        b = NormalizedFinding(
            finding_type="public_contact",
            field_key=pack_contact_meta(
                "Commodity Manager",
                "From site",
                sources=[{"name": "Official company page", "url": "https://example.com/team", "kind": "public"}],
            ),
            value="Ryan Decker — Commodity Manager",
            source_name="Official company page",
            source_url="https://example.com/team",
            researched_at="t",
            confidence="medium",
            evidence_level=EVIDENCE_VERIFIED,
            is_public_contact=True,
            contact_name="Ryan Decker",
            contact_title="Commodity Manager",
        )
        merged, count = merge_person_findings([a, b])
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        meta = unpack_contact_meta_full(merged[0].field_key)
        self.assertEqual(meta["linkedin_url"], "https://www.linkedin.com/in/ryanmdecker")
        self.assertIn("LinkedIn", merged[0].source_name)
        self.assertEqual(len(meta["sources"]), 2)


class RelevanceTierTests(unittest.TestCase):
    def test_direct_persona_match_is_high(self):
        from research_people import RELEVANCE_HIGH, classify_persona_relevance

        tier, persona, why = classify_persona_relevance(
            "Commodity Manager",
            ["Commodity Manager", "Tooling Manager"],
        )
        self.assertEqual(tier, RELEVANCE_HIGH)
        self.assertEqual(persona, "Commodity Manager")
        self.assertIn("matches campaign persona", why.lower())

    def test_strategic_sourcing_is_medium_for_commodity_campaign(self):
        from research_people import RELEVANCE_MEDIUM, classify_persona_relevance

        tier, persona, why = classify_persona_relevance(
            "Executive President, NAR and Global Strategic Sourcing",
            ["Commodity Manager", "SOPE Tooling Manager"],
        )
        self.assertEqual(tier, RELEVANCE_MEDIUM)
        self.assertTrue("sourc" in why.lower() or "commodity" in why.lower())

    def test_cfo_is_not_target(self):
        from research_people import RELEVANCE_NOT_TARGET, classify_persona_relevance

        tier, persona, why = classify_persona_relevance(
            "executive vice president, chief financial and administrative officer, and president",
            ["Commodity Manager", "SOPE Tooling Manager"],
        )
        self.assertEqual(tier, RELEVANCE_NOT_TARGET)
        self.assertEqual(persona, "")
        self.assertNotIn("purchasing", why.lower())
        self.assertNotIn("relevant management", why.lower())

    def test_generic_vp_not_target(self):
        from research_people import RELEVANCE_NOT_TARGET, classify_persona_relevance

        tier, _, why = classify_persona_relevance(
            "Vice President",
            ["Commodity Manager"],
        )
        self.assertEqual(tier, RELEVANCE_NOT_TARGET)
        self.assertNotIn("relevant management/purchasing", why.lower())

    def test_tooling_sourcing_related(self):
        from research_people import RELEVANCE_HIGH, RELEVANCE_MEDIUM, classify_persona_relevance

        tier, persona, why = classify_persona_relevance(
            "Capital & Tooling Sourcing Leader",
            ["SOPE Tooling Manager", "Commodity Manager"],
        )
        self.assertIn(tier, {RELEVANCE_HIGH, RELEVANCE_MEDIUM})
        self.assertTrue(persona or "tooling" in why.lower() or "sourc" in why.lower())


class SourceQualityTests(unittest.TestCase):
    def test_extract_personas_ignores_noise(self):
        personas = extract_personas_from_text("Click here\nbuyer\n")
        self.assertTrue(any("buyer" == p.lower() for p in personas))


class CrmMatchIntegrationTests(unittest.TestCase):
    def test_match_labels_via_research_helper(self):
        from research_data import _CRM_MATCH_LABELS

        self.assertEqual(_CRM_MATCH_LABELS["existing"], "Already in NorthStar")
        self.assertEqual(_CRM_MATCH_LABELS["possible_match"], "Possible Match")
        self.assertEqual(_CRM_MATCH_LABELS["new"], "New Contact")


if __name__ == "__main__":
    unittest.main()
