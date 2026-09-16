"""Regression tests for real-world resume layouts and number formats.

Every case here comes from a formatting convention that broke the parser on an
actual resume. They differ in how a role header is split across lines, how dates
are aligned, and which numbering conventions the candidate uses.

These matter more than they look: a layout the parser mishandles does not produce
an obvious crash, it produces a confident profile with the wrong employer, a
missing degree, or a figure understated by seven orders of magnitude.
"""

import pytest

from job_agent.config.schema import METRIC_SEPARATOR, LockedFact, split_metric_values
from job_agent.intake.heuristic import (
    _heading_key,
    parse_certifications,
    parse_education,
    parse_experience,
    parse_skills,
)
from job_agent.intake.validator import extract_potential_metrics


# ==============================================================================
# EXPERIENCE LAYOUTS
# ==============================================================================

def test_stacked_role_blocks_are_parsed():
    """Title, employer and dates on three separate lines.

    The Word and Google Docs default. Requiring the date on the same line as the
    employer lost the entire work history.
    """
    roles = parse_experience([
        "Staff Platform Engineer",
        "Helios Data",
        "March 2021 - Present",
        "• Led migration of 240 services to Kubernetes.",
        "Backend Engineer",
        "Corvus Health",
        "June 2018 - February 2021",
        "• Designed an appointment service handling 1.2M bookings monthly.",
    ])

    assert [role["company"] for role in roles] == ["Helios Data", "Corvus Health"]
    assert [role["title"] for role in roles] == ["Staff Platform Engineer", "Backend Engineer"]
    assert roles[0]["start_date"] == "2021-03"
    assert roles[0]["end_date"] == "Present"
    assert len(roles[0]["description_bullets"]) == 1


def test_right_aligned_dates_take_the_title_from_the_next_line():
    roles = parse_experience([
        "Northwind Analytics Jan 2022 – Present",
        "Senior Data Engineer",
        "• Rebuilt the ETL layer to process 45M rows nightly.",
    ])
    assert roles[0]["company"] == "Northwind Analytics"
    assert roles[0]["title"] == "Senior Data Engineer"
    assert roles[0]["description_bullets"] == ["Rebuilt the ETL layer to process 45M rows nightly."]


def test_wrapped_bullets_are_rejoined():
    """PDF extraction emits one line per visual row, splitting long bullets."""
    roles = parse_experience([
        "Acme Corp - Product Lead (2022 - Present)",
        "• Owned the product connecting MSMEs to government schemes and bank",
        "financing; scaled to 15,000+ users.",
        "• A second, separate achievement.",
    ])
    bullets = roles[0]["description_bullets"]
    assert len(bullets) == 2, bullets
    assert bullets[0].endswith("scaled to 15,000+ users.")


def test_role_dated_with_a_single_year_is_not_swallowed():
    """A short engagement dated with one year used to become a bullet above it."""
    roles = parse_experience([
        "Research Intern - Bright Labs (2024 - 2025)",
        "• Ran ablation studies.",
        "Product Analyst (Consulting) - Vizitor | Visitor Management SaaS, Remote 2025",
        "• Benchmarked the platform against competitors.",
    ])
    assert len(roles) == 2
    assert roles[1]["company"] == "Vizitor"
    assert roles[1]["start_date"] == "2025"
    assert roles[1]["end_date"] == "2025"


def test_employer_is_chosen_by_company_suffix_not_position():
    roles = parse_experience([
        "SDE Trainee - AI/ML (Backend & Data) | Antino Labs, Gurugram Jun 2024 - Present",
    ])
    assert roles[0]["company"].startswith("Antino Labs")
    assert roles[0]["title"] == "SDE Trainee"


def test_inline_headers_still_work():
    """The original single-line format must not regress."""
    roles = parse_experience([
        "ScaleFlow Technologies - Lead Infrastructure Engineer (2022 - Present)",
        "• Architected clusters supporting 200k RPS.",
    ])
    assert roles[0]["company"] == "ScaleFlow Technologies"
    assert roles[0]["title"] == "Lead Infrastructure Engineer"


# ==============================================================================
# SECTION HEADINGS
# ==============================================================================

def test_compound_section_headings_are_recognised():
    """A compound heading is still an education section; missing it lost the degree."""
    assert _heading_key("EDUCATION & PUBLICATIONS") == "education"
    assert _heading_key("AI PRODUCT PROJECTS") == "projects"
    assert _heading_key("Technical Skills & Tools") == "skills"
    assert _heading_key("PROFESSIONAL EXPERIENCE") == "experience"


def test_prose_mentioning_an_alias_is_not_a_heading():
    """Matching alias words anywhere would turn a bullet into a section break."""
    assert _heading_key("Led 3 projects") is None
    assert _heading_key("Managed several projects end to end") is None
    assert _heading_key("RAJ ARYAN") is None


# ==============================================================================
# EDUCATION LAYOUTS
# ==============================================================================

def test_degree_on_a_separate_line_completes_the_entry():
    entries = parse_education([
        "MSc Computer Science",
        "University College London",
        "2016 - 2018",
    ])
    assert entries[0]["institution"] == "University College London"
    assert entries[0]["degree"] == "MSc"
    assert entries[0]["field_of_study"] == "Computer Science"


def test_institution_without_a_keyword_is_still_captured():
    """A school named by acronym has no university/college/institute keyword."""
    entries = parse_education([
        "B.Tech, Computer Science & Engineering (AI) | CSVTU, Bhilai 2022 – 2026",
    ])
    assert len(entries) == 1
    assert entries[0]["institution"] == "CSVTU, Bhilai"
    assert entries[0]["degree"] == "B.Tech"
    assert entries[0]["start_date"] == "2022"
    assert entries[0]["end_date"] == "2026"


