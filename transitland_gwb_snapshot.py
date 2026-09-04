#!/usr/bin/env python3
"""
Pull a 24-hour snapshot of scheduled bus arrivals/departures at the
George Washington Bridge Bus Terminal (GWBBT) from the Transitland v2 REST API.

Docs: https://www.transit.land/documentation/rest-api/

Requires a free Transitland API key: https://www.transit.land/documentation/rest-api/
Set it via the TRANSITLAND_API_KEY environment variable, or pass --api-key.

Workflow
--------
1) Discover the GWBBT stop(s) in Transitland's stop index:

     python3 transitland_gwb_snapshot.py discover --search "George Washington Bridge Bus Terminal"

   This prints candidate stops (onestop_id, name, lon/lat, agencies serving them).
   GWBBT is served by NJ Transit plus several private carriers (Coach USA/Red &
   Tan, Rockland Coaches, Academy, etc.) and may be represented as more than one
   stop record (e.g. separate bay/platform stops or a parent station). Review the
   list and pick the onestop_id(s) that are actually the terminal before running
   the snapshot step -- Transitland's fuzzy name search can also surface
   unrelated "George Washington"-named stops (schools, streets, etc).

2) Pull the 24-hour snapshot for the chosen stop(s):

     python3 transitland_gwb_snapshot.py snapshot \
         --stop-id <onestop_id> [--stop-id <onestop_id> ...] \
         --date 2026-09-04 \
         --out-dir ./gwb_snapshot_2026-09-04

   This writes:
     - <out-dir>/departures_raw.csv   one row per scheduled stop_time (both
                                       arrival_time and departure_time columns)
     - <out-dir>/events.csv           melted long format: one row per event
                                       (event_type = arrival/departure), sorted
                                       by time -- this is the "buses entering
                                       and leaving" view
     - <out-dir>/raw_responses.json   the raw paginated API responses, for
                                       auditing/debugging

Notes
-----
- This pulls *scheduled* service for the given service_date, which is the
  standard way to get a full-day snapshot from Transitland for agencies that
  don't publish GTFS-RT vehicle positions (most NJ Transit-area private
  carriers don't). If a stop has GTFS-RT TripUpdates/VehiclePositions feeds
  registered in Transitland, scheduled times may be supplemented/estimated
  times in the API response, but this script does not separately query the
  realtime-only endpoints.
- GTFS service days can include times >= 24:00:00 for trips that start before
  midnight and run past it; those are still part of the requested service_date
  and are included as-is (not normalized), matching Transitland's own
  convention.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

API_BASE = "https://transit.land/api/v2/rest"


def api_get(path_or_url, api_key, params=None, max_retries=5):
    """GET a Transitland v2 REST endpoint, following the given path or a full
    pagination URL, with the API key attached and retry/backoff on 429/5xx."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{API_BASE}/{path_or_url.lstrip('/')}{query}"

    req = urllib.request.Request(url, headers={"apikey": api_key, "Accept": "application/json"})

    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SystemExit(
                    "Transitland API returned 401 Unauthorized. Check that your API "
                    "key is valid and set via --api-key or TRANSITLAND_API_KEY.\n"
                    f"Response body: {body}"
                )
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"Transitland API error {e.code} for {url}:\n{body}")
        except urllib.error.URLError as e:
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"Network error reaching Transitland API ({url}): {e}")


def paginate(path, api_key, params, item_key):
    """Yield all items from a paginated Transitland v2 endpoint, following
    meta.next until exhausted."""
    next_url = None
    page_params = dict(params)
    while True:
        data = api_get(next_url or path, api_key, None if next_url else page_params)
        for item in data.get(item_key, []):
            yield item
        next_url = (data.get("meta") or {}).get("next")
        if not next_url:
            break


def cmd_discover(args):
    api_key = require_api_key(args)
    params = {"search": args.search, "limit": 100}
    print(f"Searching Transitland stops for: {args.search!r}\n")
    count = 0
    for stop in paginate("stops", api_key, params, "stops"):
        count += 1
        agencies = sorted({
            rs.get("agency", {}).get("agency_name", "?")
            for rs in stop.get("route_stops", [])
            if rs.get("agency")
        })
        geom = stop.get("geometry", {}).get("coordinates", [None, None])
        print(f"onestop_id: {stop.get('onestop_id')}")
        print(f"  name:      {stop.get('stop_name')}")
        print(f"  lon,lat:   {geom[0]}, {geom[1]}")
        print(f"  agencies:  {', '.join(agencies) if agencies else '(none listed)'}")
        print(f"  served_by_routes: {len(stop.get('route_stops', []))}")
        print()
    if count == 0:
        print("No stops matched. Try a broader --search term.")
    else:
        print(f"{count} stop(s) found. Pick the onestop_id(s) for the terminal and "
              "pass them to the 'snapshot' subcommand with --stop-id.")


