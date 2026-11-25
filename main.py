import math
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"},
    )


def fetch_tdx_json(url: str, headers: dict, retries: int = 3):
    for attempt in range(retries + 1):
        time.sleep(0.35)
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 429 and attempt < retries:
            time.sleep(2.0 * (attempt + 1))
            continue
        res.raise_for_status()
        return res.json()


CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")
TW_TZ = timezone(timedelta(hours=8))

token_cache = {"access_token": None, "expires_at": 0}
route_cache = {}
timetable_cache = {}
schedule_cache = {}
stops_cache = {}
stop_routes_cache = {}
city_network_cache = {}
city_stops_geo_cache = {}
youbike_station_cache = {}


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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


def get_stops_cached(city: str, route_name: str, headers: dict):
    key = f"{city}_{route_name}"
    if key in stops_cache:
        return stops_cache[key]
    data = fetch_tdx_json(build_tdx_url("StopOfRoute", city, route_name), headers)
    stops_cache[key] = data
    return data


def get_timetable_cached(city: str, route: str, headers: dict):
    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    key = f"{city}_{route}_{today}"
    if key in timetable_cache:
        return timetable_cache[key]
    time.sleep(0.35)
    res = requests.get(
        build_tdx_url("DailyTimetable", city, route), headers=headers, timeout=10
    )
    data = res.json() if res.status_code == 200 else []
    timetable_cache[key] = data
    return data


def get_city_network_cached(city: str, headers: dict):
    now_ts = time.time()
    cached = city_network_cache.get(city)
    if cached and now_ts - cached["ts"] < 86400:
        return cached["data"]

    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/StopOfRoute/City/{city}?$select=RouteName,Direction,Stops&$format=JSON"
    raw_list = fetch_tdx_json(url, headers)

    route_dir_stops = {}
    stop_to_routes = {}

    for item in raw_list:
        r_name = item.get("RouteName", {}).get("Zh_tw", "")
        direction = item.get("Direction", 0)
        if not r_name:
            continue
        stops_ordered = [
            s.get("StopName", {}).get("Zh_tw", "")
            for s in item.get("Stops", [])
            if s.get("StopName", {}).get("Zh_tw")
        ]
        if not stops_ordered:
            continue
        rd_key = (r_name, direction)
        if rd_key not in route_dir_stops:
            route_dir_stops[rd_key] = stops_ordered
            for idx, st_name in enumerate(stops_ordered):
                stop_to_routes.setdefault(st_name, []).append((r_name, direction, idx))

    network = {
        "route_dir_stops": route_dir_stops,
        "stop_to_routes": stop_to_routes,
    }
    city_network_cache[city] = {"ts": now_ts, "data": network}
    return network


def get_city_stops_geo_cached(city: str, headers: dict):
    """快取單一縣市所有公車站牌名稱與經緯度（24 小時）"""
    now_ts = time.time()
    cached = city_stops_geo_cache.get(city)
    if cached and now_ts - cached["ts"] < 86400:
        return cached["data"]

    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/City/{city}?$select=StopName,StopPosition&$format=JSON"
    raw_stops = fetch_tdx_json(url, headers)

    grouped = {}
    for st in raw_stops:
        name = st.get("StopName", {}).get("Zh_tw", "")
        pos = st.get("StopPosition", {})
        lat = pos.get("PositionLat")
        lon = pos.get("PositionLon")
        if not name or lat is None or lon is None:
            continue
        if name not in grouped:
            grouped[name] = {"name": name, "lat": float(lat), "lon": float(lon)}

    stops_list = list(grouped.values())
    city_stops_geo_cache[city] = {"ts": now_ts, "data": stops_list}
    return stops_list


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Taiwan Bus API is running"}


