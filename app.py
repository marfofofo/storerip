#!/usr/bin/env python3
"""
app.py  —  StoreCSV Flask backend.

Wraps the existing scraper.py to scrape WooCommerce/Shopify stores via their
public APIs and serve a ready-to-import CSV. Optional AI copy enhancement via
the Claude API (ai_enhance.py).

Design constraints:
  - Zero database. All job state lives in a RAM dict.
  - Zero log files. Debug output goes to stdout via print() only.
  - Zero accounts. Starts with `python3 app.py`.
  - API keys come from .env only, never hardcoded.

Run:  python3 app.py   ->  http://0.0.0.0:$PORT  (default 5050)
"""

import csv
import io
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

# --- Existing scraper (imported, never modified) ---
from scraper import (
    detect_platform,
    fetch_woocommerce_products,
    fetch_shopify_products,
    wc_store_to_rows,
    shopify_to_rows,
)
import ai_enhance

# Google Gemini SDK — optional, guarded so a not-yet-installed package never
# crashes startup (the VPS installs it separately). Used by the legal generator.
try:
    import google.generativeai as genai
except ImportError:
    genai = None

load_dotenv()

APP_VERSION = "1.0"

# --- Tunable defaults, overridable via config.json in the project root ---
CONFIG_DEFAULTS = {
    "max_jobs": 5,
    "job_ttl_minutes": 30,
    "port": 5050,
    "enhance_rate_limit_sec": 1,
}


def load_config():
    """Merge config.json (if present, next to app.py) over the defaults."""
    cfg = dict(CONFIG_DEFAULTS)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            for k in CONFIG_DEFAULTS:
                if k in user_cfg and user_cfg[k] is not None:
                    cfg[k] = user_cfg[k]
            print(f"Config loaded from {path}: {cfg}")
        except Exception as e:  # noqa: BLE001 — bad config must not crash startup
            print(f"Could not read config.json ({e}); using defaults.")
    return cfg


CONFIG = load_config()

# PORT env var still wins over config.json for the listen port.
MAX_CONCURRENT_JOBS = int(CONFIG["max_jobs"])
JOB_TTL_SECONDS = int(CONFIG["job_ttl_minutes"]) * 60
ENHANCE_RATE_LIMIT_SEC = float(CONFIG["enhance_rate_limit_sec"])

# Debug comes from .env, defaults False — never debug=True in production.
DEBUG = os.environ.get("DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")

# CORS: only the Vercel landing origin may call the API cross-origin.
ALLOWED_ORIGIN = os.environ.get(
    "ALLOWED_ORIGIN",
    "https://storerip-opil7d97i-marfofofos-projects.vercel.app",
).rstrip("/")

# Per-IP rate limit on /api/scrape.
RATE_LIMIT_MAX = 3
RATE_LIMIT_WINDOW = 3600  # 1 hour, in seconds

# --- Google Gemini (Legal Pages Generator) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:  # noqa: BLE001 — never block startup on config
        print(f"[gemini] configure failed: {e}")

# Per-IP rate limit on the legal generator (separate ledger from /api/scrape).
LEGAL_RATE_LIMIT_MAX = 5
legal_ip_hits = {}
legal_ip_lock = threading.Lock()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-key")

# --- In-memory job store (no DB, no files) ---
# job_id -> { status, progress, message, log, rows, product_count,
#             platform, output, enhanced, error, domain, created }
jobs = {}
jobs_lock = threading.Lock()

# --- In-memory per-IP rate-limit ledger: ip -> [timestamps] ---
ip_hits = {}
ip_lock = threading.Lock()


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def _now():
    return time.time()


def _active_job_count():
    return sum(1 for j in jobs.values() if j["status"] == "running")


def _cleanup_jobs():
    """Drop jobs older than the TTL. Called opportunistically + by the janitor."""
    cutoff = _now() - JOB_TTL_SECONDS
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if j["created"] < cutoff]
        for jid in stale:
            jobs.pop(jid, None)
    return len(stale)