def fetch_departures(stop_id, service_date, api_key, limit=1000):
    params = {"service_date": service_date, "limit": limit}
    return list(paginate(f"stops/{stop_id}/departures", api_key, params, "stops"))


def cmd_snapshot(args):
    api_key = require_api_key(args)
    os.makedirs(args.out_dir, exist_ok=True)

    raw_all = []
    raw_rows = []
    events = []

    for stop_id in args.stop_id:
        print(f"Fetching {args.date} departures for stop {stop_id} ...")
        stop_records = fetch_departures(stop_id, args.date, api_key)
        raw_all.append({"stop_id": stop_id, "response_stops": stop_records})

        for stop_record in stop_records:
            stop_name = stop_record.get("stop_name")
            stop_onestop_id = stop_record.get("onestop_id", stop_id)
            for dep in stop_record.get("departures", []):
                trip = dep.get("trip", {}) or {}
                route = trip.get("route", {}) or {}
                agency = route.get("agency", {}) or {}
                arrival_time = dep.get("arrival", {}).get("scheduled") if isinstance(dep.get("arrival"), dict) else dep.get("arrival_time")
                departure_time = dep.get("departure", {}).get("scheduled") if isinstance(dep.get("departure"), dict) else dep.get("departure_time")

                row = {
                    "service_date": args.date,
                    "stop_onestop_id": stop_onestop_id,
                    "stop_name": stop_name,
                    "agency_name": agency.get("agency_name"),
                    "agency_onestop_id": agency.get("onestop_id"),
                    "route_onestop_id": route.get("onestop_id"),
                    "route_short_name": route.get("route_short_name"),
                    "route_long_name": route.get("route_long_name"),
                    "route_type": route.get("route_type"),
                    "trip_id": trip.get("trip_id") or trip.get("id"),
                    "trip_headsign": dep.get("trip_headsign") or trip.get("trip_headsign"),
                    "direction_id": trip.get("direction_id"),
                    "arrival_time": arrival_time,
                    "departure_time": departure_time,
                }
                raw_rows.append(row)

                if arrival_time:
                    events.append({**row, "event_time": arrival_time, "event_type": "arrival (entering terminal)"})
                if departure_time:
                    events.append({**row, "event_time": departure_time, "event_type": "departure (leaving terminal)"})

    events.sort(key=lambda r: (r["event_time"] or "", r["stop_name"] or ""))

    raw_csv_path = os.path.join(args.out_dir, "departures_raw.csv")
    events_csv_path = os.path.join(args.out_dir, "events.csv")
    raw_json_path = os.path.join(args.out_dir, "raw_responses.json")

    write_csv(raw_csv_path, raw_rows, [
        "service_date", "stop_onestop_id", "stop_name", "agency_name", "agency_onestop_id",
        "route_onestop_id", "route_short_name", "route_long_name", "route_type",
        "trip_id", "trip_headsign", "direction_id", "arrival_time", "departure_time",
    ])
    write_csv(events_csv_path, events, [
        "event_time", "event_type", "service_date", "stop_onestop_id", "stop_name",
        "agency_name", "route_short_name", "route_long_name", "trip_headsign", "trip_id",
    ])
    with open(raw_json_path, "w") as f:
        json.dump(raw_all, f, indent=2)

    n_arrivals = sum(1 for e in events if e["event_type"].startswith("arrival"))
    n_departures = sum(1 for e in events if e["event_type"].startswith("departure"))
    print()
    print(f"Wrote {len(raw_rows)} scheduled stop_time record(s) across {len(args.stop_id)} stop(s).")
    print(f"  arrivals (entering):  {n_arrivals}")
    print(f"  departures (leaving): {n_departures}")
    print(f"  -> {raw_csv_path}")
    print(f"  -> {events_csv_path}")
    print(f"  -> {raw_json_path}")


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def require_api_key(args):
    api_key = args.api_key or os.environ.get("TRANSITLAND_API_KEY")
    if not api_key:
        raise SystemExit(
            "No Transitland API key found. Get a free key at "
            "https://www.transit.land/documentation/rest-api/ and pass it via "
            "--api-key or the TRANSITLAND_API_KEY environment variable."
        )
    return api_key


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key", help="Transitland API key (defaults to TRANSITLAND_API_KEY env var)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Search for candidate GWBBT stop records")
    p_discover.add_argument("--search", default="George Washington Bridge Bus Terminal",
                             help="Text to search stop names for")
    p_discover.set_defaults(func=cmd_discover)

    p_snapshot = sub.add_parser("snapshot", help="Pull a 24-hour scheduled departures/arrivals snapshot")
    p_snapshot.add_argument("--stop-id", action="append", required=True,
                             help="Transitland stop onestop_id (repeat for multiple stops)")
    p_snapshot.add_argument("--date", default=date.today().isoformat(),
                             help="Service date, YYYY-MM-DD (default: today)")
    p_snapshot.add_argument("--out-dir", default="gwb_snapshot", help="Output directory")
    p_snapshot.set_defaults(func=cmd_snapshot)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
