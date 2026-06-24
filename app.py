#!/usr/bin/env python3
"""
app.py  —  StoreRip Flask backend.

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
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
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

load_dotenv()

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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-key")

# --- In-memory job store (no DB, no files) ---
# job_id -> { status, progress, message, log, rows, product_count,
#             platform, output, enhanced, error, domain, created }
jobs = {}
jobs_lock = threading.Lock()


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def _now():
    return time.time()


def _active_job_count():
    return sum(1 for j in jobs.values() if j["status"] == "running")


def _cleanup_jobs():
    """Drop jobs older than the TTL. Called opportunistically."""
    cutoff = _now() - JOB_TTL_SECONDS
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if j["created"] < cutoff]
        for jid in stale:
            jobs.pop(jid, None)


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
    """Background worker: detect -> fetch -> convert -> (enhance) -> store rows."""
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
#  Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        ai_available=ai_enhance.is_available(),
    )


@app.route("/api/detect", methods=["POST"])
def api_detect():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if not url:
        return jsonify({"platform": None, "error": "URL required"}), 400
    try:
        platform = detect_platform(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"platform": None, "error": str(e)}), 200
    return jsonify({"platform": platform}), 200


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    _cleanup_jobs()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    target = (data.get("target") or "auto").strip().lower()
    output = (data.get("output") or "woocommerce").strip().lower()
    enhance = bool(data.get("enhance", False))

    if not url:
        return jsonify({"error": "URL required"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url

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


if __name__ == "__main__":
    # PORT env var takes precedence; otherwise fall back to config.json / default.
    port = int(os.environ.get("PORT", CONFIG["port"]))
    print(f"StoreRip starting on http://0.0.0.0:{port}")
    print(f"  max_jobs={MAX_CONCURRENT_JOBS}  job_ttl={JOB_TTL_SECONDS // 60}min  "
          f"enhance_rate_limit={ENHANCE_RATE_LIMIT_SEC}s")
    print(f"AI enhance available: {ai_enhance.is_available()}")
    app.run(host="0.0.0.0", port=port, threaded=True)
