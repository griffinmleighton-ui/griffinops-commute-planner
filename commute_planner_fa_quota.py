"""Flight-aware commute planner with quota + cache support.

This script inspects duty events in the `Griffin Ops` Google Calendar and
creates companion "Commute option" entries for inbound and outbound flights.
Flight schedules are sourced from a local CSV fallback and optionally merged
with FlightAware AeroAPI v4 data. The FlightAware integration tracks a monthly
request cap and caches responses per city pair/date to avoid burning quota.

Environment variables:
  FLIGHTAWARE_API_KEY  – AeroAPI key (required for FlightAware lookups)
  FA_MONTHLY_CAP       – optional monthly request cap (default: 99)

Files (relative to this script):
  token.json           – Google OAuth token (desktop app)
  flights.csv          – CSV fallback schedule data
  fa_cache/            – JSON cache per city pair/date
  fa_quota.json        – persisted request counter per month
"""

from __future__ import annotations

import csv
import json
import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from dateutil import parser as dtparse
from dateutil import tz
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_SUMMARY = "Griffin Ops"

HOME_IATA = "MCO"
BASE_IATA = "ATL"

INBOUND_BUFFER_MIN = 60
OUTBOUND_BUFFER_MIN = 15
MAX_INBOUND = 2
MAX_OUTBOUND = 1
LOOKAHEAD_DAYS = 30
COMMUTE_PREFIX = "Commute option"
DEBUG = True

ET_LOCAL = tz.gettz("America/New_York")
USE_CSV_FIRST = True

FLIGHTAWARE_BASE = "https://aeroapi.flightaware.com/aeroapi"
FLIGHTAWARE_KEY = os.getenv("FLIGHTAWARE_API_KEY")
FA_MONTHLY_CAP = int(os.getenv("FA_MONTHLY_CAP", "99"))

BASE_DIR = Path(__file__).resolve().parent
TOKEN_PATH = BASE_DIR / "token.json"
CSV_FALLBACK = BASE_DIR / "flights.csv"
CACHE_DIR = BASE_DIR / "fa_cache"
CACHE_DIR.mkdir(exist_ok=True)
QUOTA_FILE = BASE_DIR / "fa_quota.json"

IATA_TO_ICAO: Dict[str, str] = {"MCO": "KMCO", "ATL": "KATL"}


# ---------------------------------------------------------------------------
# GOOGLE CALENDAR
# ---------------------------------------------------------------------------
def gcal_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    return build("calendar", "v3", credentials=creds)


def get_calendar_id(service) -> str:
    target = CALENDAR_SUMMARY.strip().lower()
    page_token: Optional[str] = None
    seen_names: List[str] = []

    while True:
        resp = (
            service.calendarList()
            .list(pageToken=page_token, maxResults=250)
            .execute()
        )
        for entry in resp.get("items", []):
            summary = (entry.get("summary") or "").strip()
            seen_names.append(summary)
            if summary.lower() == target:
                return entry["id"]

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    raise ValueError(
        f"Calendar named '{CALENDAR_SUMMARY}' not found. Available calendars: {', '.join(sorted(set(seen_names)))}"
    )


def list_upcoming_duties(service, cal_id) -> List[Tuple[datetime, datetime, str]]:
    now = datetime.now(ET_LOCAL)
    end = now + timedelta(days=LOOKAHEAD_DAYS)
    duties: List[Tuple[datetime, datetime, str]] = []
    page_token: Optional[str] = None
    duty_keywords = ("DUTY", "REPORT", "TRIP", "PAIRING", "CHECK-IN")

    while True:
        events = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )

        for event in events.get("items", []):
            summary = (event.get("summary") or "").upper()
            etype = event.get("extendedProperties", {}).get("private", {}).get("type", "")
            if etype != "DUTY" and not any(keyword in summary for keyword in duty_keywords):
                continue

            start_raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            end_raw = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            if not start_raw or not end_raw:
                continue

            report = dtparse.parse(start_raw)
            release = dtparse.parse(end_raw)
            duties.append((report, release, event["id"]))

        page_token = events.get("nextPageToken")
        if not page_token:
            break

    return duties


# ---------------------------------------------------------------------------
# QUOTA + CACHE HELPERS
# ---------------------------------------------------------------------------
def _month_key(at: Optional[datetime] = None) -> str:
    at = at or datetime.now(timezone.utc)
    return at.strftime("%Y-%m")


