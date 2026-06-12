"""
Discord 频道每日内容小结 - Web版后端
支持：每日定时抓取 + 自定义时间范围抓取
"""
from flask import Flask, render_template, request, jsonify
import requests
import time
import os
import threading
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

run_log = []
summaries_history = []
scheduler = BackgroundScheduler()

GAME_CONTEXT = """
这是一个手游的Discord服务器，服务器中有多个语言的频道（英语、泰语等），
玩家在频道中讨论角色抽取、游戏武器装备、攻略等话题。
"""

def log(msg: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    run_log.append(entry)
    print(entry)
    if len(run_log) > 200:
        run_log.pop(0)

def dt_to_snowflake(dt: datetime) -> int:
    discord_epoch = 1420070400000
    ms = int(dt.timestamp() * 1000)
    return (ms - discord_epoch) << 22

def get_discord_headers(token: str) -> dict:
    return {
        "Authorization": token.strip(),
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

def get_guild_channels(token: str, guild_id: str) -> list:
    url = f"https://discord.com/api/v10/guilds/{guild_id}/channels"
    resp = requests.get(url, headers=get_discord_headers(token))
    if resp.status_code == 401:
        log("❌ Discord Token 无效")
        return []
    if resp.status_code == 403:
        log("❌ 无权访问该服务器")
        return []
    if resp.status_code != 200:
        log(f"❌ 获取频道失败: {resp.status_code}")
        return []
    channels = resp.json()
    return [c for c in channels if c.get("type") in (0, 5)]

def get_messages_in_range(token: str, channel_id: str, after_dt: datetime, before_dt: datetime) -> list:
    """抓取指定时间范围内的消息（通用）"""
    after_sf  = dt_to_snowflake(after_dt)
    before_sf = dt_to_snowflake(before_dt)
    all_messages = []
    last_id = str(after_sf)

    while True:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100&after={last_id}"
        resp = requests.get(url, headers=get_discord_headers(token))
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            log(f"  ⏳ 限速，等待 {retry_after:.1f}s...")
            time.sleep(retry_after + 0.5)
            continue
        elif resp.status_code in (403, 401):
            break
        elif resp.status_code != 200:
            break
        messages = resp.json()
        if not messages:
            break
        messages.sort(key=lambda m: int(m["id"]))
        in_range = [m for m in messages if int(m["id"]) < before_sf]
        all_messages.extend(in_range)
        if int(messages[-1]["id"]) >= before_sf:
            break
        last_id = messages[-1]["id"]
        time.sleep(0.3)

    return all_messages

def format_messages_for_ai(channel_name: str, messages: list, label: str = "今日") -> str:
    if not messages:
        return f"【{channel_name}】\n（{label}无消息）\n"
    lines = [f"【{channel_name}】（共{len(messages)}条消息）"]
    for msg in messages[-200:]:
        author  = msg.get("author", {}).get("username", "未知用户")
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{author}: {content}")
    return "\n".join(lines) + "\n"

def analyze_with_deepseek(api_key: str, title_date: str, channels_data: dict,
                           date_range_label: str = "") -> str:
    client = OpenAI(api_key=api_key.strip(), base_url="https://api.deepseek.com")
    all_content = ""
    for ch_name, messages in channels_data.items():
        all_content += format_messages_for_ai(ch_name, messages, label=date_range_label or "该时段")

    range_desc = f"（时间范围：{date_range_label}）" if date_range_label else ""
    prompt = f"""
{GAME_CONTEXT}

以下是{title_date}{range_desc}各Discord频道的聊天记录：

{all_content}

请根据以上聊天记录，生成一份简洁的Discord频道内容小结。

要求：
1. 标题格式：{title_date}Discord情况小结
2. 每个频道单独一行描述，格式参考：
   英语频道：玩家自主讨论角色抽取话题
   泰语频道：玩家自主讨论角色抽取与游戏武器装备相关话题
   英语&泰语攻略讨论频道暂无玩家发言
3. 如果频道没有消息，注明"暂无玩家发言"
4. 语言简洁
"""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000
    )
    return response.choices[0].message.content

def run_summary(token: str, guild_id: str, api_key: str, keywords: list,
                after_dt: datetime = None, before_dt: datetime = None):
    """
    通用执行函数
    after_dt / before_dt 为 None 时默认抓今天 0:00~现在
    """
    now = datetime.now(tz=timezone.utc)

    if after_dt is None:
        after_dt  = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
        before_dt = now
        title_date = datetime.now().strftime("%m%d")
        range_label = "今日"
    else:
        title_date  = after_dt.astimezone().strftime("%m%d") + "~" + before_dt.astimezone().strftime("%m%d")
        range_label = (after_dt.astimezone().strftime("%Y-%m-%d %H:%M") +
                       " 至 " + before_dt.astimezone().strftime("%Y-%m-%d %H:%M"))

    log(f"🚀 开始抓取 [{range_label}]")

    channels = get_guild_channels(token, guild_id)
    if not channels:
        log("❌ 未获取到频道，任务终止")
        return

    if keywords:
        channels = [c for c in channels
                    if any(k.lower() in c.get("name", "").lower() for k in keywords)]

    log(f"✅ 共 {len(channels)} 个频道")

    channels_data = {}
    for ch in channels:
        ch_name = ch.get("name", "unknown")
        log(f"📨 爬取频道: #{ch_name}")
        messages = get_messages_in_range(token, ch["id"], after_dt, before_dt)
        log(f"  → {len(messages)} 条消息")
        channels_data[ch_name] = messages

    log("🤖 正在调用 DeepSeek 分析...")
    try:
        summary = analyze_with_deepseek(api_key, title_date, channels_data, range_label)
    except Exception as e:
        log(f"❌ DeepSeek 分析失败: {e}")
        return

    summaries_history.insert(0, {
        "date": range_label,
        "time": datetime.now().strftime("%H:%M"),
        "content": summary,
        "type": "range" if after_dt else "daily"
    })
    log("✅ 小结生成完成")

# ========== 路由 ==========

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/run", methods=["POST"])
def api_run():
    """今日小结（立即执行）"""
    data     = request.json
    token    = data.get("token", "").strip()
    guild_id = data.get("guild_id", "").strip()
    api_key  = data.get("api_key", "").strip()
    keywords = [k.strip() for k in data.get("keywords", "").split(",") if k.strip()]

    if not token or not guild_id or not api_key:
        return jsonify({"error": "请填写所有必填项"}), 400

    run_log.clear()
    thread = threading.Thread(target=run_summary, args=(token, guild_id, api_key, keywords))
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route("/api/run_range", methods=["POST"])
def api_run_range():
    """自定义时间范围抓取+分析"""
    data     = request.json
    token    = data.get("token", "").strip()
    guild_id = data.get("guild_id", "").strip()
    api_key  = data.get("api_key", "").strip()
    keywords = [k.strip() for k in data.get("keywords", "").split(",") if k.strip()]
    date_from = data.get("date_from", "")
    time_from = data.get("time_from", "00:00")
    date_to   = data.get("date_to", "")
    time_to   = data.get("time_to", "23:59")

    if not token or not guild_id or not api_key:
        return jsonify({"error": "请填写所有必填项"}), 400
    if not date_from or not date_to:
        return jsonify({"error": "请填写开始和结束日期"}), 400

    try:
        after_dt  = datetime.fromisoformat(f"{date_from}T{time_from}:00").astimezone(timezone.utc)
        before_dt = datetime.fromisoformat(f"{date_to}T{time_to}:00").astimezone(timezone.utc)
    except ValueError:
        return jsonify({"error": "日期格式错误"}), 400

    if after_dt >= before_dt:
        return jsonify({"error": "开始时间必须早于结束时间"}), 400

    run_log.clear()
    thread = threading.Thread(
        target=run_summary,
        args=(token, guild_id, api_key, keywords, after_dt, before_dt)
    )
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": run_log})

@app.route("/api/summaries")
def api_summaries():
    return jsonify({"summaries": summaries_history})

@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data     = request.json
    token    = data.get("token", "").strip()
    guild_id = data.get("guild_id", "").strip()
    api_key  = data.get("api_key", "").strip()
    keywords = [k.strip() for k in data.get("keywords", "").split(",") if k.strip()]
    run_time = data.get("time", "19:00")

    scheduler.remove_all_jobs()
    hour, minute = run_time.split(":")
    scheduler.add_job(
        run_summary,
        "cron",
        hour=int(hour),
        minute=int(minute),
        args=[token, guild_id, api_key, keywords]
    )
    if not scheduler.running:
        scheduler.start()

    log(f"⏰ 定时任务已设置：每天 {run_time} 执行")
    return jsonify({"status": "ok", "time": run_time})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
