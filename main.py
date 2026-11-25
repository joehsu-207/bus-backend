import os
import time
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")
TW_TZ = timezone(timedelta(hours=8))

token_cache = {"access_token": None, "expires_at": 0}
route_cache = {}
timetable_cache = {}


def get_token():
    now = time.time()
    if token_cache["access_token"] and now < token_cache["expires_at"]:
        return token_cache["access_token"]
    res = requests.post(
        "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    token_cache["access_token"] = data["access_token"]
    token_cache["expires_at"] = now + data.get("expires_in", 86400) - 60
    return token_cache["access_token"]


def build_tdx_url(endpoint: str, city: str, route: str):
    if city == "InterCity":
        return f"https://tdx.transportdata.tw/api/basic/v2/Bus/{endpoint}/InterCity/{route}?$filter=RouteName/Zh_tw eq '{route}'&$format=JSON"
    return f"https://tdx.transportdata.tw/api/basic/v2/Bus/{endpoint}/City/{city}/{route}?$filter=RouteName/Zh_tw eq '{route}'&$format=JSON"


def get_timetable_cached(city: str, route: str, headers: dict):
    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    key = f"{city}_{route}_{today}"
    if key in timetable_cache:
        return timetable_cache[key]
    res = requests.get(build_tdx_url("DailyTimeTable", city, route), headers=headers, timeout=10)
    data = res.json() if res.status_code == 200 else []
    timetable_cache[key] = data
    return data


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Taiwan Bus API is running"}


@app.get("/api/bus/accurate/{city}/{route_name}")
def get_accurate_bus(city: str, route_name: str):
    cache_key = f"{city}_{route_name}"
    now_ts = time.time()
    now_tw = datetime.now(TW_TZ)

    if cache_key in route_cache and now_ts - route_cache[cache_key]["ts"] < 10:
        return route_cache[cache_key]["payload"]

    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}

    stops_res = requests.get(build_tdx_url("StopOfRoute", city, route_name), headers=headers).json()
    eta_res = requests.get(build_tdx_url("EstimatedTimeOfArrival", city, route_name), headers=headers).json()
    near_res = requests.get(build_tdx_url("RealTimeNearStop", city, route_name), headers=headers).json()
    tt_res = get_timetable_cached(city, route_name, headers)

    vehicles = {0: [], 1: []}
    for v in near_res:
        d = v.get("Direction")
        if d in vehicles and v.get("DutyStatus") != 2:
            vehicles[d].append({
                "plate": v.get("PlateNumb", "行駛中"),
                "seq": v.get("StopSequence", 1),
                "stopName": v.get("StopName", {}).get("Zh_tw", ""),
            })

    eta_map = {0: {}, 1: {}}
    max_latency = 0
    for item in eta_res:
        d = item.get("Direction")
        seq = item.get("StopSequence")
        if d not in eta_map or seq is None:
            continue
        raw_sec = item.get("EstimateTime")
        status = item.get("StopStatus", 1)
        age = 0
        if item.get("SrcUpdateTime"):
            try:
                src_dt = datetime.fromisoformat(item["SrcUpdateTime"])
                age = max(0, int((now_tw - src_dt).total_seconds()))
                max_latency = max(max_latency, age)
            except ValueError:
                pass
        comp_sec = max(0, raw_sec - age) if (status == 0 and raw_sec is not None) else None
        eta_map[d][seq] = {"status": status, "sec": comp_sec}

    missed_list = []
    for rtt in tt_res:
        d = rtt.get("Direction", 0)
        dir_label = "去程" if d == 0 else "返程"
        for trip in rtt.get("Timetables", []):
            st = trip.get("StopTimes", [])
            if not st:
                continue
            dep_str = st[0].get("DepartureTime", "")
            if ":" not in dep_str:
                continue
            hh, mm = map(int, dep_str.split(":"))
            dep_dt = now_tw.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff_min = int((now_tw - dep_dt).total_seconds() // 60)
            if 5 <= diff_min <= 25:
                if not any(b["seq"] <= 4 for b in vehicles.get(d, [])):
                    missed_list.append(f"{route_name}路({dir_label}) 表定 {dep_str} 班次已逾時 {diff_min} 分鐘未發車")

    dirs_result = {0: [], 1: []}
    for r in stops_res:
        d = r.get("Direction")
        if d not in dirs_result or dirs_result[d]:
            continue
        seq_eta = eta_map[d]
        active_buses = vehicles[d]

        for s in r.get("Stops", []):
            seq = s.get("StopSequence")
            name = s["StopName"]["Zh_tw"]
            base = seq_eta.get(seq, {"status": 1, "sec": None})
            buses_here = [b["plate"] for b in active_buses if b["seq"] == seq]

            approaching = sorted([b for b in active_buses if b["seq"] <= seq], key=lambda x: x["seq"], reverse=True)
            timeline = []
            for idx, bus in enumerate(approaching):
                if idx == 0 and base["status"] == 0 and base["sec"] is not None:
                    rem = base["sec"]
                else:
                    b_eta = seq_eta.get(bus["seq"], {}).get("sec") or ((bus["seq"] - 1) * 85)
                    t_eta = base["sec"] or ((seq - 1) * 85)
                    rem = max(30, t_eta - b_eta)
                timeline.append({"plate": bus["plate"], "currentStop": bus["stopName"], "remSec": rem})

            if not timeline and base["status"] == 0 and base["sec"] is not None:
                timeline.append({"plate": "即將發車", "currentStop": "總站準備中", "remSec": base["sec"]})

            dirs_result[d].append({
                "seq": seq,
                "name": name,
                "status": base["status"],
                "busesHere": buses_here,
                "buses": timeline,
            })

    payload = {"0": dirs_result[0], "1": dirs_result[1], "missedAlerts": missed_list, "maxLatencySec": max_latency}
    route_cache[cache_key] = {"ts": now_ts, "payload": payload}
    return payload