def _load_quota() -> Dict[str, int]:
    if QUOTA_FILE.exists():
        try:
            return json.loads(QUOTA_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"period": _month_key(), "count": 0}


def _save_quota(payload: Dict[str, int]) -> None:
    QUOTA_FILE.write_text(json.dumps(payload))


def _ensure_quota_period() -> Dict[str, int]:
    payload = _load_quota()
    current = _month_key()
    if payload.get("period") != current:
        payload = {"period": current, "count": 0}
        _save_quota(payload)
    return payload


def quota_status() -> Tuple[str, int, int]:
    payload = _ensure_quota_period()
    return payload["period"], payload["count"], FA_MONTHLY_CAP


def quota_can_consume(n_calls: int = 1) -> bool:
    payload = _ensure_quota_period()
    return payload["count"] + n_calls <= FA_MONTHLY_CAP


def quota_consume(n_calls: int = 1) -> None:
    payload = _ensure_quota_period()
    payload["count"] += n_calls
    _save_quota(payload)


def _cache_key(dep: str, arr: str, flight_day: date) -> str:
    raw = f"{dep.upper()}-{arr.upper()}-{flight_day.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cache_get(dep: str, arr: str, flight_day: date) -> Optional[Dict[str, object]]:
    path = CACHE_DIR / f"{_cache_key(dep, arr, flight_day)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def cache_set(dep: str, arr: str, flight_day: date, flights: List[Dict[str, object]]) -> None:
    path = CACHE_DIR / f"{_cache_key(dep, arr, flight_day)}.json"
    payload = {
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "flights": _serialize_flights(flights),
    }
    path.write_text(json.dumps(payload))


def _serialize_flights(flights: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    serialized: List[Dict[str, object]] = []
    for flight in flights:
        record = dict(flight)
        record["dep_dt"] = flight["dep_dt"].isoformat()
        record["arr_dt"] = flight["arr_dt"].isoformat()
        serialized.append(record)
    return serialized


def _deserialize_flights(payload: Dict[str, object]) -> List[Dict[str, object]]:
    flights: List[Dict[str, object]] = []
    for record in payload.get("flights", []):
        try:
            dep_dt = dtparse.parse(record["dep_dt"]).astimezone(ET_LOCAL)
            arr_dt = dtparse.parse(record["arr_dt"]).astimezone(ET_LOCAL)
        except (KeyError, ValueError, TypeError):
            continue
        restored = dict(record)
        restored["dep_dt"] = dep_dt
        restored["arr_dt"] = arr_dt
        flights.append(restored)
    return flights


# ---------------------------------------------------------------------------
# FLIGHT SOURCES
# ---------------------------------------------------------------------------
def _iata_to_icao(code: str) -> str:
    code = code.upper()
    if code in IATA_TO_ICAO:
        return IATA_TO_ICAO[code]
    if len(code) == 3:
        return "K" + code
    return code


def fetch_from_csv(dep_iata: str, arr_iata: str, flight_day: date) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    if not CSV_FALLBACK.exists():
        return results

    with CSV_FALLBACK.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)

        def col(row: Dict[str, str], *names: str) -> str:
            for name in names:
                value = row.get(name)
                if value:
                    return value
            raise KeyError(names[0])

        for row in reader:
            if row.get("dep_airport") != dep_iata or row.get("arr_airport") != arr_iata:
                continue
            if row.get("date") != flight_day.isoformat():
                continue

            try:
                dep_time = col(row, "dep_time", "dep_time_local")
                arr_time = col(row, "arr_time", "arr_time_local")
            except KeyError:
                continue

            dep_dt = _parse_local_datetime(row["date"], dep_time)
            arr_dt = _parse_local_datetime(row["date"], arr_time)
            results.append(
                {
                    "carrier": row.get("carrier", ""),
                    "flight_no": row.get("flight_no", ""),
                    "dep_airport": dep_iata,
                    "arr_airport": arr_iata,
                    "dep_dt": dep_dt,
                    "arr_dt": arr_dt,
                    "source": "CSV",
                }
            )

    if DEBUG:
        print(f"[DEBUG] CSV fetched {len(results)} flights {dep_iata}->{arr_iata} on {flight_day}")
    return results


def _parse_local_datetime(day_str: str, time_str: str) -> datetime:
    try:
        combined = datetime.strptime(f"{day_str} {time_str}", "%Y-%m-%d %H:%M")
        return combined.replace(tzinfo=ET_LOCAL)
    except ValueError:
        return dtparse.parse(f"{day_str} {time_str}").astimezone(ET_LOCAL)


