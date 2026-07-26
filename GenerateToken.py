from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect
import requests
import os

# =============================================================================
# KITE CONFIG
# =============================================================================
KITE_API_KEY = "ikfiyrgi5w2dttxb"
KITE_API_SECRET = "ipynijsrgf4wrsm7ebn18hwwfucy3185"
USER_ID = "EI5633"
PASSWORD = "Allstate@123"

LOGIN_URL = f"https://kite.zerodha.com/connect/login?api_key={KITE_API_KEY}&v=3"

# =============================================================================
# RENDER CONFIG & TARGET ENV VALUES
# =============================================================================
RENDER_API_KEY = "rnd_bfpmPHYqGiHXn0euY1BP9nxttP6k"
RENDER_API = "https://api.render.com/v1"

SERVICES = {
    "intraday":"srv-d9dknpbbc2fs73e8ivmg",
}
PYTHON_VERSION = "3.11.9"

# Your target login details to be pushed to Render
APP_LOGIN_USER = "momentum"
APP_LOGIN_PASS = "55"

# =============================================================================
# SAVE TOKEN CONFIG
# =============================================================================
TOKEN_TXT_FILE = "kite_access_token.txt"


def generate_access_token():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        page.goto(LOGIN_URL)
        page.fill("input[type='text']", USER_ID)
        page.fill("input[type='password']", PASSWORD)
        page.click("button[type='submit']")

        print("👉 Enter TOTP manually...")

        with page.expect_request(lambda req: "request_token=" in req.url, timeout=120000) as request_info:
            pass

        redirect_url = request_info.value.url
        browser.close()

    parsed = urlparse(redirect_url)
    request_token = parse_qs(parsed.query)["request_token"][0]

    kite = KiteConnect(api_key=KITE_API_KEY)
    data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    access_token = data["access_token"]

    print("\n🚀 ACCESS TOKEN GENERATED:", access_token)
    return access_token


def save_access_token_to_txt(access_token: str, filename: str = TOKEN_TXT_FILE):
    # saves in the same folder where you run the script
    file_path = os.path.join(os.getcwd(), filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(access_token.strip())
    print(f"💾 ACCESS TOKEN saved to: {file_path}")


def update_render(access_token):
    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json",
    }

    for name, service_id in SERVICES.items():
        print(f"\n🔄 Fetching existing variables for [{name}]...")

        # 1. Fetch current environment variables from Render
        url = f"{RENDER_API}/services/{service_id}/env-vars"
        r = requests.get(url, headers=headers)
        r.raise_for_status()

        # Render returns: [{"envVar": {"key": "...", "value": "..."}}, ...]
        current_vars = r.json()

        # Convert to dictionary for easy merging
        merged_env = {}
        for item in current_vars:
            var = item.get("envVar", {})
            merged_env[var["key"]] = var["value"]

        # 2. Add or update target keys without losing existing ones (like APP_AUTH_SECRET)
        merged_env["KITE_API_KEY"] = KITE_API_KEY
        merged_env["KITE_ACCESS_TOKEN"] = access_token
        merged_env["PYTHON_VERSION"] = PYTHON_VERSION
        merged_env["APP_LOGIN_USER"] = APP_LOGIN_USER
        merged_env["APP_LOGIN_PASS"] = APP_LOGIN_PASS
        merged_env["COOKIE_SECURE"] = "1"  # Force secure SSL cookies on Render

        # Ensure a session secret exists; if not, create one
        if "APP_AUTH_SECRET" not in merged_env:
            import secrets
            merged_env["APP_AUTH_SECRET"] = secrets.token_urlsafe(32)

        # 3. Format back into Render API list payload
        payload = [{"key": k, "value": v} for k, v in merged_env.items()]

        print(f"🔄 Uploading merged variables to [{name}]...")
        r = requests.put(url, headers=headers, json=payload)
        r.raise_for_status()
        print(f"✅ [{name}] variables successfully updated.")

        # 4. Trigger a fresh deployment
        print(f"🚀 Triggering redeployment for [{name}]...")
        deploy_url = f"{RENDER_API}/services/{service_id}/deploys"
        r = requests.post(deploy_url, headers=headers)
        r.raise_for_status()
        print(f"🎉 [{name}] deployment initiated.")


if __name__ == "__main__":
    token = generate_access_token()
    save_access_token_to_txt(token)   # <-- saves token to kite_access_token.txt
    update_render(token)
    print("\n🎉 PIPELINE COMPLETE — Render is updating.")