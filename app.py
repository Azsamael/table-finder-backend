"""
Table Finder — backend
Two phases per request:
  1. Web search  → identify booking platform + URL
  2. Computer use + Playwright → navigate the page, read real availability
"""

import os, base64, json, asyncio, logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*", methods=["GET","POST","OPTIONS"], allow_headers=["Content-Type"])

# Belt-and-suspenders: explicit CORS headers on every response
@app.after_request
def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

SEARCH_MODEL      = "claude-sonnet-4-6"
COMPUTER_USE_MODEL = os.environ.get("COMPUTER_USE_MODEL", "claude-opus-4-6")
ONLINE_METHODS    = {"opentable", "resy", "thefork", "website"}

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

# ─── Phase 2: computer use ────────────────────────────────────────────────────

async def screenshot(page):
    return base64.b64encode(await page.screenshot()).decode()

async def do_action(page, action, params):
    try:
        if   action == "left_click":     await page.mouse.click(*params["coordinate"]); await asyncio.sleep(0.8)
        elif action == "double_click":   await page.mouse.dblclick(*params["coordinate"]); await asyncio.sleep(0.5)
        elif action == "right_click":    await page.mouse.click(*params["coordinate"], button="right"); await asyncio.sleep(0.5)
        elif action == "type":           await page.keyboard.type(params.get("text",""), delay=40); await asyncio.sleep(0.3)
        elif action == "key":            await page.keyboard.press(params.get("text","")); await asyncio.sleep(0.5)
        elif action == "scroll":
            x, y = params.get("coordinate",[640,450])
            d = 120 * params.get("amount",3) * (1 if params.get("direction","down")=="down" else -1)
            await page.mouse.wheel(0, d); await asyncio.sleep(0.3)
        elif action == "left_click_drag":
            sx,sy = params["start_coordinate"]; ex,ey = params["end_coordinate"]
            await page.mouse.move(sx,sy); await page.mouse.down()
            await page.mouse.move(ex,ey); await page.mouse.up(); await asyncio.sleep(0.3)
        elif action == "wait":
            await asyncio.sleep(min(params.get("duration",1000),5000)/1000)
    except Exception as e:
        log.warning(f"Action {action} error: {e}")
    img = await screenshot(page)
    return [{"type":"image","source":{"type":"base64","media_type":"image/png","data":img}}]

async def check_availability_async(booking_url, restaurant_name, date, te, tl, party):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-setuid-sandbox",
            "--disable-dev-shm-usage","--disable-gpu","--window-size=1280,900"
        ])
        ctx  = await browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await ctx.new_page()
        try:    await page.goto(booking_url, wait_until="networkidle", timeout=20000)
        except: await page.goto(booking_url, timeout=20000)
        await asyncio.sleep(2)

        img = await screenshot(page)
        messages = [{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/png","data":img}},
            {"type":"text","text":(
                f"Check availability at {restaurant_name}: {party} people, {date}, {te}–{tl}.\n"
                f"1. Set date={date}, party={party}, time in {te}–{tl} window.\n"
                f"2. Submit/search.\n3. Read all available slots.\n\n"
                f"Output ONLY this JSON when done:\n"
                f'{{"available_slots":["7:00 PM"],"no_availability":false,'
                f'"booking_url":"current page URL","notes":"any detail"}}\n'
                f"Set available_slots=[] and no_availability=true if nothing available."
            )}
        ]}]

        cu = anthropic.Anthropic(api_key=API_KEY)
        final = None

        for step in range(30):
            log.info(f"CU step {step+1}")
            resp = cu.beta.messages.create(
                model=COMPUTER_USE_MODEL, max_tokens=4096,
                tools=[{"type":"computer_20251124","name":"computer","display_width_px":1280,"display_height_px":900}],
                messages=messages, betas=["computer-use-2025-11-24"]
            )
            messages.append({"role":"assistant","content":resp.content})

            if resp.stop_reason == "end_turn":
                for blk in resp.content:
                    if hasattr(blk,"text") and blk.text:
                        p = parse_json(blk.text)
                        if p and "available_slots" in p:
                            final = p
                            if not final.get("booking_url"): final["booking_url"] = page.url
                break

            results = []
            for blk in resp.content:
                if blk.type == "tool_use" and blk.name == "computer":
                    content = await do_action(page, blk.input.get("action","screenshot"), blk.input)
                    results.append({"type":"tool_result","tool_use_id":blk.id,"content":content})
            if results:
                messages.append({"role":"user","content":results})

        await browser.close()
        return final or {"available_slots":[],"no_availability":True,"notes":"Search timed out.","booking_url":booking_url}

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
            info["notes"] = "Availability check failed — use the booking link directly."
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
