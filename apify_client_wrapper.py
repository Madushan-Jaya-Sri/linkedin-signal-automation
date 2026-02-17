import os
import time
from apify_client import ApifyClient

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

ACTOR_PROFILE_SEARCH = "M2FMdjRVeF1HPGFcc"


def get_client() -> ApifyClient:
    return ApifyClient(APIFY_API_TOKEN)


def _extract_email(item: dict) -> str:
    """Extract email from profile data if available."""
    # Full + email search mode may return emails in various fields
    email = item.get("email") or item.get("Email") or ""
    if email:
        return email.strip()

    # Check nested email fields
    emails = item.get("emails") or item.get("emailAddresses") or []
    if isinstance(emails, list) and emails:
        if isinstance(emails[0], dict):
            return emails[0].get("email", "") or emails[0].get("address", "")
        return str(emails[0])

    return ""


def _extract_current_position(item: dict) -> dict:
    """Extract current job title and company from profile."""
    # Try currentPosition array
    positions = item.get("currentPosition") or []
    if positions and isinstance(positions, list):
        pos = positions[0]
        return {
            "company": pos.get("companyName", ""),
            "company_url": pos.get("companyLinkedinUrl", ""),
        }

    # Fallback to experience array
    experience = item.get("experience") or []
    if experience and isinstance(experience, list):
        for exp in experience:
            end = exp.get("endDate", {})
            if end and (end.get("text", "") == "Present" or end.get("year") is None):
                return {
                    "company": exp.get("companyName", ""),
                    "company_url": exp.get("companyLinkedinUrl", ""),
                }
        # If no current position found, use the first one
        return {
            "company": experience[0].get("companyName", ""),
            "company_url": experience[0].get("companyLinkedinUrl", ""),
        }

    return {"company": "", "company_url": ""}


def _extract_skills(item: dict) -> str:
    """Extract skills as a comma-separated string."""
    skills = item.get("skills") or []
    if isinstance(skills, list):
        if skills and isinstance(skills[0], dict):
            return ", ".join(s.get("name", "") for s in skills if s.get("name"))
        return ", ".join(str(s) for s in skills)

    top_skills = item.get("topSkills") or ""
    if top_skills:
        return top_skills

    return ""


def _extract_location(item: dict) -> dict:
    """Extract location details."""
    loc = item.get("location") or {}
    if isinstance(loc, dict):
        parsed = loc.get("parsed") or {}
        return {
            "location_text": loc.get("linkedinText", ""),
            "country": parsed.get("countryFull", "") or parsed.get("country", ""),
            "city": parsed.get("city", ""),
        }

    if isinstance(loc, str):
        return {"location_text": loc, "country": "", "city": ""}

    return {"location_text": "", "country": "", "city": ""}


def _parse_profile(item: dict) -> dict:
    """Parse a raw Actor 2 profile into a structured lead dict."""
    position = _extract_current_position(item)
    location = _extract_location(item)

    return {
        "first_name": item.get("firstName", ""),
        "last_name": item.get("lastName", ""),
        "name": f"{item.get('firstName', '')} {item.get('lastName', '')}".strip(),
        "headline": item.get("headline", ""),
        "job_title": item.get("headline", ""),
        "company": position["company"],
        "company_url": position.get("company_url", ""),
        "about": item.get("about", ""),
        "skills": _extract_skills(item),
        "linkedin_url": item.get("linkedinUrl", ""),
        "email": _extract_email(item),
        "country": location["country"],
        "city": location["city"],
        "location_text": location["location_text"],
        "connections": item.get("connectionsCount", 0),
        "followers": item.get("followerCount", 0),
        "is_hiring": item.get("hiring", False),
        "is_open_to_work": item.get("openToWork", False),
        "is_premium": item.get("premium", False),
    }


def scrape_leads(keyword: str, location: str | None = None, limit: int = 20) -> list[dict]:
    """Search LinkedIn profiles using Actor 2 with full profile + email search."""
    client = get_client()

    run_input = {
        "searchQuery": keyword,
        "profileScraperMode": "Full + email search",
        "maxItems": limit,
    }

    if location:
        run_input["locations"] = [location]

    print(f"[INFO] Starting LinkedIn profile search: '{keyword}' in '{location or 'any'}' (max {limit})")

    run = client.actor(ACTOR_PROFILE_SEARCH).call(
        run_input=run_input,
        timeout_secs=300,  # Full profile + email mode takes longer
    )

    results = []
    seen = set()

    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        profile = _parse_profile(item)
        linkedin_url = profile["linkedin_url"]

        # Deduplicate by LinkedIn URL
        if linkedin_url and linkedin_url in seen:
            continue
        if linkedin_url:
            seen.add(linkedin_url)

        results.append(profile)

    with_email = sum(1 for r in results if r["email"])
    print(f"[INFO] Found {len(results)} profiles, {with_email} with emails")

    return results
