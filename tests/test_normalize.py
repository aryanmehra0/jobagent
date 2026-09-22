"""Unit tests for the shared normalization helpers in `config.normalize`.

These run ahead of every schema validator, so a regression here silently corrupts
data in all six phases.
"""

from datetime import date

import pytest

from job_agent.config.normalize import (
    clean_text,
    dedupe_preserving_order,
    is_present_token,
    looks_like_person_name,
    normalize_date_string,
    normalize_phone,
    normalize_url,
    parse_partial_date,
    parse_posting_timestamp,
    strip_bullet_prefix,
    strip_html,
    tokenize,
    truncate,
    years_between,
)


# --- Text -------------------------------------------------------------------

def test_clean_text_normalizes_pdf_artifacts():
    """Ligatures, non-breaking spaces, and smart quotes must not survive extraction."""
    assert clean_text("eﬃcient  systems") == "efficient systems"
    assert clean_text("“quoted”") == '"quoted"'
    assert clean_text("  spaced   out  ") == "spaced out"
    assert clean_text(None) == ""


def test_clean_text_converts_symbol_font_bullets():
    """PDF bullets encoded as DEL or private-use glyphs become a real bullet char."""
    assert clean_text("\x7f Architected clusters").startswith("•")
    assert clean_text(" Reduced costs").startswith("•")


def test_clean_text_strips_other_control_characters():
    """Control characters must never reach a PDF, a form field, or a spreadsheet."""
    assert clean_text("Reduced\x01 costs\x1f here") == "Reduced costs here"


def test_clean_text_strips_byte_order_mark():
    """A leading BOM (left over from decoding a UTF-8-with-BOM page as plain
    "utf-8", which doesn't strip it the way "utf-8-sig" would) must not become
    part of a saved job description or any other cleaned text."""
    assert clean_text("﻿Senior Engineer role") == "Senior Engineer role"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("• Architected clusters", "Architected clusters"),
        ("- Reduced costs", "Reduced costs"),
        ("3. Led the team", "Led the team"),
        ("\x7f Scaled the platform", "Scaled the platform"),
        ("$340k saved annually", "$340k saved annually"),  # Not a bullet marker.
    ],
)
def test_strip_bullet_prefix(line, expected):
    assert strip_bullet_prefix(clean_text(line)) == expected


def test_dedupe_preserves_first_casing():
    assert dedupe_preserving_order(["AWS", "aws", "Kubernetes", " AWS "]) == ["AWS", "Kubernetes"]


def test_truncate_marks_the_cut():
    result = truncate("x" * 100, 40)
    assert len(result) == 40
    assert result.endswith("...[truncated]")


# --- Dates ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019", "2019"),
        ("2020-06", "2020-06"),
        ("Mar 2021", "2021-03"),
        ("March 2021", "2021-03"),
        ("03/2021", "2021-03"),
        ("Present", "Present"),
        ("current", "Present"),
        (None, None),
    ],
)
def test_normalize_date_string(raw, expected):
    assert normalize_date_string(raw) == expected


def test_unparseable_date_is_preserved_not_discarded():
    """A weird date must not silently erase the role it belongs to."""
    assert normalize_date_string("sometime in the spring") == "sometime in the spring"


def test_parse_partial_date_rejects_out_of_range_years():
    assert parse_partial_date("1850") is None
    assert parse_partial_date("2020-13") is None
    assert parse_partial_date("2020-06") == date(2020, 6, 1)


def test_is_present_token():
    assert is_present_token("Present") and is_present_token("  now ")
    assert not is_present_token("2020")


def test_years_between_merges_overlapping_roles():
    """Two concurrent roles must count once, not twice."""
    overlapping = [(date(2020, 1, 1), date(2022, 1, 1)), (date(2021, 1, 1), date(2024, 1, 1))]
    assert years_between(overlapping) == 4.0

    sequential = [(date(2018, 1, 1), date(2020, 1, 1)), (date(2022, 1, 1), date(2024, 1, 1))]
    assert years_between(sequential) == 4.0


def test_parse_posting_timestamp_handles_epoch_and_iso():
    """Lever emits epoch milliseconds; Greenhouse emits ISO-8601 with an offset."""
    assert parse_posting_timestamp("1700000000000").year == 2023
    assert parse_posting_timestamp("2024-03-01T10:00:00Z").month == 3
    assert parse_posting_timestamp("not a date") is None


# --- Contact details --------------------------------------------------------

def test_normalize_url_adds_scheme_and_rejects_junk():
    assert normalize_url("linkedin.com/in/alex") == "https://linkedin.com/in/alex"
    assert normalize_url("https://github.com/alex") == "https://github.com/alex"
    assert normalize_url("N/A") is None
    assert normalize_url("not a url") is None
    assert normalize_url(None) is None


def test_normalize_phone_rejects_metrics_and_years():
    """A resume metric must never be mistaken for a phone number."""
    assert normalize_phone("+1 (415) 555-0142") == "+1 (415) 555-0142"
    assert normalize_phone("200k RPS") is None
    assert normalize_phone("99.999") is None
    assert normalize_phone("12345") is None  # Too few digits.


def test_looks_like_person_name_accepts_all_caps():
    """Many resumes set the candidate name in capitals.

    Rejecting that shape made the parser skip the real name and take the headline
    beneath it as the candidate. Section headings are filtered by the intake
    module, which knows the heading list; this check stays deliberately permissive.
    """
    assert looks_like_person_name("Alex Rivera")
    assert looks_like_person_name("RAJ ARYAN")
    assert looks_like_person_name("MARIE-CLAIRE O'BRIEN")


def test_looks_like_person_name_rejects_non_names():
    assert not looks_like_person_name("alex@example.com")
    assert not looks_like_person_name("Engineer 2020")
    assert not looks_like_person_name("CURRICULUM VITAE")
    assert not looks_like_person_name("Resume")
    assert not looks_like_person_name("https://example.com/me")


def test_strip_html_preserves_list_structure():
    """Requirement lists are the part of a posting the re-ranker depends on."""
    html = "<p>About us</p><ul><li>AWS</li><li>Kubernetes</li></ul>"
    assert strip_html(html) == "About us\n- AWS\n- Kubernetes"
    assert "&nbsp;" not in strip_html("a&nbsp;b")


def test_tokenize_lowercases_and_filters_short_tokens():
    assert tokenize("Senior Go Engineer") == {"senior", "engineer"}
    assert "go" in tokenize("Senior Go Engineer", min_length=2)
