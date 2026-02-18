from pydantic import BaseModel, field_validator
from typing import Optional


class AdvancedSearchRequest(BaseModel):
    """All parameters the Apify LinkedIn profile search actor accepts."""
    # Required
    searchQuery: str
    maxItems: int = 50
    locations: list[str] = []

    # Optional text-based filters
    currentCompanies: list[str] = []
    pastCompanies: list[str] = []
    schools: list[str] = []
    currentJobTitles: list[str] = []
    pastJobTitles: list[str] = []
    firstNames: list[str] = []
    lastNames: list[str] = []

    # Optional ID-based filters
    yearsOfExperienceIds: list[str] = []
    yearsAtCurrentCompanyIds: list[str] = []
    seniorityLevelIds: list[str] = []
    functionIds: list[str] = []
    profileLanguages: list[str] = []
    companyHeadcount: list[str] = []
    industryIds: list[str] = []

    # Boolean toggle
    recentlyChangedJobs: bool = False


class ProfileShort(BaseModel):
    """Profile data from Short scrape mode."""
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    headline: str = ""
    job_title: str = ""
    company: str = ""
    company_url: str = ""
    about: str = ""
    skills: str = ""
    linkedin_url: str = ""
    email: str = ""
    country: str = ""
    city: str = ""
    location_text: str = ""
    connections: int = 0
    followers: int = 0
    is_hiring: bool = False
    is_open_to_work: bool = False
    is_premium: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def none_to_default(cls, v, info):
        if v is None:
            field = cls.model_fields[info.field_name]
            return field.default
        return v


class LinkedInPost(BaseModel):
    """A single LinkedIn post from the posts scraper."""
    text: str = ""
    date: str = ""
    likes: int = 0
    comments: int = 0
    reposts: int = 0
    post_url: str = ""


class ProfileAnalysis(BaseModel):
    """LLM analysis result for a profile + posts."""
    relevance_score: int = 0
    activity_level: str = ""
    key_topics: list[str] = []
    areas_of_interest: list[str] = []
    recent_activity_summary: str = ""
    engagement_metrics: str = ""
    recommendation: str = ""
    reasoning: str = ""