def _set(job_id, **fields):
    """Thread-safe partial update of a job record."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _append_log(job_id, line):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job["log"].append(line)
            job["message"] = line


def _domain_from_url(url):
    netloc = urlparse(url).netloc or url
    return re.sub(r"[^A-Za-z0-9_.-]", "", netloc.replace(".", "_")) or "store"


# ── Input validation / SSRF guard ──

_BLOCKED_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


def _is_blocked_ip(ip_str):
    """True if the literal IP is loopback / private / link-local / reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_target_url(raw):
    """
    Validate a user-supplied store URL.

    Returns (clean_url, None) on success or (None, reason) on rejection.
    Rejects: empty, >500 chars, missing http(s) scheme, localhost / *.local,
    and hosts that are (or resolve to) loopback/private/link-local addresses.
    This is the primary SSRF defense around scraper.py's outbound requests.
    """
    url = (raw or "").strip()
    if not url:
        return None, "URL required."
    if len(url) > 500:
        return None, "URL too long (max 500 characters)."
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None, "URL must start with http:// or https://"

    host = urlparse(url).hostname
    if not host:
        return None, "URL has no host."
    h = host.lower()
    if h in _BLOCKED_HOSTNAMES or h.endswith(".local"):
        return None, "Local and private hosts are not allowed."
    if _is_blocked_ip(host):
        return None, "Private or loopback IP addresses are not allowed."

    # Resolve and re-check every address the host maps to (SSRF / DNS rebinding).
    try:
        for info in socket.getaddrinfo(host, None):
            if _is_blocked_ip(info[4][0]):
                return None, "Host resolves to a private or loopback address."
    except OSError:
        # Unresolvable here — let the scraper try; network errors are handled later.
        pass

    return url.rstrip("/"), None


# ── Rate limiting ──

def _client_ip():
    """Best-effort client IP, honoring a single nginx X-Forwarded-For hop."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited(ip, ledger=None, lock=None, limit=RATE_LIMIT_MAX, window=RATE_LIMIT_WINDOW):
    """Record a hit and return True if this IP is over the window limit.

    Defaults to the /api/scrape ledger; pass a separate ledger/lock/limit to
    rate-limit another endpoint independently (e.g. the legal generator).
    """
    if ledger is None:
        ledger, lock = ip_hits, ip_lock
    now = _now()
    with lock:
        hits = [t for t in ledger.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            ledger[ip] = hits
            return True
        hits.append(now)
        ledger[ip] = hits
        return False


def _prune_one_ledger(ledger, lock):
    now = _now()
    with lock:
        for ip in list(ledger.keys()):
            fresh = [t for t in ledger[ip] if now - t < RATE_LIMIT_WINDOW]
            if fresh:
                ledger[ip] = fresh
            else:
                ledger.pop(ip, None)


def _prune_ip_hits():
    _prune_one_ledger(ip_hits, ip_lock)
    _prune_one_ledger(legal_ip_hits, legal_ip_lock)


# ── Background janitor: guarantees TTL cleanup even with no traffic ──

def _janitor_loop():
    while True:
        time.sleep(300)  # every 5 minutes
        try:
            removed = _cleanup_jobs()
            _prune_ip_hits()
            if removed:
                print(f"[janitor] cleaned {removed} expired job(s).")
        except Exception as e:  # noqa: BLE001 — janitor must never die
            print(f"[janitor] error: {e}")


def start_janitor():
    threading.Thread(target=_janitor_loop, daemon=True).start()


# ──────────────────────────────────────────────
#  CSV builders (built in memory)
# ──────────────────────────────────────────────

def _build_woocommerce_csv(rows):
    """WooCommerce-format CSV — exact column behavior from scraper.write_csv."""
    all_keys = []
    for row in rows:
        for k in row.keys():
            if k not in all_keys:
                all_keys.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in all_keys})
    return buf.getvalue()


SHOPIFY_FIELDS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value", "Variant SKU", "Variant Price",
    "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
    "Variant Inventory Qty", "Variant Inventory Policy", "Image Src",
]


def _slugify(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:80]


def _build_shopify_csv(rows):
    """
    Convert the WooCommerce-format rows (from scraper.py) into Shopify's native
    product-import CSV. This is a post-transform; scraper.py columns are not
    altered. Variation rows are emitted as extra lines sharing the parent handle.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHOPIFY_FIELDS)
    writer.writeheader()

    current_handle = None
    for row in rows:
        tipo = row.get("Tipo", "simple")
        name = row.get("Nome", "")
        desc = row.get("Descrizione", "")
        body = re.sub(r"<[^>]+>", "", desc)
        cats = row.get("Categorie", "")
        tags = row.get("Tag", "").replace(", ", ",")
        imgs = row.get("Immagini", "")
        first_img = imgs.split(",")[0].strip() if imgs else ""
        price = row.get("Prezzo di listino", "")
        compare = row.get("Prezzo di vendita", "")
        qty = row.get("Stock", "") or "0"

        if tipo == "variation":
            # Reuse the parent's handle; only emit variant-level columns.
            handle = current_handle or _slugify(name)
            opt_value = (
                row.get("Attributo 1 valore(i)", "") or "Default Title"
            )
            writer.writerow({
                "Handle": handle,
                "Option1 Value": opt_value,
                "Variant SKU": row.get("SKU", ""),
                "Variant Price": price,
                "Variant Compare At Price": compare,
                "Variant Requires Shipping": "TRUE",
                "Variant Taxable": "TRUE",
                "Variant Inventory Qty": qty,
                "Variant Inventory Policy": "deny",
            })
        else:
            current_handle = _slugify(name)
            writer.writerow({
                "Handle": current_handle,
                "Title": name,
                "Body (HTML)": body,
                "Vendor": "",
                "Type": cats.split(",")[0].strip() if cats else "",
                "Tags": tags,
                "Published": "TRUE",
                "Option1 Name": row.get("Attributo 1 nome", "") or "Title",
                "Option1 Value": row.get("Attributo 1 valore(i)", "") or "Default Title",
                "Variant SKU": row.get("SKU", ""),
                "Variant Price": price,
                "Variant Compare At Price": compare,
                "Variant Requires Shipping": "TRUE",
                "Variant Taxable": "TRUE",
                "Variant Inventory Qty": qty,
                "Variant Inventory Policy": "deny",
                "Image Src": first_img,
            })
    return buf.getvalue()


