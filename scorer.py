import json
import os
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SYSTEM_PROMPT = """You are a B2B lead scoring AI. You analyse LinkedIn profile data and score leads based on their likelihood of being interested in AI-powered content marketing tools and automation.

You will be given a full LinkedIn profile with rich data. Score accurately based on what you see.

TARGET PERSONAS (score higher if the lead matches one of these):

PERSONA 1 — Brand Managers in Singapore
- Brand Manager / Digital Marketing Manager / Marketing Director
- Industries: FMCG, Retail, Fintech, Pharma
- KPI driven, under pressure to show ROI
- Triggers: Hiring for "Content Manager" or "AI Marketing", posting about AI tools/ChatGPT/content automation, speaking at marketing events, engaging with AI marketing posts

PERSONA 2 — Mid-Scale/Boutique Creative Agency Founders (Malaysia/Singapore)
- Founder / Managing Director of agencies with 20-80 staff
- Fear of becoming irrelevant
- Triggers: Posting about AI experimentation, hiring for "AI Strategist", repositioning, speaking at agency events

PERSONA 3 — Brand Directors at Large Ad Networks
- Regional Brand Director / Innovation Lead / Strategy Director
- Need innovation edge to retain enterprise clients
- Triggers: Posts about transformation, attending Cannes/tech events, sharing AI marketing articles

SCORING RUBRIC (add points for each signal detected):
- Hiring AI / Content roles (check "hiring" flag + headline/about): +25
- Posting about AI automation, ChatGPT, content tools (check about section): +20
- Engaging with AI marketing content: +15
- Recently funded or in expansion mode: +15
- Company shows content/blog activity: +10
- Speaking at marketing or tech events: +10
- Senior role with budget authority (CEO, Founder, Director, VP, Head of, MD, CMO, COO): +15
- Job title directly in marketing, branding, content, growth, digital, creative, agency: +15
- Matches one of the 3 target personas above: +10
- Has direct inquiry or content download signals: +30

DEDUCTIONS:
- Profile is clearly only technical/engineering with no marketing connection: -20
- Student or entry-level with no decision-making role: -15
- No marketing, content, or AI signals at all: -10

IMPORTANT: Start with a base score of 20. Score based on ALL the data provided — headline, about section, company, skills, hiring status, etc. A senior marketing/brand leader should score 60+. A creative agency founder should score 65+.

Return ONLY a valid JSON object in this exact format, no explanation:
{
  "intent_score": <integer 0-100>,
  "qualification_state": "<one of: Cold Awareness | Exploring AI | Actively Evaluating | Likely Buyer>",
  "top_signals": ["<signal 1>", "<signal 2>", "<signal 3>"],
  "reasoning": "<one sentence summary>",
  "matched_persona": "<one of: SG Brand Manager | MY/SG Agency Founder | Ad Network Director | None>"
}

Qualification state mapping:
- 0-29: Cold Awareness
- 30-49: Exploring AI
- 50-69: Actively Evaluating
- 70-100: Likely Buyer"""


def score_lead(lead: dict) -> dict:
    """Send lead data to GPT-4o and return scoring results."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Build a rich profile message from all available data
    hiring_status = ""
    if lead.get("is_hiring"):
        hiring_status = "YES — actively hiring"
    elif lead.get("is_open_to_work"):
        hiring_status = "Open to work"
    else:
        hiring_status = "No"

    user_message = f"""Score this LinkedIn lead:

Name: {lead.get('name', 'N/A')}
Headline: {lead.get('headline', 'N/A')}
Company: {lead.get('company', 'N/A')}
Location: {lead.get('location_text', '') or lead.get('country', 'N/A')}
LinkedIn URL: {lead.get('linkedin_url', 'N/A')}
About: {lead.get('about', 'N/A')}
Skills: {lead.get('skills', 'N/A')}
Connections: {lead.get('connections', 'N/A')}
Followers: {lead.get('followers', 'N/A')}
Hiring: {hiring_status}
Premium Member: {lead.get('is_premium', False)}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        max_tokens=300,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
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
        "intent_score": int(result.get("intent_score", 0)),
        "qualification_state": result.get("qualification_state", "Cold Awareness"),
        "top_signals": result.get("top_signals", []),
        "reasoning": result.get("reasoning", ""),
        "matched_persona": result.get("matched_persona", "None"),
    }
