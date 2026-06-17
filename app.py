"""
Table Finder — backend
Two phases per request:
  1. Web search  → identify booking platform + URL
  2. Computer use + Playwright → navigate, interact, then extract page text for reliable parsing
"""

import os, base64, json, asyncio, logging, re
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*", methods=["GET","POST","OPTIONS"], allow_headers=["Content-Type"])

@app.after_request
def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r

API_KEY            = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

SEARCH_MODEL       = "claude-sonnet-4-6"
COMPUTER_USE_MODEL = os.environ.get("COMPUTER_USE_MODEL", "claude-opus-4-6")
ONLINE_METHODS     = {"opentable", "resy", "thefork", "website"}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_json(text):
    if not text: return None
    text = text.replace("```json","").replace("```","").strip()
    depth = 0; start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if not depth: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try: return json.loads(text[start:i+1])
                except: start = -1
    return None

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close(); asyncio.set_event_loop(None)

def extract_text(response):
    return " ".join(b.text for b in response.content if hasattr(b, "text"))

# ─── Phase 1: web search ──────────────────────────────────────────────────────

def search_restaurant(restaurant, city):
    client = anthropic.Anthropic(api_key=API_KEY)
    log.info(f"Searching: {restaurant} in {city}")
    r = client.messages.create(
        model=SEARCH_MODEL, max_tokens=800,
        tools=[{"type":"web_search_20250305","name":"web_search"}],
        system="Find restaurant reservation info. Return ONLY valid JSON — no markdown, no preamble.",
        messages=[{"role":"user","content":
            f'Find booking info for "{restaurant}" in {city}. '
            f'Look for OpenTable, Resy, TheFork, website, email, or phone. '
            f'Return ONLY: {{"restaurant_name":"...","address":"...","cuisine":"...",'
            f'"booking_method":"opentable"|"resy"|"thefork"|"website"|"email"|"phone"|"walk-in",'
            f'"booking_url":"...or null","email":"...or null","phone":"...or null",'
            f'"has_booking_fee":true|false,"booking_fee_details":"...or null"}}'
        }]
    )
    result = parse_json(extract_text(r))
    if not result: raise ValueError("Could not parse restaurant info")
    return result

# ─── Phase 2: computer use + text extraction ──────────────────────────────────

async def screenshot_b64(page):
    return base64.b64encode(await page.screenshot()).decode()

async def do_action(page, action, params):
    try:
        if   action == "left_click":
            x, y = params["coordinate"]; await page.mouse.click(x, y); await asyncio.sleep(1.0)
        elif action == "double_click":
            x, y = params["coordinate"]; await page.mouse.dblclick(x, y); await asyncio.sleep(0.6)
        elif action == "right_click":
            x, y = params["coordinate"]; await page.mouse.click(x, y, button="right"); await asyncio.sleep(0.5)
        elif action == "type":
            await page.keyboard.type(params.get("text",""), delay=50); await asyncio.sleep(0.4)
        elif action == "key":
            await page.keyboard.press(params.get("text","")); await asyncio.sleep(0.6)
        elif action == "scroll":
            x, y = params.get("coordinate",[640,450])
            d = 120 * params.get("amount",3) * (1 if params.get("direction","down")=="down" else -1)
            await page.mouse.wheel(0, d); await asyncio.sleep(0.4)
        elif action == "left_click_drag":
            sx,sy = params["start_coordinate"]; ex,ey = params["end_coordinate"]
            await page.mouse.move(sx,sy); await page.mouse.down()
            await page.mouse.move(ex,ey); await page.mouse.up(); await asyncio.sleep(0.4)
        elif action == "wait":
            await asyncio.sleep(min(params.get("duration",1000),5000)/1000)
    except Exception as e:
        log.warning(f"Action {action} error: {e}")

    img = await screenshot_b64(page)
    return [{"type":"image","source":{"type":"base64","media_type":"image/png","data":img}}]


async def parse_availability_from_text(page_text, page_url, restaurant_name, date, te, tl, party):
    """
    Second-pass: send the raw page text to Claude (no computer use, no screenshots)
    and ask it to extract the available time slots. Much more reliable than reading
    slot times from a screenshot.
    """
    client = anthropic.Anthropic(api_key=API_KEY)
    r = client.messages.create(
        model=SEARCH_MODEL, max_tokens=600,
        system="Extract restaurant availability from page text. Return ONLY valid JSON.",
        messages=[{"role":"user","content":(
            f"This is the raw text of a restaurant booking page for {restaurant_name}.\n"
            f"The user searched for: {party} people on {date} between {te} and {tl}.\n\n"
            f"PAGE TEXT:\n{page_text[:6000]}\n\n"
            f"Extract every available time slot mentioned. Time slots typically look like "
            f"'7:00 PM', '19:00', '7:30 p.m.' etc.\n\n"
            f"Return ONLY this JSON:\n"
            f'{{"available_slots":["7:00 PM","7:30 PM"],'
            f'"no_availability":false,'
            f'"booking_url":"{page_url}",'
            f'"notes":"any important info like waitlist or limited seating"}}\n\n'
            f"If no slots are visible, set available_slots=[] and no_availability=true.\n"
            f"If the page shows a CAPTCHA, bot check, or login wall, set "
            f'notes="Bot detection encountered — please check manually" and no_availability=true.'
        )}]
    )
    result = parse_json(extract_text(r))
    return result or {"available_slots":[],"no_availability":True,"booking_url":page_url,
                      "notes":"Could not parse availability from page text."}


