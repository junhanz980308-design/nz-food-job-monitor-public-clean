from jobwatch.sources import adzuna_search, greenhouse
import pytest
from jobwatch.sources import ashby, lever, smartrecruiters, workable, SourceError


class FakeResponse:
    status_code = 200
    def __init__(self, content):
        self.content = content
    def json(self):
        return self.content


def test_adzuna_uses_nz_api_and_does_not_invent_original_date(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "TEST")
    monkeypatch.setenv("ADZUNA_APP_KEY", "TEST")
    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"results": [{"id": "a", "title": "Food Scientist", "company": {"display_name": "A"}, "location": {"display_name": "Auckland"}, "created": "2026-10-08T22:00:00Z", "redirect_url": "https://www.adzuna.co.nz/details/5"}]})
    monkeypatch.setattr("jobwatch.sources.requests.get", fake_get)
    result = adzuna_search("food scientist")
    assert "/jobs/nz/search/1" in calls[0][0]
    assert result.jobs[0].published_label.startswith("Adzuna listing")
    assert result.jobs[0].company == "A"


def test_greenhouse_nz_only(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse({"jobs": [{"id": 1, "title": "Research Scientist", "location": {"name": "Auckland"}, "absolute_url": "https://boards.greenhouse.io/a/jobs/1"}, {"id": 2, "title": "Research Scientist", "location": {"name": "Sydney, Australia"}, "absolute_url": "https://boards.greenhouse.io/a/jobs/2"}]})
    monkeypatch.setattr("jobwatch.sources.requests.get", fake_get)
    result = greenhouse("Example", "example", True)
    assert [j.source_id for j in result.jobs] == ["1"]


def test_workable_public_endpoint_filters_countries(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return {'jobs': [
            {'shortcode': 'NZ1', 'title': 'Food Scientist', 'location': {'country_code': 'NZ', 'city': 'Auckland'}, 'url': 'https://apply.workable.com/j/NZ1/', 'description': '<p>Food research</p>'},
            {'shortcode': 'FR1', 'title': 'Food Scientist', 'location': {'country_code': 'FR', 'city': 'Paris'}},
        ]}
    monkeypatch.setattr('jobwatch.sources.get_json', fetch)
    result = workable('Example', 'verified-board', True)
    assert [job.source_id for job in result.jobs] == ['NZ1']
    assert calls == [('https://www.workable.com/api/accounts/verified-board', {'params': {'details': 'true'}})]
    assert not result.jobs[0].published_at


def test_lever_does_not_silently_truncate_after_300(monkeypatch):
    calls = []
    def fetch(url, params):
        calls.append(params['skip'])
        count = 100 if params['skip'] < 300 else 1
        return [{'id': str(params['skip'] + i), 'text': 'Food Scientist', 'categories': {'location': 'Auckland'}} for i in range(count)]
    monkeypatch.setattr('jobwatch.sources.get_json', fetch)
    assert len(lever('Example', 'board', True).jobs) == 301
    assert calls == [0, 100, 200, 300]


def test_smartrecruiters_list_and_detail(monkeypatch):
    def fetch(url, **kwargs):
        if url.endswith('/postings'):
            return {'totalFound': 1, 'content': [{'id': '1', 'name': 'Food Scientist', 'location': {'country': 'nz', 'city': 'Auckland'}, 'releasedDate': '2026-10-01T00:00:00Z'}]}
        return {'jobAd': {'sections': {'jobDescription': {'text': '<p>Food aroma research</p>'}}}, 'applyUrl': 'https://jobs.smartrecruiters.com/Example/1'}
    monkeypatch.setattr('jobwatch.sources.get_json', fetch)
    result = smartrecruiters('Example', 'board', True)
    assert result.jobs[0].description == 'Food aroma research'
    assert result.jobs[0].published_at == '2026-10-01T00:00:00Z'


def test_ashby_hides_unlisted_jobs_and_labels_republication(monkeypatch):
    monkeypatch.setattr('jobwatch.sources.get_json', lambda *a, **kw: {'jobs': [
        {'id': '1', 'title': 'Food Scientist', 'location': 'Auckland', 'isListed': True, 'publishedAt': '2026-10-01T00:00:00Z'},
        {'id': '2', 'title': 'Food Scientist', 'isListed': False},
    ]})
    result = ashby('Example', 'board', True)
    assert [job.source_id for job in result.jobs] == ['1']
    assert 'reposting' in result.jobs[0].published_label