@app.get("/api/bus/accurate/{city}/{route_name}")
def get_accurate_bus(city: str, route_name: str):
    cache_key = f"{city}_{route_name}"
    now_ts = time.time()
    now_tw = datetime.now(TW_TZ)

    if cache_key in route_cache and now_ts - route_cache[cache_key]["ts"] < 25:
        return route_cache[cache_key]["payload"]

    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}

    stops_res = get_stops_cached(city, route_name, headers)
    eta_res = fetch_tdx_json(
        build_tdx_url("EstimatedTimeOfArrival", city, route_name), headers
    )
    near_res = fetch_tdx_json(
        build_tdx_url("RealTimeNearStop", city, route_name), headers
    )
    tt_res = get_timetable_cached(city, route_name, headers)

    vehicles = {0: [], 1: []}
    for v in near_res:
        d = v.get("Direction")
        if d in vehicles and v.get("DutyStatus") != 2:
            vehicles[d].append(
                {
                    "plate": v.get("PlateNumb", "行駛中"),
                    "seq": v.get("StopSequence", 1),
                    "stopName": v.get("StopName", {}).get("Zh_tw", ""),
                }
            )

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
        comp_sec = (
            max(0, raw_sec - age) if (status == 0 and raw_sec is not None) else None
        )
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
                    missed_list.append(
                        f"{route_name}路({dir_label}) 表定 {dep_str} 班次已逾時 {diff_min} 分鐘未發車"
                    )

    dirs_result = {0: [], 1: []}
    for r in stops_res:
        d = r.get("Direction")
        if d not in dirs_result or dirs_result[d]:
            continue
        seq_eta = eta_map[d]
        active_buses = vehicles[d]

        for s in r.get("Stops", []):
            seq = s["StopSequence"]
            name = s["StopName"]["Zh_tw"]
            base = seq_eta.get(seq, {"status": 1, "sec": None})
            buses_here = [b["plate"] for b in active_buses if b["seq"] == seq]

            approaching = sorted(
                [b for b in active_buses if b["seq"] < seq],
                key=lambda x: x["seq"],
                reverse=True,
            )
            timeline = []
            for idx, bus in enumerate(approaching):
                if idx == 0 and base["status"] == 0 and base["sec"] is not None:
                    rem = base["sec"]
                else:
                    b_eta = seq_eta.get(bus["seq"], {}).get("sec") or (
                        (bus["seq"] - 1) * 90
                    )
                    t_eta = base["sec"] or ((seq - 1) * 90)
                    rem = max(60, t_eta - b_eta)
                timeline.append(
                    {
                        "plate": bus["plate"],
                        "currentStop": bus["stopName"],
                        "remSec": rem,
                    }
                )

            if not timeline and base["status"] == 0 and base["sec"] is not None:
                timeline.append(
                    {
                        "plate": "即將發車",
                        "currentStop": "起站準備中",
                        "remSec": base["sec"],
                    }
                )

            dirs_result[d].append(
                {
                    "seq": seq,
                    "name": name,
                    "status": base["status"],
                    "busesHere": buses_here,
                    "buses": timeline,
                }
            )

    payload = {
        "0": dirs_result[0],
        "1": dirs_result[1],
        "missedAlerts": missed_list,
        "maxLatencySec": max_latency,
    }
    route_cache[cache_key] = {"ts": now_ts, "payload": payload}
    return payload


@app.get("/api/bus/timetable/{city}/{route_name}")
def get_route_full_timetable(city: str, route_name: str):
    """取得每週每日（平日／假日）完整每班發車時刻表與發車班距"""
    cache_key = f"{city}_schedule_{route_name}"
    now_ts = time.time()
    if cache_key in schedule_cache and now_ts - schedule_cache[cache_key]["ts"] < 3600:
        return schedule_cache[cache_key]["payload"]

    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}
    url = build_tdx_url("Schedule", city, route_name)
    res = requests.get(url, headers=headers, timeout=15)
    raw_schedules = res.json() if res.status_code == 200 else []

    day_keys = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    result = {
        "0": {d: {"times": [], "frequencies": []} for d in day_keys},
        "1": {d: {"times": [], "frequencies": []} for d in day_keys},
    }

    for item in raw_schedules:
        d_str = str(item.get("Direction", 0))
        if d_str not in result:
            continue

        # 固定班表 Timetables
        for tt in item.get("Timetables", []):
            service_day = tt.get("ServiceDay", {})
            st_list = tt.get("StopTimes", [])
            if not st_list:
                continue
            dep_time = st_list[0].get("DepartureTime", "")
            if not dep_time:
                continue
            for dk in day_keys:
                if service_day.get(dk, 0) == 1:
                    if dep_time not in result[d_str][dk]["times"]:
                        result[d_str][dk]["times"].append(dep_time)

        # 班距型路線 Frequencys
        for fq in item.get("Frequencys", []):
            service_day = fq.get("ServiceDay", {})
            start_t = fq.get("StartTime", "")
            end_t = fq.get("EndTime", "")
            min_h = fq.get("MinHeadwayMins", "")
            max_h = fq.get("MaxHeadwayMins", "")
            if not start_t or not end_t:
                continue
            headway_text = (
                f"{min_h} 分"
                if min_h == max_h
                else f"{min_h}～{max_h} 分"
            )
            desc = f"{start_t} - {end_t}（約 {headway_text}一班）"
            for dk in day_keys:
                if service_day.get(dk, 0) == 1:
                    if desc not in result[d_str][dk]["frequencies"]:
                        result[d_str][dk]["frequencies"].append(desc)

    # 若 Schedule 無資料，備援讀取今日 DailyTimetable
    has_any = any(
        result[d][dk]["times"] or result[d][dk]["frequencies"]
        for d in ["0", "1"]
        for dk in day_keys
    )
    if not has_any:
        daily = get_timetable_cached(city, route_name, headers)
        for item in daily:
            d_str = str(item.get("Direction", 0))
            if d_str not in result:
                continue
            for tt in item.get("Timetables", []):
                st_list = tt.get("StopTimes", [])
                if not st_list:
                    continue
                dep_time = st_list[0].get("DepartureTime", "")
                if dep_time:
                    for dk in day_keys:
                        if dep_time not in result[d_str][dk]["times"]:
                            result[d_str][dk]["times"].append(dep_time)

    for d in ["0", "1"]:
        for dk in day_keys:
            result[d][dk]["times"].sort()

    payload = {"routeName": route_name, "directions": result}
    schedule_cache[cache_key] = {"ts": now_ts, "payload": payload}
    return payload


