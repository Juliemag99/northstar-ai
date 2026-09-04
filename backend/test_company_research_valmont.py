"""Regression: Valmont-like Company Research vs empty Default campaign.

Run: python test_company_research_valmont.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import testdb

from db import DB_PATH, PRODUCTION_DB_PATH
from research_data import (
    FIT_CRITERIA_NOT_CONFIGURED,
    _build_decision_summary,
    _build_summary,
    _campaign_criteria_configured,
    _compute_fit,
)
from research_providers import _PRODUCT_PHRASES, _find_phrases_with_page


def _known(**kwargs):
    base = dict(
        company_name="Valmont",
        website="www.valmont.com",
        city="Omaha",
        state="NE",
        working_for_client_id=2,
        working_for_client_name="Brown Industries",
        working_for_record_no="NS-100006",
        working_for_status="Left Message",
        has_relationship=True,
        milestones=[],
        cross_client=[],
        opportunity_score=None,
        contacts=[],
        notes=[],
        sales_events=[],
        activities=[],
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class ValmontResearchVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())

    def test_brown_default_shell_is_not_configured_criteria(self):
        thin = {
            "summary": "Thin seed",
            "target_industries": "agriculture, industrial manufacturing",
            "target_products": "manufactured components",
            "notes": "seed",
            "primary_service": "",
            "fit_weighting_notes": "",
            "positive_fit_signals": "",
            "negative_fit_signals": "",
            "manufacturing_processes_sought": "",
        }
        self.assertFalse(_campaign_criteria_configured(thin))
        rich = {**thin, "primary_service": "metal fabrication"}
        self.assertTrue(_campaign_criteria_configured(rich))

    def test_valmont_like_fit_reports_criteria_not_configured_not_insufficient(self):
        known = _known()
        profile = {
            "summary": "Cross-client Carmeco history can inform Brown opportunity.",
            "target_industries": "industrial manufacturing, OEM, appliance, agriculture, trailer",
            "target_products": "manufactured components and production partnerships",
            "notes": "seed",
            "primary_service": "",
            "secondary_services": "",
            "ideal_customer_types": "",
            "manufacturing_processes_sought": "",
            "positive_fit_signals": "",
            "negative_fit_signals": "",
            "fit_weighting_notes": "",
            "campaign_name": "Default",
        }
        fit = _compute_fit(
            client_name="Brown Industries",
            client_id=2,
            profile=profile,
            known=known,
            verified_caps=[],
            verified_products=["vital infrastructure", "agricultural productivity"],
            verified_inds=["agriculture", "transportation"],
            campaign_id=2,
            campaign_name="Default",
            research_text_blobs=[
                "Valmont improves life by creating vital infrastructure and "
                "advancing agricultural productivity."
            ],
        )
        self.assertEqual(fit.fit_result, FIT_CRITERIA_NOT_CONFIGURED)
        self.assertIn("criteria not configured", fit.why.lower())
        self.assertNotIn("stamping", fit.why.lower())
        # Engagement absence must not be the fit reason.
        self.assertNotIn("engagement", fit.why.lower())

    def test_summary_and_decision_lead_with_company_profile_not_stamping(self):
        known = _known()
        fit = SimpleNamespace(
            fit_result=FIT_CRITERIA_NOT_CONFIGURED,
            why=(
                "Campaign criteria not configured for Brown Industries (Default). "
                "Configure primary service and fit signals to rate campaign fit. "
                "Company research findings remain available."
            ),
            campaign_name="Default",
            supporting_evidence=["Verified industries/markets: agriculture"],
            missing_information=[
                "Campaign criteria not configured: set primary service…"
            ],
        )
        summary = _build_summary(
            company_name="Valmont",
            known=known,
            fit=fit,
            verified_products=["vital infrastructure"],
            verified_caps=[],
            pages_count=4,
            verified_inds=["agriculture", "transportation"],
            overview_bits=[
                "Valmont improves life by creating vital infrastructure and "
                "advancing agricultural productivity."
            ],
            website="https://www.valmont.com",
            locations=["Omaha, NE", "Valley, NE"],
            stamping_primary=False,
        )
        self.assertIn("Company identity: Valmont", summary)
        self.assertIn("valmont.com", summary.lower())
        self.assertIn("Overview:", summary)
        self.assertIn("criteria not configured", summary.lower())
        self.assertNotIn("no meaningful stamping", summary.lower())

        decision = _build_decision_summary(
            company_name="Valmont",
            client_name="Brown Industries",
            campaign_name="Default",
            fit=fit,
            verified_products=["vital infrastructure"],
            verified_caps=[],
            overview_bits=[
                "Valmont improves life by creating vital infrastructure and "
                "advancing agricultural productivity with a commitment to conserving resources."
            ],
        )
        self.assertEqual(decision.fit_result, FIT_CRITERIA_NOT_CONFIGURED)
        self.assertIn("criteria not configured", decision.why.lower())
        self.assertNotIn("stamped/formed", decision.why.lower())

    def test_product_phrases_match_valmont_style_copy(self):
        pages = [
            (
                "https://www.valmont.com",
                "Home",
                "Home",
                "Valmont improves life by creating vital infrastructure and "
                "advancing agricultural productivity with irrigation systems "
                "and utility poles for transportation markets.",
            )
        ]
        hits = _find_phrases_with_page(pages, _PRODUCT_PHRASES)
        phrases = {h[0].lower() for h in hits}
        self.assertTrue(
            {"vital infrastructure", "agricultural productivity", "irrigation systems", "utility poles"}
            & phrases,
            phrases,
        )

    def test_engagement_history_does_not_create_possible_fit_without_criteria(self):
        known = _known(
            milestones=[
                {
                    "client_name": "Brown Industries",
                    "milestone_type": "Quote",
                    "label": "Quote",
                    "notes": "",
                }
            ],
        )
        profile = {
            "target_industries": "agriculture",
            "target_products": "components",
            "primary_service": "",
            "fit_weighting_notes": "",
            "positive_fit_signals": "",
            "negative_fit_signals": "",
            "manufacturing_processes_sought": "",
            "ideal_customer_types": "",
        }
        fit = _compute_fit(
            client_name="Brown Industries",
            client_id=2,
            profile=profile,
            known=known,
            verified_caps=[],
            verified_products=[],
            verified_inds=["agriculture"],
            campaign_id=2,
            campaign_name="Default",
        )
        self.assertEqual(fit.fit_result, FIT_CRITERIA_NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
