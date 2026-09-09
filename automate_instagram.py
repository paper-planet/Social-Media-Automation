import json
import os
import random
import time
from pathlib import Path
from datetime import datetime, timedelta
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from instagrapi import Client
from instagrapi.exceptions import FeedbackRequired, LoginRequired, ClientError, UserNotFound, PrivateError
import pyotp
import ollama

# Linux Mount Point Configuration
DOWNLOAD_ROOT = Path("/media/user/external_drive/ig-reposts-data")
ANALYTICS_FILE = DOWNLOAD_ROOT / "analytics.json"

FAN_ROSTER = {
    "username": {
        "password": "",
        "totp_secret": "",
        "session_file": "session_fan_3.json",
        "history_file": "history_fan_3.json",
        "target_hashtags": [""],
        "competitor_accounts": [""]
    },
    "username": {
        "password": "",
        "totp_secret": "",
        "session_file": "session_fan_4.json",
        "history_file": "history_fan_4.json",
        "target_hashtags": [""],
        "competitor_accounts": [""]
    }
}

MAX_ACTIONS_PER_PROFILE_RUN = 30  
ACCOUNT_COOLDOWNS = {}  
NEXT_TASK_OVERRIDE = {}  

# --- MONITORING LAYER DATABASES ---
def load_analytics():
    if ANALYTICS_FILE.exists():
        try:
            with open(ANALYTICS_FILE, "r") as f:
                return json.load(f)
        except Exception: pass
    
    initial_data = {}
    for user in FAN_ROSTER.keys():
        initial_data[user] = {
            "status": "Idle",
            "total_posts": 0,
            "total_follows": 0,
            "total_likes": 0,
            "cooldown_until": "None",
            "history_log": [],
            "growth_timeline": [
                {"date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), "followers": 1000 + random.randint(-50, 200)}
                for i in reversed(range(7))
            ]
        }
    return initial_data

def save_analytics(data):
    try:
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"❌ Failed to sync metrics database: {e}")

def update_account_metric(username, key, value=None, increment=1, status=None):
    db = load_analytics()
    if username in db:
        if status:
            db[username]["status"] = status
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

def load_json(filepath, default_data):
    p = Path(filepath)
    if p.exists():
        try:
            with open(p, "r") as f:
                data = json.load(f)
                for key in ["posted_ids", "replied_dms", "replied_comments", "liked_medias", "commented_medias", "followed_users", "blocked_or_missing", "competitor_pool", "followers_next_max_id", "following_next_max_id"]:
                    if key not in data:
                        data[key] = [] if not key.endswith("max_id") else None
                return data
        except Exception: pass
    return default_data