class FlightAwareQuotaError(RuntimeError):
    """Raised when the stored monthly quota has been exhausted."""


def fetch_from_flightaware(dep_iata: str, arr_iata: str, flight_day: date, *, max_pages: int = 2) -> List[Dict[str, object]]:
    cached = cache_get(dep_iata, arr_iata, flight_day)
    if cached:
        flights = _deserialize_flights(cached)
        if DEBUG:
            print(
                f"[DEBUG] FlightAware cache hit {dep_iata}->{arr_iata} {flight_day}: {len(flights)} flights"
            )
        return flights

    if not FLIGHTAWARE_KEY:
        raise RuntimeError("Set FLIGHTAWARE_API_KEY for FlightAware lookups")

    if not quota_can_consume(1):
        raise FlightAwareQuotaError("FlightAware monthly cap reached")

    origin_icao = _iata_to_icao(dep_iata)
    dest_icao = _iata_to_icao(arr_iata)

    start_utc = datetime.combine(flight_day, datetime.min.time(), tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)

    next_url = (
        f"{FLIGHTAWARE_BASE}/airports/{origin_icao}/flights/scheduled"
        f"?start={start_utc.isoformat().replace('+00:00', 'Z')}"
        f"&end={end_utc.isoformat().replace('+00:00', 'Z')}"
        f"&max_pages=1"
    )

    headers = {"x-apikey": FLIGHTAWARE_KEY}
    flights: List[Dict[str, object]] = []
    pages = 0

    while next_url and pages < max_pages:
        if not quota_can_consume(1):
            if DEBUG:
                print("[DEBUG] FlightAware quota would be exceeded; stopping pagination")
            break

        response = requests.get(next_url, headers=headers, timeout=20)
        quota_consume(1)

        if response.status_code == 401:
            raise RuntimeError("FlightAware 401 Unauthorized – check FLIGHTAWARE_API_KEY")
        if response.status_code == 429:
            raise RuntimeError("FlightAware 429 Too Many Requests – rate limited")
        if response.status_code != 200:
            raise RuntimeError(f"FlightAware error {response.status_code}: {response.text}")

        payload = response.json()
        items = payload.get("scheduled") or payload.get("departures") or []

        for entry in items:
            dest_code = _extract_destination(entry)
            if dest_code and dest_code.upper() != dest_icao:
                continue

            dep_iso = _extract_time(entry, "departure")
            arr_iso = _extract_time(entry, "arrival")
            if not dep_iso or not arr_iso:
                continue

            try:
                dep_dt = dtparse.parse(dep_iso).astimezone(ET_LOCAL)
                arr_dt = dtparse.parse(arr_iso).astimezone(ET_LOCAL)
            except (ValueError, TypeError):
                continue

            carrier, flight_no = _extract_flight_identity(entry)
            flights.append(
                {
                    "carrier": carrier,
                    "flight_no": flight_no,
                    "dep_airport": dep_iata,
                    "arr_airport": arr_iata,
                    "dep_dt": dep_dt,
                    "arr_dt": arr_dt,
                    "status": entry.get("status"),
                    "source": "FA",
                }
            )

        next_url = (payload.get("links") or {}).get("next")
        pages += 1

    if flights:
        cache_set(dep_iata, arr_iata, flight_day, flights)
    if DEBUG:
        print(f"[DEBUG] FlightAware fetched {len(flights)} flights {dep_iata}->{arr_iata} on {flight_day}")
    return flights


def _extract_destination(entry: Dict[str, object]) -> Optional[str]:
    arrival = entry.get("arrival") or {}
    airport = arrival.get("airport") or {}
    code = airport.get("code") or airport.get("icao")
    if code:
        return str(code)

    for key in ("destination", "destination_icao", "destinationCode"):
        value = entry.get(key)
        if value:
            if isinstance(value, dict):
                nested = value.get("code") or value.get("icao")
                if nested:
                    return str(nested)
            else:
                return str(value)
    return None


def _extract_time(entry: Dict[str, object], field: str) -> Optional[str]:
    block = entry.get(field)
    if isinstance(block, dict):
        return block.get("scheduled") or block.get("estimated")

    legacy_map = {
        "departure": ("scheduled_out", "estimated_out"),
        "arrival": ("scheduled_in", "estimated_in"),
    }
    for key in legacy_map.get(field, ()):  # type: ignore[arg-type]
        value = entry.get(key)
        if value:
            return str(value)
    return None


