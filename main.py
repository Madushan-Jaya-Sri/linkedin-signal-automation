import csv
import io
import json
import asyncio

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
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


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/api/search/stream")
async def search_leads_stream(request: Request, keyword: str, location: str = "", limit: int = 20):
    """SSE endpoint that streams progress events as leads are scraped and scored."""
    limit = min(int(limit), 50)
    location = location if location else None

    async def event_generator():
        global all_profiles, profiles_with_email, warm_leads

        # Helper to send SSE events
        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        # Step 1: Scraping
        yield sse("status", {"step": "scraping", "message": f"Searching LinkedIn for '{keyword}'..."})

        try:
            raw_leads = scrape_leads(keyword, location, limit=limit)
        except Exception as e:
            yield sse("error", {"message": f"Apify scraper failed: {e}"})
            return

        total_scraped = len(raw_leads)
        total_with_email = sum(1 for l in raw_leads if l.get("email"))

        yield sse("scrape_done", {
            "total_scraped": total_scraped,
            "total_with_email": total_with_email,
        })

        # Send all profiles to the queue (unscored)
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
        yield sse("queue", {"profiles": queue_items})

        # Step 2: Score each lead one by one
        scored_leads = []
        for i, lead_data in enumerate(raw_leads):
            # Check if client disconnected
            if await request.is_disconnected():
                return

            yield sse("scoring", {"index": i, "name": lead_data.get("name", "Unknown")})

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

            yield sse("scored", {"index": i, "lead": lead.model_dump()})

            # Small delay to let the frontend render
            await asyncio.sleep(0.05)

        # Step 3: Final results — populate funnel stores
        all_scored = [l.model_dump() for l in scored_leads]
        all_scored.sort(key=lambda x: x["intent_score"], reverse=True)

        all_profiles = all_scored
        profiles_with_email = [l for l in all_scored if l.get("email")]
        warm_leads = [l for l in all_scored if l["intent_score"] >= 60]

        yield sse("complete", {
            "total_scraped": total_scraped,
            "total_with_email": len(profiles_with_email),
            "warm_leads": len(warm_leads),
        })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
