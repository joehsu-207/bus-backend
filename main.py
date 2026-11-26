import os
import time
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Taiwan Bus Full Precise API", version="3.1.0")

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
TDX_BUS_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus"
TDX_BIKE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_api_cache: Dict[str, Dict[str, Any]] = {}

REALTIME_CACHE_TTL = 12
STATIC_CACHE_TTL = 3600


async def get_tdx_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient(timeout=15.0) as client:
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
    async with httpx.AsyncClient(timeout=20.0) as client:
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


def compute_precise_rem_sec(item: Dict[str, Any], now: datetime) -> Optional[int]:
    """扣除 SrcUpdateTime 延遲秒數後的精準剩餘秒數計算"""
    est = item.get("EstimateTime")
    if est is None:
        return None
    src_dt = parse_iso_tw(item.get("SrcUpdateTime")) or parse_iso_tw(item.get("UpdateTime"))
    elapsed = max(0, (now - src_dt).total_seconds()) if src_dt else 0
    return max(0, int(est - elapsed))


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


@app.get("/api/bus/accurate/{city}/{route_name}")
async def get_bus_accurate(city: str, route_name: str):
    now = datetime.now(TW_TZ)
    eta_url = (
        f"{TDX_BUS_URL}/EstimatedTimeOfArrival/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )
    near_url = (
        f"{TDX_BUS_URL}/RealTimeNearStop/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )
    sor_url = (
        f"{TDX_BUS_URL}/StopOfRoute/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )

    eta_list = await fetch_tdx_json(eta_url, ttl=REALTIME_CACHE_TTL)
    near_list = await fetch_tdx_json(near_url, ttl=REALTIME_CACHE_TTL)
    sor_list = await fetch_tdx_json(sor_url, ttl=STATIC_CACHE_TTL)

    result: Dict[str, List[Dict[str, Any]]] = {"0": [], "1": []}
    missed_alerts: List[str] = []

    eta_by_dir_uid: Dict[tuple, List[Dict[str, Any]]] = {}
    for item in eta_list:
        d = int(item.get("Direction", 0))
        uid = item.get("StopUID", "")
        eta_by_dir_uid.setdefault((d, uid), []).append(item)

    buses_at_stop: Dict[tuple, List[str]] = {}
    bus_current_stop_name: Dict[str, str] = {}
    for b in near_list:
        d = int(b.get("Direction", 0))
        uid = b.get("StopUID", "")
        plate = b.get("PlateNumb", "")
        s_name = b.get("StopName", {}).get("Zh_tw", "")
        if plate and s_name:
            bus_current_stop_name[plate] = s_name
        if b.get("A2EventType") == 1 and uid and plate:
            buses_at_stop.setdefault((d, uid), []).append(plate)

    sor_by_dir: Dict[int, List[Dict[str, Any]]] = {}
    for sor in sor_list:
        d = int(sor.get("Direction", 0))
        if d not in sor_by_dir and sor.get("Stops"):
            sor_by_dir[d] = sor["Stops"]

    for d in (0, 1):
        stops_raw = sor_by_dir.get(d, [])
        if not stops_raw:
            fallback_items = [x for x in eta_list if int(x.get("Direction", 0)) == d]
            fallback_items.sort(key=lambda x: x.get("StopSequence", 999))
            stops_raw = [
                {
                    "StopUID": x.get("StopUID", ""),
                    "StopSequence": x.get("StopSequence", idx + 1),
                    "StopName": x.get("StopName", {}),
                }
                for idx, x in enumerate(fallback_items)
            ]

        for idx, s in enumerate(stops_raw):
            uid = s.get("StopUID", "")
            s_name = s.get("StopName", {}).get("Zh_tw", "")
            seq = s.get("StopSequence", idx + 1)
            matched_etas = eta_by_dir_uid.get((d, uid), [])

            buses_here = buses_at_stop.get((d, uid), [])
            active_buses: List[Dict[str, Any]] = []
            status = 4
            next_dep = ""
            next_plate = ""

            for e in matched_etas:
                st = e.get("StopStatus", 4)
                if st == 0:
                    status = 0
                elif status != 0:
                    status = st

                rem_sec = compute_precise_rem_sec(e, now)
                plate = e.get("PlateNumb", "") or ""
                if st == 0 and rem_sec is not None:
                    active_buses.append({
                        "plate": plate or "行駛中",
                        "remSec": rem_sec,
                        "currentStop": bus_current_stop_name.get(plate, ""),
                    })

                nb_dt = parse_iso_tw(e.get("NextBusTime"))
                if nb_dt and not next_dep:
                    next_dep = nb_dt.strftime("%H:%M")
                    next_plate = plate

                for est_item in e.get("Estimates", []):
                    e_plate = est_item.get("PlateNumb", "")
                    e_time = est_item.get("EstimateTime")
                    if e_time is not None and e_plate and not any(x["plate"] == e_plate for x in active_buses):
                        src_dt = parse_iso_tw(e.get("SrcUpdateTime")) or parse_iso_tw(e.get("UpdateTime"))
                        elapsed = max(0, (now - src_dt).total_seconds()) if src_dt else 0
                        active_buses.append({
                            "plate": e_plate,
                            "remSec": max(0, int(e_time - elapsed)),
                            "currentStop": bus_current_stop_name.get(e_plate, ""),
                        })

            active_buses.sort(key=lambda x: x["remSec"])

            result[str(d)].append({
                "stopUid": uid,
                "seq": seq,
                "name": s_name,
                "status": status,
                "busesHere": buses_here,
                "buses": active_buses,
                "nextDeparture": next_dep,
                "nextPlate": next_plate,
            })

    return {
        "0": result["0"],
        "1": result["1"],
        "missedAlerts": missed_alerts,
        "serverTime": now.isoformat(),
    }


@app.get("/api/bus/positions/{city}/{route_name}")
async def get_bus_realtime_positions(city: str, route_name: str):
    """取得路線上所有車輛的即時 GPS 經緯度座標與沿線站牌座標"""
    now = datetime.now(TW_TZ)
    pos_url = (
        f"{TDX_BUS_URL}/RealTimeByFrequency/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )
    sor_url = (
        f"{TDX_BUS_URL}/StopOfRoute/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )

    pos_list = await fetch_tdx_json(pos_url, ttl=REALTIME_CACHE_TTL)
    sor_list = await fetch_tdx_json(sor_url, ttl=STATIC_CACHE_TTL)

    buses_out = []
    for b in pos_list:
        b_pos = b.get("BusPosition", {})
        lat = b_pos.get("PositionLat")
        lon = b_pos.get("PositionLon")
        if lat is None or lon is None:
            continue
        src_dt = parse_iso_tw(b.get("SrcUpdateTime")) or parse_iso_tw(b.get("GPSTime"))
        buses_out.append({
            "plate": b.get("PlateNumb", "未知車牌"),
            "direction": int(b.get("Direction", 0)),
            "lat": lat,
            "lon": lon,
            "speed": b.get("Speed", 0),
            "azimuth": b.get("Azimuth", 0),
            "dutyStatus": b.get("DutyStatus", 1),
            "updateTime": src_dt.strftime("%H:%M:%S") if src_dt else "",
        })

    stops_by_dir: Dict[str, List[Dict[str, Any]]] = {"0": [], "1": []}
    for sor in sor_list:
        d = str(int(sor.get("Direction", 0)))
        if d in stops_by_dir and not stops_by_dir[d]:
            for idx, s in enumerate(sor.get("Stops", [])):
                s_pos = s.get("StopPosition", {})
                s_lat = s_pos.get("PositionLat")
                s_lon = s_pos.get("PositionLon")
                if s_lat is not None and s_lon is not None:
                    stops_by_dir[d].append({
                        "seq": s.get("StopSequence", idx + 1),
                        "name": s.get("StopName", {}).get("Zh_tw", ""),
                        "lat": s_lat,
                        "lon": s_lon,
                    })

    return {
        "serverTime": now.isoformat(),
        "buses": buses_out,
        "stops": stops_by_dir,
    }


@app.get("/api/bus/timetable/{city}/{route_name}")
async def get_bus_timetable(city: str, route_name: str):
    url = (
        f"{TDX_BUS_URL}/Schedule/City/{city}/{route_name}"
        f"?$filter=RouteName/Zh_tw eq '{route_name}'&$format=JSON"
    )
    schedules = await fetch_tdx_json(url, ttl=STATIC_CACHE_TTL)

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    directions: Dict[str, Dict[str, Dict[str, List[str]]]] = {
        "0": {day: {"times": [], "frequencies": []} for day in days},
        "1": {day: {"times": [], "frequencies": []} for day in days},
    }

    for sch in schedules:
        d = str(int(sch.get("Direction", 0)))
        if d not in directions:
            continue
        for tt in sch.get("Timetables", []):
            stop_times = tt.get("StopTimes", [])
            dep = stop_times[0].get("DepartureTime", "") if stop_times else ""
            s_day = tt.get("ServiceDay", {})
            if dep:
                for day in days:
                    if s_day.get(day) == 1 and dep not in directions[d][day]["times"]:
                        directions[d][day]["times"].append(dep)

        for fq in sch.get("Frequencys", []):
            start_t = fq.get("StartTime", "")
            end_t = fq.get("EndTime", "")
            min_h = fq.get("MinHeadwayMins", "")
            max_h = fq.get("MaxHeadwayMins", "")
            s_day = fq.get("ServiceDay", {})
            label = f"{start_t} ~ {end_t}：約 {min_h}~{max_h} 分一班"
            for day in days:
                if s_day.get(day) == 1 and label not in directions[d][day]["frequencies"]:
                    directions[d][day]["frequencies"].append(label)

    for d in ("0", "1"):
        for day in days:
            directions[d][day]["times"].sort()

    return {"directions": directions}


@app.get("/api/bus/nearby/{city}")
async def get_nearby_stops(
    city: str,
    lat: float = Query(...),
    lon: float = Query(...),
    radius: int = Query(450),
):
    url = (
        f"{TDX_BUS_URL}/Stop/City/{city}"
        f"?$spatialFilter=nearby({lat},{lon},{radius})&$top=40&$format=JSON"
    )
    raw_stops = await fetch_tdx_json(url, ttl=STATIC_CACHE_TTL)

    by_name: Dict[str, int] = {}
    for s in raw_stops:
        name = s.get("StopName", {}).get("Zh_tw", "")
        if not name:
            continue
        s_lat = s.get("StopPosition", {}).get("PositionLat", lat)
        s_lon = s.get("StopPosition", {}).get("PositionLon", lon)
        dist = haversine_meters(lat, lon, s_lat, s_lon)
        if name not in by_name or dist < by_name[name]:
            by_name[name] = dist

    stops_out = [{"name": k, "distanceMeters": v} for k, v in by_name.items()]
    stops_out.sort(key=lambda x: x["distanceMeters"])
    return {"stops": stops_out}


@app.get("/api/bus/stop/{city}/{stop_name}")
async def get_stop_routes(city: str, stop_name: str):
    now = datetime.now(TW_TZ)
    url = (
        f"{TDX_BUS_URL}/EstimatedTimeOfArrival/City/{city}"
        f"?$filter=StopName/Zh_tw eq '{stop_name}'&$format=JSON"
    )
    etas = await fetch_tdx_json(url, ttl=REALTIME_CACHE_TTL)

    seen = set()
    routes_out = []
    for e in etas:
        r_name = e.get("RouteName", {}).get("Zh_tw", "")
        d = int(e.get("Direction", 0))
        if not r_name or (r_name, d) in seen:
            continue
        seen.add((r_name, d))
        st = e.get("StopStatus", 1)
        rem_sec = compute_precise_rem_sec(e, now) if st == 0 else None
        routes_out.append({
            "routeName": r_name,
            "direction": d,
            "status": st,
            "remSec": rem_sec,
        })

    routes_out.sort(key=lambda x: (0 if x["status"] == 0 and x["remSec"] is not None else 1, x["remSec"] or 999999))
    return {"stopName": stop_name, "routes": routes_out}


@app.get("/api/bus/transfer/{city}")
async def get_transfer_plan(
    city: str,
    origin: str = Query(...),
    destination: str = Query(...),
    exclude_route: str = Query(""),
):
    now = datetime.now(TW_TZ)
    orig_url = (
        f"{TDX_BUS_URL}/EstimatedTimeOfArrival/City/{city}"
        f"?$filter=StopName/Zh_tw eq '{origin}'&$format=JSON"
    )
    dest_url = (
        f"{TDX_BUS_URL}/EstimatedTimeOfArrival/City/{city}"
        f"?$filter=StopName/Zh_tw eq '{destination}'&$format=JSON"
    )
    orig_etas = await fetch_tdx_json(orig_url, ttl=REALTIME_CACHE_TTL)
    dest_etas = await fetch_tdx_json(dest_url, ttl=REALTIME_CACHE_TTL)

    dest_map = {
        (e.get("RouteName", {}).get("Zh_tw", ""), int(e.get("Direction", 0))): e
        for e in dest_etas
    }

    direct = []
    for e in orig_etas:
        r_name = e.get("RouteName", {}).get("Zh_tw", "")
        d = int(e.get("Direction", 0))
        if not r_name or (exclude_route and r_name == exclude_route):
            continue
        if (r_name, d) in dest_map:
            o_seq = e.get("StopSequence", 0)
            d_seq = dest_map[(r_name, d)].get("StopSequence", 0)
            if d_seq >= o_seq:
                st = e.get("StopStatus", 1)
                rem_sec = compute_precise_rem_sec(e, now) if st == 0 else None
                direct.append({
                    "routeName": r_name,
                    "direction": d,
                    "status": st,
                    "remSec": rem_sec,
                    "stopsCount": max(1, d_seq - o_seq),
                    "targetStop": destination,
                })

    direct.sort(key=lambda x: (0 if x["status"] == 0 and x["remSec"] is not None else 1, x["remSec"] or 999999))
    return {"direct": direct, "transfers": []}


@app.get("/api/youbike/nearby/{city}")
async def get_nearby_youbike(
    city: str,
    stop_name: str = Query(...),
    radius: int = Query(300),
):
    stop_url = (
        f"{TDX_BUS_URL}/Stop/City/{city}"
        f"?$filter=StopName/Zh_tw eq '{stop_name}'&$top=1&$format=JSON"
    )
    stops = await fetch_tdx_json(stop_url, ttl=STATIC_CACHE_TTL)
    if not stops:
        return {"stations": []}

    lat = stops[0].get("StopPosition", {}).get("PositionLat")
    lon = stops[0].get("StopPosition", {}).get("PositionLon")
    if lat is None or lon is None:
        return {"stations": []}

    st_url = (
        f"{TDX_BIKE_URL}/Station/City/{city}"
        f"?$spatialFilter=nearby({lat},{lon},{radius})&$format=JSON"
    )
    av_url = (
        f"{TDX_BIKE_URL}/Availability/City/{city}"
        f"?$spatialFilter=nearby({lat},{lon},{radius})&$format=JSON"
    )
    stations = await fetch_tdx_json(st_url, ttl=STATIC_CACHE_TTL)
    avails = await fetch_tdx_json(av_url, ttl=REALTIME_CACHE_TTL)

    av_map = {a.get("StationUID"): a for a in avails}
    out = []
    for st in stations:
        uid = st.get("StationUID", "")
        av = av_map.get(uid, {})
        s_lat = st.get("StationPosition", {}).get("PositionLat", lat)
        s_lon = st.get("StationPosition", {}).get("PositionLon", lon)
        dist = haversine_meters(lat, lon, s_lat, s_lon)
        raw_name = st.get("StationName", {}).get("Zh_tw", "").replace("YouBike2.0_", "")
        out.append({
            "name": raw_name,
            "distanceMeters": dist,
            "rentBikes": av.get("AvailableRentBikes", 0),
            "returnSpaces": av.get("AvailableReturnBikes", 0),
        })

    out.sort(key=lambda x: x["distanceMeters"])
    return {"stations": out}