@app.get("/api/bus/stop/{city}/{stop_name}")
def get_other_routes_at_stop(city: str, stop_name: str):
    cache_key = f"{city}_stop_{stop_name}"
    now_ts = time.time()
    now_tw = datetime.now(TW_TZ)

    if cache_key in stop_routes_cache and now_ts - stop_routes_cache[cache_key]["ts"] < 20:
        return stop_routes_cache[cache_key]["payload"]

    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}
    escaped_name = stop_name.replace("'", "''")
    filter_expr = quote(f"StopName/Zh_tw eq '{escaped_name}'")
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/{city}?$filter={filter_expr}&$format=JSON"

    eta_list = fetch_tdx_json(url, headers)

    grouped = {}
    for item in eta_list:
        r_name = item.get("RouteName", {}).get("Zh_tw", "")
        if not r_name:
            continue
        direction = item.get("Direction", 0)
        status = item.get("StopStatus", 1)
        raw_sec = item.get("EstimateTime")
        age = 0
        if item.get("SrcUpdateTime"):
            try:
                src_dt = datetime.fromisoformat(item["SrcUpdateTime"])
                age = max(0, int((now_tw - src_dt).total_seconds()))
            except ValueError:
                pass
        comp_sec = (
            max(0, raw_sec - age) if (status == 0 and raw_sec is not None) else None
        )

        key = f"{r_name}_{direction}"
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "routeName": r_name,
                "direction": direction,
                "status": status,
                "remSec": comp_sec,
            }
        else:
            if comp_sec is not None and (
                existing["remSec"] is None or comp_sec < existing["remSec"]
            ):
                existing["status"] = 0
                existing["remSec"] = comp_sec

    def sort_key(x):
        if x["status"] == 0 and x["remSec"] is not None:
            return (0, x["remSec"], x["routeName"])
        return (1, 999999, x["routeName"])

    routes_sorted = sorted(grouped.values(), key=sort_key)
    payload = {"stopName": stop_name, "routes": routes_sorted}
    stop_routes_cache[cache_key] = {"ts": now_ts, "payload": payload}
    return payload


@app.get("/api/bus/nearby/{city}")
def get_nearby_bus_stops(city: str, lat: float, lon: float, radius: int = 450):
    """根據 GPS 經緯度找出方圓 radius 公尺內最近的公車站牌"""
    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}
    stops_geo = get_city_stops_geo_cached(city, headers)

    nearby = []
    for st in stops_geo:
        dist = haversine_meters(lat, lon, st["lat"], st["lon"])
        if dist <= radius:
            nearby.append(
                {
                    "name": st["name"],
                    "distanceMeters": int(round(dist)),
                    "lat": st["lat"],
                    "lon": st["lon"],
                }
            )

    nearby.sort(key=lambda x: x["distanceMeters"])
    return {"stops": nearby[:12]}


