from pydantic import BaseModel, field_validator
from typing import Optional


class SearchRequest(BaseModel):
    keyword: str
    location: Optional[str] = None
    limit: int = 20


class Lead(BaseModel):
    # Identity Data
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    headline: str = ""
    job_title: str = ""
    company: str = ""
    company_url: str = ""
    country: str = ""
    city: str = ""
    location_text: str = ""
    linkedin_url: str = ""
    email: str = ""

    # Profile Data
    about: str = ""
    skills: str = ""
    connections: int = 0
    followers: int = 0
    is_hiring: bool = False
    is_open_to_work: bool = False
    is_premium: bool = False

    # Intent & Scoring
    intent_score: int = 0
    qualification_state: str = "Cold Awareness"
    top_signals: list[str] = []
    reasoning: str = ""
    matched_persona: str = "None"

    # Convert None to defaults for all string fields
    @field_validator("*", mode="before")
    @classmethod
    def none_to_default(cls, v, info):
        if v is None:
            field = cls.model_fields[info.field_name]
            return field.default
        return v


class SearchResponse(BaseModel):
    total_scraped: int
    total_with_email: int
    warm_leads: int
    leads: list[Lead]
