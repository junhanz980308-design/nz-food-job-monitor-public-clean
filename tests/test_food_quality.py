"""Synthetic matching and offline delivery-eligibility regressions."""
import copy
import json
import logging

import pytest

from jobwatch.audit import Audit
from jobwatch.matching import ALERT_TIERS, match_job
from jobwatch.models import Job, SourceResult
from jobwatch.runner import process_results, run
from jobwatch.state import State


def job(title, description="", ident="1"):
    return Job("SEEK email alert", ident, title, description=description,
               url=f"https://nz.seek.com/job/{ident}")


@pytest.mark.parametrize("title,description", [
    ("Food Quality Officer", ""),
    ("Quality Assurance Officer", "Quality assurance for a food manufacturer."),
    ("QA Technician", "Quality control in beverage production."),
    ("QC Technician", "Test ingredients at a food production site."),
    ("QA/QC Officer", "Inspect milk and cheese production."),
    ("Quality Control Analyst", "Test food ingredients using chromatography."),
    ("Food Safety Specialist", "Maintain HACCP systems."),
    ("Quality Technologist", "Quality control of dairy products."),
    ("Food Quality Manager", ""),
    ("HACCP Coordinator", "Manage food safety in food manufacturing."),
    ("Food Compliance Officer", ""),
    ("Regulatory Affairs Officer", "Ensure regulatory compliance for food ingredients."),
    ("Regulatory Affairs Associate", "Beverage labelling and product compliance."),
    ("Food Auditor", ""),
    ("Food Inspector", ""),
    ("Quality Auditor", "Inspect food production sites."),
    ("Compliance Advisor", "Food manufacturing compliance."),
    ("Site Officer", "Responsible for quality assurance in food production."),
    ("Pharmaceutical Quality Officer", "Responsible for quality control of food ingredients in a separate food division."),
])
def test_food_quality_is_alert_eligible_below_science_priority(title, description):
    result = match_job(job(title, description))
    assert result.tier == "POSSIBLE MATCH" and result.tier in ALERT_TIERS
    assert 42 <= result.score < 70
    assert {"title.quality", "evidence.quality_duties"} & set(result.signals)
    assert result.rights_flag.startswith("未核实")


@pytest.mark.parametrize("title,description", [
    ("Software QA Engineer", "Software testing for a food delivery application."),
    ("QA Technician", "Testing software for food retailers."),
    ("IT Quality Officer", "Quality assurance of a beverage ordering application."),
    ("QA Analyst", "Selenium and test automation for an ingredients marketplace."),
    ("Construction Quality Inspector", "Inspect concrete at a food factory construction site."),
    ("Quality Control Analyst", "Quality control for concrete construction. Free food."),
    ("Pharmaceutical Quality Analyst", "Quality control of active pharmaceutical ingredients."),
    ("Quality Assurance Officer", "Pharmaceutical batch release. Free food at work."),
    ("Quality Technician", "Test medical devices; food vouchers provided."),
    ("Regulatory Affairs Officer", "Regulatory compliance for medicines and drug products."),
    ("Food Sales Manager", "Manage quality assurance for food customers."),
])
def test_unrelated_quality_never_notifies(title, description):
    result = match_job(job(title, description))
    assert result.tier == "LOW MATCH" and result.tier not in ALERT_TIERS
    assert "penalty.unrelated" in result.signals


@pytest.mark.parametrize("title,description", [
    ("QA Technician", ""), ("QC Analyst", None),
    ("Quality Assurance Officer", "Quality Assurance Officer"),
    ("Regulatory Affairs Officer", "Maintain regulatory compliance."),
    ("Quality Officer", "Free food and subsidised food are benefits. Food vouchers provided."),
])
def test_insufficient_food_context_is_diagnostic_only(title, description):
    result = match_job(job(title, description))
    assert result.tier == "AMBIGUOUS" and result.score < 42
    assert result.tier not in ALERT_TIERS


def test_quality_bonus_cannot_overtake_science_or_override_rights():
    description = "Food research, analytical chemistry, emulsions and shelf-life."
    result = match_job(job("Quality Control Analyst", description))
    assert (result.score, result.tier) == (69, "POSSIBLE MATCH")
    restricted = match_job(job("Food Quality Officer", "NZ citizens or permanent residents only."))
    assert restricted.score == 19 and restricted.tier == "LOW MATCH"
    assert "restriction.citizenship_or_pr" in restricted.signals
    work_rights = match_job(job("Food Quality Officer", "Current NZ work rights required."))
    assert work_rights.tier == "POSSIBLE MATCH"
    assert "restriction.existing_work_rights" in work_rights.signals


