import pytest
from jobwatch.matching import match_job
from jobwatch.models import Job


def match(title, description=''):
    return match_job(Job('test', 'id', title, description=description))


@pytest.mark.parametrize('title,tier', [
    ('Analytical Scientist', 'AMBIGUOUS'), ('Applications Technologist', 'AMBIGUOUS'),
    ('Research Fellow', 'AMBIGUOUS'), ('Postdoctoral Fellow', 'AMBIGUOUS'),
    ('Post-doctoral Researcher', 'AMBIGUOUS'), ('NPD Technologist', 'POSSIBLE MATCH'),
    ('R&D Technologist', 'POSSIBLE MATCH'), ('Product Development Scientist', 'POSSIBLE MATCH'),
    ('Formulation Scientist', 'POSSIBLE MATCH'), ('Dairy Scientist', 'POSSIBLE MATCH'),
])
def test_title_only_is_useful_but_not_overconfident(title, tier):
    result = match(title)
    assert result.tier == tier and result.score < 70
    assert 'context.title_only' in result.signals
    assert match(title, title).tier == tier
    assert match(title, None).tier == tier


@pytest.mark.parametrize('title,description', [
    ('Analytical Scientist', 'Food chemistry using chromatography and GC×GC-TOFMS.'),
    ('Applications Technologist', 'Develop dairy ingredients and food formulations.'),
    ('Research Fellow', 'Plant-based milk research with hydrocolloids and emulsions.'),
    ('Postdoctoral Fellow', 'Food flavours and mass spectrometry research.'),
    ('NPD Technologist', 'Food research and formulation.'),
])
def test_relevant_evidence_high(title, description):
    assert match(title, description).tier == 'HIGH MATCH'


@pytest.mark.parametrize('title,description', [
    ('Research Fellow', 'Quantum computing and cosmology research.'),
    ('Analytical Scientist', 'Financial modelling for investment funds.'),
    ('Postdoctoral Fellow', 'Clinical trials in oncology.'),
    ('Product Development Scientist', 'Software development and computer vision.'),
    ('Sales Manager', 'Food ingredients, hydrocolloids and emulsions.'),
])
def test_unrelated_research_or_commercial_work_is_low(title, description):
    result = match(title, description)
    assert result.tier == 'LOW MATCH'
    assert 'penalty.unrelated' in result.signals


@pytest.mark.parametrize('restriction', [
    'Candidates must be New Zealand citizens only.',
    'NZ citizens or permanent residents only.',
    'Applicants must hold New Zealand citizenship.',
    'Applicants must be citizens or permanent residents of New Zealand.',
])
def test_citizenship_restrictions_override_relevance(restriction):
    result = match('Food Scientist', 'Dairy research and formulation. ' + restriction)
    assert result.tier == 'LOW MATCH' and result.score <= 19
    assert 'restriction.citizenship_or_pr' in result.signals


def test_generic_analytical_evidence_is_not_high_or_sponsored():
    result = match('Research Scientist', 'Analytical chemistry research and chromatography.')
    assert result.tier == 'POSSIBLE MATCH' and result.score < 70
    assert result.rights_flag.startswith('未核实')
