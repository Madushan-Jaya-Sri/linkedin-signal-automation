import asyncio
import csv
import io
import json
import threading
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from apify_client_wrapper import scrape_leads
from models import Lead
from scorer import score_lead

app = FastAPI(title="LinkedIn Lead Intelligence")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory stores for funnel stages
all_profiles: list[dict] = []
profiles_with_email: list[dict] = []
warm_leads: list[dict] = []

# Job store for polling-based search
jobs: dict[str, dict] = {}


def _run_search_job(job_id: str, keyword: str, location: str | None, limit: int):
    """Background thread that scrapes and scores leads, updating job state as it goes."""
    global all_profiles, profiles_with_email, warm_leads
    job = jobs[job_id]

    # Step 1: Scraping
    job["step"] = "scraping"
    try:
        raw_leads = scrape_leads(keyword, location, limit=limit)
    except Exception as e:
        job["step"] = "error"
        job["error"] = f"Apify scraper failed: {e}"
        return

    total_scraped = len(raw_leads)
    total_with_email = sum(1 for l in raw_leads if l.get("email"))

    # Build queue profiles
    queue_items = []
    for i, lead_data in enumerate(raw_leads):
        queue_items.append({
            "index": i,
            "name": lead_data.get("name", "Unknown"),
            "headline": lead_data.get("headline", ""),
            "company": lead_data.get("company", ""),
            "email": lead_data.get("email", ""),
            "linkedin_url": lead_data.get("linkedin_url", ""),
        })

    job["step"] = "scoring"
    job["total_scraped"] = total_scraped
    job["total_with_email"] = total_with_email
    job["queue"] = queue_items
    job["total_to_score"] = len(raw_leads)

    # Step 2: Score each lead
    scored_leads = []
    for i, lead_data in enumerate(raw_leads):
        job["scoring_index"] = i
        job["scoring_name"] = lead_data.get("name", "Unknown")

        try:
            scoring = score_lead(lead_data)
            lead_data.update(scoring)
            print(f"[SCORED {i+1}/{len(raw_leads)}] {lead_data.get('name', '?')} -> {scoring['intent_score']}")
        except Exception as e:
            print(f"[FAILED {i+1}/{len(raw_leads)}] {lead_data.get('name', '?')}: {e}")
            lead_data["intent_score"] = 0
            lead_data["qualification_state"] = "Cold Awareness"
            lead_data["top_signals"] = []
            lead_data["reasoning"] = ""
            lead_data["matched_persona"] = "None"

        lead = Lead(**{k: v for k, v in lead_data.items() if k in Lead.model_fields})
        scored_leads.append(lead)
        job["scored_leads"].append(lead.model_dump())
        job["scored_count"] = i + 1

    # Step 3: Final results
    all_scored = [l.model_dump() for l in scored_leads]
    all_scored.sort(key=lambda x: x["intent_score"], reverse=True)

    all_profiles = all_scored
    profiles_with_email = [l for l in all_scored if l.get("email")]
    warm_leads = [l for l in all_scored if l["intent_score"] >= 60]

    job["step"] = "complete"
    job["total_with_email"] = len(profiles_with_email)
    job["warm_count"] = len(warm_leads)


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/api/search/start")
async def start_search(keyword: str, location: str = "", limit: int = 20):
    """Start a search job in the background and return a job_id for polling."""
    limit = min(int(limit), 50)
    loc = location if location else None

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "step": "starting",
        "total_scraped": 0,
        "total_with_email": 0,
        "total_to_score": 0,
        "scored_count": 0,
        "scored_leads": [],
        "scoring_index": -1,
        "scoring_name": "",
        "queue": [],
        "warm_count": 0,
        "error": "",
    }

    thread = threading.Thread(
        target=_run_search_job,
        args=(job_id, keyword, loc, limit),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/search/progress/{job_id}")
async def get_progress(job_id: str):
    """Poll this endpoint to get current job progress.

    Keeps Lambda alive for 3 seconds so the background thread can make progress
    (Lambda freezes threads between invocations).
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Keep Lambda execution context alive so the background thread can work
    if job["step"] not in ("complete", "error"):
        await asyncio.sleep(3)

    return job


def _build_csv(leads: list[dict], filename: str):
    """Build a CSV response from a list of lead dicts."""
    if not leads:
        raise HTTPException(status_code=404, detail="No results to export. Run a search first.")

    output = io.StringIO()
    fieldnames = [
        "First Name", "Last Name", "Headline", "Company", "Country",
        "City", "Email", "LinkedIn URL", "Connections", "Hiring",
        "Intent Score", "Qualification State", "Matched Persona",
        "Top Signals", "Reasoning",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for lead in leads:
        writer.writerow({
            "First Name": lead.get("first_name", ""),
            "Last Name": lead.get("last_name", ""),
            "Headline": lead.get("headline", ""),
            "Company": lead.get("company", ""),
            "Country": lead.get("country", ""),
            "City": lead.get("city", ""),
            "Email": lead.get("email", ""),
            "LinkedIn URL": lead.get("linkedin_url", ""),
            "Connections": lead.get("connections", 0),
            "Hiring": "Yes" if lead.get("is_hiring") else "No",
            "Intent Score": lead.get("intent_score", 0),
            "Qualification State": lead.get("qualification_state", ""),
            "Matched Persona": lead.get("matched_persona", ""),
            "Top Signals": "; ".join(lead.get("top_signals", [])),
            "Reasoning": lead.get("reasoning", ""),
        })

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/export/all")
async def export_all():
    return _build_csv(all_profiles, "all_profiles.csv")


@app.get("/api/export/with-email")
async def export_with_email():
    return _build_csv(profiles_with_email, "profiles_with_email.csv")


@app.get("/api/export/warm")
async def export_warm():
    return _build_csv(warm_leads, "warm_leads.csv")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("  LinkedIn Lead Intelligence")
    print("  Find warm leads with AI scoring")
    print("=" * 60)
    print("\n  Server starting at: http://localhost:8000")
    print("  API docs at:        http://localhost:8000/docs")
    print("\n  Make sure your .env file has:")
    print("    APIFY_API_TOKEN=your_token")
    print("    OPENAI_API_KEY=your_key")
    print("=" * 60 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
