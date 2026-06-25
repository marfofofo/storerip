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
import math
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
# Cap uploads at 5MB (CSV validator / enhancer) — larger requests get 413.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# --- In-memory job store (no DB, no files) ---
# job_id -> { status, progress, message, log, rows, product_count,
#             platform, output, enhanced, error, domain, created }
jobs = {}
jobs_lock = threading.Lock()

# --- In-memory per-IP rate-limit ledger: ip -> [timestamps] ---
ip_hits = {}
ip_lock = threading.Lock()

# --- In-memory enhancer jobs (bulk AI description rewrite) ---
enhancer_jobs = {}
enhancer_jobs_lock = threading.Lock()

# --- In-memory translator jobs (bulk AI catalog translation) ---
translator_jobs = {}
translator_jobs_lock = threading.Lock()


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
            cutoff = _now() - JOB_TTL_SECONDS
            with enhancer_jobs_lock:
                for jid in [j for j, v in enhancer_jobs.items() if v["created_at"] < cutoff]:
                    enhancer_jobs.pop(jid, None)
            with translator_jobs_lock:
                for jid in [j for j, v in translator_jobs.items() if v["created_at"] < cutoff]:
                    translator_jobs.pop(jid, None)
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


@app.errorhandler(413)
def _on_too_large(e):
    return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413


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


