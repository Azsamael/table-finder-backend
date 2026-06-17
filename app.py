"""
Table Finder — backend
Runs two phases per request:
  1. Web search  → identify booking platform + URL
  2. Computer use + Playwright → navigate the booking page, read real availability
"""

import os
import base64
import json
import asyncio
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
from playwright.async_api import async_playwright

# ─── Setup ────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app,
     origins="*",
     methods=["GET", "POST", "OPTIONS"],
     allow_headers=["Content-Type"])

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

# Model for web search phase (fast, cheap)
SEARCH_MODEL = "claude-sonnet-4-6"

# Model for computer use phase — opus navigates booking UIs more reliably.
# Change to "claude-sonnet-4-6" if you want to reduce cost at the expense of reliability.
COMPUTER_USE_MODEL = os.environ.get("COMPUTER_USE_MODEL", "claude-opus-4-6")

ONLINE_METHODS = {"opentable", "resy", "thefork", "website"}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_parse_json(text: str) -> dict | None:
    """Extract the first valid JSON object from a string."""
    if not text:
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if not depth:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = -1  # malformed, keep looking
    return None


def run_async(coro):
    """Run an async coroutine safely from a sync Flask route."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ─── Phase 1: Web search ──────────────────────────────────────────────────────

def search_restaurant(restaurant: str, city: str) -> dict:
    """Identify booking platform, URL, and contact info via web search."""
    client = anthropic.Anthropic(api_key=API_KEY)

    log.info(f"Searching for '{restaurant}' in {city}")

    response = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=800,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system="You find restaurant reservation information. Return ONLY valid JSON — no markdown, no preamble.",
        messages=[{
            "role": "user",
            "content": (
                f'Find reservation/booking info for "{restaurant}" in {city}. '
                f'Look for: OpenTable, Resy, TheFork, direct website booking, email, or phone. '
                f'Return ONLY this JSON:\n'
                f'{{\n'
                f'  "restaurant_name": "official name",\n'
                f'  "address": "full address",\n'
                f'  "cuisine": "cuisine type",\n'
                f'  "booking_method": "opentable"|"resy"|"thefork"|"website"|"email"|"phone"|"walk-in",\n'
                f'  "booking_url": "direct booking page URL or null",\n'
                f'  "email": "reservations email or null",\n'
                f'  "phone": "phone or null",\n'
                f'  "has_booking_fee": true|false,\n'
                f'  "booking_fee_details": "description or null"\n'
                f'}}'
            ),
        }],
    )

    text = " ".join(b.text for b in response.content if hasattr(b, "text"))
    result = safe_parse_json(text)
    if not result:
        raise ValueError(f"Could not parse restaurant info from search response")
    return result


# ─── Phase 2: Computer use availability check ─────────────────────────────────

async def _screenshot(page) -> str:
    data = await page.screenshot()
    return base64.b64encode(data).decode()


async def _execute_action(page, action: str, params: dict) -> list:
    """Execute one computer-use action, return screenshot content block."""
    try:
        if action == "left_click":
            x, y = params["coordinate"]
            await page.mouse.click(x, y)
            await asyncio.sleep(0.8)
        elif action == "double_click":
            x, y = params["coordinate"]
            await page.mouse.dblclick(x, y)
            await asyncio.sleep(0.5)
        elif action == "right_click":
            x, y = params["coordinate"]
            await page.mouse.click(x, y, button="right")
            await asyncio.sleep(0.5)
        elif action == "left_click_drag":
            sx, sy = params["start_coordinate"]
            ex, ey = params["end_coordinate"]
            await page.mouse.move(sx, sy)
            await page.mouse.down()
            await page.mouse.move(ex, ey)
            await page.mouse.up()
            await asyncio.sleep(0.3)
        elif action == "type":
            await page.keyboard.type(params.get("text", ""), delay=40)
            await asyncio.sleep(0.3)
        elif action == "key":
            await page.keyboard.press(params.get("text", ""))
            await asyncio.sleep(0.5)
        elif action == "scroll":
            x, y = params.get("coordinate", [640, 450])
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            delta = amount * 120 * (1 if direction == "down" else -1)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(0.3)
        elif action == "wait":
            ms = min(params.get("duration", 1000), 5000)
            await asyncio.sleep(ms / 1000)
        # "screenshot" and "cursor_position" just fall through to the screenshot below
    except Exception as e:
        log.warning(f"Action '{action}' raised: {e}")

    img = await _screenshot(page)
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}]


async def check_availability_async(
    booking_url: str,
    restaurant_name: str,
    date: str,
    time_earliest: str,
    time_latest: str,
    party_size: int,
) -> dict:
    """
    Open the booking URL in a headless browser and use Claude computer use
    to find and read the available time slots.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,900",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        try:
            await page.goto(booking_url, wait_until="networkidle", timeout=20_000)
        except Exception:
            await page.goto(booking_url, timeout=20_000)
        await asyncio.sleep(2)

        img = await _screenshot(page)

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"You are checking dinner availability at {restaurant_name}.\n\n"
                            f"Goal: find available time slots for {party_size} people "
                            f"on {date} between {time_earliest} and {time_latest}.\n\n"
                            f"Steps:\n"
                            f"1. Locate the date / party-size / time fields on this booking page.\n"
                            f"2. Set the date to {date}.\n"
                            f"3. Set party size to {party_size}.\n"
                            f"4. Choose a time in the {time_earliest}–{time_latest} window "
                            f"(or search the full window if the UI allows a range).\n"
                            f"5. Trigger the search / check availability.\n"
                            f"6. Read every available slot shown.\n\n"
                            f"When you have the results, output ONLY this JSON (no other text):\n"
                            f'{{\n'
                            f'  "available_slots": ["6:30 PM", "7:00 PM"],\n'
                            f'  "no_availability": false,\n'
                            f'  "booking_url": "URL to use for booking (current page URL is fine)",\n'
                            f'  "notes": "any important detail (e.g. walk-in bar seats only)"\n'
                            f"}}\n\n"
                            f"If nothing is available set available_slots to [] and no_availability to true."
                        ),
                    },
                ],
            }
        ]

        cu_client = anthropic.Anthropic(api_key=API_KEY)
        final_result = None

        for step in range(30):
            log.info(f"Computer use step {step + 1}")

            response = cu_client.beta.messages.create(
                model=COMPUTER_USE_MODEL,
                max_tokens=4096,
                tools=[
                    {
                        "type": "computer_20251124",
                        "name": "computer",
                        "display_width_px": 1280,
                        "display_height_px": 900,
                    }
                ],
                messages=messages,
                betas=["computer-use-2025-11-24"],
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        parsed = safe_parse_json(block.text)
                        if parsed and "available_slots" in parsed:
                            final_result = parsed
                            if not final_result.get("booking_url"):
                                final_result["booking_url"] = page.url
                        break
                break

            # Execute tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "computer":
                    action = block.input.get("action", "screenshot")
                    content = await _execute_action(page, action, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        await browser.close()

    if final_result:
        return final_result

    return {
        "available_slots": [],
        "no_availability": True,
        "notes": "Availability check timed out or could not read results.",
        "booking_url": booking_url,
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/check", methods=["POST", "OPTIONS"])
def check():
    data = request.get_json(force=True) or {}
    restaurant  = data.get("restaurant", "").strip()
    city        = data.get("city", "").strip()
    date        = data.get("date", "").strip()
    time_early  = data.get("timeEarliest", "18:00")
    time_late   = data.get("timeLatest", "21:00")
    party_size  = int(data.get("partySize", 2))

    if not all([restaurant, city, date]):
        return jsonify({"error": "restaurant, city, and date are required"}), 400

    try:
        # Phase 1 — web search
        info = search_restaurant(restaurant, city)
    except Exception as e:
        log.error(f"Search failed: {e}")
        return jsonify({"error": "Could not find restaurant. Check the name and city."}), 404

    method = info.get("booking_method", "unknown")
    log.info(f"Booking method: {method}")

    # Phase 2 — computer use (only for online booking platforms)
    if method in ONLINE_METHODS and info.get("booking_url"):
        try:
            slots = run_async(
                check_availability_async(
                    info["booking_url"],
                    info.get("restaurant_name", restaurant),
                    date, time_early, time_late, party_size,
                )
            )
            return jsonify({**info, **slots})
        except Exception as e:
            log.error(f"Computer use failed: {e}")
            # Fall back to returning platform info without slots
            info["notes"] = "Availability check failed — use the booking link directly."
            info["available_slots"] = []
            return jsonify(info)

    # Email / phone / walk-in — return info as-is for the frontend to handle
    return jsonify(info)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "computer_use_model": COMPUTER_USE_MODEL})


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