# ──────────────────────────────────────────────
#  Worker
# ──────────────────────────────────────────────

def _run_scrape(job_id, url, target, output, enhance):
    """Background worker: detect -> fetch -> convert -> (enhance) -> store rows.

    Any unhandled exception is caught and recorded as job status "error" so a
    crashed thread never leaves a job stuck on "running".
    """
    try:
        url = url.rstrip("/")
        _set(job_id, progress=5)
        _append_log(job_id, f"Target: {url}")

        # Resolve source platform.
        platform = target if target in ("woocommerce", "shopify") else None
        if platform is None:
            _append_log(job_id, "Detecting platform...")
            platform = detect_platform(url)

        if platform not in ("woocommerce", "shopify"):
            _set(job_id, status="error",
                 error="Platform not detected. Check URL or select a platform manually.")
            _append_log(job_id, "Platform not detected.")
            return

        _set(job_id, platform=platform, progress=15)
        _append_log(job_id, f"Platform: {platform}")

        # Fetch raw products.
        _append_log(job_id, f"Fetching {platform} products...")
        if platform == "woocommerce":
            raw = fetch_woocommerce_products(url)
        else:
            raw = fetch_shopify_products(url)

        if not raw:
            _set(job_id, status="error",
                 error="No products returned. The store may be empty or the API is closed.")
            _append_log(job_id, "No products returned.")
            return

        _set(job_id, product_count=len(raw), progress=55)
        _append_log(job_id, f"{len(raw)} products fetched.")

        # Convert to WooCommerce-format rows.
        if platform == "woocommerce":
            rows = wc_store_to_rows(raw)
        else:
            rows = shopify_to_rows(raw)

        _set(job_id, progress=70)
        _append_log(job_id, f"{len(rows)} rows built (incl. variations).")

        # Optional AI enhancement.
        enhanced_done = False
        if enhance:
            if ai_enhance.is_available():
                if len(raw) > ai_enhance.LARGE_CATALOG_THRESHOLD:
                    _append_log(job_id, ai_enhance.LARGE_CATALOG_WARNING)
                _append_log(job_id, "AI enhancing copy (Claude)...")

                def _cb(done, tot):
                    pct = 70 + int(25 * (done / tot)) if tot else 95
                    _set(job_id, progress=min(pct, 95))
                    if done % 5 == 0 or done == tot:
                        _append_log(job_id, f"  enhanced {done}/{tot}")

                count = ai_enhance.enhance_rows(
                    rows, progress_cb=_cb, rate_limit_sec=ENHANCE_RATE_LIMIT_SEC
                )
                enhanced_done = count > 0
                _append_log(job_id, f"AI enhanced {count} products.")
            else:
                _append_log(job_id, "AI enhance requested but API key not configured — skipped.")

        with jobs_lock:
            job = jobs.get(job_id)
            if job is not None:
                job["rows"] = rows
                job["output"] = output if output in ("woocommerce", "shopify") else "woocommerce"
                job["enhanced"] = enhanced_done
                job["product_count"] = len(raw)
                job["row_count"] = len(rows)
                job["progress"] = 100
                job["status"] = "done"
                job["message"] = "Complete."
        _append_log(job_id, "Complete.")

    except Exception as e:  # noqa: BLE001 — never crash the thread
        print(f"[job {job_id}] error: {e}")
        _set(job_id, status="error", error=f"Scrape failed: {e}")