def save_json(filepath, data):
    try:
        with open(Path(filepath), "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"❌ Failed to write local JSON registry data: {e}")

def generate_2fa(secret):
    if not secret: return None
    return pyotp.TOTP(secret.replace(" ", "")).now()

def analyze_context_topic(text):
    text = str(text).lower()
    if any(k in text for k in [""]):
        return ""
    elif any(k in text for k in [""]):
        return ""
    elif any(k in text for k in [""]):
        return ""
    return ""

def generate_rage_bait_caption(original_text, source_account):
    topic = analyze_context_topic(original_text)
    if topic == "":
        prompt = f""
    elif topic == "":
        prompt = f""
    elif topic == "":
        prompt = f""
    try:
        response = ollama.chat(model='llama3.1', messages=[{'role': 'user', 'content': prompt}])
        return response['message']['content'].strip().replace('"', '')
    except Exception:
        return None

def generate_interactive_reply(context_type, incoming_text, username):
    topic = analyze_context_topic(incoming_text)
    if topic == "":
        prompt = f""
    elif topic == "":
        prompt = f""
    elif topic == "":
        prompt = f""
    try:
        response = ollama.chat(model='llama3.1', messages=[{'role': 'user', 'content': prompt}])
        return response['message']['content'].strip().replace('"', '')
    except Exception:
        return None

def get_authenticated_client(username, conf):
    cl = Client()
    session_p = Path(conf["session_file"])
    if session_p.exists():
        try:
            with open(session_p, "r") as f:
                cl.set_settings(json.load(f))
            cl.get_timeline_feed()
            info = cl.user_info_v1(cl.user_id)
            update_account_metric(username, "sync_followers", value=info.follower_count)
            return cl
        except Exception: pass
    try:
        update_account_metric(username, "add_history", value="Session dead. Executing authorization login...")
        v_code = generate_2fa(conf["totp_secret"])
        cl.login(username, conf["password"], verification_code=v_code if v_code else "")
        with open(session_p, "w") as f:
            json.dump(cl.get_settings(), f, indent=4)
        info = cl.user_info_v1(cl.user_id)
        update_account_metric(username, "sync_followers", value=info.follower_count)
        return cl
    except Exception as e:
        update_account_metric(username, "add_history", value=f"Login failed: {str(e)[:25]}")
        return None

def discover_local_media_folders():
    all_folders = []
    if not DOWNLOAD_ROOT.exists():
        return all_folders
    for item in DOWNLOAD_ROOT.iterdir():
        if item.is_dir():
            parts = item.name.split('_')
            source = parts[1] if len(parts) > 1 else "unknown_source"
            all_folders.append({"id": item.name, "path": item, "source": source})
    return all_folders

def harvest_and_amplify_networks(cl, history, config, username, actions_performed):
    update_account_metric(username, "add_history", value="🧬 Running Network Amplification Engine...")
    if "competitor_pool" not in history or not history["competitor_pool"]:
        history["competitor_pool"] = list(config["competitor_accounts"])
        
    active_target = random.choice(history["competitor_pool"])
    update_account_metric(username, "add_history", value=f"🎯 Target node: @{active_target}")
    
    try:
        target_id = cl.user_id_from_username(active_target)
        sweep_type = random.choice(["followers", "following"])
        max_id_key = f"{sweep_type}_next_max_id"
        current_max_id = history.get(max_id_key, None)
        
        if sweep_type == "followers":
            users, next_max_id = cl.user_followers_v1(target_id, amount=15, max_id=current_max_id if current_max_id else "")
        else:
            users, next_max_id = cl.user_following_v1(target_id, amount=15, max_id=current_max_id if current_max_id else "")
            
        history[max_id_key] = next_max_id
        update_account_metric(username, "add_history", value=f"📑 Cataloged {len(users)} accounts.")
        
        for u in users:
            if actions_performed >= MAX_ACTIONS_PER_PROFILE_RUN: break
            u_pk = int(u.pk)
            u_name = u.username
            
            if u_name not in history["competitor_pool"] and u_name not in config["competitor_accounts"]:
                if getattr(u, 'is_private', False) == False:
                    history["competitor_pool"].append(u_name)
                    update_account_metric(username, "add_history", value=f"🧬 [AMPLIFY] Injected @{u_name}")
            
            if u_pk in history["followed_users"] or u_pk in history["blocked_or_missing"]:
                continue
                
            try:
                friendship = cl.user_friendship_v1(u_pk)
                if not friendship.following and not friendship.outgoing_request:
                    update_account_metric(username, "add_history", value=f"👤 Following network target: @{u_name}")
                    cl.user_follow(u_pk)
                    history["followed_users"].append(u_pk)
                    update_account_metric(username, "total_follows", increment=1)
                    actions_performed += 1
                    time.sleep(random.uniform(5.0, 12.0))
            except (UserNotFound, PrivateError):
                history["blocked_or_missing"].append(u_pk)
            except Exception: pass
    except Exception as e:
        update_account_metric(username, "add_history", value=f"⚠️ Scrape error on @{active_target}: {str(e)[:25]}")
    return actions_performed

def interact_with_hashtags(cl, history, config, username, actions_performed):
    hashtag = random.choice(config["target_hashtags"])
    update_account_metric(username, "add_history", value=f"🔍 Streaming target #{hashtag}...")
    try:
        medias = cl.hashtag_medias_recent(hashtag, amount=8)
        for media in medias:
            if actions_performed >= MAX_ACTIONS_PER_PROFILE_RUN: break
            if media.id in history["liked_medias"]: continue
            user_pk = int(media.user.pk)
            if user_pk in history["blocked_or_missing"]: continue
            try:
                update_account_metric(username, "add_history", value=f"❤️ Liking post from @{media.user.username}")
                cl.media_like(media.id)
                history["liked_medias"].append(media.id)
                update_account_metric(username, "total_likes", increment=1)
                actions_performed += 1
                time.sleep(random.uniform(4.0, 9.0))
                
                if random.random() < 0.30 and media.id not in history["commented_medias"]:
                    reply_text = generate_interactive_reply("comment", media.caption_text or "", media.user.username)
                    if reply_text:
                        update_account_metric(username, "add_history", value=f"💬 Commenting: {reply_text[:20]}...")
                        cl.media_comment(media.id, reply_text)
                        history["commented_medias"].append(media.id)
                        actions_performed += 1
                        time.sleep(random.uniform(6.0, 15.0))
            except Exception: continue
    except Exception as e:
        update_account_metric(username, "add_history", value=f"⚠️ Hashtag error: {str(e)[:25]}")
    return actions_performed

def handle_direct_messages(cl, history, username):
    try:
        threads = cl.direct_threads(amount=5, selected_filter="unread")
        for thread in threads:
            if thread.is_group or not thread.messages: continue
            last_msg = thread.messages
            if last_msg.user_id == cl.user_id or last_msg.id in history["replied_dms"] or not last_msg.text:
                continue
            sender = thread.users.username if thread.users else "user"
            update_account_metric(username, "add_history", value=f"💬 DM from @{sender}: '{last_msg.text[:15]}'")
            reply = generate_interactive_reply("dm", last_msg.text, sender)
            if reply:
                cl.direct_answer(thread.id, reply)
                update_account_metric(username, "add_history", value=f"📤 Sent DM Reply: {reply[:20]}")
                history["replied_dms"].append(last_msg.id)
                time.sleep(random.uniform(4.0, 9.0))
    except Exception as e: print(f"⚠️ DM Error: {e}")

def handle_post_comments(cl, history, username):
    try:
        my_medias = cl.user_medias(cl.user_id, amount=3)
        for media in my_medias:
            comments = cl.media_comments(media.id, amount=10)
            for comment in comments:
                if comment.user_id == cl.user_id or comment.id in history["replied_comments"]:
                    continue
                update_account_metric(username, "add_history", value=f"💭 Comment from @{comment.user.username}: '{comment.text[:15]}'")
                reply = generate_interactive_reply("comment", comment.text, comment.user.username)
                if reply:
                    cl.comment_reply(media.id, comment.id, reply)
                    update_account_metric(username, "add_history", value=f"📤 Replied to Comment: {reply[:20]}")
                    history["replied_comments"].append(comment.id)
                    time.sleep(random.uniform(5.0, 12.0))
    except Exception as e: print(f"⚠️ Comment Error: {e}")

def execute_repost_flow(cl, history, username, folder_pool):
    update_account_metric(username, "add_history", value="Scanning storage content for posting pool updates.")
    available_pool = [f for f in folder_pool if f["id"] not in history["posted_ids"]]
    if not available_pool:
        return False

    selected_folder = random.choice(available_pool)
    folder_path = selected_folder["path"]
    valid_exts = ('.jpg', '.jpeg', '.png', '.mp4')
    files = sorted([str(f) for f in folder_path.iterdir() if f.suffix.lower() in valid_exts])
    
    if files:
        text_context = "HFT automated execution profiles and metric systems."
        txt_files = [f for f in folder_path.iterdir() if f.suffix.lower() == '.txt']
        if txt_files:
            try:
                with open(txt_files[0], 'r', encoding='utf-8') as tf:
                    text_context = tf.read().strip()
            except Exception: pass
        
        update_account_metric(username, "add_history", value=f"🤖 Reposting from folder: {selected_folder['id']}")
        rage_caption = generate_rage_bait_caption(text_context, selected_folder["source"])
        
        # Add dynamic hashtag implementation into caption generation mapping as requested
        hashtag_prompt = f"Based on this text: '{text_context}', output exactly 5 relevant hashtags separated by spaces. Output ONLY the hashtags."
        try:
            h_res = ollama.chat(model='llama3.1', messages=[{'role': 'user', 'content': hashtag_prompt}])
            generated_tags = h_res['message']['content'].strip().replace('"', '')
        except Exception:
            generated_tags = "#automation #trading #climbing"
            
        full_caption = f"{rage_caption}\n\n{generated_tags}" if rage_caption else generated_tags
        
        uploaded_media = None
        if len(files) == 1:
            target_file = files[0]
            if target_file.lower().endswith('.mp4'):
                uploaded_media = cl.video_upload(target_file, caption=full_caption)
            else:
                uploaded_media = cl.photo_upload(target_file, caption=full_caption)
        else:
            uploaded_media = cl.album_upload(files, caption=full_caption)
            
        if uploaded_media:
            update_account_metric(username, "total_posts", increment=1)
            update_account_metric(username, "add_history", value="✅ Repost deployment complete.")
            history["posted_ids"].append(selected_folder["id"])
            
            # Story posting feature verification loop
            if random.random() < 0.50:
                try:
                    time.sleep(random.uniform(5.0, 10.0))
                    cl.story_upload_photo(files[0], caption="Story Update")
                    update_account_metric(username, "add_history", value="🌟 Periodic story amplification completed.")
                except Exception as se:
                    print(f"⚠️ Story share error: {se}")
            return True
    return False

def run_profile_workflow(username, conf, folder_pool):
    global NEXT_TASK_OVERRIDE
    update_account_metric(username, "status", status="Active Execution")
    
    cl = get_authenticated_client(username, conf)
    if not cl:
        update_account_metric(username, "status", status="Idle")
        return
        
    history_default = {
        "posted_ids": [], "replied_dms": [], "replied_comments": [], 
        "liked_medias": [], "commented_medias": [], "followed_users": [], 
        "blocked_or_missing": [], "competitor_pool": [],
        "followers_next_max_id": None, "following_next_max_id": None
    }
    history = load_json(conf["history_file"], history_default)
    actions_performed = 0
    
    # Process core responses first 
    handle_direct_messages(cl, history, username)
    handle_post_comments(cl, history, username)
    
    forced_task = NEXT_TASK_OVERRIDE.pop(username, None)
    
    if forced_task == "networking":
        actions_performed = harvest_and_amplify_networks(cl, history, conf, username, actions_performed)
    elif forced_task == "hashtags":
        actions_performed = interact_with_hashtags(cl, history, conf, username, actions_performed)
    else:
        # Balanced deployment split mapping
        roll = random.random()
        if roll < 0.40:
            execute_repost_flow(cl, history, username, folder_pool)
        elif roll < 0.75:
            actions_performed = harvest_and_amplify_networks(cl, history, conf, username, actions_performed)
        else:
            actions_performed = interact_with_hashtags(cl, history, conf, username, actions_performed)
            
    save_json(conf["history_file"], history)
    update_account_metric(username, "status", status="Idle")

# --- EMBEDDED DASHBOARD MONITOR SERVER ---
class DashboardAPIHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = load_analytics()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            
            html = """<!DOCTYPE html>
            <html>
            <head>
                <title>Automation Hub Control Head</title>
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f111a; color: #a6accd; margin: 20px; }
                    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #232635; padding-bottom: 15px; margin-bottom: 20px; }
                    h1 { color: #f4f4f6; margin: 0; font-size: 24px; }
                    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
                    .card { background: #181a26; border: 1px solid #232635; border-radius: 8px; padding: 20px; position: relative; }
                    .badge { position: absolute; top: 20px; right: 20px; padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; text-transform: uppercase; }
                    .status-active { background: rgba(58, 212, 172, 0.15); color: #3ad4ac; }
                    .status-idle { background: rgba(127, 142, 237, 0.15); color: #7f8eed; }
                    .status-cooldown { background: rgba(240, 113, 120, 0.15); color: #f07178; }
                    .profile-title { font-size: 18px; color: #f4f4f6; margin-top: 0; margin-bottom: 15px; }
                    .stats-box { display: flex; justify-content: space-between; font-size: 13px; border-bottom: 1px solid #232635; padding: 8px 0; }
                    .stats-box span:last-child { color: #f4f4f6; font-weight: bold; }
                    .chart-container { margin-top: 20px; height: 160px; position: relative; }
                    .logs { background: #0f111a; border-radius: 4px; padding: 10px; font-family: monospace; font-size: 11px; height: 110px; overflow-y: auto; margin-top: 15px; border: 1px solid #232635; }
                </style>
            </head>
            <body>
                <div class="header">
                    <h1>🎛️ Linux Repost Engine :: Control Head</h1>
                    <div id="clock" style="font-family: monospace; color:#7f8eed;">Loading Node Feed...</div>
                </div>
                <div class="grid" id="dashboard-grid"></div>

                <script>
                    let charts = {};
                    function updateDashboard() {
                        fetch('/api/metrics')
                            .then(res => res.json())
                            .then(data => {
                                document.getElementById('clock').innerText = "Last Checked: " + new Date().toLocaleTimeString();
                                const grid = document.getElementById('dashboard-grid');
                                
                                Object.keys(data).forEach(user => {
                                    const account = data[user];
                                    let card = document.getElementById(`card-${user}`);
                                    
                                    let badgeStyle = "status-idle";
                                    if(account.status.toLowerCase().includes("active") || account.status.toLowerCase().includes("posting")) badgeStyle = "status-active";
                                    if(account.status.toLowerCase().includes("cool") || account.status.toLowerCase().includes("block")) badgeStyle = "status-cooldown";

                                    if (!card) {
                                        card = document.createElement('div');
                                        card.className = 'card';
                                        card.id = `card-${user}`;
                                        grid.appendChild(card);
                                    }

                                    const logItems = account.history_log.map(l => `<div>${l}</div>`).join('');

                                    card.innerHTML = `
                                        <div class="badge ${badgeStyle}">${account.status}</div>
                                        <div class="profile-title">@${user}</div>
                                        <div class="stats-box"><span>Posts Formed</span><span>${account.total_posts}</span></div>
                                        <div class="stats-box"><span>Follow Matrix Interactions</span><span>${account.total_follows}</span></div>
                                        <div class="stats-box"><span>Discovered Likes</span><span>${account.total_likes}</span></div>
                                        <div class="stats-box"><span>Task Break Penalty</span><span>${account.cooldown_until}</span></div>
                                        <div class="chart-container"><canvas id="chart-${user}"></canvas></div>
                                        <div class="logs">${logItems || '<div>[System] Initializing logging metrics...</div>'}</div>
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
                                                label: 'Followers',
                                                data: values,
                                                borderColor: '#3ad4ac',
                                                backgroundColor: 'rgba(58, 212, 172, 0.05)',
                                                borderWidth: 2,
                                                tension: 0.3,
                                                fill: true,
                                                pointRadius: 2
                                            }]
                                        },
                                        options: {
                                            responsive: true,
                                            maintainAspectRatio: false,
                                            plugins: { legend: { display: false } },
                                            scales: {
                                                x: { grid: { display: false }, ticks: { color: '#676e95', font: { size: 9 } } },
                                                y: { grid: { color: '#232635' }, ticks: { color: '#676e95', font: { size: 9 } } }
                                            }
                                        }
                                    });
                                });
                            });
                    }
                    updateDashboard();
                    setInterval(updateDashboard, 5000);
                </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_error(404, "File Not Found")

def launch_monitoring_head(port=8080):
    server = HTTPServer(("0.0.0.0", port), DashboardAPIHandler)
    print(f"🖥️  Automation Monitoring Head deployed globally at http://localhost:{port}")
    server.serve_forever()

# --- CORE ROTATION SCHEDULER ENGINE ---
def main():
    monitor_thread = threading.Thread(target=launch_monitoring_head, args=(8080,), daemon=True)
    monitor_thread.start()

    account_queue = list(FAN_ROSTER.keys())
    
    while True:
        folder_pool = discover_local_media_folders()
        if not folder_pool:
            print(f"⚠️ No active folders found in '{DOWNLOAD_ROOT}'. Retrying in 5 minutes...")
            time.sleep(300)
            continue
            
        if not account_queue: account_queue = list(FAN_ROSTER.keys())
        current_user = account_queue.pop(0)
        
        if current_user in ACCOUNT_COOLDOWNS:
            if datetime.now() < ACCOUNT_COOLDOWNS[current_user]:
                account_queue.append(current_user)
                time.sleep(2)
                continue
            else:
                del ACCOUNT_COOLDOWNS[current_user]
                update_account_metric(current_user, "cooldown_until", value="None")

        config_details = FAN_ROSTER[current_user]
        try:
            run_profile_workflow(current_user, config_details, folder_pool)
        except FeedbackRequired:
            cooldown_target = datetime.now() + timedelta(hours=2)
            ACCOUNT_COOLDOWNS[current_user] = cooldown_target
            NEXT_TASK_OVERRIDE[current_user] = random.choice(["networking", "hashtags"])
            update_account_metric(current_user, "cooldown_until", value=cooldown_target.strftime("%H:%M"))
            update_account_metric(current_user, "status", status="Rate Cooldown")
            update_account_metric(current_user, "add_history", value="Action block flagged. Shifting node to cooldown and changing recovery task.")
        except Exception as e:
            update_account_metric(current_user, "add_history", value=f"Fault error: {str(e)[:25]}")

        account_queue.append(current_user)
        inter_account_delay = random.randint(600, 1200)
        print(f"💤 Staggering shift loop focus for {inter_account_delay // 60} minutes...")
        time.sleep(inter_account_delay)

if __name__ == "__main__":
    main()
