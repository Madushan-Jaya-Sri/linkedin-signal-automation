import json
import os
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ─── Outreach Sender Config ────────────────────────────────────
# Update these to match the user running the tool
SENDER_NAME    = os.getenv("SENDER_NAME",    "Madushan Jayasri")
SENDER_EMAIL   = os.getenv("SENDER_EMAIL",   "madushan.jayasri@momentro.com")
COMPANY_NAME   = os.getenv("COMPANY_NAME",   "Momentro")
COMPANY_PITCH  = os.getenv("COMPANY_PITCH",  (
    "an AI-driven marketing agency that delivers end-to-end AI-powered marketing strategies — "
    "from intelligent campaign automation and content generation to data-driven audience targeting "
    "and performance analytics."
))


def _build_system_prompt(search_query: str, search_params: dict) -> str:
    """Build a dynamic system prompt based on the user's search context."""

    # Summarize the filters applied
    filter_summary = []
    if search_params.get("locations"):
        filter_summary.append(f"Locations: {', '.join(search_params['locations'])}")
    if search_params.get("currentJobTitles"):
        filter_summary.append(f"Job Titles: {', '.join(search_params['currentJobTitles'])}")
    if search_params.get("currentCompanies"):
        filter_summary.append(f"Companies: {', '.join(search_params['currentCompanies'])}")
    if search_params.get("seniorityLevelIds"):
        filter_summary.append(f"Seniority filters applied")
    if search_params.get("functionIds"):
        filter_summary.append(f"Function filters applied")
    if search_params.get("industryIds"):
        filter_summary.append(f"Industry filters applied")

    filters_text = "\n".join(f"- {f}" for f in filter_summary) if filter_summary else "None"

    return f"""You are a LinkedIn profile analyst. Your job is to analyze a LinkedIn profile and their recent posts to determine how well they match the user's search intent.

THE USER'S SEARCH INTENT:
Search Query: {search_query}
Filters Applied:
{filters_text}

ANALYSIS INSTRUCTIONS:
1. Relevance Score (0-100): How well does this person match "{search_query}"? Consider their job title, headline, company, skills, and activity.
2. Activity Level: Based on their posts — how active are they on LinkedIn? (High = posts weekly+, Medium = posts monthly, Low = posts rarely, Inactive = no recent posts)
3. Key Topics: What are the main themes they discuss in their posts?
4. Areas of Interest: What professional domains are they engaged with?
5. Recent Activity Summary: What have they been posting about recently?
6. Engagement Metrics: Summarize their posting patterns — avg likes, comments, how often they post.
7. Recommendation: Is this a strong match for "{search_query}"? Why or why not?

Return ONLY a valid JSON object in this exact format:
{{
  "relevance_score": <integer 0-100>,
  "activity_level": "<High|Medium|Low|Inactive>",
  "key_topics": ["topic1", "topic2", "topic3"],
  "areas_of_interest": ["area1", "area2", "area3"],
  "recent_activity_summary": "<2-3 sentences about what they post about>",
  "engagement_metrics": "<summary of posting frequency and engagement>",
  "recommendation": "<1-2 sentences: is this a strong match and why>",
  "reasoning": "<brief explanation of the relevance score>"
}}"""


def _format_posts(posts: list[dict], max_posts: int = 25) -> str:
    """Format posts for inclusion in the LLM prompt."""
    if not posts:
        return "No posts available."

    lines = []
    for i, post in enumerate(posts[:max_posts]):
        text = (post.get("text", "") or "")[:500]  # Truncate long posts
        date = post.get("date", "unknown date")
        likes = post.get("likes", 0)
        comments = post.get("comments", 0)
        reposts = post.get("reposts", 0)

        lines.append(
            f"Post {i+1} ({date}) — {likes} likes, {comments} comments, {reposts} reposts:\n{text}"
        )

    return "\n\n".join(lines)


def analyze_profile(profile: dict, posts: list[dict], search_query: str, search_params: dict) -> dict:
    """Analyze a profile + posts using GPT-4o with dynamic scoring based on search context."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    system_prompt = _build_system_prompt(search_query, search_params)
    formatted_posts = _format_posts(posts)

    hiring_status = "YES — actively hiring" if profile.get("is_hiring") else (
        "Open to work" if profile.get("is_open_to_work") else "No"
    )

    user_message = f"""Profile:
Name: {profile.get('name', 'N/A')}
Headline: {profile.get('headline', 'N/A')}
Company: {profile.get('company', 'N/A')}
Location: {profile.get('location_text', '') or profile.get('country', 'N/A')}
LinkedIn URL: {profile.get('linkedin_url', 'N/A')}
About: {profile.get('about', 'N/A')}
Skills: {profile.get('skills', 'N/A')}
Connections: {profile.get('connections', 'N/A')}
Followers: {profile.get('followers', 'N/A')}
Hiring: {hiring_status}
Premium Member: {profile.get('is_premium', False)}

