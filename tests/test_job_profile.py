from job_profile import (
    ApplicantProfile,
    domain_of,
    load_site_credentials,
    to_browser_use_dict,
)


def test_from_profile_dict_empty():
    p = ApplicantProfile.from_profile_dict({})
    assert p.first_name == "" and not p.is_configured()


def test_from_profile_dict_missing_applicant_key():
    p = ApplicantProfile.from_profile_dict({"owner": {"name": "Arjun"}})
    assert p.first_name == ""


def test_from_profile_dict_full():
    profile = {
        "applicant": {
            "first_name": "Alex",
            "last_name": "Kim",
            "email": "alex@example.com",
            "phone": "555-0100",
            "address": {"line1": "1 Main St", "city": "Metropolis", "postal_code": "12345", "country": "USA"},
            "linkedin": "https://linkedin.com/in/alexkim",
            "resume_path": "documents/resume.pdf",
            "cover_letter_path": "documents/cover.pdf",
            "work_authorization": {"us_citizen": True, "sponsorship_needed": False},
            "demographics": {"gender": "Prefer not to say"},
            "custom_qa": {"Why do you want to work here?": "Great mission"},
        }
    }
    p = ApplicantProfile.from_profile_dict(profile)
    assert p.first_name == "Alex" and p.email == "alex@example.com"
    assert p.is_configured()
    assert p.custom_qa["Why do you want to work here?"] == "Great mission"


def test_is_configured_requires_name_and_email():
    assert not ApplicantProfile(first_name="Alex").is_configured()
    assert not ApplicantProfile(email="a@b.com").is_configured()
    assert ApplicantProfile(first_name="Alex", email="a@b.com").is_configured()


def test_to_browser_use_dict_shape():
    p = ApplicantProfile(
        first_name="Alex", last_name="Kim", email="alex@example.com", phone="555-0100",
        address={"line1": "1 Main St", "city": "Metropolis", "postal_code": "12345", "country": "USA"},
        work_authorization={"us_citizen": True, "sponsorship_needed": False},
        demographics={"gender": "Prefer not to say", "veteran_status": "Not a veteran"},
    )
    flat = to_browser_use_dict(p)
    assert flat["first_name"] == "Alex"
    assert flat["US_citizen"] is True
    assert flat["sponsorship_needed"] is False
    assert flat["postal_code"] == "12345"
    assert flat["address"] == "1 Main St"
    assert flat["Veteran_status"] == "Not a veteran"
    assert "age" not in flat  # deliberately excluded from our schema


def test_to_browser_use_dict_defaults_are_safe_types():
    flat = to_browser_use_dict(ApplicantProfile())
    assert flat["US_citizen"] is False
    assert flat["postal_code"] == ""


def test_load_site_credentials_exact_match():
    profile = {"site_credentials": {"greenhouse.io": {"username_field_value": "a@b.com", "password": "secret"}}}
    creds = load_site_credentials(profile, "greenhouse.io")
    assert creds == {"username_field_value": "a@b.com", "password": "secret"}


def test_load_site_credentials_subdomain_match():
    profile = {"site_credentials": {"greenhouse.io": {"password": "secret"}}}
    creds = load_site_credentials(profile, "boards.greenhouse.io")
    assert creds is not None and creds["password"] == "secret"


def test_load_site_credentials_no_match():
    profile = {"site_credentials": {"greenhouse.io": {"password": "secret"}}}
    assert load_site_credentials(profile, "workday.com") is None


def test_load_site_credentials_missing_block():
    assert load_site_credentials({}, "greenhouse.io") is None


def test_domain_of():
    assert domain_of("https://boards.greenhouse.io/acme/jobs/1?x=1") == "boards.greenhouse.io"
