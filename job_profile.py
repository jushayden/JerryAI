"""Applicant profile for job applications — loaded from profile.yaml's
`applicant:`/`site_credentials:` blocks (added alongside the existing
owner/social/agent sections, reusing config.load_profile()).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class ApplicantProfile:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address: dict = field(default_factory=dict)  # line1, city, postal_code, country
    linkedin: str = ""
    resume_path: str = ""
    cover_letter_path: str = ""
    work_authorization: dict = field(default_factory=dict)  # us_citizen, sponsorship_needed
    demographics: dict = field(default_factory=dict)  # gender, race, veteran_status, disability_status
    custom_qa: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_profile_dict(cls, profile: dict) -> "ApplicantProfile":
        a = (profile or {}).get("applicant") or {}
        if not isinstance(a, dict):
            return cls()
        return cls(
            first_name=str(a.get("first_name", "")),
            last_name=str(a.get("last_name", "")),
            email=str(a.get("email", "")),
            phone=str(a.get("phone", "")),
            address=dict(a.get("address") or {}),
            linkedin=str(a.get("linkedin", "")),
            resume_path=str(a.get("resume_path", "")),
            cover_letter_path=str(a.get("cover_letter_path", "")),
            work_authorization=dict(a.get("work_authorization") or {}),
            demographics=dict(a.get("demographics") or {}),
            custom_qa={str(k): str(v) for k, v in (a.get("custom_qa") or {}).items()},
        )

    def is_configured(self) -> bool:
        """Bare minimum to attempt an application: a name and an email."""
        return bool(self.first_name and self.email)


def to_browser_use_dict(p: ApplicantProfile) -> dict:
    """Flatten to the shape browser-use's own official apply_to_job.py example
    uses, so the task text/sensitive_data mapping matches a pattern the
    library's own prompting is already tuned for.

    Note: our schema has no `age` field (deliberately — most real ATS forms
    don't ask for it, and it's a sensitive field better handled per-application
    via custom_qa if a specific site needs it)."""
    addr = p.address or {}
    demo = p.demographics or {}
    return {
        "first_name": p.first_name,
        "last_name": p.last_name,
        "email": p.email,
        "phone": p.phone,
        "US_citizen": bool(p.work_authorization.get("us_citizen", False)),
        "sponsorship_needed": bool(p.work_authorization.get("sponsorship_needed", False)),
        "postal_code": str(addr.get("postal_code", "")),
        "country": str(addr.get("country", "")),
        "city": str(addr.get("city", "")),
        "address": str(addr.get("line1", "")),
        "gender": str(demo.get("gender", "")),
        "race": str(demo.get("race", "")),
        "Veteran_status": str(demo.get("veteran_status", "")),
        "disability_status": str(demo.get("disability_status", "")),
    }


def load_site_credentials(profile: dict, domain: str) -> dict[str, str] | None:
    """{field_placeholder: value} for the given domain, or None if unconfigured.

    `domain` should be the target URL's hostname (e.g. urlsplit(url).netloc);
    matches are also tried against the registrable suffix (e.g. a credential
    keyed "greenhouse.io" matches "boards.greenhouse.io")."""
    creds = (profile or {}).get("site_credentials") or {}
    if not isinstance(creds, dict):
        return None
    host = domain.lower()
    for key, val in creds.items():
        key = str(key).lower()
        if host == key or host.endswith("." + key):
            return {str(k): str(v) for k, v in dict(val).items()}
    return None


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower()