Recent LinkedIn Posts ({len(posts)} total):
{formatted_posts}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        max_tokens=500,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )

    content = response.choices[0].message.content.strip()
    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    result = json.loads(content)
    return {
        "relevance_score": int(result.get("relevance_score", 0)),
        "activity_level": result.get("activity_level", "Unknown"),
        "key_topics": result.get("key_topics", []),
        "areas_of_interest": result.get("areas_of_interest", []),
        "recent_activity_summary": result.get("recent_activity_summary", ""),
        "engagement_metrics": result.get("engagement_metrics", ""),
        "recommendation": result.get("recommendation", ""),
        "reasoning": result.get("reasoning", ""),
    }


# ─── Profile Chat ──────────────────────────────────────────────

def _build_chat_system_prompt(profile: dict, posts: list[dict]) -> str:
    """Build a comprehensive system prompt with all available profile data."""
    hiring_status = (
        "Actively hiring" if profile.get("is_hiring")
        else ("Open to work" if profile.get("is_open_to_work") else "No")
    )

    lines = [
        "You are a knowledgeable assistant with complete access to a specific LinkedIn professional's data.",
        "Answer questions ONLY based on the information provided below.",
        "If something is not in the data, say clearly that you don't have that information.",
        "Be concise, conversational, and helpful. Never fabricate details.",
        "",
        "=== PROFILE ===",
        f"Name: {profile.get('name', 'N/A')}",
        f"Headline: {profile.get('headline', 'N/A')}",
        f"Current Company: {profile.get('company', 'N/A')}",
        f"Location: {profile.get('location_text', '') or profile.get('country', 'N/A')}",
        f"Connections: {profile.get('connections', 'N/A')}  |  Followers: {profile.get('followers', 'N/A')}",
        f"Premium Member: {profile.get('is_premium', False)}  |  Hiring: {hiring_status}",
    ]

    if profile.get("about"):
        lines += ["", "About:", profile["about"]]

    if profile.get("skills"):
        lines += ["", f"Skills: {profile['skills']}"]

    if profile.get("experience_summary"):
        lines += ["", "Work Experience:", profile["experience_summary"]]

    if profile.get("education_summary"):
        lines += ["", "Education:", profile["education_summary"]]

    if profile.get("certifications_summary"):
        lines += ["", "Certifications:", profile["certifications_summary"]]

    # Include all posts for chat (more context than scoring)
    post_count = len(posts)
    formatted = _format_posts(posts, max_posts=80)
    lines += ["", f"=== LINKEDIN POSTS ({post_count} total) ===", formatted]

    return "\n".join(lines)


def chat_with_profile(profile: dict, message: str, history: list[dict]) -> str:
    """Answer a question about a profile using its full data as context."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    posts = profile.get("posts", [])
    system_prompt = _build_chat_system_prompt(profile, posts)

    messages = [{"role": "system", "content": system_prompt}]

    # Include last 10 turns of conversation history
    for turn in history[-10:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})

    messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.7,
        max_tokens=600,
        messages=messages,
    )
    return response.choices[0].message.content.strip()


# ─── Outreach Email Drafter ────────────────────────────────────

def draft_outreach_email(profile: dict) -> dict:
    """Generate a personalised cold outreach email for a profile."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Gather the most useful context for personalisation
    posts = profile.get("posts", [])
    recent_posts_text = "\n".join(
        f"- {(p.get('text') or '')[:200].strip()}"
        for p in posts[:5] if (p.get("text") or "").strip()
    ) or "No recent posts available."

    analysis = profile.get("analysis", {})
    activity_summary = analysis.get("recent_activity_summary", "")
    key_topics = ", ".join(analysis.get("key_topics", []))

    prompt = f"""You are helping {SENDER_NAME}, CEO of {COMPANY_NAME}, write a short personalised cold outreach email.

ABOUT {COMPANY_NAME.upper()}:
{COMPANY_PITCH}

SENDER:
Name: {SENDER_NAME}
Email: {SENDER_EMAIL}
Company: {COMPANY_NAME}

RECIPIENT PROFILE:
Name: {profile.get('name', 'N/A')}
Title: {profile.get('headline', 'N/A')}
Company: {profile.get('company', 'N/A')}
Location: {profile.get('location_text', 'N/A')}
About: {(profile.get('about') or 'N/A')[:400]}
Key Topics they post about: {key_topics or 'N/A'}
Recent Activity: {activity_summary or 'N/A'}
Sample recent posts:
{recent_posts_text}

TASK:
Write a concise, personalised cold outreach email (120–160 words max).
Rules:
- Open with a specific, genuine observation about their role or recent activity (not generic flattery)
- Briefly explain what {COMPANY_NAME} does and why it is specifically relevant to them or their company
- End with a single soft call-to-action (e.g. a 20-minute call)
- Tone: warm, professional, peer-to-peer — NOT salesy or templated
- Sign off with {SENDER_NAME}, {COMPANY_NAME}

Return ONLY valid JSON in this exact format:
{{"subject": "<compelling subject line>", "body": "<full email body with line breaks as \\n>"}}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.8,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    result = json.loads(content)
    return {
        "subject": result.get("subject", ""),
        "body": result.get("body", ""),
        "to": profile.get("email", ""),
    }
