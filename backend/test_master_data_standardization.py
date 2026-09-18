"""Phase 3A/3B standardization helper tests. No production writes."""

from __future__ import annotations

import unittest

from _phase3a_standardization_preview import (
    NO_CHANGE,
    SAFE,
    standardize_street_address,
    strip_trailing_legal_suffixes,
)
from _phase3b_safe_apply import partition_safe_phone
from contact_phone import format_us_phone_display, store_phone_parts, split_phone_extension, SAFE_SPLIT


class NameStandardizationTests(unittest.TestCase):
    def test_strips_trailing_legal_suffixes_only(self) -> None:
        self.assertEqual(strip_trailing_legal_suffixes("ABC Manufacturing, Inc."), "ABC Manufacturing")
        self.assertEqual(strip_trailing_legal_suffixes("ABC Manufacturing LLC"), "ABC Manufacturing")
        self.assertEqual(strip_trailing_legal_suffixes("E J Ajax & Sons Co Inc"), "E J Ajax & Sons")
        self.assertEqual(strip_trailing_legal_suffixes("K Manufacturing Inc"), "K Manufacturing")

    def test_does_not_strip_keep_words(self) -> None:
        self.assertEqual(strip_trailing_legal_suffixes("Holden Industries"), "Holden Industries")
        self.assertEqual(strip_trailing_legal_suffixes("Ruskin Company"), "Ruskin")


class AddressStandardizationTests(unittest.TestCase):
    def test_street_name_directionals_are_not_abbreviated(self) -> None:
        for current in ("1900 North St", "715 South St", "93 East Ave", "1525 E North St"):
            proposed, klass, _reason = standardize_street_address(current)
            self.assertEqual(proposed, current)
            self.assertEqual(klass, NO_CHANGE)

    def test_prefix_directional_and_suite(self) -> None:
        proposed, klass, _reason = standardize_street_address("410 North Walnut Ave, Ste 105")
        self.assertEqual(proposed, "410 N Walnut Ave Ste 105")
        self.assertEqual(klass, SAFE)


class PhoneFormattingTests(unittest.TestCase):
    def test_ten_digit_no_extension_formats(self) -> None:
        self.assertEqual(format_us_phone_display("5155551212"), "(515) 555-1212")
        action, proposed = partition_safe_phone("515-555-1212")
        self.assertEqual(action, "format")
        self.assertEqual(proposed, "(515) 555-1212")

    def test_extension_is_deferred(self) -> None:
        action, proposed = partition_safe_phone("(847) 437-3900 x330")
        self.assertEqual(action, "defer_ext")
        self.assertEqual(proposed, "")

    def test_xt_and_trailing_digits_are_deferred(self) -> None:
        self.assertEqual(partition_safe_phone("(708) 388-8770 xt 16")[0], "defer_ext")
        self.assertEqual(partition_safe_phone("(262) 569-1960 10130")[0], "defer_other")


class PhoneExtensionSplitTests(unittest.TestCase):
    def test_safe_markers_split_to_main_and_digits(self) -> None:
        cases = [
            ("(515) 555-1212 ext 44", "(515) 555-1212", "44"),
            ("(708) 388-8770 xt 16", "(708) 388-8770", "16"),
            ("(847) 437-3900 x330", "(847) 437-3900", "330"),
            ("(847) 768-1008x101", "(847) 768-1008", "101"),
            ("6206636161ext7238", "(620) 663-6161", "7238"),
        ]
        for raw, main, ext in cases:
            stored_main, stored_ext = store_phone_parts(raw)
            self.assertEqual(stored_main, main, raw)
            self.assertEqual(stored_ext, ext, raw)
            self.assertEqual(format_us_phone_display(raw), f"{main} x{ext}")

    def test_review_examples_are_not_split(self) -> None:
        leftover = store_phone_parts("(262) 569-1960 10130")
        self.assertEqual(leftover, ("(262) 569-1960 10130", ""))
        malformed = store_phone_parts("(734)4265-2803 x2166")
        self.assertEqual(malformed[1], "")
        note = store_phone_parts("(712) 248-8776 (direct #)")
        self.assertEqual(note[1], "")

    def test_no_extension_callers_still_format(self) -> None:
        from contact_phone import format_phone_with_extension

        main, ext = store_phone_parts("515-555-1212")
        self.assertEqual(main, "(515) 555-1212")
        self.assertEqual(ext, "")
        self.assertEqual(format_phone_with_extension(main, ext), "(515) 555-1212")
        self.assertEqual(
            format_phone_with_extension("(515) 555-1212", "44"),
            "(515) 555-1212 x44",
        )


if __name__ == "__main__":
    unittest.main()