async def check_availability_async(booking_url, restaurant_name, date, te, tl, party):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-setuid-sandbox",
            "--disable-dev-shm-usage","--disable-gpu","--window-size=1280,900"
        ])
        ctx = await browser.new_context(
            viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = await ctx.new_page()

        # Navigate to the booking page
        try:    await page.goto(booking_url, wait_until="networkidle", timeout=25000)
        except: await page.goto(booking_url, timeout=25000)
        await asyncio.sleep(3)  # Let JS fully render

        img = await screenshot_b64(page)
        messages = [{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/png","data":img}},
            {"type":"text","text":(
                f"You are checking availability at {restaurant_name}.\n"
                f"Goal: {party} people on {date} between {te} and {tl}.\n\n"
                f"IMPORTANT STEPS:\n"
                f"1. First, dismiss any cookie banners, popups, or overlays by clicking Accept/Close.\n"
                f"2. Locate the date, party size, and time fields.\n"
                f"3. Set party size to {party}.\n"
                f"4. Set the date to {date} — use the exact format the date picker expects.\n"
                f"5. Set time to something in the {te}–{tl} range, or the closest available.\n"
                f"6. Click the Search / Find a Table / Check availability button.\n"
                f"7. Wait for results to appear — they may take a moment to load.\n"
                f"8. Once you can see the results page (with slots OR a 'no availability' message), "
                f"call the screenshot action ONE FINAL TIME and then stop — do NOT try to read "
                f"the slots yourself. Just say 'DONE' when the results are visible.\n\n"
                f"Say 'DONE' when you reach the results page."
            )}
        ]}]

        cu = anthropic.Anthropic(api_key=API_KEY)

        for step in range(25):
            log.info(f"CU step {step+1}")
            resp = cu.beta.messages.create(
                model=COMPUTER_USE_MODEL, max_tokens=4096,
                tools=[{"type":"computer_20251124","name":"computer",
                        "display_width_px":1280,"display_height_px":900}],
                messages=messages, betas=["computer-use-2025-11-24"]
            )
            messages.append({"role":"assistant","content":resp.content})

            # Check if Claude says it's done navigating
            if resp.stop_reason == "end_turn":
                for blk in resp.content:
                    if hasattr(blk,"text") and "DONE" in blk.text.upper():
                        log.info("CU reached results page — switching to text extraction")
                        break
                break  # Either way, move to text extraction

            results = []
            for blk in resp.content:
                if blk.type == "tool_use" and blk.name == "computer":
                    content = await do_action(page, blk.input.get("action","screenshot"), blk.input)
                    results.append({"type":"tool_result","tool_use_id":blk.id,"content":content})
            if results:
                messages.append({"role":"user","content":results})

        # ── Second pass: extract text from the final page state ───────────────
        await asyncio.sleep(2)  # One final wait for any last JS render
        try:
            page_text = await page.evaluate("() => document.body.innerText")
        except:
            page_text = ""

        current_url = page.url
        await browser.close()

        log.info(f"Extracted {len(page_text)} chars of page text — running text parser")
        return await parse_availability_from_text(page_text, current_url, restaurant_name, date, te, tl, party)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/check", methods=["POST","OPTIONS"])
def check():
    if request.method == "OPTIONS": return "", 204
    d = request.get_json(force=True) or {}
    restaurant = d.get("restaurant","").strip()
    city       = d.get("city","").strip()
    date       = d.get("date","").strip()
    te         = d.get("timeEarliest","18:00")
    tl         = d.get("timeLatest","21:00")
    party      = int(d.get("partySize",2))
    if not all([restaurant, city, date]):
        return jsonify({"error":"restaurant, city, and date are required"}), 400
    try:
        info = search_restaurant(restaurant, city)
    except Exception as e:
        log.error(f"Search failed: {e}")
        return jsonify({"error":"Could not find restaurant. Check the name and city."}), 404
    method = info.get("booking_method","unknown")
    if method in ONLINE_METHODS and info.get("booking_url"):
        try:
            slots = run_async(check_availability_async(
                info["booking_url"], info.get("restaurant_name",restaurant),
                date, te, tl, party))
            return jsonify({**info, **slots})
        except Exception as e:
            log.error(f"Computer use failed: {e}")
            info["notes"] = "Availability check failed — use the booking link to check manually."
            info["available_slots"] = []
            return jsonify(info)
    return jsonify(info)


@app.route("/draft-email", methods=["POST","OPTIONS"])
def draft_email():
    if request.method == "OPTIONS": return "", 204
    d = request.get_json(force=True) or {}
    client = anthropic.Anthropic(api_key=API_KEY)
    r = client.messages.create(
        model=SEARCH_MODEL, max_tokens=500,
        system="Draft concise restaurant reservation emails. Return ONLY valid JSON.",
        messages=[{"role":"user","content":(
            f'Draft a reservation email.\n'
            f'Restaurant: {d.get("restaurant_name","")}\n'
            f'Date: {d.get("date","")}\n'
            f'Time: {d.get("timeEarliest","")} – {d.get("timeLatest","")}\n'
            f'Party: {d.get("partySize",2)} people\n'
            f'Guest: {d.get("yourName","")}\nReply-to: {d.get("yourEmail","")}\n'
            f'{("Requests: " + d.get("notes","")) if d.get("notes") else ""}\n\n'
            f'Return ONLY: {{"subject":"...","body":"..."}}'
        )}]
    )
    result = parse_json(extract_text(r))
    if not result: return jsonify({"error":"Failed to draft email"}), 500
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","computer_use_model":COMPUTER_USE_MODEL})


if __name__ == "__main__":
    port = int(os.environ.get("PORT",8080))
    app.run(host="0.0.0.0", port=port, debug=False)
