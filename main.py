import os
import time
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Taiwan Bus Full Precise API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TW_TZ = timezone(timedelta(hours=8))

TDX_CLIENT_ID = os.getenv("TDX_CLIENT_ID", "你的_TDX_CLIENT_ID")
TDX_CLIENT_SECRET = os.getenv("TDX_CLIENT_SECRET", "你的_TDX_CLIENT_SECRET")
TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus"

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_api_cache: Dict[str, Dict[str, Any]] = {}

REALTIME_CACHE_TTL = 15
STATIC_CACHE_TTL = 3600


async def get_tdx_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            TDX_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": TDX_CLIENT_ID,
                "client_secret": TDX_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"TDX Token 取得失敗: {resp.text}")
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 86400)
        return _token_cache["access_token"]


async def fetch_tdx_json(url: str, ttl: int = REALTIME_CACHE_TTL) -> Any:
    now = time.time()
    if url in _api_cache and (now - _api_cache[url]["timestamp"] < ttl):
        return _api_cache[url]["data"]

    token = await get_tdx_token()
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
            },
        )
        if resp.status_code == 429 and url in _api_cache:
            return _api_cache[url]["data"]
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"TDX API 錯誤: {resp.text}")

        data = resp.json()
        _api_cache[url] = {"timestamp": now, "data": data}
        return data


def parse_iso_tw(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str).astimezone(TW_TZ)
    except ValueError:
        return None


