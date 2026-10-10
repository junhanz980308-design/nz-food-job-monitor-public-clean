from jobwatch.matching import match_job
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
