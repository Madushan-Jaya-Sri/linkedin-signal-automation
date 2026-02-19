import os
import re
from apify_client import ApifyClient

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

ACTOR_PROFILE_SEARCH = "M2FMdjRVeF1HPGFcc"
ACTOR_USER_POSTS = "vyWtXDqJ3xKyA5ayO"


def get_client() -> ApifyClient:
    return ApifyClient(APIFY_API_TOKEN)


# ─── Profile Parsing Helpers ─────────────────────────────────

def _extract_email(item: dict) -> str:
    email = item.get("email") or item.get("Email") or ""
    if email:
        return email.strip()
    emails = item.get("emails") or item.get("emailAddresses") or []
    if isinstance(emails, list) and emails:
        if isinstance(emails[0], dict):
            return emails[0].get("email", "") or emails[0].get("address", "")
        return str(emails[0])
    return ""


def _extract_current_position(item: dict) -> dict:
    positions = item.get("currentPosition") or []
    if positions and isinstance(positions, list):
        pos = positions[0]
        return {
            "company": pos.get("companyName", ""),
            "company_url": pos.get("companyLinkedinUrl", ""),
        }
    experience = item.get("experience") or []
    if experience and isinstance(experience, list):
        for exp in experience:
            end = exp.get("endDate", {})
            if end and (end.get("text", "") == "Present" or end.get("year") is None):
                return {
                    "company": exp.get("companyName", ""),
                    "company_url": exp.get("companyLinkedinUrl", ""),
                }
        return {
            "company": experience[0].get("companyName", ""),
            "company_url": experience[0].get("companyLinkedinUrl", ""),
        }
    return {"company": "", "company_url": ""}


