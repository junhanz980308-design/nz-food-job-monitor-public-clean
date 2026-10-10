from jobwatch.models import Job, canonical_url, is_nz_location


def test_canonical_urls_remove_trackers():
    assert canonical_url("https://nz.seek.com/job/12345?utm_source=email&trackingid=123") == "https://nz.seek.com/job/12345"
    assert canonical_url("https://www.linkedin.com/comm/jobs/view/987654?trk=email") == "https://www.linkedin.com/jobs/view/987654"
    assert canonical_url("https://nz.indeed.com/viewjob?jk=abc123&utm_campaign=weekly") == "https://nz.indeed.com/viewjob?jk=abc123"
    assert canonical_url("javascript:alert(1)") == ""


def test_dedupe_aliases_include_cross_source_role():
    a = Job("Adzuna", "100", "Food Scientist", "Food Co Ltd", "Auckland", url="https://adzuna.co.nz/details/100")
    b = Job("LinkedIn email alert", "200", "Food Scientist", "Food Co", "Auckland", url="https://linkedin.com/jobs/view/200")
    assert set(a.tokens()) & set(b.tokens())


def test_nz_location_filter():
    assert is_nz_location("Palmerston North, New Zealand")
    assert is_nz_location("Hamilton, Waikato")
    assert not is_nz_location("Melbourne, Australia")
    assert not is_nz_location("Remote")


def test_location_variants_deduped():
    first = Job("Adzuna", "a", "Food Scientist", "Good Foods", "Auckland, Auckland", url="https://adzuna.co.nz/details/10")
    second = Job("LinkedIn email alert", "b", "Food Scientist", "Good Foods", "Auckland", url="https://linkedin.com/jobs/view/20")
    assert set(first.tokens()) & set(second.tokens())