# ──────────────────────────────────────────────
#  Cross-origin + error handling
# ──────────────────────────────────────────────

@app.after_request
def _apply_cors(resp):
    """Only echo CORS headers back to the approved Vercel origin."""
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.errorhandler(Exception)
def _on_unhandled(e):
    """Never leak a stack trace to the client; return clean JSON instead."""
    if isinstance(e, HTTPException):
        return e
    print(f"[unhandled] {type(e).__name__}: {e}")
    return jsonify({"error": "Internal server error."}), 500


# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        ai_available=ai_enhance.is_available(),
    )


@app.route("/api/health")
def api_health():
    with jobs_lock:
        n = len(jobs)
    return jsonify({"status": "ok", "jobs": n}), 200


@app.route("/api/detect", methods=["POST"])
def api_detect():
    data = request.get_json(silent=True) or {}
    url, err = validate_target_url(data.get("url"))
    if err:
        return jsonify({"platform": None, "error": err}), 400
    try:
        platform = detect_platform(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"platform": None, "error": str(e)}), 200
    return jsonify({"platform": platform}), 200


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    _cleanup_jobs()
    data = request.get_json(silent=True) or {}

    # Validate + sanitize the URL (length, scheme, SSRF guard).
    url, err = validate_target_url(data.get("url"))
    if err:
        return jsonify({"error": err}), 400

    target = (data.get("target") or "auto").strip().lower()
    output = (data.get("output") or "woocommerce").strip().lower()
    enhance = bool(data.get("enhance", False))

    # Per-IP rate limit: max RATE_LIMIT_MAX scrapes per RATE_LIMIT_WINDOW.
    ip = _client_ip()
    if _rate_limited(ip):
        return jsonify({
            "error": f"Rate limit reached: max {RATE_LIMIT_MAX} scrapes per hour per IP. "
                     "Try again later."
        }), 429

    if _active_job_count() >= MAX_CONCURRENT_JOBS:
        return jsonify({"error": "Too many concurrent jobs. Try again shortly."}), 429

    job_id = uuid.uuid4().hex[:6]
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "progress": 0,
            "message": "Queued...",
            "log": [],
            "rows": None,
            "product_count": 0,
            "row_count": 0,
            "platform": None,
            "output": output,
            "enhanced": False,
            "error": None,
            "domain": _domain_from_url(url),
            "created": _now(),
        }

    t = threading.Thread(
        target=_run_scrape,
        args=(job_id, url, target, output, enhance),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "running"}), 200


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"status": "error", "error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "log": job["log"][-30:],
            "product_count": job["product_count"],
            "row_count": job["row_count"],
            "platform": job["platform"],
            "output": job["output"],
            "enhanced": job["enhanced"],
            "error": job["error"],
        }), 200


