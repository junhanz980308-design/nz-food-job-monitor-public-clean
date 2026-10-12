import pytest

from jobwatch.matching import ALERT_TIERS, match_job
from jobwatch.models import Job


def job(title, description):
    return Job("test", "123", title, "Company", "Auckland", description, "https://example.com/job/123")


def test_food_technologist_high_match():
    match = match_job(job("Food Technologist", "Food formulation and product development in dairy."))
    assert match.tier == "HIGH MATCH"
    assert match.score >= 70


def test_postdoc_food_aroma_high_match():
    match = match_job(job("Postdoctoral Researcher", "Research in food aroma using GC-MS analytical chemistry."))
    assert match.tier == "HIGH MATCH"


def test_sales_role_filtered_out():
    match = match_job(job("Sales Manager", "Sell food ingredients to customers."))
    assert match.tier == "LOW MATCH"


def test_restriction_flag():
    match = match_job(job("Food Scientist", "Candidates must be New Zealand citizens only."))
    assert match.tier == "LOW MATCH"
    assert "公民" in match.rights_flag


def test_irrelevant_research_low_match():
    result = match_job(job("Research Scientist", "Cyber security and cryptographic algorithms."))
    assert result.tier == "LOW MATCH"


@pytest.mark.parametrize("title", ["Laboratory Technician", "Lab Technician"])
@pytest.mark.parametrize("description,score", [
    ("Test food ingredients in a production laboratory.", 47),
    ("Routine testing of dairy and milk products.", 47),
    ("Chemical analysis using GC-MS.", 42),
    ("Chemical analysis using HPLC.", 42),
    ("Chemical analysis using UHPLC.", 42),
    ("Chemical analysis using ICP-MS.", 42),
    ("Food testing using HPLC and ICP-MS.", 65),
    ("Food testing using GC-MS and HPLC.", 65),
])
def test_relevant_lab_technician_is_possible_and_telegram_eligible(title, description, score):
    result = match_job(job(title, description))
    assert (result.score, result.tier) == (score, "POSSIBLE MATCH")
    assert result.tier in ALERT_TIERS
    assert "title.lab_technician" in result.signals
    assert result.rights_flag.startswith("未核实")


@pytest.mark.parametrize("title", ["Laboratory Technician", "Lab Technician"])
@pytest.mark.parametrize("description", [
    "", None, "General laboratory duties and cleaning equipment.",
    "Routine research support.", "Free food and food vouchers are staff benefits.",
    "Maintain the HPLCX inventory system.",
])
def test_generic_lab_technician_remains_ambiguous_without_notification(title, description):
    result = match_job(job(title, description))
    assert result.tier == "AMBIGUOUS" and result.score < 42
    assert result.tier not in ALERT_TIERS
    assert "context.insufficient_relevance" in result.signals


@pytest.mark.parametrize("title,description", [
    ("Clinical Laboratory Technician", "Analyse samples using GC-MS and HPLC."),
    ("Lab Technician", "Clinical diagnostics using UHPLC and ICP-MS."),
    ("Laboratory Technician", "Pathology and histology testing of patient samples."),
    ("Medical Lab Technician", "Routine haematology and chemical analysis using HPLC."),
    ("Dental Lab Technician", "Prepare dental devices. Free food provided."),
    ("Lab Technician", "Veterinary diagnostics using GC-MS."),
    ("Laboratory Technician", "Concrete and geotechnical testing for construction."),
    ("Laboratory Technician", "Software testing for food delivery applications."),
    ("Sales Laboratory Technician", "Sell food analysis equipment and HPLC systems."),
])
def test_unrelated_lab_roles_stay_excluded_even_with_methods_or_food_words(title, description):
    result = match_job(job(title, description))
    assert result.tier == "LOW MATCH" and result.tier not in ALERT_TIERS
    assert "penalty.unrelated" in result.signals


@pytest.mark.parametrize("title", ["Food Laboratory Technician", "Dairy Lab Technician", "Senior LAB TECHNICIAN", "Laboratory Technicians"])
def test_lab_title_variations_are_recognized(title):
    result = match_job(job(title, "Food testing with UHPLC."))
    assert result.tier == "POSSIBLE MATCH" and "title.lab_technician" in result.signals


@pytest.mark.parametrize("title", ["Laboratory Technician - R&D", "Lab Technician - Product Development"])
def test_generic_lab_development_title_still_needs_relevant_evidence(title):
    result = match_job(job(title, "General laboratory duties."))
    assert result.tier == "AMBIGUOUS" and result.tier not in ALERT_TIERS


def test_food_lab_is_not_excluded_by_unrelated_facility_or_benefit_words():
    result = match_job(job("Lab Technician", "Food analysis using HPLC. A new site is under construction. Medical insurance provided."))
    assert result.tier == "POSSIBLE MATCH" and result.tier in ALERT_TIERS


def test_lab_technician_is_capped_and_citizenship_still_overrides():
    description = "Food research, emulsions, analytical chemistry and GC-MS."
    result = match_job(job("Laboratory Technician", description))
    assert (result.score, result.tier) == (69, "POSSIBLE MATCH")
    restricted = match_job(job("Lab Technician", description + " NZ citizens or permanent residents only."))
    assert restricted.tier == "LOW MATCH" and restricted.score <= 19
    assert "restriction.citizenship_or_pr" in restricted.signals


@pytest.mark.parametrize("title,description,score,tier", [
    ("Food Scientist", "Dairy research and formulation.", 84, "HIGH MATCH"),
    ("Analytical Scientist", "Food chemistry using chromatography and GC-MS.", 75, "HIGH MATCH"),
    ("NPD Technologist", "Food research and formulation.", 70, "HIGH MATCH"),
    ("Research Fellow", "HPLC and UHPLC testing.", 41, "AMBIGUOUS"),
    ("Food Quality Officer", "", 47, "POSSIBLE MATCH"),
])
def test_lab_support_does_not_change_existing_science_or_quality_scores(title, description, score, tier):
    result = match_job(job(title, description))
    assert (result.score, result.tier) == (score, tier)
    assert "title.lab_technician" not in result.signals