def _extract_experience_summary(item: dict) -> str:
    experience = item.get("experience") or []
    if not experience:
        return ""
    lines = []
    for exp in experience[:6]:
        title = exp.get("position", "")
        company = exp.get("companyName", "")
        start = (exp.get("startDate") or {}).get("text", "")
        end = (exp.get("endDate") or {}).get("text", "Present")
        duration = exp.get("duration", "")
        date_str = f"{start} – {end}" if start else duration
        skills_list = exp.get("skills") or []
        desc = (exp.get("description") or "")[:300].strip()
        parts = [f"{title} at {company} ({date_str})"]
        if desc:
            parts.append(f"  {desc}")
        if skills_list:
            parts.append(f"  Skills: {', '.join(skills_list[:6])}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _extract_education_summary(item: dict) -> str:
    education = item.get("education") or []
    if not education:
        return ""
    lines = []
    for edu in education[:4]:
        school = edu.get("schoolName", "")
        degree = edu.get("degree", "")
        field = edu.get("fieldOfStudy", "")
        period = edu.get("period", "")
        qual = ", ".join(filter(None, [degree, field]))
        entry = f"{qual} — {school}" if qual else school
        if period:
            entry += f" ({period})"
        lines.append(entry)
    return "\n".join(lines)


def _extract_certifications_summary(item: dict) -> str:
    certs = item.get("certifications") or []
    if not certs:
        return ""
    lines = []
    for cert in certs[:6]:
        title = cert.get("title", "")
        issued_by = cert.get("issuedBy", "")
        issued_at = cert.get("issuedAt", "")
        parts = [title]
        if issued_by:
            parts.append(f"by {issued_by}")
        if issued_at:
            parts.append(f"({issued_at})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _extract_skills(item: dict) -> str:
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
        "photo": item.get("photo", "") or "",
        "experience_summary": _extract_experience_summary(item),
        "education_summary": _extract_education_summary(item),
        "certifications_summary": _extract_certifications_summary(item),
    }


# ─── Actor 1: Advanced Profile Search ────────────────────────

def scrape_profiles_advanced(params: dict, progress_callback=None) -> list[dict]:
    """Search LinkedIn profiles using all advanced filter parameters.

    Uses profileScraperMode: 'Full + email search' for rich data including emails.
    progress_callback(n) is called after each profile is downloaded so callers
    can update real-time counters while iterate_items() is running.
    """
    client = get_client()

    # Build actor input — only include non-empty parameters
    run_input = {
        "searchQuery": params["searchQuery"],
        "maxItems": params.get("maxItems", 50),
        "profileScraperMode": "Full + email search",
    }

    # List-type optional params
    list_fields = [
        "locations", "currentCompanies", "pastCompanies", "schools",
        "currentJobTitles", "pastJobTitles", "firstNames", "lastNames",
        "yearsOfExperienceIds", "yearsAtCurrentCompanyIds",
        "seniorityLevelIds", "functionIds", "profileLanguages",
        "companyHeadcount", "industryIds",
    ]
    for field in list_fields:
        val = params.get(field, [])
        if val:
            run_input[field] = val

    # Boolean toggle
    if params.get("recentlyChangedJobs"):
        run_input["recentlyChangedJobs"] = True

    print(f"[INFO] Starting advanced LinkedIn search: query='{params['searchQuery']}' maxItems={run_input['maxItems']}")
    print(f"[INFO] Filters: {', '.join(f'{k}={v}' for k, v in run_input.items() if k not in ('searchQuery', 'maxItems', 'profileScraperMode'))}")

    run = client.actor(ACTOR_PROFILE_SEARCH).call(
        run_input=run_input,
        timeout_secs=600,
    )

    print("[INFO] Actor run complete — downloading dataset results...")
    results = []
    seen = set()
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        profile = _parse_profile(item)
        linkedin_url = profile["linkedin_url"]
        if linkedin_url and linkedin_url in seen:
            continue
        if linkedin_url:
            seen.add(linkedin_url)
        results.append(profile)
        # Notify caller so UI can show real-time download progress
        if progress_callback:
            progress_callback(len(results))

    with_email = sum(1 for r in results if r["email"])
    print(f"[INFO] Found {len(results)} profiles, {with_email} with emails")
    return results


# ─── Local Profile Filter ────────────────────────────────────

def filter_profiles(profiles: list[dict], search_query: str) -> list[dict]:
    """Apply only NOT exclusions from the search query.

    LinkedIn's search engine already handles positive term matching, so
    applying a second positive-match filter on Short-mode profiles (which
    have sparse text) would incorrectly discard all results.

    Only NOT terms are applied locally — e.g. 'brand manager NOT intern'
    will exclude any profile whose text contains 'intern'.
    """
    query = search_query.strip()
    if not query:
        return profiles

    # Extract NOT terms only
    not_terms = re.findall(r'\bNOT\s+(\S+)', query, re.IGNORECASE)
    not_match = [t.lower() for t in not_terms]

    if not not_match:
        print(f"[INFO] No local filter applied — returning all {len(profiles)} profiles from LinkedIn search")
        return profiles

    filtered = []
    for profile in profiles:
        blob = ' '.join([
            profile.get('name', ''),
            profile.get('headline', ''),
            profile.get('company', ''),
            profile.get('about', ''),
            profile.get('skills', ''),
            profile.get('job_title', ''),
        ]).lower()

        if not any(term in blob for term in not_match):
            filtered.append(profile)

    print(f"[INFO] NOT filter: {len(profiles)} -> {len(filtered)} profiles (excluded terms: {not_match})")
    return filtered


# ─── Actor 2: LinkedIn User Posts ─────────────────────────────

def scrape_posts(linkedin_url: str, max_posts: int = 30) -> list[dict]:
    """Scrape LinkedIn posts for a single user profile."""
    client = get_client()

    # Extract username or use full URL
    profile_input = linkedin_url
    if "/in/" in linkedin_url:
        # Extract just the username part for cleaner input
        parts = linkedin_url.rstrip("/").split("/in/")
        if len(parts) > 1:
            profile_input = parts[1].split("/")[0].split("?")[0]

    run_input = {
        "profile": profile_input,
        "maxPosts": max_posts,
    }

    print(f"[INFO] Scraping posts for: {linkedin_url}")

    run = client.actor(ACTOR_USER_POSTS).call(
        run_input=run_input,
        timeout_secs=120,
    )

    posts = []
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        stats = item.get("stats", {}) or {}
        posted_at = item.get("posted_at", {}) or {}
        media = item.get("media", {}) or {}

        # Extract image URLs from media.images array
        raw_images = media.get("images") or []
        media_images = [img["url"] for img in raw_images if isinstance(img, dict) and img.get("url")]

        posts.append({
            "text": item.get("text", ""),
            "date": posted_at.get("date", "") if isinstance(posted_at, dict) else "",
            "relative": (posted_at.get("relative", "") or "").split("•")[0].strip() if isinstance(posted_at, dict) else "",
            "likes": stats.get("total_reactions", 0),
            "comments": stats.get("comments", 0),
            "reposts": stats.get("reposts", 0),
            "post_url": item.get("url", ""),
            "media_type": media.get("type", ""),
            "media_url": media.get("url", "") or "",
            "media_thumbnail": media.get("thumbnail", "") or "",
            "media_images": media_images,
        })

    print(f"[INFO] Found {len(posts)} posts for {linkedin_url}")
    return posts
