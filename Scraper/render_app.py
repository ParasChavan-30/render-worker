"""
render_app.py — Lightweight HTTP Web Service for Render (Platform 5).
Listens on $PORT to satisfy Render's health checks and executes COA scraping tasks.
"""

import os
import json
import base64
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import address_extractor
import session_manager

# Load Environment Variables from Render Dashboard
PORT = int(os.environ.get("PORT", 10000))
REMOTE_API_URL = os.environ.get("REMOTE_API_URL", "https://site40243-dn3p3f.scloudsite101.com/Architecture/api.php")
REMOTE_API_KEY = os.environ.get("REMOTE_API_KEY", "coa_worker_secret_key_2026")
CAPTCHA_SOLVER_URL = os.environ.get("CAPTCHA_SOLVER_URL", "https://friendly-perfection-production-de1de426.up.railway.app/solve")
WORKER_ID = os.environ.get("WORKER_ID", "render_platform5")

def solve_via_railway(image_bytes: bytes) -> str:
    """Send captcha bytes to dedicated Railway OCR microservice."""
    b64_img = base64.b64encode(image_bytes).decode("utf-8")
    resp = requests.post(
        CAPTCHA_SOLVER_URL,
        json={"image_base64": b64_img},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("captcha_text", "").strip()

def scrape_one_record():
    """Fetch 1 pending record from PHP API, scrape COA, and save results back."""
    # 1. Fetch pending record
    headers = {"X-API-Key": REMOTE_API_KEY, "Content-Type": "application/json"}
    fetch_resp = requests.post(
        REMOTE_API_URL,
        headers=headers,
        json={"action": "get_pending", "batch_size": 1, "worker_id": WORKER_ID},
        timeout=15
    )
    fetch_data = fetch_resp.json()
    records = fetch_data.get("records", [])
    if not records:
        return {"status": "idle", "message": "No pending records found in queue."}

    target = records[0]
    reg_no = target.get("registration_no", "").strip()

    # 2. Open session & download captcha
    session = session_manager.create_session()
    if not session_manager.initialize_session(session):
        return {"status": "error", "error": f"Failed to initialize COA session for {reg_no}"}

    img_bytes = session_manager.download_captcha(session)
    if not img_bytes:
        return {"status": "error", "error": f"Failed to download captcha for {reg_no}"}

    # 3. Solve CAPTCHA via Railway
    solved_text = solve_via_railway(img_bytes)

    # 4. Search COA & parse
    html = session_manager.search_architect(session, reg_no, solved_text)
    fields = address_extractor.extract_all_fields(html, reg_no)

    # 5. Save back to PHP API
    save_status = 2 if fields.get("address") else 3
    save_payload = {
        "action": "save_scraped",
        "worker_id": WORKER_ID,
        "data": [{
            "registration_no": reg_no,
            "name": fields.get("name", ""),
            "address": fields.get("address", ""),
            "mobile": fields.get("mobile", ""),
            "email": fields.get("email", ""),
            "status": save_status
        }]
    }
    save_resp = requests.post(REMOTE_API_URL, headers=headers, json=save_payload, timeout=15)
    return {
        "status": "success",
        "worker_id": WORKER_ID,
        "scraped": reg_no,
        "saved_summary": save_resp.json()
    }

class RenderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "healthy",
                "platform": "Platform 5 (Render.com)",
                "worker_id": WORKER_ID
            }).encode("utf-8"))
            return

        if self.path.startswith("/scrape"):
            result = scrape_one_record()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    print(f"🚀 Render Platform 5 Worker starting on 0.0.0.0:{PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), RenderHandler)
    server.serve_forever()
