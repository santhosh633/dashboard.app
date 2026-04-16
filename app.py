from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import uvicorn
import math
import re
import urllib.request
import urllib.error

app = FastAPI(title="Axximum Analytics API")

# ── Google Sheets proxy ──────────────────────────────────────────────────────
# Converts any Google Sheets share/edit URL into a CSV export and proxies it
# so the browser never hits CORS restrictions.
@app.get("/api/fetch-sheet")
async def fetch_sheet(url: str = Query(..., description="Google Sheets URL")):
    try:
        # 1. Extract spreadsheet ID
        id_match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
        if not id_match:
            return JSONResponse({"error": "Invalid Google Sheets URL – no spreadsheet ID found."}, status_code=400)
        sheet_id = id_match.group(1)

        # 2. Extract tab GID (defaults to first tab = 0)
        gid = "0"
        gid_match = re.search(r'[#&?]gid=(\d+)', url)
        if gid_match:
            gid = gid_match.group(1)

        # 3. Build the CSV export URL
        csv_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid={gid}"
        )

        # 4. Fetch – Google redirects once, urllib follows automatically
        req = urllib.request.Request(
            csv_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Axximum/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            csv_data = resp.read().decode("utf-8", errors="replace")

        return PlainTextResponse(csv_data, media_type="text/csv")

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return JSONResponse(
                {"error": "Sheet is private. Set sharing to 'Anyone with the link can view'."},
                status_code=403
            )
        return JSONResponse({"error": f"Google returned HTTP {exc.code}."}, status_code=502)
    except urllib.error.URLError as exc:
        return JSONResponse({"error": f"Network error: {exc.reason}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
# ────────────────────────────────────────────────────────────────────────────

from fastapi import Request
from sqlalchemy import create_engine, Column, String, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker
import json

# Initialize SQLite Database
SQLALCHEMY_DATABASE_URL = "sqlite:///./analytics.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Project(Base):
    __tablename__ = "projects"
    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    color = Column(String)
    syncUrl = Column(String)
    isR365 = Column(Boolean, default=False)
    hasQC = Column(Boolean, default=False)
    # Store dynamic schema mappings
    columnMap = Column(Text, nullable=True)

Base.metadata.create_all(bind=engine)

@app.get("/api/state")
async def get_state():
    db = SessionLocal()
    try:
        projects = db.query(Project).all()
        sheets = []
        for p in projects:
            sheets.append({
                "id": p.id,
                "name": p.name,
                "color": p.color,
                "syncUrl": p.syncUrl,
                "isR365": p.isR365,
                "hasQC": p.hasQC,
                "columnMap": json.loads(p.columnMap) if p.columnMap else None,
                "csvData": {"headers": [], "rows": []},
                "processed": False,
                "counts": {"done": 0, "inProcess": 0, "work": 0, "total": 0, "users": 0, "totalPages": 0},
                "userStats": {},
                "dailyStats": {},
                "aiInsights": []
            })
        return JSONResponse({"sheets": sheets})
    finally:
        db.close()

@app.post("/api/state")
async def save_state(request: Request):
    db = SessionLocal()
    try:
        data = await request.json()
        
        if "sheets" in data:
            # Sync projects to database
            existing_ids = set()
            for s in data["sheets"]:
                proj_id = s.get("id")
                existing_ids.add(proj_id)
                proj = db.query(Project).filter(Project.id == proj_id).first()
                if not proj:
                    proj = Project(id=proj_id)
                    db.add(proj)
                
                proj.name = s.get("name")
                proj.color = s.get("color")
                proj.syncUrl = s.get("syncUrl")
                proj.isR365 = s.get("isR365", False)
                proj.hasQC = s.get("hasQC", False)
                proj.columnMap = json.dumps(s.get("columnMap")) if s.get("columnMap") else None
            
            # Remove deleted projects
            db.query(Project).filter(~Project.id.in_(existing_ids)).delete(synchronize_session=False)
            
        db.commit()
        return {"status": "ok"}
    except Exception as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        db.close()

class ChatRequest(BaseModel):
    query: str
    sheets: List[Dict[str, Any]]

@app.get("/")
async def serve_dashboard():
    # Serves your main Dashboard.html file at the root URL
    return FileResponse("Dashboard.html")

@app.get("/Dashboard")
async def serve_dashboard_noext():
    # Also serves the Dashboard file without extension identically
    return FileResponse("Dashboard")

@app.get("/api/status")
async def api_status():
    return {
        "status": "online", 
        "project": "Axximum Analytics",
        "version": "1.0"
    }

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    query = request.query.lower()
    
    # Process sheets data natively in Python!
    all_users = {}
    total_pages = 0
    total_done = 0
    total_pending = 0
    
    for sheet in request.sheets:
        user_stats = sheet.get('userStats', {})
        for name, user in user_stats.items():
            if name not in all_users:
                all_users[name] = user.copy()
            else:
                all_users[name]['done'] = all_users[name].get('done', 0) + user.get('done', 0)
                all_users[name]['inProcess'] = all_users[name].get('inProcess', 0) + user.get('inProcess', 0)
                all_users[name]['work'] = all_users[name].get('work', 0) + user.get('work', 0)
                all_users[name]['pagesDone'] = all_users[name].get('pagesDone', 0) + user.get('pagesDone', 0)
                all_users[name]['total'] = all_users[name].get('total', 0) + user.get('total', 0)
                all_users[name]['qcFiles'] = all_users[name].get('qcFiles', 0) + user.get('qcFiles', 0)
            
            total_pages += user.get('pagesDone', 0)
            total_done += user.get('done', 0)
            total_pending += user.get('work', 0)
            
    users_array = list(all_users.values())
    
    if "top performer" in query or "best" in query:
        top_user = max(users_array, key=lambda x: x.get('pagesDone', 0), default=None)
        if top_user:
            success_rate = (top_user.get('done', 0) / max(top_user.get('total', 0), 1)) * 100
            return {
                "text": f"🏆 Top Performer: {top_user.get('name')}\n\n✓ Completed: {top_user.get('done')} tasks\n✓ Pages: {top_user.get('pagesDone', 0)}\n✓ Success Rate: {success_rate:.1f}%"
            }
            
    if "pending" in query or "work" in query:
        pending_users = [u for u in users_array if u.get('work', 0) > 0]
        pending_users.sort(key=lambda x: x.get('work', 0), reverse=True)
        if pending_users:
            top_3 = pending_users[:3]
            list_str = "\n".join([f"• {u.get('name')}: {u.get('work')} pending" for u in top_3])
            return {
                "text": f"📋 Pending Work Summary:\n\n{list_str}\n\nTotal Pending: {total_pending} tasks"
            }
            
    if "qc" in query or "quality" in query:
        qc_users = [u for u in users_array if u.get('qcFiles', 0) > 0]
        qc_users.sort(key=lambda x: x.get('qcFiles', 0), reverse=True)
        if qc_users:
            top_3 = qc_users[:3]
            list_str = "\n".join([f"• {u.get('name')}: {u.get('qcFiles')} files" for u in top_3])
            return {
                "text": f"🔍 QC Leaders:\n\n{list_str}"
            }
            
    if "chart" in query or "graph" in query or "visual" in query:
        users_array.sort(key=lambda x: x.get('pagesDone', 0) or x.get('done', 0), reverse=True)
        top_5 = users_array[:5]
        chart_type = "pie" if "pie" in query else "bar"
        return {
            "text": f"📊 Generating performance chart for top {len(top_5)} contributors...",
            "chart": True,
            "chartType": chart_type,
            "chartData": {
                "labels": [u.get('name') for u in top_5],
                "data": [u.get('pagesDone', 0) or u.get('done', 0) for u in top_5]
            }
        }
        
    if "average" in query or "velocity" in query:
        avg = round(total_pages / len(users_array)) if users_array else 0
        perf_msg = "Excellent productivity!" if avg > 50 else "Good progress, room for improvement." if avg > 25 else "Low activity detected. Check workload distribution."
        return {
            "text": f"📈 Team Velocity:\n\n• Average per member: {avg} pages\n• Total completed: {total_done} tasks\n• Total pages: {total_pages}\n\n{perf_msg}"
        }
        
    return {
        "text": f"📊 Overall Summary:\n\n• Total Projects: {len(request.sheets)}\n• Team Members: {len(users_array)}\n• Completed Tasks: {total_done}\n• Total Pages: {total_pages}\n\n**Processed natively by FastAPI Backend! 🚀**\n\nAsk me about:\n- Top performers\n- Pending work\n- QC reports\n- Generate charts\n- Average velocity"
    }

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8080, reload=True)