# Exact scores from main before the quality extension, including title-only jobs.
@pytest.mark.parametrize("title,description,score,tier", [
    ("Food Scientist", "", 71, "HIGH MATCH"),
    ("Food Scientist", "Dairy research and formulation.", 84, "HIGH MATCH"),
    ("NPD Technologist", "", 47, "POSSIBLE MATCH"),
    ("NPD Technologist", "Food research and formulation.", 70, "HIGH MATCH"),
    ("R&D Technologist", "", 47, "POSSIBLE MATCH"),
    ("Product Development Scientist", "Develop plant-based foods and emulsions.", 82, "HIGH MATCH"),
    ("Formulation Scientist", "Food emulsions and texture.", 82, "HIGH MATCH"),
    ("Research Fellow", "", 41, "AMBIGUOUS"),
    ("Postdoctoral Fellow", "Food flavours and mass spectrometry research.", 88, "HIGH MATCH"),
    ("Analytical Scientist", "Food chemistry using chromatography and GC×GC-TOFMS.", 75, "HIGH MATCH"),
    ("Applications Technologist", "Develop dairy ingredients and food formulations.", 70, "HIGH MATCH"),
    ("Dairy Scientist", "", 48, "POSSIBLE MATCH"),
    ("Research Scientist", "Analytical chemistry research and chromatography.", 65, "POSSIBLE MATCH"),
])
def test_science_scores_unchanged_even_with_quality_duties(title, description, score, tier):
    for text in (description, description + " Manage quality assurance."):
        result = match_job(job(title, text))
        assert (result.score, result.tier) == (score, tier)
        assert not {"title.quality", "evidence.quality_duties"} & set(result.signals)


def test_only_eligible_new_jobs_enter_telegram_queue_and_old_quality_stays_suppressed(tmp_path):
    state = State(tmp_path / "unused.json")
    state.set_initialized("gmail-alerts")
    historical = job("Food Quality Officer", ident="1")  # Previously LOW on main.
    old_id, _ = state.observe(historical)
    old_record = copy.deepcopy(state.data["records"][old_id])
    state.data.update(mail_seen=["synthetic-ack"], mail_retry=["synthetic-retry"])
    audit, candidates = Audit(state), {}
    entries = [historical, job("Food Quality Officer", ident="2"),
               job("Food Scientist", ident="3"), job("QA Technician", ident="4"),
               job("Software QA Engineer", "Food delivery software testing.", "5")]
    process_results(state, SourceResult("gmail-alerts", entries, "ALERT_ONLY"),
                    candidates, dry_run=True, audit=audit)
    assert {item.source_id for item, _ in candidates.values()} == {"2", "3"}
    assert {match.tier for _, match in candidates.values()} == ALERT_TIERS
    assert audit.counts["historical_suppressed"] == 1
    assert state.data["records"][old_id] == old_record
    assert state.data["mail_seen"] == ["synthetic-ack"]
    assert state.data["mail_retry"] == ["synthetic-retry"]
    baseline = State(tmp_path / "baseline.json")
    candidates = {}
    process_results(baseline, SourceResult("gmail-alerts", entries, "ALERT_ONLY"),
                    candidates, dry_run=True)
    assert not candidates


def test_fully_mocked_quality_dry_run_preserves_history_and_private_logs(tmp_path, monkeypatch, caplog):
    cfg, history = tmp_path / "config.json", tmp_path / "state.json"
    cfg.write_text(json.dumps({"adzuna_queries": ["food scientist"]}))
    state = State(history)
    for source in ("gmail-alerts", "adzuna:food scientist"):
        state.set_initialized(source)
    state.data.update(mail_seen=["synthetic-ack"], mail_retry=["synthetic-retry"])
    state.save()
    original = history.read_bytes()
    fresh = job("Food Quality Officer")
    monkeypatch.setattr("jobwatch.runner.gmail_alerts", lambda _: (
        SourceResult("gmail-alerts", [fresh], "ALERT_ONLY"), ["synthetic-new-ack"]))
    calls = []
    def adzuna(query):
        calls.append(query)
        return SourceResult("adzuna:" + query, [fresh])
    monkeypatch.setattr("jobwatch.runner.adzuna_search", adzuna)
    monkeypatch.setattr("jobwatch.runner.send_message", lambda _: pytest.fail("Telegram called"))
    monkeypatch.setattr(State, "save", lambda _: pytest.fail("History written"))
    caplog.set_level(logging.INFO)
    assert run(str(cfg), str(history), "all", dry_run=True) == 0
    assert calls == ["food scientist"] and history.read_bytes() == original
    summary = json.loads(next(r.message.split("AUDIT_SUMMARY: ")[1]
                              for r in caplog.records if "AUDIT_SUMMARY: " in r.message))
    assert summary["counts"]["possible_match"] == 2
    assert summary["counts"]["duplicates"] == 1
    for private in ("Food Quality Officer", "https://", "synthetic-ack", "synthetic-retry", "synthetic-new-ack"):
        assert private not in caplog.text
