#!/usr/bin/env python3
"""
ai_enhance.py  —  Optional Claude-powered copy enhancement for StoreRip.

Takes scraped product rows (WooCommerce-format dicts produced by scraper.py)
and rewrites the "Nome" / "Descrizione" fields for SEO + conversion using the
Claude API. Fully graceful: any failure keeps the original values.

Activated only when:
  - the user requests enhancement (enhance=true), AND
  - ANTHROPIC_API_KEY is present in the environment (.env)

No keys are hardcoded — the Anthropic SDK reads ANTHROPIC_API_KEY from env.
"""

import json
import os
import re
import time

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are an e-commerce copywriter. Improve the product name and description "
    "for SEO and conversion. Keep the same language as the input. Return ONLY "
    'valid JSON: {"Nome": "...", "Descrizione": "..."}'
)

# Internal rate limit: at most 1 request per second.
_MIN_INTERVAL = 1.0

# Above this product count, enhancement is slow enough to warrant a heads-up
# in the job message (one Claude call per product at ~1 req/sec).
LARGE_CATALOG_THRESHOLD = 50
LARGE_CATALOG_WARNING = "AI enhance on large catalogs may take several minutes"


def is_available():
    """True if enhancement can run (API key present + SDK importable)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _get_client():
    import anthropic
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


def _extract_json(text):
    """Pull the first JSON object out of a model response."""
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def _enhance_one(client, name, description):
    """Return (new_name, new_description) or raise on failure."""
    user_content = json.dumps(
        {"Nome": name, "Descrizione": description}, ensure_ascii=False
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    data = _extract_json(text)
    new_name = (data.get("Nome") or "").strip()
    new_desc = (data.get("Descrizione") or "").strip()
    return new_name, new_desc


def enhance_rows(rows, progress_cb=None, rate_limit_sec=None):
    """
    Enhance Nome/Descrizione on each parent product row in place.

    rows            : list of WooCommerce-format dicts from scraper.py
    progress_cb     : optional callable(done, total) for progress reporting
    rate_limit_sec  : min seconds between API requests (defaults to _MIN_INTERVAL)

    Variation rows (Tipo == "variation") are skipped — they inherit copy from
    their parent. On any per-row failure the original values are preserved.
    Returns the number of rows successfully enhanced.
    """
    interval = _MIN_INTERVAL if rate_limit_sec is None else max(0.0, float(rate_limit_sec))
    if not is_available():
        return 0

    try:
        client = _get_client()
    except Exception as e:  # noqa: BLE001
        print(f"  [ai_enhance] client init failed: {e}")
        return 0

    # Only enhance real products, not variations (they have empty copy anyway).
    targets = [r for r in rows if r.get("Tipo") != "variation"]
    total = len(targets)
    enhanced = 0
    last_call = 0.0

    for i, row in enumerate(targets, 1):
        name = row.get("Nome", "")
        desc = row.get("Descrizione", "")
        if not name and not desc:
            if progress_cb:
                progress_cb(i, total)
            continue

        # Rate limit: configurable min interval between requests.
        wait = interval - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.monotonic()

        try:
            new_name, new_desc = _enhance_one(client, name, desc)
            if new_name:
                row["Nome"] = new_name
            if new_desc:
                row["Descrizione"] = new_desc
            enhanced += 1
        except Exception as e:  # noqa: BLE001 — graceful degradation
            print(f"  [ai_enhance] row {i} kept original ({e})")

        if progress_cb:
            progress_cb(i, total)

    return enhanced
