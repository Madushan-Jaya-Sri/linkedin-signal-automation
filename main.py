import asyncio
import csv
import io
import json
import threading
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from apify_client_wrapper import scrape_profiles_advanced, scrape_posts, filter_profiles
from scorer import analyze_profile, chat_with_profile, draft_outreach_email

app = FastAPI(title="LinkedIn Lead Intelligence")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Job store for multi-phase search
jobs: dict[str, dict] = {}


def _cleanup_old_jobs():
    """Remove jobs older than 30 minutes to free memory."""
    import time
    now = time.time()
    expired = [jid for jid, j in jobs.items() if now - j.get("created_at", now) > 1800]
    for jid in expired:
        del jobs[jid]


# ─── Phase 1: Scrape + Filter ────────────────────────────────

def _run_phase1(job_id: str, search_params: dict):
    """Background thread: Apify advanced search → local filter → split by email."""
    job = jobs[job_id]

    # Step 1: Scrape — progress_callback updates profiles_found in real-time
    # so the frontend shows a live download counter instead of a frozen spinner.
    job["phase"] = "scraping"
    job["scrape_status"] = "running_actor"
    try:
        def _on_profile_downloaded(n: int):
            job["profiles_found"] = n
            job["scrape_status"] = "downloading"

        raw_profiles = scrape_profiles_advanced(search_params, progress_callback=_on_profile_downloaded)
    except Exception as e:
        job["phase"] = "error"
        job["error"] = f"Profile search failed: {e}"
        return

    job["profiles_found"] = len(raw_profiles)
    job["scrape_status"] = "done"

    # Step 2: Filter (only NOT exclusions — LinkedIn already matched positive terms)
    job["phase"] = "filtering"
    filtered = filter_profiles(raw_profiles, search_params["searchQuery"])
    job["profiles_filtered"] = len(filtered)

    if not filtered:
        job["phase"] = "error"
        job["error"] = (
            f"LinkedIn returned {len(raw_profiles)} profiles but all were excluded by your NOT filters. "
            "Try removing the NOT terms or broadening your query."
            if len(raw_profiles) > 0 else
            "No profiles found for this search. Try a different query or fewer filters."
        )
        return

    # Split by email presence
    with_email = [p for p in filtered if p.get("email")]
    without_email = [p for p in filtered if not p.get("email")]

    job["profiles_with_email"] = with_email
    job["profiles_without_email"] = without_email
    job["phase"] = "awaiting_selection"


# ─── Phase 2: Scrape Posts + Analyze ─────────────────────────

def _run_phase2(job_id: str, selected_urls: list[str]):
    """Background thread: Scrape posts for selected → LLM analysis for each."""
    job = jobs[job_id]

    # Gather selected profiles from both lists
    all_profiles = job["profiles_with_email"] + job["profiles_without_email"]
    selected = [p for p in all_profiles if p["linkedin_url"] in selected_urls]

    if not selected:
        job["phase"] = "error"
        job["error"] = "No matching profiles found for the selected URLs."
        return

    # Step 3: Scrape posts
    job["phase"] = "scraping_posts"
    job["posts_total"] = len(selected)
    job["posts_scraped"] = 0

    for i, profile in enumerate(selected):
        job["current_profile_name"] = profile.get("name", "Unknown")
        try:
            posts = scrape_posts(profile["linkedin_url"], max_posts=100)
            profile["posts"] = posts
        except Exception as e:
            print(f"[WARN] Failed to scrape posts for {profile.get('name')}: {e}")
            profile["posts"] = []
        job["posts_scraped"] = i + 1

    # Step 4: LLM Analysis
    job["phase"] = "analyzing"
    job["analyzed_total"] = len(selected)
    job["analyzed_count"] = 0
    job["analyzed_profiles"] = []

    search_query = job["search_query"]
    search_params = job["search_params"]

    for i, profile in enumerate(selected):
        job["current_profile_name"] = profile.get("name", "Unknown")
        try:
            analysis = analyze_profile(
                profile, profile.get("posts", []),
                search_query, search_params,
            )
            profile["analysis"] = analysis
            print(f"[ANALYZED {i+1}/{len(selected)}] {profile.get('name')} -> {analysis['relevance_score']}")
        except Exception as e:
            print(f"[FAILED {i+1}/{len(selected)}] {profile.get('name')}: {e}")
            profile["analysis"] = {
                "relevance_score": 0,
                "activity_level": "Unknown",
                "key_topics": [],
                "areas_of_interest": [],
                "recent_activity_summary": "Analysis failed.",
                "engagement_metrics": "",
                "recommendation": "",
                "reasoning": f"Error: {e}",
            }

        job["analyzed_profiles"].append(profile)
        job["analyzed_count"] = i + 1

    # Sort by relevance score
    job["analyzed_profiles"].sort(key=lambda x: x.get("analysis", {}).get("relevance_score", 0), reverse=True)
    job["phase"] = "complete"