@app.get("/api/youbike/nearby/{city}")
def get_nearby_youbike(city: str, stop_name: str, radius: int = 300):
    """查詢指定公車站牌方圓 300 公尺內的 YouBike 2.0 即時可借／可還數量"""
    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}
    stops_geo = get_city_stops_geo_cached(city, headers)

    target = next((s for s in stops_geo if s["name"] == stop_name), None)
    if target is None:
        target = next(
            (s for s in stops_geo if stop_name in s["name"] or s["name"] in stop_name),
            None,
        )
    if target is None:
        return {"stopName": stop_name, "stations": []}

    # 站點基本資料快取 24 小時
    now_ts = time.time()
    cached_st = youbike_station_cache.get(city)
    if cached_st and now_ts - cached_st["ts"] < 86400:
        stations_map = cached_st["data"]
    else:
        st_url = f"https://tdx.transportdata.tw/api/basic/v2/Bike/Station/City/{city}?$select=StationUID,StationName,StationPosition&$format=JSON"
        st_res = requests.get(st_url, headers=headers, timeout=15)
        raw_st = st_res.json() if st_res.status_code == 200 else []
        stations_map = {}
        for item in raw_st:
            uid = item.get("StationUID")
            pos = item.get("StationPosition", {})
            if uid and pos.get("PositionLat") and pos.get("PositionLon"):
                stations_map[uid] = {
                    "uid": uid,
                    "name": item.get("StationName", {})
                    .get("Zh_tw", "")
                    .replace("YouBike2.0_", ""),
                    "lat": float(pos["PositionLat"]),
                    "lon": float(pos["PositionLon"]),
                }
        youbike_station_cache[city] = {"ts": now_ts, "data": stations_map}

    # 篩選方圓 radius 公尺內的 YouBike 站點
    candidates = {}
    for uid, info in stations_map.items():
        dist = haversine_meters(target["lat"], target["lon"], info["lat"], info["lon"])
        if dist <= radius:
            candidates[uid] = {**info, "distanceMeters": int(round(dist))}

    if not candidates:
        return {"stopName": stop_name, "stations": []}

    # 即時抓取可借可還車位
    av_url = f"https://tdx.transportdata.tw/api/basic/v2/Bike/Availability/City/{city}?$select=StationUID,AvailableRentBikes,AvailableReturnBikes,ServiceStatus&$format=JSON"
    av_res = requests.get(av_url, headers=headers, timeout=15)
    raw_av = av_res.json() if av_res.status_code == 200 else []

    results = []
    for av in raw_av:
        uid = av.get("StationUID")
        if uid in candidates:
            info = candidates[uid]
            results.append(
                {
                    "name": info["name"],
                    "distanceMeters": info["distanceMeters"],
                    "rentBikes": av.get("AvailableRentBikes", 0),
                    "returnSpaces": av.get("AvailableReturnBikes", 0),
                    "serviceStatus": av.get("ServiceStatus", 1),
                }
            )

    results.sort(key=lambda x: x["distanceMeters"])
    return {"stopName": stop_name, "stations": results[:8]}


