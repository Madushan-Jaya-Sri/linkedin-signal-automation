# Lead Scoring Mechanism

## Overview

Each LinkedIn profile is scored individually using **GPT-4o** to produce a **relevance score (0–100)** along with activity level, key topics, areas of interest, and a recommendation. Scoring is fully dynamic — the model adapts its criteria based on the user's search query and applied filters at the time of each search.

---

## How It Works

### Phase 1 — Profile Search & Filter

1. **Scrape**: The Apify LinkedIn Profile Search actor (`M2FMdjRVeF1HPGFcc`) retrieves profiles in `Short` mode using all specified filters (job titles, seniority, industry, company size, location, etc.).
2. **Local Filter**: Profiles are filtered client-side using the search query string with basic Boolean support:
   - Quoted phrases: `"brand manager"` → exact match
   - NOT terms: `NOT engineer` → exclusion
   - Remaining words treated as OR-match candidates
3. **Split by Email**: Filtered profiles are split into two groups — those with emails and those without — and presented for selection.

### Phase 2 — Post Scraping & Analysis

4. **Post Scrape**: For each selected profile (up to 25), recent posts are retrieved via the Apify User Posts actor (`vyWtXDqJ3xKyA5ayO`), up to 100 posts per profile.
5. **LLM Analysis**: Each profile + their posts are analyzed by GPT-4o with a dynamically generated system prompt that incorporates the user's original search query and active filters.

---

## Scoring Output Fields

| Field | Description |
|-------|-------------|
| `relevance_score` | Integer 0–100: how well the person matches the search intent |
| `activity_level` | `High` / `Medium` / `Low` / `Inactive` based on posting frequency |
| `key_topics` | Up to 3 main themes discussed in their posts |
| `areas_of_interest` | Professional domains they engage with |
| `recent_activity_summary` | 2–3 sentence summary of recent posting content |
| `engagement_metrics` | Summary of posting frequency and avg engagement |
| `recommendation` | 1–2 sentences: whether this is a strong match and why |
| `reasoning` | Brief explanation of the relevance score |

---

## Relevance Score Thresholds (UI)

| Score | Color | Label |
|-------|-------|-------|
| 70–100 | Green | High relevance |
| 40–69 | Yellow | Medium relevance |
| 0–39 | Grey | Low relevance |

The slide animation during analysis also uses **score ≥ 60** as the "Strong Match" threshold.

---

## Dynamic Prompt Construction

The system prompt sent to GPT-4o includes:

- The user's raw search query (e.g., `"brand manager" AND marketing NOT intern`)
- A summary of all applied filters:
  - Locations
  - Current Job Titles
  - Current Companies
  - Seniority Level (if set)
  - Job Function (if set)
  - Industry (if set)

This means the scoring rubric shifts automatically based on what the user is looking for — a search for "AI startup founder" produces different scoring criteria than a search for "enterprise sales director".

---

## Profile Data Sent for Scoring

- Name, Headline, Company
- Location (full text)
- LinkedIn URL
- About section
- Skills
- Connections & Followers count
- Hiring / Open to Work / Premium status
- Up to 25 most recent posts (each truncated to 500 characters), with date, likes, comments, reposts

---

## Model Configuration

- **Model:** `gpt-4o`
- **Temperature:** `0` (deterministic output)
- **Max Tokens:** `500`
- **Response Format:** JSON (markdown code fences stripped automatically)