def _extract_flight_identity(entry: Dict[str, object]) -> Tuple[str, str]:
    ident = entry.get("ident") or ""
    flight = entry.get("flight") or {}
    operator = entry.get("operator", "")
    carrier = (flight.get("iata") or flight.get("icao") or operator or ident[:2] or "XX")
    carrier = carrier[:3]
    number = (
        flight.get("number")
        or flight.get("iata")
        or ident[len(carrier):]
        or entry.get("flight_number")
        or "0000"
    )
    return carrier.strip(), str(number).strip()


# ---------------------------------------------------------------------------
# SCHEDULE MERGE AND PICK LOGIC
# ---------------------------------------------------------------------------
def fetch_schedules(dep_iata: str, arr_iata: str, flight_day: date) -> List[Dict[str, object]]:
    csv_flights = fetch_from_csv(dep_iata, arr_iata, flight_day) if USE_CSV_FIRST else []

    fa_flights: List[Dict[str, object]] = []
    try:
        fa_flights = fetch_from_flightaware(dep_iata, arr_iata, flight_day)
    except FlightAwareQuotaError as exc:
        print(f"[FlightAware quota] {exc}; relying on CSV data for {dep_iata}->{arr_iata} {flight_day}")
    except Exception as exc:
        print(f"[FlightAware unavailable: {exc}] -> using CSV only for {dep_iata}->{arr_iata} {flight_day}")

    merged: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for source_name, flights in (("CSV", csv_flights), ("FA", fa_flights)):
        for flight in flights:
            key = (flight.get("carrier", ""), flight.get("flight_no", ""), flight["dep_dt"].isoformat())
            # Prefer FlightAware details when duplicates exist.
            if key in merged and source_name == "CSV":
                continue
            merged[key] = flight

    combined = list(merged.values())
    if DEBUG:
        print(
            f"[DEBUG] {flight_day} {dep_iata}->{arr_iata}: CSV={len(csv_flights)} FA={len(fa_flights)} merged={len(combined)}"
        )
    return combined


