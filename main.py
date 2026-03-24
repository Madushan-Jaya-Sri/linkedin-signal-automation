import csv
import io
import json
import os
import threading
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
import bcrypt as _bcrypt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

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

# ─── MongoDB Setup ─────────────────────────────────────────────
_mongo_uri = os.getenv("MONGO_CONNECTION_STRING", "")
db = None

if _mongo_uri:
    try:
        _mongo_client = MongoClient(_mongo_uri, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
        db = _mongo_client["linkedin_intel"]
        db.users.create_index("email", unique=True)
        print("[DB] MongoDB connected")
    except Exception as e:
        print(f"[DB] MongoDB connection failed: {e}")
        db = None

# ─── Auth Setup ────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET", "linkedin-intel-dev-secret-change-in-prod")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7

security_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def create_token(email: str) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": email, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security_scheme)) -> dict:
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"email": email}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ─── Admin Auth Helper ─────────────────────────────────────────

def require_admin(request: Request):
    secret = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


# ─── Admin Endpoints ───────────────────────────────────────────

@app.post("/api/admin/verify")
async def admin_verify(request: Request):
    """Verify admin secret — used by admin UI login."""
    require_admin(request)
    return {"ok": True}


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    """Admin creates a user account."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    body = await request.json()
    name = body.get("name", "").strip()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    if not name or not email or not password:
        raise HTTPException(status_code=400, detail="Name, email and password are required")
    try:
        db.users.insert_one({
            "name": name,
            "email": email,
            "password": hash_password(password),
            "created_at": datetime.utcnow(),
        })
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email already registered")
    return {"ok": True, "name": name, "email": email}


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    """Admin lists all users."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    users = list(db.users.find({}, {"_id": 0, "name": 1, "email": 1, "created_at": 1}))
    for u in users:
        if isinstance(u.get("created_at"), datetime):
            u["created_at"] = u["created_at"].isoformat()
    return users


@app.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, request: Request):
    """Admin deletes a user account."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    result = db.users.delete_one({"email": email.lower()})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.get("/api/admin/searches")
async def admin_get_searches(request: Request):
    """Admin views all users' search activity."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    cycles = list(db.cycles.find(
        {},
        {"analyzed_profiles": 0}
    ).sort("completed_at", -1).limit(200))
    for c in cycles:
        c["_id"] = str(c["_id"])
        if "completed_at" in c:
            c["completed_at"] = c["completed_at"].isoformat()
    return cycles


@app.get("/api/admin/usage")
async def admin_get_usage(request: Request):
    """Admin views deep search usage per user."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    DEEP_SEARCH_LIMIT = 50
    users = list(db.users.find({}, {"_id": 0, "email": 1, "name": 1}))
    result = []
    for u in users:
        used = db.cycles.count_documents({"user_email": u["email"]})
        result.append({
            "email":     u["email"],
            "name":      u.get("name", ""),
            "used":      used,
            "limit":     DEEP_SEARCH_LIMIT,
            "remaining": max(0, DEEP_SEARCH_LIMIT - used),
        })
    result.sort(key=lambda x: x["used"], reverse=True)
    return result


@app.get("/api/admin/costs")
async def admin_get_costs(request: Request):
    """Admin views cost summary per user and overall totals."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    cycles = list(db.cycles.find({}, {"_id": 0, "user_email": 1, "cost": 1, "completed_at": 1, "analyzed_count": 1}))
    user_costs = {}
    total_cost  = 0.0
    for c in cycles:
        email = c.get("user_email", "unknown")
        cost  = c.get("cost") or calculate_cycle_cost(c.get("analyzed_count", 0))
        ct    = cost.get("total", 0)
        total_cost += ct
        if email not in user_costs:
            user_costs[email] = {"email": email, "cycles": 0, "total_cost": 0.0,
                                  "apify_phase1": 0.0, "apify_phase2": 0.0, "openai": 0.0}
        user_costs[email]["cycles"]      += 1
        user_costs[email]["total_cost"]  += ct
        user_costs[email]["apify_phase1"] += cost.get("apify_phase1", 0)
        user_costs[email]["apify_phase2"] += cost.get("apify_phase2", 0)
        user_costs[email]["openai"]       += cost.get("openai", 0)
    for u in user_costs.values():
        u["total_cost"]  = round(u["total_cost"], 4)
        u["apify_phase1"] = round(u["apify_phase1"], 4)
        u["apify_phase2"] = round(u["apify_phase2"], 4)
        u["openai"]       = round(u["openai"], 4)
    return {
        "total_cost": round(total_cost, 4),
        "users": sorted(user_costs.values(), key=lambda x: x["total_cost"], reverse=True),
    }