def compute_precise_timing(
    item: Dict[str, Any],
    now: datetime,
    is_at_stop: bool = False,
) -> Dict[str, Any]:
    stop_status = item.get("StopStatus", 0)
    estimate_sec = item.get("EstimateTime")
    src_dt = parse_iso_tw(item.get("SrcUpdateTime")) or parse_iso_tw(item.get("UpdateTime"))
    elapsed_sec = max(0, (now - src_dt).total_seconds()) if src_dt else 0

    remaining_sec: Optional[int] = None
    exact_arrival_iso: Optional[str] = None
    exact_arrival_hhmm: Optional[str] = None
    status_label = "未發車"

    if stop_status == 0:
        if is_at_stop:
            remaining_sec = 0
            exact_arrival_iso = now.isoformat()
            exact_arrival_hhmm = now.strftime("%H:%M")
            status_label = "進站中"
        elif estimate_sec is not None:
            remaining_sec = max(0, int(estimate_sec - elapsed_sec))
            arrival_dt = now + timedelta(seconds=remaining_sec)
            exact_arrival_iso = arrival_dt.isoformat()
            exact_arrival_hhmm = arrival_dt.strftime("%H:%M")
            if remaining_sec <= 25:
                status_label = "進站中"
            elif remaining_sec <= 90:
                status_label = "即將進站"
            else:
                status_label = "行駛中"
    elif stop_status == 1:
        next_bus_dt = parse_iso_tw(item.get("NextBusTime"))
        if next_bus_dt:
            remaining_sec = max(0, int((next_bus_dt - now).total_seconds()))
            exact_arrival_iso = next_bus_dt.isoformat()
            exact_arrival_hhmm = next_bus_dt.strftime("%H:%M")
            status_label = f"{exact_arrival_hhmm}發車"
        else:
            status_label = "尚未發車"
    elif stop_status == 2:
        status_label = "交管不停靠"
    elif stop_status == 3:
        status_label = "末班車已過"
    elif stop_status == 4:
        status_label = "今日未營運"

    return {
        "stop_status": stop_status,
        "status_label": status_label,
        "remaining_sec": remaining_sec,
        "exact_arrival_iso": exact_arrival_iso,
        "exact_arrival_hhmm": exact_arrival_hhmm,
        "src_update_time": src_dt.strftime("%H:%M:%S") if src_dt else "",
    }


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return int(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


@app.get("/")
def health_check():
    return {"status": "ok", "server_time": datetime.now(TW_TZ).isoformat()}


@app.get("/api/bus/routes")
async def search_bus_routes(
    city: str = Query("Taichung", description="縣市代碼，例如 Taichung, Taipei"),
    keyword: str = Query("", description="路線關鍵字"),
):
    url = f"{TDX_BASE_URL}/Route/City/{city}?$format=JSON"
    if keyword.strip():
        url += f"&$filter=contains(RouteName/Zh_tw, '{keyword.strip()}')"

    raw_routes = await fetch_tdx_json(url, ttl=STATIC_CACHE_TTL)
    results = []
    for r in raw_routes:
        results.append({
            "route_uid": r.get("RouteUID", ""),
            "route_name": r.get("RouteName", {}).get("Zh_tw", ""),
            "departure_stop": r.get("DepartureStopNameZh", ""),
            "destination_stop": r.get("DestinationStopNameZh", ""),
            "city": city,
        })
    return {"city": city, "routes": results}


@app.get("/api/bus/realtime")
async def get_precise_bus_realtime(
    city: str = Query(..., description="縣市英文代碼，例如 Taichung"),
    route_name: str = Query(..., description="路線名稱，例如 54, 358"),
    direction: int = Query(0, description="0=去程, 1=返程"),
):
    now = datetime.now(TW_TZ)

    eta_url = (
        f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}' and Direction eq {direction}&$format=JSON"
    )
    near_url = (
        f"{TDX_BASE_URL}/RealTimeNearStop/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}' and Direction eq {direction}&$format=JSON"
    )
    stop_of_route_url = (
        f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}' and Direction eq {direction}&$format=JSON"
    )

    eta_list = await fetch_tdx_json(eta_url, ttl=REALTIME_CACHE_TTL)
    near_list = await fetch_tdx_json(near_url, ttl=REALTIME_CACHE_TTL)
    sor_list = await fetch_tdx_json(stop_of_route_url, ttl=STATIC_CACHE_TTL)

    ordered_stops: List[Dict[str, Any]] = []
    if sor_list and len(sor_list) > 0:
        for s in sor_list[0].get("Stops", []):
            ordered_stops.append({
                "stop_uid": s.get("StopUID", ""),
                "stop_sequence": s.get("StopSequence", 0),
                "stop_name": s.get("StopName", {}).get("Zh_tw", ""),
                "lat": s.get("StopPosition", {}).get("PositionLat"),
                "lon": s.get("StopPosition", {}).get("PositionLon"),
            })

    eta_map = {item.get("StopUID"): item for item in eta_list if item.get("StopUID")}

    near_stop_map: Dict[str, Dict[str, Any]] = {}
    for bus in near_list:
        uid = bus.get("StopUID")
        if uid:
            near_stop_map[uid] = {
                "plate_numb": bus.get("PlateNumb", ""),
                "event_type": bus.get("A2EventType", 0),
            }

    if not ordered_stops:
        eta_list.sort(key=lambda x: x.get("StopSequence", 999))
        ordered_stops = [
            {
                "stop_uid": x.get("StopUID", ""),
                "stop_sequence": x.get("StopSequence", idx + 1),
                "stop_name": x.get("StopName", {}).get("Zh_tw", ""),
                "lat": None,
                "lon": None,
            }
            for idx, x in enumerate(eta_list)
        ]

    stops_result: List[Dict[str, Any]] = []
    current_leading_stop = ""
    missed_bus_detected = False

    for idx, s_base in enumerate(ordered_stops):
        uid = s_base["stop_uid"]
        stop_name = s_base["stop_name"]
        eta_item = eta_map.get(uid, {"StopStatus": 4})

        near_info = near_stop_map.get(uid)
        is_at_stop = bool(near_info and near_info["event_type"] == 1)
        plate_numb = (near_info["plate_numb"] if near_info else "") or eta_item.get("PlateNumb", "")

        if is_at_stop:
            current_leading_stop = stop_name

        timing = compute_precise_timing(eta_item, now, is_at_stop=is_at_stop)

        if near_info and near_info["event_type"] == 0:
            missed_bus_detected = True

        prev_info = ""
        if is_at_stop:
            prev_info = "車輛目前停靠本站"
        elif current_leading_stop and timing["stop_status"] == 0:
            prev_info = f"前車位於：{current_leading_stop}"

        stops_result.append({
            "stop_uid": uid,
            "stop_sequence": s_base["stop_sequence"] or (idx + 1),
            "stop_name": stop_name,
            "lat": s_base["lat"],
            "lon": s_base["lon"],
            "plate_numb": plate_numb,
            "prev_stop_info": prev_info,
            **timing,
        })

    return {
        "server_time": now.isoformat(),
        "city": city,
        "route_name": route_name,
        "direction": direction,
        "missed_bus_alert": missed_bus_detected,
        "stops": stops_result,
    }