@app.get("/api/bus/transfer/{city}")
def get_transfer_alternatives(
    city: str, origin: str, destination: str, exclude_route: str = ""
):
    """當錯過原本公車時，尋找從 origin 到 destination 的其他直達與一次轉乘替代方案"""
    headers = {"authorization": f"Bearer {get_token()}", "Accept-Encoding": "gzip"}
    network = get_city_network_cached(city, headers)
    route_dir_stops = network["route_dir_stops"]
    stop_to_routes = network["stop_to_routes"]

    origin_clean = origin.strip()
    dest_clean = destination.strip()

    matched_dest_stops = [
        s for s in stop_to_routes.keys() if dest_clean in s or s in dest_clean
    ]
    if dest_clean in stop_to_routes:
        matched_dest_stops = [dest_clean]

    origin_entries = stop_to_routes.get(origin_clean, [])
    if not origin_entries or not matched_dest_stops:
        return {
            "origin": origin_clean,
            "destination": dest_clean,
            "direct": [],
            "transfers": [],
        }

    eta_lookup = {}
    try:
        escaped_origin = origin_clean.replace("'", "''")
        filter_expr = quote(f"StopName/Zh_tw eq '{escaped_origin}'")
        eta_url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/{city}?$filter={filter_expr}&$format=JSON"
        eta_list = fetch_tdx_json(eta_url, headers)
        now_tw = datetime.now(TW_TZ)
        for item in eta_list:
            rn = item.get("RouteName", {}).get("Zh_tw", "")
            d = item.get("Direction", 0)
            st = item.get("StopStatus", 1)
            raw_sec = item.get("EstimateTime")
            age = 0
            if item.get("SrcUpdateTime"):
                try:
                    src_dt = datetime.fromisoformat(item["SrcUpdateTime"])
                    age = max(0, int((now_tw - src_dt).total_seconds()))
                except ValueError:
                    pass
            comp = max(0, raw_sec - age) if (st == 0 and raw_sec is not None) else None
            k = (rn, d)
            if k not in eta_lookup or (
                comp is not None
                and (eta_lookup[k]["remSec"] is None or comp < eta_lookup[k]["remSec"])
            ):
                eta_lookup[k] = {"status": st, "remSec": comp}
    except Exception:
        pass

    direct_options = []
    seen_direct = set()
    for r_name, direction, o_idx in origin_entries:
        if exclude_route and r_name == exclude_route:
            continue
        stops_seq = route_dir_stops.get((r_name, direction), [])
        for d_idx in range(o_idx + 1, len(stops_seq)):
            if stops_seq[d_idx] in matched_dest_stops:
                key = (r_name, direction)
                if key in seen_direct:
                    break
                seen_direct.add(key)
                eta_info = eta_lookup.get(key, {"status": 1, "remSec": None})
                direct_options.append(
                    {
                        "routeName": r_name,
                        "direction": direction,
                        "stopsCount": d_idx - o_idx,
                        "targetStop": stops_seq[d_idx],
                        "status": eta_info["status"],
                        "remSec": eta_info["remSec"],
                    }
                )
                break

    direct_options.sort(
        key=lambda x: (
            0 if (x["status"] == 0 and x["remSec"] is not None) else 1,
            x["remSec"] if x["remSec"] is not None else 999999,
            x["stopsCount"],
        )
    )

    dest_route_map = {}
    for d_stop in matched_dest_stops:
        for r2, dir2, d2_idx in stop_to_routes.get(d_stop, []):
            stops2 = route_dir_stops.get((r2, dir2), [])
            for t_idx in range(0, d2_idx):
                t_stop = stops2[t_idx]
                if t_stop == origin_clean:
                    continue
                existing = dest_route_map.get((t_stop, r2, dir2))
                leg2_stops = d2_idx - t_idx
                if existing is None or leg2_stops < existing["leg2Stops"]:
                    dest_route_map[(t_stop, r2, dir2)] = {
                        "route2": r2,
                        "dir2": dir2,
                        "leg2Stops": leg2_stops,
                        "targetStop": d_stop,
                    }

    seen_transfer_pair = {}
    for r1, dir1, o1_idx in origin_entries:
        if exclude_route and r1 == exclude_route:
            continue
        stops1 = route_dir_stops.get((r1, dir1), [])
        eta1 = eta_lookup.get((r1, dir1), {"status": 1, "remSec": None})
        for t1_idx in range(o1_idx + 1, min(len(stops1), o1_idx + 25)):
            t_stop = stops1[t1_idx]
            leg1_stops = t1_idx - o1_idx
            for (cand_stop, r2, dir2), info2 in dest_route_map.items():
                if cand_stop != t_stop or r2 == r1:
                    continue
                pair_key = (r1, dir1, r2, dir2)
                total_stops = leg1_stops + info2["leg2Stops"]
                cand = {
                    "route1": r1,
                    "dir1": dir1,
                    "status1": eta1["status"],
                    "remSec1": eta1["remSec"],
                    "transferStop": t_stop,
                    "leg1Stops": leg1_stops,
                    "route2": r2,
                    "dir2": dir2,
                    "leg2Stops": info2["leg2Stops"],
                    "targetStop": info2["targetStop"],
                    "totalStops": total_stops,
                }
                if (
                    pair_key not in seen_transfer_pair
                    or total_stops < seen_transfer_pair[pair_key]["totalStops"]
                ):
                    seen_transfer_pair[pair_key] = cand

    transfers_sorted = sorted(
        seen_transfer_pair.values(),
        key=lambda x: (
            0 if (x["status1"] == 0 and x["remSec1"] is not None) else 1,
            x["remSec1"] if x["remSec1"] is not None else 999999,
            x["totalStops"],
        ),
    )[:12]

    return {
        "origin": origin_clean,
        "destination": dest_clean,
        "direct": direct_options[:10],
        "transfers": transfers_sorted,
    }
