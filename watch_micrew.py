from __future__ import annotations
import os, sys, time, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------
BASE_DIR = Path.home() / "pilot_schedule"
TOKEN_FILE = BASE_DIR / "token.json"
CLIENT_SECRET = BASE_DIR / "client_secret.json"
STATE_FILE = BASE_DIR / ".micrew_state.json"

SOURCE_CAL_NAME = "MiCrew"        # Apple Calendar mirrored in Google
TARGET_CAL_NAME = "Griffin Ops"   # Calendar for commute flight entries
POLL_EVERY_SEC = 300              # 5 minutes

# ---------------------------------------------------------------------
# GOOGLE CALENDAR SETUP
# ---------------------------------------------------------------------
def build_service():
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = Credentials.from_authorized_user_file(
        str(TOKEN_FILE), scopes=scopes
    )
    return build("calendar", "v3", credentials=creds)

# ---------------------------------------------------------------------
# STATE MANAGEMENT
# ---------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"updatedMin": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------------------------------------------------------------------
# EVENT HELPERS
# ---------------------------------------------------------------------
def get_calendar_id_by_name(svc, name):
    calendars = svc.calendarList().list().execute()
    for cal in calendars.get("items", []):
        if cal.get("summary") == name:
            return cal["id"]
    raise ValueError(f"Calendar '{name}' not found in your account.")

def iter_duty_events(svc, cal_id, updated_min=None):
    now = datetime.now(timezone.utc).isoformat()
    query = {"calendarId": cal_id, "singleEvents": True,
             "orderBy": "startTime", "timeMin": now}
    if updated_min:
        query["updatedMin"] = updated_min

    while True:
        resp = svc.events().list(**query).execute()
        for ev in resp.get("items", []):
            summary = ev.get("summary", "").lower()
            if "report" in summary or "duty" in summary:
                yield ev
        page = resp.get("nextPageToken")
        if not page:
            break
        query["pageToken"] = page

# ---------------------------------------------------------------------
# PROCESS ONE DUTY EVENT
# ---------------------------------------------------------------------
def parse_event_datetime(raw):
    if not raw:
        return None

    # Normalize UTC designator returned by Google ("Z") to RFC3339 compatible
    # format that `datetime.fromisoformat` understands.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        # Fall back to the narrower format we expect from Calendar entries.
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")


def process_one_duty(svc, dst_cal_id, ev):
    start_raw = ev["start"].get("dateTime")
    end_raw = ev["end"].get("dateTime")
    if not start_raw or not end_raw:
        return

    report_dt = parse_event_datetime(start_raw)
    if not report_dt:
        return

    # Call commute planner for this report
    print(f"[INFO] Found duty event: {ev.get('summary')} {report_dt}")
    os.system(f"python {BASE_DIR}/commute_planner_fa_quota.py")

# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def main():
    state = load_state()
    updated_min_iso = state.get("updatedMin")
    svc = build_service()

    src_id = get_calendar_id_by_name(svc, SOURCE_CAL_NAME)
    dst_id = get_calendar_id_by_name(svc, TARGET_CAL_NAME)

    print(f"[INFO] Monitoring {SOURCE_CAL_NAME} → {TARGET_CAL_NAME}")

    for ev in iter_duty_events(svc, src_id, updated_min_iso):
        process_one_duty(svc, dst_id, ev)

    # Save watermark for next run
    now_iso = datetime.now(timezone.utc).isoformat()
    state["updatedMin"] = now_iso
    save_state(state)

# ---------------------------------------------------------------------
# LOOP MODE
# ---------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        while True:
            try:
                main()
            except Exception as e:
                print("[watch_micrew] error:", e)
            time.sleep(POLL_EVERY_SEC)
    else:
        main()