def test_institution_name_is_not_eaten_by_the_date_regex():
    """A loose month prefix consumed the word before the year."""
    entries = parse_education(["University of Waterloo 2015 – 2019"])
    assert entries[0]["institution"] == "University of Waterloo"
    assert entries[0]["start_date"] == "2015"


def test_compound_degree_abbreviations_are_not_clipped():
    entries = parse_education(["BASc in Computer Engineering | University of Waterloo 2015 - 2019"])
    assert entries[0]["degree"] == "BASc"
    assert entries[0]["field_of_study"] == "Computer Engineering"


# ==============================================================================
# SKILLS AND CERTIFICATIONS
# ==============================================================================

def test_wrapped_skill_lines_are_rejoined():
    skills = parse_skills([
        "• Product: Requirements & roadmapping, A/B testing, stakeholder",
        "management, Jira, Figma.",
    ])
    flat = [item for values in skills.values() for item in values]
    assert "stakeholder management" in flat
    assert "Figma" in flat, "a trailing full stop must not stick to the skill"


def test_certification_level_is_kept_with_its_name():
    entries = parse_certifications([
        "AWS Certified Developer - Associate — Amazon Web Services (2022)",
    ])
    assert entries[0]["name"] == "AWS Certified Developer - Associate"
    assert entries[0]["issuer"] == "Amazon Web Services"
    assert entries[0]["issue_date"] == "2022"


# ==============================================================================
# NUMBERS AND METRICS
# ==============================================================================

def test_indian_magnitude_units_are_not_truncated():
    """Dropping "crore" understated a real figure by seven orders of magnitude."""
    assert extract_potential_metrics("facilitated ₹45+ crore in financing") == ["₹45+ crore"]
    assert extract_potential_metrics("₹1.5 lakh saved monthly") == ["₹1.5 lakh"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("scaled to 15,000+ users", "15,000+ users"),
        ("ingesting 3.28M+ records", "3.28M+ records"),
        ("a 50+ parameter eligibility-matching engine", "50+ parameter eligibility-matching engine"),
        ("user research with 500+ SMBs", "500+ SMBs"),
        ("across 15 nationalized bank partners", "15 nationalized bank partners"),
        ("contributing to 2 manuscripts under review", "2 manuscripts"),
        ("reaching 92%+ accuracy", "92%+"),
        ("saved $2.5M", "$2.5M"),
        ("sped up pipelines by 4x", "4x"),
        ("cut costs by £95k per year", "£95k"),
    ],
)
def test_metric_shapes_are_captured(text, expected):
    assert expected in extract_potential_metrics(text)


def test_metrics_do_not_capture_version_numbers_or_model_names():
    """Over-capture would seal junk as a verified achievement."""
    assert extract_potential_metrics("into PostgreSQL 17 with Parquet-based deduplication") == []
    assert extract_potential_metrics("Built 1D CNN and spectral-feature models") == []
    assert extract_potential_metrics("two-step (Basic + Bearer) auth") == []


def test_metric_phrase_stops_at_the_first_connective():
    assert extract_potential_metrics("supporting over 200k RPS with 99.999% SLA") == ["200k RPS", "99.999%"]


def test_thousands_separator_survives_storage_round_trip():
    """Joining metrics with a comma shredded "15,000+ users" into two fragments."""
    fact = LockedFact(
        category="scale",
        statement="Scaled the platform to 15,000+ users.",
        metric_value=METRIC_SEPARATOR.join(["15,000+ users", "₹45+ crore"]),
    )
    assert fact.metrics() == ["15,000+ users", "₹45+ crore"]


def test_legacy_comma_joined_metrics_still_split():
    """Profiles sealed before the separator changed must keep working."""
    assert split_metric_values("200k RPS, 99.999%") == ["200k RPS", "99.999%"]
    assert split_metric_values("15,000+ users, ₹45+ crore") == ["15,000+ users", "₹45+ crore"]


# ==============================================================================
# LOCATION AND COUNTRY INFERENCE
# ==============================================================================

@pytest.mark.parametrize(
    "location,expected",
    [
        ("San Francisco, CA", "United States"),
        ("Austin, TX", "United States"),
        ("Toronto, ON", "Canada"),
        ("Vancouver, BC", "Canada"),
        ("London, United Kingdom", "United Kingdom"),
        ("Bengaluru, India", "India"),
        # A bare city names no country, and guessing one would drive wrong
        # answers to every work-authorization question on an application form.
        ("Gurugram", "Unspecified"),
        ("Remote", "Unspecified"),
    ],
)
def test_country_is_inferred_only_when_it_is_actually_known(location, expected):
    from job_agent.intake.heuristic import _infer_work_authorization

    assert _infer_work_authorization("", location)["current_country"] == expected


def test_location_is_read_from_a_based_in_line():
    from job_agent.intake.heuristic import extract_contact

    header = [
        "RAJ ARYAN",
        "Associate Product Manager | AI/ML",
        "+91-6287278385 | someone@example.com",
        "Based in Gurugram \u2022 Open to relocate to Mumbai",
    ]
    contact = extract_contact(header, "\n".join(header))
    assert contact["location"] == "Gurugram"
    assert contact["full_name"] == "RAJ ARYAN", "an all-caps name must not be skipped"