# ─── Endpoints ────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.post("/api/search/start")
async def start_search(request: Request):
    """Start a search job with advanced parameters. Returns job_id for polling."""
    import time
    _cleanup_old_jobs()

    params = await request.json()
    search_query = params.get("searchQuery", "").strip()
    if not search_query:
        raise HTTPException(status_code=400, detail="searchQuery is required")

    params["maxItems"] = max(int(params.get("maxItems", 50)), 50)

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "phase": "starting",
        "created_at": time.time(),
        "search_query": search_query,
        "search_params": params,
        "profiles_found": 0,
        "profiles_filtered": 0,
        "profiles_with_email": [],
        "profiles_without_email": [],
        "posts_total": 0,
        "posts_scraped": 0,
        "analyzed_total": 0,
        "analyzed_count": 0,
        "analyzed_profiles": [],
        "current_profile_name": "",
        "error": "",
    }

    thread = threading.Thread(target=_run_phase1, args=(job_id, params), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/api/search/progress/{job_id}")
async def get_progress(job_id: str):
    """Poll this endpoint to get current job progress.

    Keeps Lambda alive for 3 seconds so the background thread can make progress.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Keep Lambda execution context alive so the background thread can work
    if job["phase"] not in ("complete", "error", "awaiting_selection"):
        await asyncio.sleep(3)

    return job


@app.post("/api/search/{job_id}/select")
async def select_profiles(job_id: str, request: Request):
    """Submit selected profile URLs to trigger post scraping + analysis."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["phase"] != "awaiting_selection":
        raise HTTPException(status_code=400, detail="Job is not awaiting selection")

    body = await request.json()
    selected_urls = body.get("linkedin_urls", [])
    if not selected_urls:
        raise HTTPException(status_code=400, detail="No profiles selected")

    # Cap at 25 profiles
    selected_urls = selected_urls[:25]

    thread = threading.Thread(target=_run_phase2, args=(job_id, selected_urls), daemon=True)
    thread.start()

    return {"status": "started", "selected_count": len(selected_urls)}


# ─── CSV Export Endpoints ─────────────────────────────────────

def _build_profile_csv(profiles: list[dict], filename: str):
    """Build CSV from profile dicts (before analysis)."""
    if not profiles:
        raise HTTPException(status_code=404, detail="No profiles to export.")

    output = io.StringIO()
    fieldnames = [
        "Name", "Headline", "Company", "Country", "City",
        "Email", "LinkedIn URL", "Connections", "Followers", "Hiring",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for p in profiles:
        writer.writerow({
            "Name": p.get("name", ""),
            "Headline": p.get("headline", ""),
            "Company": p.get("company", ""),
            "Country": p.get("country", ""),
            "City": p.get("city", ""),
            "Email": p.get("email", ""),
            "LinkedIn URL": p.get("linkedin_url", ""),
            "Connections": p.get("connections", 0),
            "Followers": p.get("followers", 0),
            "Hiring": "Yes" if p.get("is_hiring") else "No",
        })
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _build_analyzed_csv(profiles: list[dict], filename: str):
    """Build CSV from analyzed profile dicts (with analysis + posts summary)."""
    if not profiles:
        raise HTTPException(status_code=404, detail="No analyzed profiles to export.")

    output = io.StringIO()
    fieldnames = [
        "Name", "Headline", "Company", "Country", "City",
        "Email", "LinkedIn URL", "Connections", "Followers", "Hiring",
        "Relevance Score", "Activity Level", "Key Topics",
        "Areas of Interest", "Recent Activity Summary",
        "Engagement Metrics", "Recommendation", "Reasoning",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for p in profiles:
        analysis = p.get("analysis", {})
        writer.writerow({
            "Name": p.get("name", ""),
            "Headline": p.get("headline", ""),
            "Company": p.get("company", ""),
            "Country": p.get("country", ""),
            "City": p.get("city", ""),
            "Email": p.get("email", ""),
            "LinkedIn URL": p.get("linkedin_url", ""),
            "Connections": p.get("connections", 0),
            "Followers": p.get("followers", 0),
            "Hiring": "Yes" if p.get("is_hiring") else "No",
            "Relevance Score": analysis.get("relevance_score", 0),
            "Activity Level": analysis.get("activity_level", ""),
            "Key Topics": "; ".join(analysis.get("key_topics", [])),
            "Areas of Interest": "; ".join(analysis.get("areas_of_interest", [])),
            "Recent Activity Summary": analysis.get("recent_activity_summary", ""),
            "Engagement Metrics": analysis.get("engagement_metrics", ""),
            "Recommendation": analysis.get("recommendation", ""),
            "Reasoning": analysis.get("reasoning", ""),
        })
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/export/with-email/{job_id}")
async def export_with_email(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _build_profile_csv(job["profiles_with_email"], "profiles_with_email.csv")


@app.get("/api/export/without-email/{job_id}")
async def export_without_email(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _build_profile_csv(job["profiles_without_email"], "profiles_without_email.csv")


@app.get("/api/export/analyzed/{job_id}")
async def export_analyzed(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _build_analyzed_csv(job["analyzed_profiles"], "analyzed_profiles.csv")


# ─── Profile Chat Endpoint ────────────────────────────────────

@app.post("/api/chat/{job_id}")
async def chat_profile(job_id: str, request: Request):
    """Chat with an AI agent about a specific analyzed profile.

    The frontend sends profile_data in the body so this works even after a
    Lambda cold start when the in-memory jobs dict has been wiped.
    """
    body = await request.json()
    linkedin_url = body.get("linkedin_url", "").strip()
    message = body.get("message", "").strip()
    history = body.get("history", [])

    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # 1) Try in-memory job store (works when Lambda is warm and job exists)
    profile = None
    job = jobs.get(job_id)
    if job:
        all_profiles = job.get("analyzed_profiles", [])
        profile = next((p for p in all_profiles if p.get("linkedin_url") == linkedin_url), None)

    # 2) Fallback: use profile_data sent directly from the frontend cache
    if not profile:
        profile = body.get("profile_data")

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found — please re-run the analysis")

    try:
        reply = chat_with_profile(profile, message, history)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chat failed: {e}")


# ─── Outreach Email Draft Endpoint ────────────────────────────

@app.post("/api/draft-email/{job_id}")
async def draft_email(job_id: str, request: Request):
    """Generate a personalised outreach email draft for a profile.

    The frontend sends profile_data in the body so this works even after a
    Lambda cold start when the in-memory jobs dict has been wiped.
    """
    body = await request.json()
    linkedin_url = body.get("linkedin_url", "").strip()

    # 1) Try in-memory job store (works when Lambda is warm and job exists)
    profile = None
    job = jobs.get(job_id)
    if job:
        all_profiles = job.get("analyzed_profiles", [])
        profile = next((p for p in all_profiles if p.get("linkedin_url") == linkedin_url), None)

    # 2) Fallback: use profile_data sent directly from the frontend cache
    if not profile:
        profile = body.get("profile_data")

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found — please re-run the analysis")

    try:
        draft = draft_outreach_email(profile)
        return draft
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email draft failed: {e}")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("  LinkedIn Lead Intelligence v2")
    print("  Advanced Search + Post Analysis + AI Scoring")
    print("=" * 60)
    print("\n  Server starting at: http://localhost:8000")
    print("  API docs at:        http://localhost:8000/docs")
    print("\n  Make sure your .env file has:")
    print("    APIFY_API_TOKEN=your_token")
    print("    OPENAI_API_KEY=your_key")
    print("=" * 60 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
