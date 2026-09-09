import json
import os
import random
import time
from pathlib import Path
from datetime import datetime, timedelta
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
import ollama

# Playwright browser automation components
from playwright.sync_api import sync_playwright

# Linux Mount Point Configuration
DOWNLOAD_ROOT = Path("/media/user/external_drive/tiktok-reposts-data")
ANALYTICS_FILE = DOWNLOAD_ROOT / "analytics.json"

# Roster config matching your layout
TIKTOK_ROSTER = {
    "username": {
        "password": "",
        "session_dir": "session_tt_3",
        "history_file": "history_tt_3.json",
        "target_hashtags": [""],
        "competitor_accounts": [""]
    },
    "username": {
        "password": "",
        "session_dir": "session_tt_4",
        "history_file": "history_tt_4.json",
        "target_hashtags": [""],
        "competitor_accounts": [""]
    }
}

MAX_ACTIONS_PER_PROFILE_RUN = 15
ACCOUNT_COOLDOWNS = {}
NEXT_TASK_OVERRIDE = {}

# --- MONITORING LAYER DATABASES ---
def load_analytics():
    if not DOWNLOAD_ROOT.exists():
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    if ANALYTICS_FILE.exists():
        try:
            with open(ANALYTICS_FILE, "r") as f: return json.load(f)
        except Exception: pass
    
    initial_data = {}
    for user in TIKTOK_ROSTER.keys():
        initial_data[user] = {
            "status": "Idle",
            "total_posts": 0,
            "total_follows": 0,
            "total_likes": 0,
            "cooldown_until": "None",
            "history_log": [],
            "growth_timeline": [
                {"date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), "followers": 500 + random.randint(-20, 100)}
                for i in reversed(range(7))
            ]
        }
    return initial_data

def save_analytics(data):
    try:
        with open(ANALYTICS_FILE, "w") as f: json.dump(data, f, indent=4)
    except Exception as e: print(f"❌ Metrics sync fault: {e}")

def update_account_metric(username, key, value=None, increment=1, status=None):
    db = load_analytics()
    if username in db:
        if status: db[username]["status"] = status
        if key in ["total_posts", "total_follows", "total_likes"]:
            db[username][key] += increment
        elif key == "cooldown_until":
            db[username][key] = str(value)
        elif key == "add_history":
            db[username]["history_log"].insert(0, f"[{datetime.now().strftime('%M:%S')}] {value}")
            db[username]["history_log"] = db[username]["history_log"][:15]
        elif key == "sync_followers":
            today = datetime.now().strftime("%Y-%m-%d")
            timeline = db[username]["growth_timeline"]
            if timeline and timeline[-1]["date"] == today:
                timeline[-1]["followers"] = value
            else:
                timeline.append({"date": today, "followers": value})
                if len(timeline) > 30: timeline.pop(0)
    save_analytics(db)

def load_history(file_path):
    p = DOWNLOAD_ROOT / file_path
    if p.exists():
        try:
            with open(p, "r") as f: return json.load(f)
        except Exception: pass
    return {"posted_ids": [], "replied_dms": [], "replied_comments": [], "liked_videos": [], "followed_users": []}

def save_history(file_path, data):
    with open(DOWNLOAD_ROOT / file_path, "w") as f: json.dump(data, f, indent=4)

# --- EMBEDDED DASHBOARD MONITOR SERVER ---
class DashboardAPIHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(load_analytics()).encode("utf-8"))
        elif self.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """<!DOCTYPE html>
            <html>
            <head>
                <title>TikTok Automation Hub Control Head</title>
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0c10; color: #c5c6c7; margin: 20px; }
                    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1f2833; padding-bottom: 15px; margin-bottom: 20px; }
                    h1 { color: #f50057; margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }
                    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
                    .card { background: #1f2833; border: 1px solid #45a29e; border-radius: 12px; padding: 20px; position: relative; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
                    .badge { position: absolute; top: 20px; right: 20px; padding: 4px 10px; border-radius: 8px; font-size: 11px; font-weight: bold; text-transform: uppercase; }
                    .status-active { background: rgba(0, 245, 212, 0.15); color: #00f5d4; }
                    .status-idle { background: rgba(141, 153, 174, 0.15); color: #8d99ae; }
                    .status-cooldown { background: rgba(245, 0, 87, 0.15); color: #f50057; }
                    .profile-title { font-size: 18px; color: #ffffff; margin-top: 0; margin-bottom: 15px; font-weight: 700; }
                    .stats-box { display: flex; justify-content: space-between; font-size: 13px; border-bottom: 1px solid #1f2833; padding: 8px 0; }
                    .stats-box span:last-child { color: #66fcf1; font-weight: bold; }
                    .chart-container { margin-top: 20px; height: 160px; position: relative; }
                    .logs { background: #0b0c10; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 11px; height: 110px; overflow-y: auto; margin-top: 15px; border: 1px solid #45a29e; color: #66fcf1; }
                </style>
            </head>
            <body>
                <div class="header">
                    <h1>🎵 TikTok Repost Engine :: Control Head</h1>
                    <div id="clock" style="font-family: monospace; color:#66fcf1;">Loading Node Feed...</div>
                </div>
                <div class="grid" id="dashboard-grid"></div>
                <script>
                    let charts = {};
                    function updateDashboard() {
                        fetch('/api/metrics').then(res => res.json()).then(data => {
                            document.getElementById('clock').innerText = "Last Checked: " + new Date().toLocaleTimeString();
                            const grid = document.getElementById('dashboard-grid');
                            Object.keys(data).forEach(user => {
                                const account = data[user];
                                let card = document.getElementById(`card-${user}`);
                                let badgeStyle = "status-idle";
                                if(account.status.toLowerCase().includes("active") || account.status.toLowerCase().includes("upload")) badgeStyle = "status-active";
                                if(account.status.toLowerCase().includes("cool") || account.status.toLowerCase().includes("rate")) badgeStyle = "status-cooldown";

                                if (!card) {
                                    card = document.createElement('div'); card.className = 'card'; card.id = `card-${user}`;
                                    grid.appendChild(card);
                                }
                                const logItems = account.history_log.map(l => `<div>${l}</div>`).join('');
                                card.innerHTML = `
                                    <div class="badge ${badgeStyle}">${account.status}</div>
                                    <div class="profile-title">@${user}</div>
                                    <div class="stats-box"><span>Videos Posted</span><span>${account.total_posts}</span></div>
                                    <div class="stats-box"><span>Follower Matrix Growth</span><span>${account.total_follows}</span></div>
                                    <div class="stats-box"><span>Likes Dispatched</span><span>${account.total_likes}</span></div>
                                    <div class="stats-box"><span>Cooldown Status</span><span>${account.cooldown_until}</span></div>
                                    <div class="chart-container"><canvas id="chart-${user}"></canvas></div>
                                    <div class="logs">${logItems || '<div>[System] Initializing pipeline updates...</div>'}</div>
                                `;
                                const ctx = document.getElementById(`chart-${user}`).getContext('2d');
                                const labels = account.growth_timeline.map(t => t.date.split('-').slice(1).join('/'));
                                const values = account.growth_timeline.map(t => t.followers);
                                if(charts[user]) charts[user].destroy();
                                charts[user] = new Chart(ctx, {
                                    type: 'line',
                                    data: {
                                        labels: labels,
                                        datasets: [{
                                            data: values, borderColor: '#f50057', backgroundColor: 'rgba(245, 0, 87, 0.05)',
                                            borderWidth: 2, tension: 0.3, fill: true, pointRadius: 2
                                        }]
                                    },
                                    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
                                        scales: { x: { grid: { display: false }, ticks: { color: '#c5c6c7', font: { size: 9 } } }, y: { grid: { color: '#1f2833' }, ticks: { color: '#c5c6c7', font: { size: 9 } } } }
                                    }
                                });
                            });
                        });
                    }
                    updateDashboard(); setInterval(updateDashboard, 5000);
                </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else: self.send_error(404)

def launch_monitoring_head(port=8080):
    server = HTTPServer(("0.0.0.0", port), DashboardAPIHandler)
    print(f"🖥️  TikTok Monitoring Head online at http://localhost:{port}")
    server.serve_forever()

# --- BACKEND AI ROUTINES ---
def analyze_context_topic(text):
    text = str(text).lower()
    if any(k in text for k in [""]): return ""
    if any(k in text for k in [""]): return ""
    if any(k in text for k in [""]): return ""
    return ""

def generate_rage_bait_caption(original_text, source_account):
    topic = analyze_context_topic(original_text)
    if topic == "":
        prompt = f""
    elif topic == "":
        prompt = f""
    else:
         prompt = f""
    try:
        response = ollama.chat(model='llama3.1', messages=[{'role': 'user', 'content': prompt}])
        return response['message']['content'].strip().replace('"', '')
    except Exception: return "Trending data matrix check. #foryou"

def generate_hashtags(original_text):
    topic = analyze_context_topic(original_text)
    if topic == "": return "#foryou #fyp"
    if topic == "": return "#foryou #fyp"
    return "#foryou #fyp"

def generate_interactive_reply(context_type, incoming_text, username):
    prompt = f"Write a natural, completely free-thinking, short TikTok response to @{username}'s {context_type}: '{incoming_text}'. Match their energy natively. Maximum 15 words. Output ONLY raw text."
    try:
        response = ollama.chat(model='llama3.1', messages=[{'role': 'user', 'content': prompt}])
        return response['message']['content'].strip().replace('"', '')
    except Exception: return None

# --- BROWSER AUTOMATION WORKFLOWS (PLAYWRIGHT) ---
def get_browser_context(p, username, conf):
    user_data_dir = DOWNLOAD_ROOT / conf["session_dir"]
    if not user_data_dir.exists():
        user_data_dir.mkdir(parents=True, exist_ok=True)
        
    context = p.chromium.launch_persistent_context(
        user_data_dir,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,720",
            "--no-sandbox"
        ],
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return context

def check_login_status(page, username, conf):
    page.goto("https://www.tiktok.com/foryou", wait_until="networkidle")
    time.sleep(3)
    
    if page.locator("a[data-e2e='nav-login-button']").is_visible() or page.locator("button:has-text('Log in')").is_visible():
        update_account_metric(username, "add_history", value="🔓 Authorization required. Please set headless=False once to login manually.")
        return False
    
    try:
        page.goto(f"https://www.tiktok.com/@{username}", wait_until="domcontentloaded")
        time.sleep(2)
        follower_element = page.locator("strong[data-e2e='followers-count']").first
        if follower_element.is_visible():
            count_text = follower_element.inner_text()
            if 'K' in count_text: count = int(float(count_text.replace('K', '')) * 1000)
            elif 'M' in count_text: count = int(float(count_text.replace('M', '')) * 1000000)
            else: count = int(count_text)
            update_account_metric(username, "sync_followers", value=count)
    except Exception: pass
    return True

def handle_direct_messages(page, history, username):
    try:
        page.goto("https://www.tiktok.com/messages", wait_until="networkidle")
        time.sleep(3)
        
        unread_threads = page.locator("[class*='MessageItem']").all()
        processed = 0
        for thread in unread_threads[:2]:
            if processed >= 2: break
            thread.click()
            time.sleep(2)
            
            messages = page.locator("[class*='ChatBubble']").all()
            if not messages: continue
            last_msg_text = messages[-1].inner_text()
            
            msg_hash = str(hash(last_msg_text))
            if msg_hash in history["replied_dms"]: continue
            
            reply = generate_interactive_reply("dm", last_msg_text, "user")
            if reply:
                input_box = page.locator("div[contenteditable='true']").first
                input_box.fill(reply)
                page.keyboard.press("Enter")
                history["replied_dms"].append(msg_hash)
                update_account_metric(username, "add_history", value=f"DM processed: '{reply[:15]}'")
                processed += 1
                time.sleep(random.uniform(3, 6))
    except Exception as e:
        print(f"⚠️ DM Pipeline skipped: {e}")

def handle_post_comments(page, history, username):
    try:
        page.goto(f"https://www.tiktok.com/@{username}", wait_until="networkidle")
        time.sleep(3)
        
        first_video = page.locator("[data-e2e='user-post-item']").first
        if not first_video.is_visible(): return
        first_video.click()
        time.sleep(3)
        
        comment_containers = page.locator("[data-e2e='comment-item']").all()
        processed = 0
        for container in comment_containers[:3]:
            if processed >= 2: break
            comment_text = container.locator("[data-e2e='comment-text']").inner_text()
            comment_user = container.locator("[data-e2e='comment-username']").inner_text()
            
            comm_hash = str(hash(comment_text + comment_user))
            if comm_hash in history["replied_comments"]: continue
            
            reply = generate_interactive_reply("comment", comment_text, comment_user)
            if reply:
                container.locator("span:has-text('Reply')").first.click()
                time.sleep(1)
                page.locator("div[contenteditable='true']").first.fill(reply)
                page.locator("[data-e2e='comment-post-button']").click()
                
                history["replied_comments"].append(comm_hash)
                update_account_metric(username, "add_history", value=f"Comment reply deployed to @{comment_user}")
                processed += 1
                time.sleep(random.uniform(4, 7))
    except Exception as e:
        print(f"⚠️ Comment matrix pass skipped: {e}")

def execute_repost_flow(page, history, username, conf, folder_pool):
    update_account_metric(username, "add_history", value="Scanning storage drives for video assets.")
    available_pool = [f for f in folder_pool if f["id"] not in history["posted_ids"]]
    if not available_pool: return False

    selected_folder = random.choice(available_pool)
    folder_path = selected_folder["path"]
    
    videos = sorted([str(f) for f in folder_path.iterdir() if f.suffix.lower() == '.mp4'])
    if not videos: return False
    target_video = videos[0]

    text_context = "Media Update"
    txt_files = [f for f in folder_path.iterdir() if f.suffix.lower() == '.txt']
    if txt_files:
        try:
            with open(txt_files, 'r', encoding='utf-8') as tf: text_context = tf.read().strip()
        except Exception: pass

    base_caption = generate_rage_bait_caption(text_context, selected_folder["source"])
    tags = generate_hashtags(text_context)
    full_caption = f"{base_caption} {tags}"

    update_account_metric(username, "add_history", value=f"Uploading file asset: {selected_folder['id'][:15]}")
    
    try:
        page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="networkidle")
        time.sleep(5)
        
        file_input = page.locator("input[type='file']")
        file_input.set_input_files(target_video)
        time.sleep(5)
        
        caption_editor = page.locator("div[class*='public-DraftEditor-content']")
        caption_editor.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        caption_editor.fill(full_caption)
        time.sleep(3)
        
        post_btn = page.locator("button:has-text('Post')").first
        page.wait_for_function("btn => !btn.disabled", post_btn)
        post_btn.click()
        
        time.sleep(15)
        
        history["posted_ids"].append(selected_folder["id"])
        update_account_metric(username, "total_posts", increment=1)
        update_account_metric(username, "add_history", value="Video successfully dispatched to feed.")
        return True
    except Exception as e:
        update_account_metric(username, "add_history", value=f"Upload workflow block: {str(e)[:25]}")
        return False

def interact_with_feed(page, history, username, conf):
    try:
        hashtag = random.choice(conf["target_hashtags"])
        update_account_metric(username, "add_history", value=f"Surfing channel feed indices for #{hashtag}")
        page.goto(f"https://www.tiktok.com/tag/{hashtag}", wait_until="networkidle")
        time.sleep(4)
        
        first_video = page.locator("[data-e2e='challenge-item']").first
        if not first_video.is_visible(): return
        first_video.click()
        time.sleep(3)
        
        actions = 0
        for _ in range(3):
            if actions >= MAX_ACTIONS_PER_PROFILE_RUN: break
            
            like_icon = page.locator("[data-e2e='browse-like']").first
            if like_icon.is_visible() and random.random() > 0.3:
                like_icon.click()
                update_account_metric(username, "total_likes", increment=1)
                actions += 1
                time.sleep(random.uniform(2, 4))
                
            page.keyboard.press("ArrowDown")
            time.sleep(random.uniform(6, 12))
    except Exception: pass

def discover_local_media_folders():
    all_folders = []
    if not DOWNLOAD_ROOT.exists(): return all_folders
    for item in DOWNLOAD_ROOT.iterdir():
        if item.is_dir():
            parts = item.name.split('_')
            source = parts if len(parts) > 1 else "unknown"
            all_folders.append({"id": item.name, "path": item, "source": source})
    return all_folders

# --- SYSTEM PIPELINE CONTROL WORKFLOW ---
def run_profile_workflow(username, conf, folder_pool):
    global NEXT_TASK_OVERRIDE
    update_account_metric(username, "status", status="Active Profile Step")
    
    history = load_history(conf["history_file"])
    
    with sync_playwright() as p:
        context = get_browser_context(p, username, conf)
        page = context.new_page()
        
        if not check_login_status(page, username, conf):
            context.close()
            update_account_metric(username, "status", status="Auth Needed")
            return
            
        handle_direct_messages(page, history, username)
        handle_post_comments(page, history, username)
        
        forced_task = NEXT_TASK_OVERRIDE.pop(username, None)
        if forced_task == "feed_surf" or random.random() > 0.50:
            execute_repost_flow(page, history, username, conf, folder_pool)
        else:
            interact_with_feed(page, history, username, conf)
            
        context.close()
        
    save_history(conf["history_file"], history)
    update_account_metric(username, "status", status="Idle")

# --- CORE SCHEDULER MATRIX ---
def main():
    threading.Thread(target=launch_monitoring_head, args=(8080,), daemon=True).start()
    
    account_queue = list(TIKTOK_ROSTER.keys())
    
    while True:
        folder_pool = discover_local_media_folders()
        if not folder_pool:
            print(f"⚠️ Media staging drive empty or unmounted at '{DOWNLOAD_ROOT}'. Retrying...")
            time.sleep(30)
            continue
            
        if not account_queue: account_queue = list(TIKTOK_ROSTER.keys())
        current_user = account_queue.pop(0)
        
        if current_user in ACCOUNT_COOLDOWNS:
            if datetime.now() < ACCOUNT_COOLDOWNS[current_user]:
                account_queue.append(current_user)
                time.sleep(2)
                continue
            else:
                del ACCOUNT_COOLDOWNS[current_user]
                update_account_metric(current_user, "cooldown_until", value="None")
                
        config_details = TIKTOK_ROSTER[current_user]
        try:
            run_profile_workflow(current_user, config_details, folder_pool)
        except Exception as e:
            update_account_metric(current_user, "add_history", value=f"Error encountered: {str(e)[:25]}")
            cooldown_target = datetime.now() + timedelta(minutes=45)
            ACCOUNT_COOLDOWNS[current_user] = cooldown_target
            NEXT_TASK_OVERRIDE[current_user] = "feed_surf"
            update_account_metric(current_user, "cooldown_until", value=cooldown_target.strftime("%H:%M"))
            update_account_metric(current_user, "status", status="Error Cooldown")
            
        account_queue.append(current_user)
        inter_account_delay = random.randint(400, 800)
        time.sleep(inter_account_delay)

if __name__ == "__main__":
    main()