def _minutes_between(late: datetime, early: datetime) -> int:
    return int((late - early).total_seconds() // 60)


def pick_inbound(report_dt: datetime) -> List[Dict[str, object]]:
    local_report = report_dt.astimezone(ET_LOCAL)
    cutoff = local_report - timedelta(minutes=INBOUND_BUFFER_MIN)
    flight_day = local_report.date()

    same_day = fetch_schedules(HOME_IATA, BASE_IATA, flight_day)
    prev_day = fetch_schedules(HOME_IATA, BASE_IATA, flight_day - timedelta(days=1))

    same_eligible = [f for f in same_day if f["arr_dt"] <= cutoff]
    prev_eligible = [f for f in prev_day if f["arr_dt"] <= cutoff]

    same_eligible.sort(key=lambda f: (f["arr_dt"], f["dep_dt"]), reverse=True)
    prev_eligible.sort(key=lambda f: (f["arr_dt"], f["dep_dt"]), reverse=True)

    picks: List[Dict[str, object]] = []
    if same_eligible:
        picks.extend(same_eligible[:MAX_INBOUND])
        if len(picks) < MAX_INBOUND:
            picks.extend(prev_eligible[: MAX_INBOUND - len(picks)])
    else:
        picks.extend(prev_eligible[:MAX_INBOUND])

    for pick in picks:
        pick["night_before"] = pick["arr_dt"].date() < flight_day
    return picks


def pick_outbound(release_dt: datetime) -> List[Dict[str, object]]:
    local_release = release_dt.astimezone(ET_LOCAL)
    cutoff = local_release + timedelta(minutes=OUTBOUND_BUFFER_MIN)
    flight_day = local_release.date()

    same_day = fetch_schedules(BASE_IATA, HOME_IATA, flight_day)
    next_day = fetch_schedules(BASE_IATA, HOME_IATA, flight_day + timedelta(days=1))

    eligible = [f for f in (same_day + next_day) if f["dep_dt"] >= cutoff]
    eligible.sort(key=lambda f: (f["dep_dt"], f["arr_dt"]))
    return eligible[:MAX_OUTBOUND]


# ---------------------------------------------------------------------------
# CALENDAR UPDATES
# ---------------------------------------------------------------------------
def upsert_commute(service, cal_id: str, base_uid: str, pick: Dict[str, object], direction: str, anchor_dt: datetime) -> None:
    title = f"{COMMUTE_PREFIX}: {pick['carrier']}{pick['flight_no']} {pick['dep_airport']}-{pick['arr_airport']}"

    if direction == "IN":
        buffer_minutes = _minutes_between(anchor_dt.astimezone(ET_LOCAL), pick["arr_dt"])
        extra = "\nNight-before option" if pick.get("night_before") else ""
        description = (
            f"Departs: {pick['dep_dt'].astimezone(ET_LOCAL).strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"Arrives: {pick['arr_dt'].astimezone(ET_LOCAL).strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"Buffer before REPORT: {buffer_minutes} minutes{extra}"
        )
        etype = "COMMUTE_IN"
        uid_tag = f"COMMUTE-IN-{base_uid}-{pick['carrier']}{pick['flight_no']}"
    else:
        buffer_minutes = _minutes_between(pick["dep_dt"], anchor_dt.astimezone(ET_LOCAL))
        description = (
            f"Departs: {pick['dep_dt'].astimezone(ET_LOCAL).strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"Arrives: {pick['arr_dt'].astimezone(ET_LOCAL).strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"Buffer after RELEASE: {buffer_minutes} minutes"
        )
        etype = "COMMUTE_OUT"
        uid_tag = f"COMMUTE-OUT-{base_uid}-{pick['carrier']}{pick['flight_no']}"

    query = (
        service.events()
        .list(
            calendarId=cal_id,
            privateExtendedProperty=f"uid={uid_tag}",
            maxResults=1,
            singleEvents=True,
        )
        .execute()
    )

    body = {
        "summary": title,
        "location": f"{pick['dep_airport']}->{pick['arr_airport']}",
        "description": description,
        "start": {"dateTime": pick["dep_dt"].isoformat()},
        "end": {"dateTime": pick["arr_dt"].isoformat()},
        "extendedProperties": {"private": {"uid": uid_tag, "type": etype}},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
    }

    if query.get("items"):
        service.events().patch(calendarId=cal_id, eventId=query["items"][0]["id"], body=body).execute()
        print(f"updated: {title}")
    else:
        service.events().insert(calendarId=cal_id, body=body).execute()
        print(f"created: {title}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main() -> None:
    service = gcal_service()
    cal_id = get_calendar_id(service)
    period, used, cap = quota_status()
    print(f"[FlightAware quota] Period {period} — used {used}/{cap}")

    duties = list_upcoming_duties(service, cal_id)
    if DEBUG:
        print(f"[DEBUG] Found {len(duties)} DUTY-like events in next {LOOKAHEAD_DAYS} days")

    if not duties:
        print("No upcoming DUTY events found.")
        return

    for index, (report_dt, release_dt, event_id) in enumerate(duties, start=1):
        if DEBUG:
            print(f"[DEBUG] Duty #{index}: REPORT {report_dt}  RELEASE {release_dt}")

        inbound = pick_inbound(report_dt)
        if DEBUG:
            print(f"[DEBUG] Inbound picks: {len(inbound)}")
            for pick in inbound:
                tag = " (night-before)" if pick.get("night_before") else ""
                print(
                    f"    {pick['carrier']}{pick['flight_no']} {pick['dep_airport']}->{pick['arr_airport']}  "
                    f"DEP {pick['dep_dt']}  ARR {pick['arr_dt']}{tag}"
                )

        if inbound:
            for pick in inbound:
                upsert_commute(service, cal_id, event_id, pick, "IN", report_dt)
        else:
            print("No inbound options that meet REPORT-60. Add night-before rows to flights.csv or check FlightAware.")

        outbound = pick_outbound(release_dt)
        if DEBUG:
            print(f"[DEBUG] Outbound picks: {len(outbound)}")
            for pick in outbound:
                print(
                    f"    {pick['carrier']}{pick['flight_no']} {pick['dep_airport']}->{pick['arr_airport']}  "
                    f"DEP {pick['dep_dt']}  ARR {pick['arr_dt']}"
                )

        if outbound:
            for pick in outbound:
                upsert_commute(service, cal_id, event_id, pick, "OUT", release_dt)
        else:
            print("No outbound options that meet RELEASE+15. Add ATL->MCO rows to flights.csv or check FlightAware.")


if __name__ == "__main__":
    main()