@app.route("/api/debug/jobs")
def debug_jobs():
    """Temporary debug — shows all active jobs (no PII, counts only)."""
    with jobs_lock:
        scrape = {
            k: {
                "status": v.get("status"),
                "rows_count": len(v.get("rows") or []),
                "domain": v.get("domain"),
                "output": v.get("output"),
            } for k, v in jobs.items()
        }
    with enhancer_jobs_lock:
        enhancer = {
            k: {
                "status": v.get("status"),
                "rows_count": len(v.get("rows") or []),
            } for k, v in enhancer_jobs.items()
        }
    return jsonify({"scrape_jobs": scrape, "enhancer_jobs": enhancer}), 200


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
            "target": target,
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
            return jsonify({"error": "job_not_found",
                            "message": "Job expired or not found"}), 404
        if job.get("status") != "done":
            return jsonify({"error": "not_ready", "message": "Job not ready"}), 400
        rows = job.get("rows") or []
        if not rows:
            return jsonify({"error": "no_data", "message": "No rows to export"}), 400
        output = job.get("output", "woocommerce")
        domain = job.get("domain", "store")

    # Keep the format the user selected — do NOT flatten Shopify back to Woo columns.
    if output == "shopify":
        csv_text = _build_shopify_csv(rows)
    else:
        csv_text = _build_woocommerce_csv(rows)

    # Write an explicit UTF-8 BOM so Excel + the platform importers read accents.
    csv_bytes = io.BytesIO()
    csv_bytes.write(b"\xef\xbb\xbf")
    csv_bytes.write(csv_text.encode("utf-8"))
    csv_bytes.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"{domain}_{output}_{timestamp}.csv"

    # Cleanup: drop the job from memory after handing off the file.
    with jobs_lock:
        jobs.pop(job_id, None)

    return send_file(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
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

def _parse_gemini_json(raw):
    """Best-effort parse of a Gemini JSON reply.

    Gemini 2.5 Flash sometimes wraps JSON in ```fences``` or adds prose even
    when asked for raw JSON. Strip fences, try a direct parse, then fall back
    to extracting the outermost {...} object. Returns a dict, or None on failure.
    """
    raw = (raw or "").strip()

    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return None
        return None


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

IMPORTANT: Return ONLY a raw JSON object. No markdown, no code fences, no
explanation. Start your response with {{ and end with }}
""".strip()

        # --- Gemini call ---
        # Note: response_mime_type is intentionally omitted — Gemini 2.5 Flash
        # sometimes ignores it and wraps JSON in markdown anyway. The prompt
        # enforces raw JSON and _parse_gemini_json() cleans up any stray fences.
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 8192,
            },
            system_instruction=LEGAL_SYSTEM_INSTRUCTION,
        )
        response = model.generate_content(prompt)

        parsed = _parse_gemini_json(getattr(response, "text", ""))
        if parsed is None:
            print(f"[legal] parse failed; raw preview: {getattr(response, 'text', '')[:200]!r}")
            return jsonify({
                "error": "parse_failed",
                "message": "AI returned invalid response, try again",
                "raw_preview": (getattr(response, "text", "") or "")[:200],
            }), 502

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


# ──────────────────────────────────────────────
#  CSV Validator
# ──────────────────────────────────────────────

WC_REQUIRED = ["SKU", "Nome", "Tipo", "Prezzo di listino", "Pubblicato", "In stock?"]
WC_VALID_TYPES = {"simple", "variable", "variation", "external", "grouped"}
SH_REQUIRED = ["Handle", "Title", "Vendor", "Type", "Published", "Variant Price"]


def _validator_result(platform, stats, errors, warnings, passed, checks_total):
    return {
        "valid": len(errors) == 0,
        "platform": platform,
        "stats": stats,
        "errors": errors,
        "warnings": warnings,
        "passed": passed,
        "checks_total": checks_total,
        "summary": f"{len(errors)} errors, {len(warnings)} warnings "
                   f"found in {stats['total_rows']} rows.",
    }


def _validate_woocommerce(rows, headers, used_latin1):
    headers = headers or []
    errors, warnings = [], []

    def err(code, message, row=None):
        errors.append({"level": "error", "code": code, "message": message, "row": row})

    def warn(code, message, row=None):
        warnings.append({"level": "warning", "code": code, "message": message, "row": row})

    # 1) required columns
    for col in WC_REQUIRED:
        if col not in headers:
            err("MISSING_COLUMN", f"Missing required column: {col}")

    # global-attribute columns present in the file
    attr_global_cols = {}
    for h in headers:
        m = re.match(r"Attributo (\d+) globale", h)
        if m:
            attr_global_cols[int(m.group(1))] = h

    sku_counts, sku_set = {}, set()
    for row in rows:
        sku = (row.get("SKU") or "").strip()
        if sku:
            sku_set.add(sku)
            sku_counts[sku] = sku_counts.get(sku, 0) + 1

    n_simple = n_variable = n_variation = 0
    for i, row in enumerate(rows, start=2):  # +1 header, +1 to 1-index
        tipo = (row.get("Tipo") or "").strip()
        sku = (row.get("SKU") or "").strip()
        price = (row.get("Prezzo di listino") or "").strip()
        genitore = (row.get("Genitore") or "").strip()

        if tipo == "simple":
            n_simple += 1
        elif tipo == "variable":
            n_variable += 1
        elif tipo == "variation":
            n_variation += 1

        if not sku:                                            # 9
            err("EMPTY_SKU", f"Row {i}: empty SKU", i)
        if tipo and tipo not in WC_VALID_TYPES:                # 2
            err("INVALID_TYPE", f"Row {i}: invalid Tipo '{tipo}'", i)
        if tipo == "variation" and not genitore:               # 3
            err("VARIATION_NO_PARENT", f"Row {i}: variation missing Genitore (parent SKU)", i)
        if genitore and genitore not in sku_set:               # 4
            err("PARENT_NOT_FOUND", f"Row {i}: Genitore '{genitore}' not found as SKU", i)
        if tipo == "variable" and price:                       # 5
            warn("PRICE_ON_PARENT",
                 f"Row {i}: variable parent has price — price should be on variation rows", i)
        if tipo == "variation" and not price:                  # 6
            warn("VARIATION_NO_PRICE", f"Row {i}: variation missing price", i)
        if tipo == "variable":                                 # 8
            for idx_attr, col in attr_global_cols.items():
                if (row.get(col) or "").strip() != "1":
                    warn("ATTR_GLOBAL",
                         f"Row {i}: Attributo {idx_attr} globale should be 1 on variable parent", i)

    for sku, c in sku_counts.items():                          # 7
        if c > 1:
            err("DUPLICATE_SKU", f"Duplicate SKU: '{sku}' appears {c} times")
    if used_latin1:                                            # 10
        warn("ENCODING",
             "File encoding is not UTF-8. Re-save as UTF-8 with BOM for best WooCommerce compatibility")

    stats = {
        "total_rows": len(rows),
        "products": n_simple + n_variable,
        "variations": n_variation,
        "simple": n_simple,
    }
    checks = [
        ("Required columns", {"MISSING_COLUMN"}),
        ("Product types", {"INVALID_TYPE"}),
        ("Variation parent present", {"VARIATION_NO_PARENT"}),
        ("Parent SKU exists", {"PARENT_NOT_FOUND"}),
        ("Price on parent rows", {"PRICE_ON_PARENT"}),
        ("Price on variation rows", {"VARIATION_NO_PRICE"}),
        ("Duplicate SKU", {"DUPLICATE_SKU"}),
        ("Global attributes", {"ATTR_GLOBAL"}),
        ("Non-empty SKU", {"EMPTY_SKU"}),
        ("UTF-8 encoding", {"ENCODING"}),
    ]
    seen = {e["code"] for e in errors} | {w["code"] for w in warnings}
    passed = [name for name, codes in checks if not (codes & seen)]
    return _validator_result("woocommerce", stats, errors, warnings, passed, len(checks))


def _validate_shopify(rows, headers):
    headers = headers or []
    errors, warnings = [], []

    def err(code, message, row=None):
        errors.append({"level": "error", "code": code, "message": message, "row": row})

    def warn(code, message, row=None):
        warnings.append({"level": "warning", "code": code, "message": message, "row": row})

    for col in SH_REQUIRED:                                    # 1
        if col not in headers:
            err("MISSING_COLUMN", f"Missing required column: {col}")

    has_image = "Image Src" in headers
    handle_titles, handle_counts = {}, {}
    for row in rows:
        h = (row.get("Handle") or "").strip()
        if h:
            handle_counts[h] = handle_counts.get(h, 0) + 1
            t = (row.get("Title") or "").strip()
            if t:
                handle_titles.setdefault(h, set()).add(t)

    for i, row in enumerate(rows, start=2):
        if not (row.get("Variant Price") or "").strip():       # 3
            warn("EMPTY_VARIANT_PRICE", f"Row {i}: empty Variant Price", i)
        if has_image:                                          # 4
            img = (row.get("Image Src") or "").strip()
            if img and not re.match(r"^https?://", img, re.IGNORECASE):
                warn("BAD_IMAGE_URL", f"Row {i}: Image Src is not a valid URL", i)
        pub = (row.get("Published") or "").strip()             # 5
        if pub and pub.upper() not in ("TRUE", "FALSE"):
            warn("BAD_PUBLISHED", f"Row {i}: Published value '{pub}' should be TRUE or FALSE", i)

    for h, titles in handle_titles.items():                    # 2
        if len(titles) > 1:
            warn("HANDLE_TITLE", f"Handle '{h}': inconsistent Title across rows")

    unique_handles = len(handle_counts)
    stats = {
        "total_rows": len(rows),
        "products": unique_handles,
        "variations": max(0, len(rows) - unique_handles),
        "simple": sum(1 for c in handle_counts.values() if c == 1),
    }
    checks = [
        ("Required columns", {"MISSING_COLUMN"}),
        ("Consistent Title per Handle", {"HANDLE_TITLE"}),
        ("Variant Price present", {"EMPTY_VARIANT_PRICE"}),
        ("Image URL format", {"BAD_IMAGE_URL"}),
        ("Published values", {"BAD_PUBLISHED"}),
    ]
    seen = {e["code"] for e in errors} | {w["code"] for w in warnings}
    passed = [name for name, codes in checks if not (codes & seen)]
    return _validator_result("shopify", stats, errors, warnings, passed, len(checks))


@app.route("/validator")
def validator_page():
    return render_template("validator.html")


@app.route("/api/validator/check", methods=["POST"])
def api_validator_check():
    try:
        file = request.files.get("file")
        platform = (request.form.get("platform") or "woocommerce").strip().lower()
        if file is None or not file.filename:
            return jsonify({"error": "no_file", "message": "No CSV file uploaded."}), 400

        raw = file.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413

        used_latin1 = False
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            used_latin1 = True

        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        rows = list(reader)

        if platform == "shopify":
            result = _validate_shopify(rows, headers)
        else:
            result = _validate_woocommerce(rows, headers, used_latin1)
        return jsonify(result), 200

    except Exception as e:  # noqa: BLE001 — never crash the server
        print(f"[validator] error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Validation failed. Check the file is a valid CSV."}), 500


# ──────────────────────────────────────────────
#  Bulk Price Editor (no AI — pure CSV math)
# ──────────────────────────────────────────────

# Per-platform column mapping for the price + filter fields.
_PRICE_COLS = {
    "woocommerce": {"price": "Prezzo di listino", "sale_price": "Prezzo di vendita",
                    "category": "Categorie", "type": "Tipo"},
    "shopify": {"price": "Variant Price", "sale_price": "Variant Compare At Price",
                "category": "Type", "type": None},
}


def _parse_price(raw):
    """Parse a price cell to float, tolerating €, spaces, and comma decimals.

    Returns None for empty or non-numeric cells (which are then left untouched).
    """
    s = re.sub(r"[^\d,.\-]", "", (raw or "").strip())  # drop currency / spaces
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")   # European 1.234,56 -> 1234.56
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _round_price(price, round_to):
    """Round price UP to the nearest .99/.95/.00-style ending (per spec)."""
    base = math.ceil(round_to) or 1
    return math.ceil(price / base) * base - (base - round_to)


def _apply_price_op(price, op):
    t = op.get("type")
    if t == "percentage":
        return price * (1 + float(op.get("value", 0)) / 100)
    if t == "fixed":
        return price + float(op.get("value", 0))
    if t == "set":
        return float(op.get("value", 0))
    if t == "round":
        return _round_price(price, float(op.get("round_to", 0.99)))
    return price


def _row_matches_filters(row, cols, op):
    fcat = (op.get("filter_category") or "").strip().lower()
    if fcat and fcat not in (row.get(cols["category"]) or "").lower():
        return False
    ftype = (op.get("filter_type") or "").strip().lower()
    if ftype:
        if cols["type"] is None:            # Shopify has no product-type column
            return False
        if (row.get(cols["type"]) or "").strip().lower() != ftype:
            return False
    return True


def _op_target_cols(op, cols):
    field = (op.get("field") or "price").strip().lower()
    if field == "both":
        return [cols["price"], cols["sale_price"]]
    if field == "sale_price":
        return [cols["sale_price"]]
    return [cols["price"]]


def _process_price_row(row, platform, cols, operations):
    """Apply every operation, in order, to one row. Returns True if changed."""
    # Never touch the (empty) price on a WooCommerce variable parent row.
    if platform == "woocommerce" and (row.get(cols["type"]) or "").strip() == "variable":
        return False

    changed = False
    for op in operations:
        if not _row_matches_filters(row, cols, op):
            continue
        for col in _op_target_cols(op, cols):
            if not col:
                continue
            cur = _parse_price(row.get(col))
            if cur is None:                 # empty / non-numeric -> leave as-is
                continue
            new_price = _apply_price_op(cur, op)
            if new_price < 0:
                new_price = 0.0
            row[col] = f"{new_price:.2f}"
            changed = True
    return changed


@app.route("/price-editor")
def price_editor_page():
    return render_template("price-editor.html")


@app.route("/api/price-editor/process", methods=["POST"])
def api_price_editor_process():
    try:
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "no_file", "message": "No CSV file uploaded."}), 400

        platform = (request.form.get("platform") or "woocommerce").strip().lower()
        if platform not in _PRICE_COLS:
            platform = "woocommerce"
        cols = _PRICE_COLS[platform]

        try:
            operations = json.loads(request.form.get("operations") or "[]")
        except (json.JSONDecodeError, TypeError):
            return jsonify({"error": "bad_operations",
                            "message": "Operations payload is not valid JSON."}), 400
        if not isinstance(operations, list) or not operations:
            return jsonify({"error": "no_operations",
                            "message": "Add at least one price operation."}), 400

        raw = file.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            return jsonify({"error": "empty_csv", "message": "CSV has no columns."}), 400
        rows = list(reader)

        modified = sum(1 for row in rows
                       if _process_price_row(row, platform, cols, operations))
        skipped = len(rows) - modified

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

        csv_bytes = io.BytesIO()
        csv_bytes.write(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel / importers
        csv_bytes.write(buf.getvalue().encode("utf-8"))
        csv_bytes.seek(0)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        resp = send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name=f"price_edited_{timestamp}.csv")
        resp.headers["X-Rows-Modified"] = str(modified)
        resp.headers["X-Rows-Skipped"] = str(skipped)
        # Allow the cross-origin Vercel front-end JS to read the stat headers.
        resp.headers["Access-Control-Expose-Headers"] = "X-Rows-Modified, X-Rows-Skipped"
        return resp

    except Exception as e:  # noqa: BLE001 — never crash the server
        print(f"[price-editor] error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Processing failed. Check the file is a valid CSV."}), 500


# ──────────────────────────────────────────────
#  Google Shopping Feed generator (no AI — CSV -> GMC TSV)
# ──────────────────────────────────────────────

FEED_VALID_COUNTRIES = ("IT", "FR", "DE", "ES", "GB", "CH")
FEED_VALID_CURRENCIES = ("EUR", "GBP", "CHF")
FEED_VALID_CONDITIONS = ("new", "used", "refurbished")

# Google Merchant Center attribute columns, in output order.
FEED_COLUMNS = [
    "id", "title", "description", "link", "image_link", "availability",
    "price", "sale_price", "brand", "condition", "gtin", "mpn",
    "product_type", "google_product_category", "identifier_exists", "shipping",
]

# Heuristic product_type -> Google product category. First substring match wins.
_GPC_MAP = {
    "elettronica": "Electronics",
    "electronic": "Electronics",
    "informatica": "Electronics > Computers",
    "computer": "Electronics > Computers",
    "telefoni": "Electronics > Communications",
    "phone": "Electronics > Communications",
    "abbigliamento": "Apparel & Accessories",
    "clothing": "Apparel & Accessories",
    "scarpe": "Apparel & Accessories > Shoes",
    "shoes": "Apparel & Accessories > Shoes",
    "casa": "Home & Garden",
    "home": "Home & Garden",
    "giardino": "Home & Garden",
    "garden": "Home & Garden",
    "sport": "Sporting Goods",
    "beauty": "Health & Beauty",
    "bellezza": "Health & Beauty",
    "cosmetici": "Health & Beauty",
    "giocattoli": "Toys & Games",
    "toys": "Toys & Games",
    "auto": "Vehicles & Parts",
    "alimentari": "Food, Beverages & Tobacco",
    "food": "Food, Beverages & Tobacco",
    "libri": "Media > Books",
    "books": "Media > Books",
}


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "")


def _tsv_clean(s, limit=None):
    """Make a value safe for a tab-delimited feed: no tabs/newlines, collapsed."""
    s = re.sub(r"\s+", " ", (s or "").replace("\t", " ")).strip()
    return s[:limit] if limit else s


def _gpc_category(product_type):
    p = (product_type or "").lower()
    for key, val in _GPC_MAP.items():
        if key in p:
            return val
    return ""


def _shipping_value(price_val, params):
    """Google shipping format 'COUNTRY:::PRICE CUR' (region+service left empty)."""
    thr = params["shipping_free_threshold"]
    cost = 0.0 if (thr > 0 and price_val >= thr) else params["shipping_price"]
    return f"{params['country']}:::{cost:.2f} {params['currency']}"


def _wc_feed_rows(rows, params):
    cur, base = params["currency"], params["base_url"]
    items, cur_name, cur_img = [], "", ""
    for row in rows:
        tipo = (row.get("Tipo") or "").strip().lower()
        if tipo in ("simple", "variable"):
            cur_name = (row.get("Nome") or "").strip() or cur_name
            imgs = (row.get("Immagini") or "").strip()
            if imgs:
                cur_img = imgs.split(",")[0].strip()
        if tipo not in ("simple", "variation"):
            continue
        sku = (row.get("SKU") or "").strip()
        if not sku:
            continue
        price_val = _parse_price(row.get("Prezzo di listino"))
        if price_val is None or price_val <= 0:
            continue

        name = (row.get("Nome") or "").strip()
        if tipo == "variation":
            base_name = name or cur_name
            attr = (row.get("Attributo 1 valore(i)") or "").strip()
            title = (base_name + (" - " + attr if attr else "")).strip()
        else:
            title = name
        title = _tsv_clean(title) or sku

        desc = (row.get("Descrizione breve") or "").strip() or (row.get("Descrizione") or "").strip()
        desc = _tsv_clean(_strip_html(desc), 500) or title

        imgs = (row.get("Immagini") or "").strip()
        img = (imgs.split(",")[0].strip() if imgs else "") or cur_img

        stock_raw = row.get("In stock?")
        if stock_raw is None or not stock_raw.strip():
            avail = "in stock"
        else:
            avail = ("in stock" if stock_raw.strip().lower() in
                     ("1", "yes", "true", "si", "sì", "instock") else "out of stock")

        sale_val = _parse_price(row.get("Prezzo di vendita"))
        sale_str = f"{sale_val:.2f} {cur}" if (sale_val and 0 < sale_val < price_val) else ""

        brand = (row.get("Brand") or row.get("Marca") or row.get("Marchio") or "").strip() \
            or params["brand"]
        gtin = (row.get("GTIN") or row.get("EAN") or row.get("Codice a barre") or "").strip()
        ptype = ((row.get("Categorie") or "").split(",")[0]).strip()

        items.append({
            "id": sku,
            "title": title,
            "description": desc,
            "link": f"{base}/prodotto/{_slugify(name or cur_name or title)}",
            "image_link": _tsv_clean(img),
            "availability": avail,
            "price": f"{price_val:.2f} {cur}",
            "sale_price": sale_str,
            "brand": _tsv_clean(brand),
            "condition": params["condition"],
            "gtin": _tsv_clean(gtin),
            "mpn": sku,
            "product_type": _tsv_clean(ptype),
            "google_product_category": _gpc_category(ptype),
            "identifier_exists": "yes" if (gtin or (sku and brand)) else "no",
            "shipping": _shipping_value(price_val, params),
        })
    return items


def _shopify_feed_rows(rows, params):
    cur, base = params["currency"], params["base_url"]
    items = []
    cur_title = cur_body = cur_type = cur_vendor = cur_img = ""
    last_handle, pos = None, 0
    for row in rows:
        handle = (row.get("Handle") or "").strip()
        if handle and handle != last_handle:
            last_handle, pos = handle, 0
            cur_title = (row.get("Title") or "").strip()
            cur_body = (row.get("Body (HTML)") or "").strip()
            cur_type = (row.get("Type") or "").strip()
            cur_vendor = (row.get("Vendor") or "").strip()
            cur_img = (row.get("Image Src") or "").strip()
        else:
            img = (row.get("Image Src") or "").strip()
            if img and not cur_img:
                cur_img = img

        variant_price = _parse_price(row.get("Variant Price"))
        if variant_price is None or variant_price <= 0:
            continue  # image-only / empty rows

        pos += 1
        h = handle or last_handle or ""
        vsku = (row.get("Variant SKU") or "").strip()
        item_id = vsku or f"{h}-{pos}"

        opts = []
        for col in ("Option1 Value", "Option2 Value", "Option3 Value"):
            v = (row.get(col) or "").strip()
            if v and v.lower() != "default title":
                opts.append(v)
        title = cur_title or h
        if opts:
            title = f"{title} - {' / '.join(opts)}"
        title = _tsv_clean(title) or item_id

        desc = _tsv_clean(_strip_html(cur_body), 500) or title
        img = (row.get("Image Src") or "").strip() or cur_img

        qty_raw = (row.get("Variant Inventory Qty") or "").strip()
        if not qty_raw:
            avail = "in stock"
        else:
            try:
                avail = "in stock" if float(qty_raw) > 0 else "out of stock"
            except ValueError:
                avail = "in stock"

        # Compare-At is the original (higher) price -> it's the Google "price";
        # the (lower) Variant Price becomes the sale_price. With no Compare-At,
        # Variant Price is simply the regular price and there is no sale.
        compare = _parse_price(row.get("Variant Compare At Price"))
        if compare and compare > variant_price:
            regular_val, sale_str = compare, f"{variant_price:.2f} {cur}"
        else:
            regular_val, sale_str = variant_price, ""

        gtin = (row.get("Variant Barcode") or "").strip()
        brand = cur_vendor or params["brand"]

        items.append({
            "id": item_id,
            "title": title,
            "description": desc,
            "link": f"{base}/products/{h}",
            "image_link": _tsv_clean(img),
            "availability": avail,
            "price": f"{regular_val:.2f} {cur}",
            "sale_price": sale_str,
            "brand": _tsv_clean(brand),
            "condition": params["condition"],
            "gtin": _tsv_clean(gtin),
            "mpn": vsku,
            "product_type": _tsv_clean(cur_type),
            "google_product_category": _gpc_category(cur_type),
            "identifier_exists": "yes" if (gtin or (vsku and brand)) else "no",
            "shipping": _shipping_value(regular_val, params),
        })
    return items


@app.route("/shopping-feed")
def shopping_feed_page():
    return render_template("shopping-feed.html")


@app.route("/api/shopping-feed/generate", methods=["POST"])
def api_shopping_feed_generate():
    try:
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "no_file", "message": "No CSV file uploaded."}), 400

        platform = (request.form.get("platform") or "woocommerce").strip().lower()
        country = (request.form.get("country") or "IT").strip().upper()
        if country not in FEED_VALID_COUNTRIES:
            country = "IT"
        currency = (request.form.get("currency") or "EUR").strip().upper()
        if currency not in FEED_VALID_CURRENCIES:
            currency = "EUR"
        condition = (request.form.get("condition") or "new").strip().lower()
        if condition not in FEED_VALID_CONDITIONS:
            condition = "new"
        brand = (request.form.get("brand") or "").strip()

        store_url = (request.form.get("store_url") or "").strip().rstrip("/")
        if store_url and not re.match(r"^https?://", store_url, re.IGNORECASE):
            store_url = "https://" + store_url
        base_url = store_url or "https://YOURSTORE.COM"

        params = {
            "country": country,
            "currency": currency,
            "condition": condition,
            "brand": brand,
            "base_url": base_url,
            "shipping_price": _parse_price(request.form.get("shipping_price")) or 0.0,
            "shipping_free_threshold": _parse_price(request.form.get("shipping_free_threshold")) or 0.0,
        }

        raw = file.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        if not (reader.fieldnames or []):
            return jsonify({"error": "empty_csv", "message": "CSV has no columns."}), 400
        rows = list(reader)

        items = (_shopify_feed_rows(rows, params) if platform == "shopify"
                 else _wc_feed_rows(rows, params))
        if not items:
            return jsonify({
                "error": "no_products",
                "message": "No eligible products found. Check the platform and that "
                           "rows have a SKU/price.",
            }), 400

        # Build the TSV by hand: Google reads raw tabs, so values are sanitized
        # (no tabs/newlines) and joined directly — never CSV-quoted.
        lines = ["\t".join(FEED_COLUMNS)]
        for it in items:
            lines.append("\t".join(it.get(c, "") for c in FEED_COLUMNS))
        tsv = "\r\n".join(lines) + "\r\n"

        feed_bytes = io.BytesIO()
        feed_bytes.write(b"\xef\xbb\xbf")  # UTF-8 BOM
        feed_bytes.write(tsv.encode("utf-8"))
        feed_bytes.seek(0)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        resp = send_file(feed_bytes, mimetype="text/tab-separated-values; charset=utf-8",
                         as_attachment=True,
                         download_name=f"shopping_feed_{country}_{timestamp}.tsv")
        resp.headers["X-Products-Count"] = str(len(items))
        resp.headers["X-Feed-Country"] = country
        resp.headers["Access-Control-Expose-Headers"] = "X-Products-Count, X-Feed-Country"
        return resp

    except Exception as e:  # noqa: BLE001 — never crash the server
        print(f"[shopping-feed] error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Feed generation failed. Check the file is a valid CSV."}), 500


# ──────────────────────────────────────────────
#  Product Description Enhancer (Google Gemini Flash 2.5)
# ──────────────────────────────────────────────

ENHANCER_MAX_PRODUCTS = 200
ENHANCER_VALID_LANGS = ("it", "fr", "de", "es", "en")
ENHANCER_VALID_TONES = ("professional", "friendly", "luxury", "technical")

# Per-platform column mapping for the enhanceable fields.
_ENH_COLS = {
    "woocommerce": {"name": "Nome", "short_description": "Descrizione breve",
                    "description": "Descrizione", "category": "Categorie"},
    "shopify": {"name": "Title", "short_description": None,
                "description": "Body (HTML)", "category": "Type"},
}


def _enhancer_targets(rows, platform):
    """Return the indices of rows that should be enhanced."""
    targets = []
    if platform == "shopify":
        seen = set()
        for idx, row in enumerate(rows):
            h = (row.get("Handle") or "").strip()
            if h and h not in seen:
                seen.add(h)
                targets.append(idx)
    else:  # woocommerce: only simple/variable parents (skip variations)
        for idx, row in enumerate(rows):
            if (row.get("Tipo") or "").strip() in ("simple", "variable"):
                targets.append(idx)
    return targets


def _enhance_one(row, platform, fields, language, tone):
    """Rewrite a single product row in place via Gemini. Raises on failure."""
    cols = _ENH_COLS.get(platform, _ENH_COLS["woocommerce"])
    name = (row.get(cols["name"]) or "").strip() if cols["name"] else ""
    desc_col, sdesc_col = cols["description"], cols["short_description"]
    description = (row.get(desc_col) or "").strip() if desc_col else ""
    category = (row.get(cols["category"]) or "").strip() if cols["category"] else ""

    prompt = f"""
You are an expert e-commerce copywriter.
Rewrite the product content below.

Product name: {name}
Category: {category}
Current description: {description[:500]}
Target language: {language}
Tone: {tone}

Rules:
- Write in {language}
- Tone: {tone}
- Short description: 1-2 sentences, benefit-focused, max 160 chars, SEO-optimized
- Long description: 3-4 paragraphs, features + benefits, include relevant keywords naturally
- Do not invent specifications not in the original
- Return ONLY JSON, no markdown:
{{"short_description": "...", "description": "..."}}

CRITICAL: Return ONLY a raw JSON object. No markdown. No code fences. No
explanation. Start with {{ and end with }}
""".strip()

    # response_mime_type intentionally omitted — Gemini 2.5 Flash sometimes
    # ignores it and wraps JSON in markdown anyway. The prompt enforces raw
    # JSON and _parse_gemini_json() strips any stray fences (matches legal side).
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config={
            "temperature": 0.7,
            "max_output_tokens": 1024,
        },
    )
    response = model.generate_content(prompt)
    enhanced = _parse_gemini_json(getattr(response, "text", ""))
    if enhanced is None:
        raise ValueError("Gemini returned unparseable JSON")

    new_short = (enhanced.get("short_description") or "").strip()
    new_desc = (enhanced.get("description") or "").strip()
    if "short_description" in fields and sdesc_col and new_short:
        row[sdesc_col] = new_short
    if "description" in fields and desc_col and new_desc:
        row[desc_col] = new_desc


def _run_enhancer(job_id):
    """Background worker: enhance each target row, updating job progress."""
    with enhancer_jobs_lock:
        job = enhancer_jobs.get(job_id)
    if not job:
        return
    try:
        rows = job["original_rows"]
        platform, fields = job["platform"], job["_fields"]
        language, tone = job["language"], job["tone"]
        targets = job["_targets"]
        total = len(targets)
        processed = 0

        for idx in targets:
            try:
                _enhance_one(rows[idx], platform, fields, language, tone)
            except Exception as e:  # noqa: BLE001 — keep original on per-row failure
                print(f"[enhancer {job_id}] row {idx} kept original: {e}")

            processed += 1
            with enhancer_jobs_lock:
                j = enhancer_jobs.get(job_id)
                if not j:
                    return  # job was deleted/aborted
                j["processed"] = processed
                j["progress"] = int(processed / total * 100) if total else 100
                j["message"] = f"Processing product {processed} of {total}"
            time.sleep(0.5)  # rate limit between calls

        with enhancer_jobs_lock:
            j = enhancer_jobs.get(job_id)
            if j:
                j["rows"] = rows
                j["status"] = "done"
                j["progress"] = 100
                j["message"] = f"Done — {total} descriptions enhanced."

    except Exception as e:  # noqa: BLE001 — never crash the thread
        print(f"[enhancer {job_id}] error: {e}")
        with enhancer_jobs_lock:
            j = enhancer_jobs.get(job_id)
            if j:
                j["status"] = "error"
                j["error"] = "Enhancement failed. Please try again."


@app.route("/enhancer")
def enhancer_page():
    return render_template("enhancer.html", ai_available=bool(GEMINI_API_KEY))


@app.route("/api/enhancer/start", methods=["POST"])
def api_enhancer_start():
    try:
        if not GEMINI_API_KEY or genai is None:
            return jsonify({
                "error": "api_key_missing",
                "message": "Enhancer requires Gemini API key. Add GEMINI_API_KEY to .env",
            }), 503

        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "no_file", "message": "No CSV file uploaded."}), 400

        platform = (request.form.get("platform") or "woocommerce").strip().lower()
        language = (request.form.get("language") or "en").strip().lower()
        tone = (request.form.get("tone") or "professional").strip().lower()
        fields_raw = request.form.get("fields") or "short_description,description"
        fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        if language not in ENHANCER_VALID_LANGS:
            language = "en"
        if tone not in ENHANCER_VALID_TONES:
            tone = "professional"

        raw = file.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        rows = list(reader)
        targets = _enhancer_targets(rows, platform)

        truncated = len(targets) > ENHANCER_MAX_PRODUCTS
        if truncated:
            targets = targets[:ENHANCER_MAX_PRODUCTS]
        if not targets:
            return jsonify({"error": "no_products",
                            "message": "No enhanceable products found in this CSV."}), 400

        total = len(targets)
        message = "Starting..."
        if truncated:
            message = f"Starting... (limited to the first {ENHANCER_MAX_PRODUCTS} products)"

        job_id = uuid.uuid4().hex[:6]
        with enhancer_jobs_lock:
            enhancer_jobs[job_id] = {
                "status": "running",
                "progress": 0,
                "message": message,
                "total": total,
                "processed": 0,
                "rows": [],
                "original_rows": rows,
                "fieldnames": fieldnames,
                "platform": platform,
                "language": language,
                "tone": tone,
                "skipped": max(0, len(rows) - total),
                "truncated": truncated,
                "error": None,
                "created_at": _now(),
                "_targets": targets,
                "_fields": fields,
            }

        threading.Thread(target=_run_enhancer, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running", "total": total}), 200

    except Exception as e:  # noqa: BLE001
        print(f"[enhancer] start error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Could not start enhancement."}), 500


@app.route("/api/enhancer/status/<job_id>")
def api_enhancer_status(job_id):
    with enhancer_jobs_lock:
        job = enhancer_jobs.get(job_id)
        if not job:
            return jsonify({"status": "error", "error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "processed": job["processed"],
            "total": job["total"],
            "platform": job["platform"],
            "language": job["language"],
            "skipped": job["skipped"],
            "error": job["error"],
        }), 200


@app.route("/api/enhancer/download/<job_id>")
def api_enhancer_download(job_id):
    with enhancer_jobs_lock:
        job = enhancer_jobs.get(job_id)
        if not job:
            return jsonify({"error": "job_not_found",
                            "message": "Job expired or not found"}), 404
        if job.get("status") != "done":
            return jsonify({"error": "not_ready", "message": "Job not ready"}), 400
        rows = job.get("rows") or []
        if not rows:
            return jsonify({"error": "no_data", "message": "No rows to export"}), 400
        fieldnames = job["fieldnames"]
        platform = job["platform"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})

    csv_bytes = io.BytesIO()
    csv_bytes.write(b"\xef\xbb\xbf")  # UTF-8 BOM
    csv_bytes.write(buf.getvalue().encode("utf-8"))
    csv_bytes.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"enhanced_{platform}_{timestamp}.csv"

    with enhancer_jobs_lock:
        enhancer_jobs.pop(job_id, None)

    return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name=filename)


# ──────────────────────────────────────────────
#  Catalog Translator (Google Gemini Flash 2.5)
# ──────────────────────────────────────────────

TRANSLATOR_MAX_PRODUCTS = 300
TRANSLATOR_VALID_LANGS = ("it", "fr", "de", "es", "en")
TRANSLATOR_SOURCE_LANGS = ("it", "fr", "de", "es", "en", "auto")

_LANG_LABELS = {"it": "Italian", "fr": "French", "de": "German",
                "es": "Spanish", "en": "English"}

# Per-platform column mapping for the translatable fields.
_TRANS_COLS = {
    "woocommerce": {"name": "Nome", "short_description": "Descrizione breve",
                    "description": "Descrizione"},
    "shopify": {"name": "Title", "short_description": None,
                "description": "Body (HTML)"},
}


def _translate_one(row, platform, fields, source_lang, target_lang):
    """Translate a single product row in place via Gemini. Raises on failure.

    Returns the row's resulting name (translated or original) so the worker can
    propagate it to WooCommerce variation rows.
    """
    cols = _TRANS_COLS.get(platform, _TRANS_COLS["woocommerce"])
    name_col, sdesc_col, desc_col = cols["name"], cols["short_description"], cols["description"]
    name = (row.get(name_col) or "").strip() if name_col else ""
    short_desc = (row.get(sdesc_col) or "").strip() if sdesc_col else ""
    description = (row.get(desc_col) or "").strip() if desc_col else ""

    source_lang_label = {
        "it": "Italian", "fr": "French", "de": "German",
        "es": "Spanish", "en": "English", "auto": "the source language",
    }.get(source_lang, "the source language")
    target_lang_label = _LANG_LABELS.get(target_lang, "English")

    prompt = f"""
Translate the following product content from
{source_lang_label} to {target_lang_label}.

Product name: {name}
Short description: {short_desc}
Long description: {description[:600]}

Rules:
- Translate naturally, not literally
- Keep product names recognizable
  (brand names, model numbers stay in original)
- Keep HTML tags if present in description
- Keep technical specs (sizes, weights, measurements) as-is
- Return ONLY raw JSON, no markdown, no code fences:
{{"name": "...", "short_description": "...",
  "description": "..."}}

CRITICAL: Start with {{ and end with }}
""".strip()

    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config={"temperature": 0.2, "max_output_tokens": 1024},
    )
    response = model.generate_content(prompt)
    translated = _parse_gemini_json(getattr(response, "text", ""))
    if translated is None:
        raise ValueError("Gemini returned unparseable JSON")

    new_name = (translated.get("name") or "").strip()
    new_short = (translated.get("short_description") or "").strip()
    new_desc = (translated.get("description") or "").strip()
    if "name" in fields and name_col and new_name:
        row[name_col] = new_name
    if "short_description" in fields and sdesc_col and new_short:
        row[sdesc_col] = new_short
    if "description" in fields and desc_col and new_desc:
        row[desc_col] = new_desc

    return (row.get(name_col) or "").strip() if name_col else name


def _run_translator(job_id):
    """Background worker: translate each target row, propagate names to variations."""
    with translator_jobs_lock:
        job = translator_jobs.get(job_id)
    if not job:
        return
    try:
        rows = job["_all_rows"]
        platform, fields = job["platform"], job["_fields"]
        source_lang, target_lang = job["source_lang"], job["target_lang"]
        targets = job["_targets"]
        cols = _TRANS_COLS.get(platform, _TRANS_COLS["woocommerce"])
        total = len(targets)
        processed = skipped = 0
        name_by_sku = {}

        for idx in targets:
            row = rows[idx]
            label = (row.get(cols["name"]) or "").strip() or "product"
            try:
                _translate_one(row, platform, fields, source_lang, target_lang)
                if platform == "woocommerce" and "name" in fields:
                    sku = (row.get("SKU") or "").strip()
                    if sku:
                        name_by_sku[sku] = (row.get(cols["name"]) or "").strip()
            except Exception as e:  # noqa: BLE001 — keep original on per-row failure
                skipped += 1
                print(f"[translator {job_id}] row {idx} kept original: {e}")

            processed += 1
            with translator_jobs_lock:
                j = translator_jobs.get(job_id)
                if not j:
                    return  # aborted / expired
                j["processed"] = processed
                j["skipped"] = skipped
                j["progress"] = int(processed / total * 100) if total else 100
                j["message"] = f"Translating product {processed} of {total} — {label}"
            time.sleep(0.4)  # rate limit between calls

        # Propagate translated parent names onto WooCommerce variation rows.
        if platform == "woocommerce" and "name" in fields and name_by_sku:
            for row in rows:
                if (row.get("Tipo") or "").strip() == "variation":
                    parent = (row.get("Genitore") or "").strip()
                    if parent in name_by_sku:
                        row["Nome"] = name_by_sku[parent]

        with translator_jobs_lock:
            j = translator_jobs.get(job_id)
            if j:
                j["rows"] = rows
                j["status"] = "done"
                j["progress"] = 100
                j["skipped"] = skipped
                j["message"] = f"Done — {processed - skipped} products translated."

    except Exception as e:  # noqa: BLE001 — never crash the thread
        print(f"[translator {job_id}] error: {e}")
        with translator_jobs_lock:
            j = translator_jobs.get(job_id)
            if j:
                j["status"] = "error"
                j["error"] = "Translation failed. Please try again."


@app.route("/translator")
def translator_page():
    return render_template("translator.html", ai_available=bool(GEMINI_API_KEY))


@app.route("/api/translator/start", methods=["POST"])
def api_translator_start():
    try:
        if not GEMINI_API_KEY or genai is None:
            return jsonify({
                "error": "api_key_missing",
                "message": "Translator requires Gemini API key. Add GEMINI_API_KEY to .env",
            }), 503

        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "no_file", "message": "No CSV file uploaded."}), 400

        platform = (request.form.get("platform") or "woocommerce").strip().lower()
        source_lang = (request.form.get("source_lang") or "auto").strip().lower()
        target_lang = (request.form.get("target_lang") or "en").strip().lower()
        if source_lang not in TRANSLATOR_SOURCE_LANGS:
            source_lang = "auto"
        if target_lang not in TRANSLATOR_VALID_LANGS:
            target_lang = "en"
        if source_lang == target_lang:
            return jsonify({"error": "same_language",
                            "message": "Source and target language must be different."}), 400

        fields_raw = request.form.get("fields") or "name,short_description,description"
        fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        if not fields:
            return jsonify({"error": "no_fields",
                            "message": "Select at least one field to translate."}), 400

        raw = file.read()
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"error": "too_large", "message": "File too large (max 5MB)."}), 413
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        rows = list(reader)
        targets = _enhancer_targets(rows, platform)  # same selection rules

        truncated = len(targets) > TRANSLATOR_MAX_PRODUCTS
        if truncated:
            targets = targets[:TRANSLATOR_MAX_PRODUCTS]
        if not targets:
            return jsonify({"error": "no_products",
                            "message": "No translatable products found in this CSV."}), 400

        total = len(targets)
        message = "Starting translation..."
        if truncated:
            message = f"Starting translation... (limited to the first {TRANSLATOR_MAX_PRODUCTS} products)"

        job_id = uuid.uuid4().hex[:6]
        with translator_jobs_lock:
            translator_jobs[job_id] = {
                "status": "running",
                "progress": 0,
                "message": message,
                "total": total,
                "processed": 0,
                "skipped": 0,
                "rows": [],
                "platform": platform,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "truncated": truncated,
                "error": None,
                "created_at": _now(),
                "fieldnames": fieldnames,
                "_all_rows": rows,
                "_targets": targets,
                "_fields": fields,
            }

        threading.Thread(target=_run_translator, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running", "total": total}), 200

    except Exception as e:  # noqa: BLE001
        print(f"[translator] start error: {type(e).__name__}: {e}")
        return jsonify({"error": "server_error",
                        "message": "Could not start translation."}), 500


@app.route("/api/translator/status/<job_id>")
def api_translator_status(job_id):
    with translator_jobs_lock:
        job = translator_jobs.get(job_id)
        if not job:
            return jsonify({"status": "error", "error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "processed": job["processed"],
            "skipped": job["skipped"],
            "total": job["total"],
            "platform": job["platform"],
            "source_lang": job["source_lang"],
            "target_lang": job["target_lang"],
            "error": job["error"],
        }), 200


@app.route("/api/translator/download/<job_id>")
def api_translator_download(job_id):
    with translator_jobs_lock:
        job = translator_jobs.get(job_id)
        if not job:
            return jsonify({"error": "job_not_found",
                            "message": "Job expired or not found"}), 404
        if job.get("status") != "done":
            return jsonify({"error": "not_ready", "message": "Job not ready"}), 400
        rows = job.get("rows") or []
        if not rows:
            return jsonify({"error": "no_data", "message": "No rows to export"}), 400
        fieldnames = job["fieldnames"]
        platform = job["platform"]
        target_lang = job["target_lang"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})

    csv_bytes = io.BytesIO()
    csv_bytes.write(b"\xef\xbb\xbf")  # UTF-8 BOM
    csv_bytes.write(buf.getvalue().encode("utf-8"))
    csv_bytes.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"translated_{target_lang}_{platform}_{timestamp}.csv"

    with translator_jobs_lock:
        translator_jobs.pop(job_id, None)

    return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name=filename)


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
