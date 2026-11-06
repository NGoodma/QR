"""Put the bot on Vercel and point Telegram at it. Safe to run again and again.

    VERCEL_TOKEN=... TELEGRAM_BOT_TOKEN=... BOT_NAME=qr OWNER=ngoodma python scripts/deploy.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

VERCEL = ["npx", "--yes", "vercel@65.12.1"]
vtoken = os.environ["VERCEL_TOKEN"]
tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
name = re.sub(r"[^a-z0-9-]", "-", os.environ.get("BOT_NAME", "qr").lower())
domain = f"{name}-{re.sub(r'[^a-z0-9-]', '-', os.environ.get('OWNER', 'bot').lower())}.vercel.app"


def vercel(*args, stdin=None, check=True):
    print("$ vercel", " ".join(args), flush=True)
    r = subprocess.run([*VERCEL, *args, "--token", vtoken], input=stdin, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"vercel {args[0]} failed:\n{r.stdout}\n{r.stderr}")
    return r


def api(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


print(f"== project {name}")
vercel("project", "create", name, check=False)  # fails harmlessly if it already exists
vercel("link", "--yes", "--project", name)

print("== bot token -> Vercel environment variable")
vercel("env", "add", "TELEGRAM_BOT_TOKEN", "-e", "production", "--sensitive", "--force", "--yes", stdin=tg_token)

print("== turn off Vercel login screen (Telegram must reach the webhook)")
vercel("project", "protection", "disable", name, "--sso")

print("== deploy")
out = vercel("deploy", "--prod", "--yes").stdout
url = re.findall(r"https://[\w.-]+\.vercel\.app", out)[-1]
print(url)
vercel("alias", "set", url, domain)
print("https://" + domain)

print("== check the function answers")
with urllib.request.urlopen(f"https://{domain}/api/telegram", timeout=60) as resp:
    assert resp.status == 200, resp.status

print("== Telegram webhook")
tg = f"https://api.telegram.org/bot{tg_token}/"
api("POST", tg + "setWebhook", {
    "url": f"https://{domain}/api/telegram",
    "secret_token": hashlib.sha256(tg_token.encode()).hexdigest()[:32],
    "allowed_updates": ["message", "callback_query"],
})
info = api("POST", tg + "getWebhookInfo", {})["result"]
print({"url": info["url"], "pending": info["pending_update_count"], "last_error": info.get("last_error_message", "none")})