@app.get("/api/admin/cycles/{cycle_id}/emails")
async def admin_get_cycle_emails(cycle_id: str, request: Request):
    """Admin views all emails found in a specific search cycle."""
    require_admin(request)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    from bson import ObjectId
    try:
        oid = ObjectId(cycle_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cycle ID")
    cycle = db.cycles.find_one(
        {"_id": oid},
        {"analyzed_profiles": 1, "search_query": 1, "user_email": 1}
    )
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    profiles = cycle.get("analyzed_profiles", [])
    emails = [
        {
            "name": p.get("name", ""),
            "email": p.get("email", ""),
            "headline": p.get("headline", ""),
            "company": p.get("company", ""),
            "linkedin_url": p.get("linkedin_url", ""),
        }
        for p in profiles if p.get("email")
    ]
    return {
        "search_query": cycle.get("search_query", ""),
        "user_email": cycle.get("user_email", ""),
        "emails": emails
    }


# ─── Auth Endpoints ────────────────────────────────────────────

@app.post("/api/auth/signin")
async def signin(request: Request):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    user = db.users.find_one({"email": email})
    if not user or not verify_password(password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(email)
    return {"token": token, "name": user["name"], "email": email}


# ─── MongoDB Cycle Save ────────────────────────────────────────

# ─── Cost Constants ───────────────────────────────────────────
COST_APIFY_PHASE1   = 0.80   # per cycle (up to 50 profiles)
COST_APIFY_PHASE2   = 0.10   # per selected profile (post scraping)
COST_OPENAI_CYCLE   = 0.50   # per cycle (AI analysis)

def calculate_cycle_cost(analyzed_count: int) -> dict:
    apify_phase1 = COST_APIFY_PHASE1
    apify_phase2 = round(COST_APIFY_PHASE2 * analyzed_count, 4)
    openai       = COST_OPENAI_CYCLE
    total        = round(apify_phase1 + apify_phase2 + openai, 4)
    return {
        "apify_phase1": apify_phase1,
        "apify_phase2": apify_phase2,
        "openai":       openai,
        "total":        total,
    }


def save_cycle_to_db(job: dict):
    """Save a completed search cycle to MongoDB (posts stripped to reduce doc size)."""
    if db is None:
        return
    user_email = job.get("user_email")
    if not user_email:
        return
    try:
        profiles_for_db = list(job.get("analyzed_profiles", []))
        cost = calculate_cycle_cost(len(profiles_for_db))
        db.cycles.insert_one({
            "user_email": user_email,
            "search_query": job.get("search_query", ""),
            "search_params": job.get("search_params", {}),
            "completed_at": datetime.utcnow(),
            "profiles_with_email_count": len(job.get("profiles_with_email", [])),
            "profiles_without_email_count": len(job.get("profiles_without_email", [])),
            "analyzed_count": len(profiles_for_db),
            "analyzed_profiles": profiles_for_db,
            "cost": cost,
        })
        print(f"[DB] Saved cycle for {user_email} — {len(profiles_for_db)} profiles — cost ${cost['total']}")
    except Exception as e:
        print(f"[DB] Failed to save cycle: {e}")


# ─── Job store ────────────────────────────────────────────────
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

    with_email = [p for p in filtered if p.get("email")]
    without_email = [p for p in filtered if not p.get("email")]

    job["profiles_with_email"] = with_email
    job["profiles_without_email"] = without_email
    job["phase"] = "awaiting_selection"


# ─── Phase 2: Scrape Posts + Analyze ─────────────────────────

def _run_phase2(job_id: str, selected_urls: list[str]):
    """Background thread: Scrape posts for selected → LLM analysis for each."""
    job = jobs[job_id]

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
            posts = scrape_posts(profile["linkedin_url"], max_posts=20)
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

    job["analyzed_profiles"].sort(key=lambda x: x.get("analysis", {}).get("relevance_score", 0), reverse=True)
    job["phase"] = "complete"

    # Persist completed cycle to MongoDB
    save_cycle_to_db(job)


# ─── Endpoints ────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/admin")
async def serve_admin():
    return FileResponse("static/admin.html")


@app.post("/api/search/start")
async def start_search(request: Request, user: dict = Depends(get_current_user)):
    """Start a search job. Requires valid JWT token."""
    import time
    _cleanup_old_jobs()

    params = await request.json()
    search_query = params.get("searchQuery", "").strip()
    if not search_query:
        raise HTTPException(status_code=400, detail="searchQuery is required")

    # Always cap at 50 profiles
    params["maxItems"] = 50

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "phase": "starting",
        "created_at": time.time(),
        "user_email": user["email"],
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
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/usage")
async def get_usage(user: dict = Depends(get_current_user)):
    """Return current user's deep search usage vs limit."""
    DEEP_SEARCH_LIMIT = 50
    used = db.cycles.count_documents({"user_email": user["email"]}) if db is not None else 0
    return {
        "used":      used,
        "limit":     DEEP_SEARCH_LIMIT,
        "remaining": max(0, DEEP_SEARCH_LIMIT - used),
    }


@app.post("/api/search/{job_id}/select")
async def select_profiles(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    """Submit selected profile URLs to trigger post scraping + analysis.

    Accepted in both 'awaiting_selection' and 'complete' phases so user can
    re-run Phase 2 with a different selection without starting a new search.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["phase"] not in ("awaiting_selection", "complete"):
        raise HTTPException(status_code=400, detail=f"Cannot start Phase 2 — job is in '{job['phase']}' phase")

    # ── Usage limit check ──────────────────────────────────────
    DEEP_SEARCH_LIMIT = 50
    if db is not None:
        used = db.cycles.count_documents({"user_email": user["email"]})
        if used >= DEEP_SEARCH_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Deep search limit reached ({DEEP_SEARCH_LIMIT}/{DEEP_SEARCH_LIMIT}). Please contact support to upgrade your plan."
            )

    body = await request.json()
    selected_urls = body.get("linkedin_urls", [])
    if not selected_urls:
        raise HTTPException(status_code=400, detail="No profiles selected")

    selected_urls = selected_urls[:25]

    if job["phase"] == "complete":
        job["analyzed_profiles"] = []
        job["analyzed_count"] = 0

    thread = threading.Thread(target=_run_phase2, args=(job_id, selected_urls), daemon=True)
    thread.start()

    return {"status": "started", "selected_count": len(selected_urls)}


# ─── CSV Export ───────────────────────────────────────────────

def _build_profile_csv(profiles: list[dict], filename: str):
    if not profiles:
        raise HTTPException(status_code=404, detail="No profiles to export.")
    output = io.StringIO()
    fieldnames = ["Name", "Headline", "Company", "Country", "City", "Email", "LinkedIn URL", "Connections", "Followers", "Hiring"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for p in profiles:
        writer.writerow({
            "Name": p.get("name", ""), "Headline": p.get("headline", ""),
            "Company": p.get("company", ""), "Country": p.get("country", ""),
            "City": p.get("city", ""), "Email": p.get("email", ""),
            "LinkedIn URL": p.get("linkedin_url", ""), "Connections": p.get("connections", 0),
            "Followers": p.get("followers", 0), "Hiring": "Yes" if p.get("is_hiring") else "No",
        })
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


def _build_analyzed_csv(profiles: list[dict], filename: str):
    if not profiles:
        raise HTTPException(status_code=404, detail="No analyzed profiles to export.")
    output = io.StringIO()
    fieldnames = [
        "Name", "Headline", "Company", "Country", "City", "Email", "LinkedIn URL",
        "Connections", "Followers", "Hiring", "Relevance Score", "Activity Level",
        "Key Topics", "Areas of Interest", "Recent Activity Summary",
        "Engagement Metrics", "Recommendation", "Reasoning",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for p in profiles:
        a = p.get("analysis", {})
        writer.writerow({
            "Name": p.get("name", ""), "Headline": p.get("headline", ""),
            "Company": p.get("company", ""), "Country": p.get("country", ""),
            "City": p.get("city", ""), "Email": p.get("email", ""),
            "LinkedIn URL": p.get("linkedin_url", ""), "Connections": p.get("connections", 0),
            "Followers": p.get("followers", 0), "Hiring": "Yes" if p.get("is_hiring") else "No",
            "Relevance Score": a.get("relevance_score", 0), "Activity Level": a.get("activity_level", ""),
            "Key Topics": "; ".join(a.get("key_topics", [])),
            "Areas of Interest": "; ".join(a.get("areas_of_interest", [])),
            "Recent Activity Summary": a.get("recent_activity_summary", ""),
            "Engagement Metrics": a.get("engagement_metrics", ""),
            "Recommendation": a.get("recommendation", ""),
            "Reasoning": a.get("reasoning", ""),
        })
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


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


# ─── Profile Chat ─────────────────────────────────────────────

@app.post("/api/chat/{job_id}")
async def chat_profile(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    body = await request.json()
    linkedin_url = body.get("linkedin_url", "").strip()
    message = body.get("message", "").strip()
    history = body.get("history", [])
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    profile = None
    job = jobs.get(job_id)
    if job:
        profile = next((p for p in job.get("analyzed_profiles", []) if p.get("linkedin_url") == linkedin_url), None)
    if not profile:
        profile = body.get("profile_data")
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found — please re-run the analysis")

    try:
        reply = chat_with_profile(profile, message, history)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chat failed: {e}")


# ─── Outreach Email ───────────────────────────────────────────

@app.post("/api/draft-email/{job_id}")
async def draft_email(job_id: str, request: Request, user: dict = Depends(get_current_user)):
    body = await request.json()
    linkedin_url = body.get("linkedin_url", "").strip()

    profile = None
    job = jobs.get(job_id)
    if job:
        profile = next((p for p in job.get("analyzed_profiles", []) if p.get("linkedin_url") == linkedin_url), None)
    if not profile:
        profile = body.get("profile_data")
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found — please re-run the analysis")

    try:
        draft = draft_outreach_email(profile)
        return draft
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email draft failed: {e}")


# ─── History Endpoints ───────────────────────────────────────

@app.get("/api/history")
async def get_history(user: dict = Depends(get_current_user)):
    """Return summary list of past cycles for the current user (newest first)."""
    if db is None:
        return []
    from bson import ObjectId
    cycles = list(db.cycles.find(
        {"user_email": user["email"]},
        {"_id": 1, "search_query": 1, "completed_at": 1,
         "analyzed_count": 1, "profiles_with_email_count": 1,
         "profiles_without_email_count": 1}
    ).sort("completed_at", -1).limit(50))
    for c in cycles:
        c["_id"] = str(c["_id"])
    return cycles


@app.get("/api/history/{cycle_id}")
async def get_history_cycle(cycle_id: str, user: dict = Depends(get_current_user)):
    """Return full analyzed profiles for a specific past cycle."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    from bson import ObjectId
    try:
        oid = ObjectId(cycle_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cycle ID")
    cycle = db.cycles.find_one({"_id": oid, "user_email": user["email"]})
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    cycle["_id"] = str(cycle["_id"])
    return cycle


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("  LinkedIn Lead Intelligence")
    print("=" * 60)
    print("\n  Server: http://localhost:8000")
    print("  API docs: http://localhost:8000/docs")
    print("=" * 60 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