@app.get("/api/bus/nearby")
async def get_nearby_stops(
    city: str = Query("Taichung"),
    lat: float = Query(..., description="緯度"),
    lon: float = Query(..., description="經度"),
    radius: int = Query(500, description="搜尋半徑（公尺）"),
):
    now = datetime.now(TW_TZ)
    stop_url = (
        f"{TDX_BASE_URL}/Stop/City/{city}"
        f"?$spatialFilter=nearby({lat},{lon},{radius})&$top=20&$format=JSON"
    )
    stops = await fetch_tdx_json(stop_url, ttl=STATIC_CACHE_TTL)

    eta_url = (
        f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}"
        f"?$spatialFilter=nearby({lat},{lon},{radius})&$top=100&$format=JSON"
    )
    etas = await fetch_tdx_json(eta_url, ttl=REALTIME_CACHE_TTL)

    eta_by_stop: Dict[str, List[Dict[str, Any]]] = {}
    for item in etas:
        uid = item.get("StopUID")
        if not uid:
            continue
        timing = compute_precise_timing(item, now)
        eta_by_stop.setdefault(uid, []).append({
            "route_name": item.get("RouteName", {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "plate_numb": item.get("PlateNumb", ""),
            **timing,
        })

    results = []
    for s in stops:
        uid = s.get("StopUID", "")
        s_lat = s.get("StopPosition", {}).get("PositionLat", lat)
        s_lon = s.get("StopPosition", {}).get("PositionLon", lon)
        dist = haversine_meters(lat, lon, s_lat, s_lon)
        routes_here = eta_by_stop.get(uid, [])
        routes_here.sort(key=lambda x: x["remaining_sec"] if x["remaining_sec"] is not None else 999999)

        results.append({
            "stop_uid": uid,
            "stop_name": s.get("StopName", {}).get("Zh_tw", ""),
            "distance_m": dist,
            "routes": routes_here,
        })

    results.sort(key=lambda x: x["distance_m"])
    return {"server_time": now.isoformat(), "stops": results}


@app.get("/api/bus/timetable")
async def get_route_timetable(
    city: str = Query("Taichung"),
    route_name: str = Query(...),
    direction: int = Query(0),
):
    url = (
        f"{TDX_BASE_URL}/Schedule/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}' and Direction eq {direction}&$format=JSON"
    )
    schedules = await fetch_tdx_json(url, ttl=STATIC_CACHE_TTL)

    weekday_times: List[str] = []
    weekend_times: List[str] = []

    for sch in schedules:
        for tt in sch.get("Timetables", []):
            dep_time = tt.get("StopTimes", [{}])[0].get("DepartureTime", "")
            if not dep_time:
                continue
            service_day = tt.get("ServiceDay", {})
            if service_day.get("Monday") == 1 or service_day.get("Wednesday") == 1:
                weekday_times.append(dep_time)
            if service_day.get("Saturday") == 1 or service_day.get("Sunday") == 1:
                weekend_times.append(dep_time)

    return {
        "route_name": route_name,
        "direction": direction,
        "weekday_departures": sorted(list(set(weekday_times))),
        "weekend_departures": sorted(list(set(weekend_times))),
    }


@app.get("/api/bus/transfer")
async def suggest_transfer_routes(
    city: str = Query("Taichung"),
    stop_name: str = Query(..., description="目前所在站名"),
    exclude_route: str = Query(..., description="剛錯過的路線名稱"),
):
    now = datetime.now(TW_TZ)
    eta_url = (
        f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}"
        f"?$filter=StopName/Zh_tw eq '{stop_name}' and StopStatus eq 0&$format=JSON"
    )
    etas = await fetch_tdx_json(eta_url, ttl=REALTIME_CACHE_TTL)

    alternatives = []
    for item in etas:
        r_name = item.get("RouteName", {}).get("Zh_tw", "")
        if not r_name or r_name == exclude_route:
            continue
        timing = compute_precise_timing(item, now)
        if timing["remaining_sec"] is not None and timing["remaining_sec"] <= 1800:
            alternatives.append({
                "route_name": r_name,
                "direction": item.get("Direction", 0),
                "plate_numb": item.get("PlateNumb", ""),
                **timing,
            })

    alternatives.sort(key=lambda x: x["remaining_sec"] or 999999)
    return {
        "stop_name": stop_name,
        "excluded_route": exclude_route,
        "alternatives": alternatives[:5],
    }
