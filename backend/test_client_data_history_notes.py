"""Isolated tests for CDI history-note marketing exclusion and dedupe.

Run: python test_client_data_history_notes.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import testdb

from client_data_history_notes import (
    compute_history_event_hash,
    history_content_fingerprint,
    history_event_already_present,
    is_routine_marketing_send_note,
    load_existing_history_dedupe_index,
    normalize_history_note_text,
)
from client_data_import import (
    _apply_history_events,
    parse_history_with_mapping,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import NorthStarUser
from shared_note_history_import import HistoryEventRow, ensure_shared_note_history_schema


def _actor() -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE is_administrator = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    return NorthStarUser(
        id=int(row["id"]),
        email=str(row["email"] or ""),
        full_name=str(row["full_name"] or "Admin"),
        is_administrator=True,
        is_internal_northstar=True,
        active=True,
        created_at=str(row["created_at"] or ""),
    )


def _client_id(conn) -> int:
    row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb needs a client.")
    return int(row["id"])


class NormalizeAndMarketingTests(unittest.TestCase):
    def test_spacing_and_capitalization_normalize_equal(self):
        a = normalize_history_note_text("  Hello   WORLD \n\n")
        b = normalize_history_note_text("hello world")
        self.assertEqual(a, b)

    def test_marketing_email_sent_excluded(self):
        self.assertTrue(is_routine_marketing_send_note("Marketing email sent"))
        self.assertTrue(is_routine_marketing_send_note("Email marketing sent"))
        self.assertTrue(
            is_routine_marketing_send_note(
                "22-Apr-2026 10:30 AM - E-Marketing\n"
                "Email was sent to Sue Lamar - slamar@foggfiller.com"
            )
        )

    def test_meaningful_email_reply_retained(self):
        self.assertFalse(
            is_routine_marketing_send_note(
                "Re: Quote follow-up\nThanks — please call me Thursday about the line."
            )
        )
        mixed = (
            "22-Apr-2026 10:30 AM - E-Marketing\n"
            "Email was sent to Sue Lamar - slamar@foggfiller.com\n\n"
            "22-Apr-2026 9:20 AM - Robert Kirstein\n"
            "Karen said things are slow; requested a call every six months."
        )
        self.assertFalse(is_routine_marketing_send_note(mixed))


class FingerprintAndDedupeTests(unittest.TestCase):
    _seq = 0

    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        FingerprintAndDedupeTests._seq += 1
        self.record_no = f"HN-{FingerprintAndDedupeTests._seq:04d}"
        with get_connection() as conn:
            ensure_shared_note_history_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)
            self.actor = _actor()
            self.company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, 'History Note Co', '', '', 'IA', '', '')
                    """,
                    (self.record_no,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'New')
                """,
                (self.client_id, self.company_id),
            )
            conn.commit()

    def test_exact_duplicate_by_source_id_skipped(self):
        with get_connection() as conn:
            source_id = "LM-NOTE-99"
            event = HistoryEventRow(
                record_no=self.record_no,
                event_at="2024-01-01T12:00:00Z",
                author="Rep",
                event_type="Note",
                attribution="Brown",
                attribution_evidence="",
                source_file="",
                note_text="Called about packaging line",
                event_hash=source_id,
                contact_no="C1",
            )
            inserted, already = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=[event],
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((inserted, already), (1, 0))
            inserted2, already2 = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=[event],
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((inserted2, already2), (0, 1))
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE company_id=?",
                (self.company_id,),
            ).fetchone()["n"]
            self.assertEqual(int(n), 1)

    def test_spacing_caps_duplicate_without_source_id(self):
        with get_connection() as conn:
            h1 = compute_history_event_hash(
                client_id=self.client_id,
                company_id=self.company_id,
                contact_key="C1",
                note_text="Left message with gatekeeper",
                event_at="2024-02-01 09:00",
            )
            e1 = HistoryEventRow(
                record_no=self.record_no,
                event_at="2024-02-01 09:00",
                author="Rep",
                event_type="Note",
                attribution="",
                attribution_evidence="",
                source_file="",
                note_text="Left message with gatekeeper",
                event_hash=h1,
                contact_no="C1",
            )
            _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=[e1],
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            h2 = compute_history_event_hash(
                client_id=self.client_id,
                company_id=self.company_id,
                contact_key="C1",
                note_text="  LEFT   MESSAGE  with Gatekeeper\n",
                event_at="2024-02-01 09:00",
            )
            e2 = HistoryEventRow(
                record_no=self.record_no,
                event_at="2024-02-01 09:00",
                author="Rep",
                event_type="Note",
                attribution="",
                attribution_evidence="",
                source_file="",
                note_text="  LEFT   MESSAGE  with Gatekeeper\n",
                event_hash=h2,
                contact_no="C1",
            )
            self.assertEqual(h1, h2)
            inserted, already = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=[e2],
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((inserted, already), (0, 1))

    def test_same_text_different_dates_both_insert(self):
        with get_connection() as conn:
            events = []
            for at in ("2024-03-01", "2024-04-01"):
                note = "Discussed conveyor quote"
                events.append(
                    HistoryEventRow(
                        record_no=self.record_no,
                        event_at=at,
                        author="Rep",
                        event_type="Note",
                        attribution="",
                        attribution_evidence="",
                        source_file="",
                        note_text=note,
                        event_hash=compute_history_event_hash(
                            client_id=self.client_id,
                            company_id=self.company_id,
                            contact_key="",
                            note_text=note,
                            event_at=at,
                        ),
                    )
                )
            inserted, already = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=events,
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((inserted, already), (2, 0))

    def test_same_text_different_contacts_both_insert(self):
        with get_connection() as conn:
            note = "Sent brochure PDF"
            at = "2024-05-01T10:00:00Z"
            events = []
            for contact in ("873892", "873893"):
                events.append(
                    HistoryEventRow(
                        record_no=self.record_no,
                        event_at=at,
                        author="Rep",
                        event_type="Note",
                        attribution="",
                        attribution_evidence="",
                        source_file="",
                        note_text=note,
                        event_hash=compute_history_event_hash(
                            client_id=self.client_id,
                            company_id=self.company_id,
                            contact_key=contact,
                            note_text=note,
                            event_at=at,
                        ),
                        contact_no=contact,
                    )
                )
            self.assertNotEqual(events[0].event_hash, events[1].event_hash)
            inserted, already = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=events,
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((inserted, already), (2, 0))

    def test_matches_legacy_note_without_source_id(self):
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, 'Older LeadMaster blob about fillers', 'comments')
                """,
                (self.client_id, self.company_id),
            )
            conn.commit()
            index = load_existing_history_dedupe_index(
                conn, client_id=self.client_id, company_id=self.company_id
            )
            self.assertTrue(
                history_event_already_present(
                    index,
                    client_id=self.client_id,
                    company_id=self.company_id,
                    contact_key="",
                    note_text="  older leadmaster blob about fillers ",
                    event_at="2024-06-01",
                    event_hash="fresh-hash",
                )
            )

    def test_parse_excludes_marketing_keeps_reply(self):
        csv_text = (
            "LeadMaster Record No.,Event Timestamp,Author,Note Text,Event Hash,Contact No.\n"
            f"{self.record_no},2024-01-01,System,Marketing email sent,m1,\n"
            f"{self.record_no},2024-01-02,Rep,Customer asked for pricing on line 2,,C9\n"
        )
        parsed = parse_history_with_mapping(
            csv_text,
            {
                "history_record_no": "LeadMaster Record No.",
                "history_event_at": "Event Timestamp",
                "history_author": "Author",
                "history_note_text": "Note Text",
                "history_event_hash": "Event Hash",
                "history_contact_no": "Contact No.",
            },
            client_id=self.client_id,
        )
        self.assertEqual(parsed.excluded_marketing_events, 1)
        self.assertEqual(len(parsed.events), 1)
        self.assertIn("pricing", parsed.events[0].note_text)

    def test_reimport_same_file_twice(self):
        with get_connection() as conn:
            csv_text = (
                "LeadMaster Record No.,Event Timestamp,Author,Note Text,Event Hash\n"
                f"{self.record_no},2024-07-01,Rep,First call notes,hash-reimport-1\n"
                f"{self.record_no},2024-07-02,Rep,Second call notes,hash-reimport-2\n"
            )
            mapping = {
                "history_record_no": "LeadMaster Record No.",
                "history_event_at": "Event Timestamp",
                "history_author": "Author",
                "history_note_text": "Note Text",
                "history_event_hash": "Event Hash",
            }
            parsed = parse_history_with_mapping(
                csv_text, mapping, client_id=self.client_id
            )
            self.assertEqual(len(parsed.events), 2)
            i1, a1 = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=parsed.events,
                closed_record_nos=set(),
                batch_id=1,
            )
            conn.commit()
            self.assertEqual((i1, a1), (2, 0))
            parsed2 = parse_history_with_mapping(
                csv_text, mapping, client_id=self.client_id
            )
            i2, a2 = _apply_history_events(
                conn,
                client_id=self.client_id,
                actor=self.actor,
                events=parsed2.events,
                closed_record_nos=set(),
                batch_id=2,
            )
            conn.commit()
            self.assertEqual((i2, a2), (0, 2))
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE company_id=?",
                (self.company_id,),
            ).fetchone()["n"]
            self.assertEqual(int(n), 2)


if __name__ == "__main__":
    unittest.main()