@app.route("/api/download/<job_id>")
def api_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown job"}), 404
        if job["status"] != "done" or not job["rows"]:
            return jsonify({"error": "Job not ready"}), 409
        rows = job["rows"]
        output = job["output"]
        domain = job["domain"]

    if output == "shopify":
        csv_text = _build_shopify_csv(rows)
    else:
        csv_text = _build_woocommerce_csv(rows)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{domain}_{output}_{timestamp}.csv"

    # utf-8-sig so Excel and WooCommerce/Shopify importers read accents correctly.
    payload = io.BytesIO(csv_text.encode("utf-8-sig"))
    payload.seek(0)

    # Cleanup: drop the job from memory after handing off the file.
    with jobs_lock:
        jobs.pop(job_id, None)

    return send_file(
        payload,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/abort/<job_id>", methods=["POST"])
def api_abort(job_id):
    # We can't kill the requests calls mid-flight, but we can drop the job so
    # the UI stops polling and the result is discarded.
    with jobs_lock:
        existed = jobs.pop(job_id, None) is not None
    return jsonify({"aborted": existed}), 200


# ──────────────────────────────────────────────
#  Legal Pages Generator (Google Gemini Flash 2.5)
# ──────────────────────────────────────────────

LEGAL_VALID_LANGS = ("it", "fr", "de", "es", "en")

LEGAL_SYSTEM_INSTRUCTION = """
You are a legal document specialist for European e-commerce
businesses. Generate professional, legally compliant documents.
Follow GDPR, EU Consumer Rights Directive, and local law.
Return ONLY a valid JSON object with no markdown, no preamble.
Format: {"privacy":"...","cgv":"...","cookies":"...",
         "returns":"...","shipping":"...","legal_notice":"..."}
Only include keys for documents that were requested.
Each document must be complete, use markdown headers (## ###),
minimum 400 words, professional tone, ready to publish.
""".strip()


@app.route("/legal")
def legal_page():
    return render_template("legal.html", ai_available=bool(GEMINI_API_KEY))


@app.route("/api/legal/generate", methods=["POST"])
def api_legal_generate():
    try:
        # Gemini must be configured (key present + SDK installed).
        if not GEMINI_API_KEY or genai is None:
            return jsonify({
                "error": "api_key_missing",
                "message": "Legal generator requires Gemini API key. "
                           "Add GEMINI_API_KEY to .env",
            }), 503

        data = request.get_json(silent=True) or {}
        business_name = (data.get("business_name") or "").strip()
        email = (data.get("email") or "").strip()
        country = (data.get("country") or "").strip()
        language = (data.get("language") or "").strip().lower()
        documents = data.get("documents") or []

        # --- Validation (400) ---
        if not business_name or len(business_name) > 200:
            return jsonify({"error": "validation",
                            "message": "business_name is required (max 200 chars)."}), 400
        if "@" not in email or "." not in email:
            return jsonify({"error": "validation",
                            "message": "A valid email is required."}), 400
        if not country:
            return jsonify({"error": "validation",
                            "message": "country is required."}), 400
        if language not in LEGAL_VALID_LANGS:
            return jsonify({"error": "validation",
                            "message": "language must be one of it, fr, de, es, en."}), 400
        if not isinstance(documents, list) or len(documents) < 1:
            return jsonify({"error": "validation",
                            "message": "Select at least one document."}), 400

        # --- Rate limit: 5 / hour / IP (separate ledger) ---
        ip = _client_ip()
        if _rate_limited(ip, legal_ip_hits, legal_ip_lock, LEGAL_RATE_LIMIT_MAX):
            return jsonify({
                "error": "rate_limited",
                "message": f"Rate limit reached: max {LEGAL_RATE_LIMIT_MAX} "
                           "generations per hour per IP. Try again later.",
            }), 429

        # --- Build prompt ---
        prompt = f"""
Generate legal documents for this business:

Business name: {business_name}
Business type: {data.get("business_type", "")}
VAT/Registration: {data.get("vat_number", "")}
Address: {data.get("address", "")}, {data.get("city", "")}, {country}
Email: {email}
Phone: {data.get("phone", "")}
Website: {data.get("website", "")}
Store type: {data.get("store_type", "")}
Products/Services: {data.get("products_category", "")}
Target market: {data.get("target_market", "")}
Language: {language}
Documents requested: {', '.join(documents)}

Rules:
- Write entirely in {language}
- Apply {data.get("target_market", "")} consumer law
- Include GDPR article references in privacy policy
- Include 14-day withdrawal right in CGV
- Use business name and website throughout
- Year: 2026
- Be specific to the products/services category
""".strip()

        # --- Gemini call ---
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "response_mime_type": "application/json",
            },
            system_instruction=LEGAL_SYSTEM_INSTRUCTION,
        )
        response = model.generate_content(prompt)

        try:
            parsed = json.loads(response.text)
        except (ValueError, AttributeError) as e:
            print(f"[legal] parse failed: {e}")
            return jsonify({"error": "parse_failed",
                            "message": "AI returned invalid response, try again"}), 502

        return jsonify({
            "success": True,
            "documents": parsed,
            "generated_at": datetime.now().isoformat(),
            "language": language,
            "business_name": business_name,
        }), 200

    except Exception as e:  # noqa: BLE001 — never crash the server
        print(f"[legal] error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Generation failed. Please try again."}), 500


def _print_banner(port):
    ai_state = "enabled" if ai_enhance.is_available() else "disabled"
    print("================================")
    print(f"StoreCSV Backend v{APP_VERSION}")
    print(f"Port: {port} | Debug: {DEBUG}")
    print(f"Max jobs: {MAX_CONCURRENT_JOBS} | AI: {ai_state}")
    print("================================")


if __name__ == "__main__":
    # PORT env var takes precedence; otherwise fall back to config.json / default.
    port = int(os.environ.get("PORT", CONFIG["port"]))
    _print_banner(port)
    start_janitor()
    app.run(host="0.0.0.0", port=port, debug=DEBUG, threaded=True)
