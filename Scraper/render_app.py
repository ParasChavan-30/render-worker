"""
render_app.py — Production Web Service Worker for Render (Platform 5).
Handles Render health checks on $PORT and executes sequential COA scraping jobs.
"""

import os
import sys
import json
import time
import base64
import logging
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

import address_extractor
import session_manager

# ─────────────────────────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("render_worker")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 10000))
REMOTE_API_URL = os.environ.get(
    "REMOTE_API_URL",
    "https://site40243-dn3p3f.scloudsite101.com/Architecture/api.php"
)
REMOTE_API_KEY = os.environ.get("REMOTE_API_KEY", "coa_worker_secret_key_2026")
CAPTCHA_SOLVER_URL = os.environ.get(
    "CAPTCHA_SOLVER_URL",
    "https://friendly-perfection-production-de1de426.up.railway.app/solve"
)
WORKER_ID = os.environ.get("WORKER_ID", "render_platform5")
MAX_RETRIES = 3


# ─────────────────────────────────────────────────────────────────────────────
# Railway CAPTCHA Solver Helper
# ─────────────────────────────────────────────────────────────────────────────
def solve_via_railway(image_bytes: bytes) -> str:
    """Send captcha bytes to dedicated Railway OCR microservice."""
    try:
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        resp = requests.post(
            CAPTCHA_SOLVER_URL,
            json={"image_base64": b64_img},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        solved = data.get("captcha_text") or data.get("text") or ""
        return str(solved).strip()
    except Exception as e:
        logger.error("Railway CAPTCHA solver error: %s", e)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Scraper Logic (Single Record)
# ─────────────────────────────────────────────────────────────────────────────
def scrape_one_record() -> dict:
    """
    Fetch 1 pending record from PHP API, scrape COA, and save results back.
    Includes up to 3 retries per record.
    """
    headers = {
        "X-API-Key": REMOTE_API_KEY,
        "Content-Type": "application/json"
    }

    # 1. Fetch pending record from central PHP API
    try:
        fetch_resp = requests.post(
            REMOTE_API_URL,
            headers=headers,
            json={
                "action": "get_pending",
                "batch_size": 1,
                "worker_id": WORKER_ID
            },
            timeout=15
        )
        fetch_resp.raise_for_status()
        fetch_data = fetch_resp.json()
    except Exception as e:
        logger.error("Failed to fetch pending record from API: %s", e)
        return {
            "status": "error",
            "error": f"Failed to connect to central API: {str(e)}"
        }

    records = fetch_data.get("records", [])
    if not records:
        return {
            "status": "idle",
            "worker_id": WORKER_ID,
            "message": "No pending records (status=0) available in queue."
        }

    target = records[0]
    reg_no = target.get("registration_no", "").strip()
    logger.info("→ Processing: %s (Worker: %s)", reg_no, WORKER_ID)

    # 2. Try scraping up to MAX_RETRIES times
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("  [%s] Attempt %d/%d", reg_no, attempt, MAX_RETRIES)

            # Fresh session
            session = session_manager.create_session()
            if not session_manager.initialize_session(session):
                logger.warning("  [%s] Session init failed on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            # Download captcha
            img_bytes = session_manager.download_captcha(session)
            if not img_bytes:
                logger.warning("  [%s] CAPTCHA download failed on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            # Solve via Railway OCR
            solved_text = solve_via_railway(img_bytes)
            if not solved_text:
                logger.warning("  [%s] CAPTCHA solver returned empty on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            logger.info("  [%s] Solved CAPTCHA: %s", reg_no, solved_text)

            # Submit search form
            search_resp = session_manager.submit_search(session, reg_no, solved_text)
            if search_resp is None:
                logger.warning("  [%s] POST request failed on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            html = search_resp.text

            # Check response conditions
            if address_extractor.is_redirected_away(html):
                logger.warning("  [%s] Redirected away on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            if address_extractor.is_captcha_invalid(html):
                logger.warning("  [%s] Invalid CAPTCHA on attempt %d", reg_no, attempt)
                time.sleep(0.5)
                continue

            # Check if record is not found on COA
            if address_extractor.is_not_found(html):
                logger.info("  [%s] Not found on COA site", reg_no)
                save_payload = {
                    "action": "save_scraped",
                    "worker_id": WORKER_ID,
                    "data": [{
                        "registration_no": reg_no,
                        "address": "NOT_FOUND",
                        "status": 3
                    }]
                }
                save_resp = requests.post(REMOTE_API_URL, headers=headers, json=save_payload, timeout=15)
                return {
                    "status": "not_found",
                    "worker_id": WORKER_ID,
                    "registration_no": reg_no,
                    "saved": save_resp.json()
                }

            # Extract fields
            fields = address_extractor.extract_all_fields(html)
            address = fields.get("address") or address_extractor.extract_address(html) or ""
            name = fields.get("name", "")
            mobile = fields.get("mobile", "")
            email = fields.get("email", "")

            # Save scraped data back to central PHP API
            save_status = 2 if (address or name) else 3
            save_payload = {
                "action": "save_scraped",
                "worker_id": WORKER_ID,
                "data": [{
                    "registration_no": reg_no,
                    "name": name,
                    "email": email,
                    "mobile": mobile,
                    "address": address,
                    "status": save_status
                }]
            }

            save_resp = requests.post(REMOTE_API_URL, headers=headers, json=save_payload, timeout=15)
            save_data = save_resp.json()
            logger.info("  [%s] ✅ Scraped & Saved: %s (Status: %d)", reg_no, name, save_status)

            return {
                "status": "success",
                "worker_id": WORKER_ID,
                "registration_no": reg_no,
                "name": name,
                "mobile": mobile,
                "email": email,
                "address": address[:50] + "..." if len(address) > 50 else address,
                "saved_summary": save_data
            }

        except Exception as e:
            logger.error("  [%s] Error on attempt %d: %s", reg_no, attempt, e)
            time.sleep(0.5)

    # If all attempts failed, mark record status 3
    logger.error("  [%s] ❌ Exhausted all %d attempts", reg_no, MAX_RETRIES)
    try:
        fail_payload = {
            "action": "save_scraped",
            "worker_id": WORKER_ID,
            "data": [{
                "registration_no": reg_no,
                "status": 3
            }]
        }
        requests.post(REMOTE_API_URL, headers=headers, json=fail_payload, timeout=15)
    except Exception:
        pass

    return {
        "status": "failed",
        "worker_id": WORKER_ID,
        "registration_no": reg_no,
        "message": f"Failed after {MAX_RETRIES} attempts"
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Request Handler for Render
# ─────────────────────────────────────────────────────────────────────────────
class RenderHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        """Helper to send JSON response safely."""
        try:
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.error("Error writing HTTP response: %s", e)

    def do_GET(self):
        try:
            clean_path = self.path.split("?")[0].rstrip("/")

            # 1. Health check route
            if clean_path in ("", "/health"):
                self._send_json({
                    "status": "healthy",
                    "platform": "Platform 5 (Render.com)",
                    "worker_id": WORKER_ID,
                    "connected_api": REMOTE_API_URL
                })
                return

            # 2. Scrape route
            if clean_path == "/scrape":
                result = scrape_one_record()
                self._send_json(result, 200)
                return

            # 3. 404 Not Found
            self._send_json({
                "status": "error",
                "message": f"Route not found: {self.path}. Supported: /health, /scrape"
            }, 404)

        except Exception as e:
            # Catch all unhandled exceptions so the server NEVER crashes with 502
            err_trace = traceback.format_exc()
            logger.error("Unhandled exception in do_GET:\n%s", err_trace)
            self._send_json({
                "status": "error",
                "error": str(e),
                "traceback": err_trace.splitlines()[-3:]
            }, 500)

    def log_message(self, format, *args):
        """Override to route HTTP logs through python logger."""
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🚀 Render Platform 5 Worker starting on 0.0.0.0:%d...", PORT)
    server = HTTPServer(("0.0.0.0", PORT), RenderHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down.")
        server.server_close()
