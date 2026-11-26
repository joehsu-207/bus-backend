import asyncio
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Taiwan Bus & YouBike Accurate API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TDX_CLIENT_ID = os.getenv("TDX_CLIENT_ID", "")
TDX_CLIENT_SECRET = os.getenv("TDX_CLIENT_SECRET", "")
TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic"

TW_TZ = timezone(timedelta(hours=8))

_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_API_CACHE: Dict[str, Dict[str, Any]] = {}
_YOUBIKE_CACHE: Dict[str, Dict[str, Any]] = {}
_ROUTE_CACHE_POOL: Dict[str, Dict[str, Any]] = {}

# 內建臺中常用站點座標備援（避免 TDX 限流時無法計算 YouBike 距離）
_BUILTIN_STOP_COORDS: Dict[str, Tuple[float, float]] = {
    "三厝里": (24.15165, 120.63528),
    "逢甲大學(福星路)": (24.17885, 120.64682),
    "逢甲大學(逢甲路)": (24.17820, 120.64515),
    "黎明公益路口": (24.15042, 120.63558),
    "永春東七公益路口": (24.14895, 120.63112),
    "台中火車站": (24.13678, 120.68501),
    "臺中車站": (24.13678, 120.68501),
}

DAY_KEYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _normalize_stop(name: str) -> str:
    return re.split(r"[\(（]", name)[0].strip()


async def get_tdx_token() -> str:
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"] - 60:
        return _TOKEN_CACHE["token"]

    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="伺服器尚未設定 TDX_CLIENT_ID / TDX_CLIENT_SECRET")

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            TDX_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": TDX_CLIENT_ID,
                "client_secret": TDX_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail=f"TDX Token 取得失敗: {res.text}")
        data = res.json()
        _TOKEN_CACHE["token"] = data["access_token"]
        _TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in", 86400))
        return _TOKEN_CACHE["token"]


async def tdx_get(path: str, params: Optional[Dict[str, Any]] = None, ttl: int = 15) -> Any:
    query_params = {"$format": "JSON"}
    if params:
        query_params.update(params)

    cache_key = f"{path}|{sorted(query_params.items())}"
    now = time.time()
    cached = _API_CACHE.get(cache_key)
    if cached and now - cached["ts"] < ttl:
        return cached["data"]

    token = await get_tdx_token()
    url = f"{TDX_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        res = await client.get(
            url,
            params=query_params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if res.status_code == 429:
            if cached:
                return cached["data"]
            raise HTTPException(status_code=429, detail="TDX API 查詢頻率達上限 (429)")
        if res.status_code != 200:
            if cached:
                return cached["data"]
            raise HTTPException(status_code=res.status_code, detail=f"TDX 請求失敗: {res.text}")
        data = res.json()
        _API_CACHE[cache_key] = {"ts": now, "data": data}
        return data


@app.get("/")
def health_check():
    return {"status": "ok", "service": "Taiwan Bus & YouBike API"}


@app.get("/api/youbike/nearby/{city}")
async def get_nearby_youbike(
    city: str,
    stop_name: str = Query(...),
    radius: int = Query(500),
):
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None

    for k, coords in _BUILTIN_STOP_COORDS.items():
        if k in stop_name or stop_name in k:
            target_lat, target_lon = coords
            break

    # 若內建座標未命中，嘗試從已快取的公車路線池取得站牌經緯度（零 TDX 額度消耗）
    if target_lat is None:
        base_target = _normalize_stop(stop_name)
        for ck, payload in _ROUTE_CACHE_POOL.items():
            if not ck.startswith(f"{city}_"):
                continue
            for dir_key in ("0", "1"):
                for st in payload.get(dir_key, []):
                    s_name = st.get("name", "")
                    if (stop_name in s_name or (base_target and base_target in s_name)) and st.get("lat") and st.get("lon"):
                        target_lat = float(st["lat"])
                        target_lon = float(st["lon"])
                        break
                if target_lat is not None:
                    break
            if target_lat is not None:
                break

    stations: List[Dict[str, Any]] = []
    if city == "Taichung":
        now = time.time()
        cached = _YOUBIKE_CACHE.get("Taichung")
        if cached and now - cached["ts"] < 45:
            records = cached["data"]
        else:
            url = "https://newdatacenter.taichung.gov.tw/api/v1/no-auth/resource.download?rid=cab8f056-93e3-4aec-8909-1ba45d2b5d0d"
            try:
                async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                    res = await client.get(url)
                    res.raise_for_status()
                    raw = res.json()
                    records = raw if isinstance(raw, list) else raw.get("retVal", [])
                    _YOUBIKE_CACHE["Taichung"] = {"ts": now, "data": records}
            except Exception:
                records = cached["data"] if cached else []

        base_target = _normalize_stop(stop_name)
        for item in records:
            name = str(item.get("sna", "")).replace("YouBike2.0_", "")
            try:
                s_lat = float(item.get("lat") or 0)
                s_lon = float(item.get("lng") or 0)
                rent = int(item.get("available_rent_bikes") or item.get("sbi") or 0)
                ret = int(item.get("available_return_bikes") or item.get("bemp") or 0)
            except (ValueError, TypeError):
                continue

            if target_lat is not None and target_lon is not None and s_lat and s_lon:
                dist = _haversine(target_lat, target_lon, s_lat, s_lon)
                if dist <= radius:
                    stations.append({
                        "name": name,
                        "distanceMeters": dist,
                        "rentBikes": rent,
                        "returnSpaces": ret,
                    })
            elif (base_target and base_target in name) or (name and name in stop_name):
                stations.append({
                    "name": name,
                    "distanceMeters": 100,
                    "rentBikes": rent,
                    "returnSpaces": ret,
                })

    stations.sort(key=lambda x: x["distanceMeters"])
    return {"stopName": stop_name, "stations": stations[:6]}


@app.get("/api/bus/accurate/{city}/{route_name}")
async def get_accurate_bus(city: str, route_name: str):
    safe_route = route_name.replace("'", "''")
    filter_expr = f"RouteName/Zh_tw eq '{safe_route}'"

    stops_data, eta_data, vehicles_data = await asyncio.gather(
        tdx_get(f"/v2/Bus/StopOfRoute/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=3600),
        tdx_get(f"/v2/Bus/EstimatedTimeOfArrival/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=12),
        tdx_get(f"/v2/Bus/RealTimeNearStop/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=12),
    )

    dir_stops: Dict[str, List[Dict[str, Any]]] = {"0": [], "1": []}
    for r in stops_data:
        if r.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        d = str(r.get("Direction", 0))
        if d not in dir_stops or dir_stops[d]:
            continue
        for idx, s in enumerate(r.get("Stops", [])):
            pos = s.get("StopPosition", {})
            dir_stops[d].append({
                "stopUID": s.get("StopUID", ""),
                "stopID": s.get("StopID", ""),
                "name": s.get("StopName", {}).get("Zh_tw", ""),
                "seq": s.get("StopSequence", idx + 1),
                "lat": pos.get("PositionLat"),
                "lon": pos.get("PositionLon"),
                "status": 1,
                "busesHere": [],
                "buses": [],
                "nextDeparture": "",
                "nextPlate": "",
            })

    buses_at_stop: Dict[Tuple[str, str], List[str]] = {}
    bus_current_stop: Dict[str, str] = {}
    for v in vehicles_data:
        if v.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        d = str(v.get("Direction", 0))
        plate = v.get("PlateNumb", "")
        s_uid = v.get("StopUID", "")
        s_name = v.get("StopName", {}).get("Zh_tw", "")
        event_type = v.get("A2EventType", 1)
        if plate and s_name:
            bus_current_stop[plate] = s_name
        if plate and s_uid and event_type == 1:
            buses_at_stop.setdefault((d, s_uid), []).append(plate)

    eta_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in eta_data:
        if e.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        d = str(e.get("Direction", 0))
        s_uid = e.get("StopUID", "")
        eta_map[(d, s_uid)] = e

    for d in ("0", "1"):
        for st in dir_stops[d]:
            s_uid = st["stopUID"]
            st["busesHere"] = buses_at_stop.get((d, s_uid), [])
            e = eta_map.get((d, s_uid))
            if e:
                status = e.get("StopStatus", 1)
                st["status"] = status
                est = e.get("EstimateTime")
                plate = e.get("PlateNumb", "")
                if status == 0 and est is not None:
                    st["buses"].append({
                        "plate": plate,
                        "remSec": int(est),
                        "currentStop": bus_current_stop.get(plate, ""),
                    })
                next_bus_time = e.get("NextBusTime", "")
                if next_bus_time and "T" in next_bus_time:
                    st["nextDeparture"] = next_bus_time.split("T")[1][:5]
                if plate:
                    st["nextPlate"] = plate

    payload = {"0": dir_stops["0"], "1": dir_stops["1"], "missedAlerts": []}
    _ROUTE_CACHE_POOL[f"{city}_{route_name}"] = payload
    return payload


@app.get("/api/bus/positions/{city}/{route_name}")
async def get_bus_positions(city: str, route_name: str):
    safe_route = route_name.replace("'", "''")
    filter_expr = f"RouteName/Zh_tw eq '{safe_route}'"

    pos_data, stops_data = await asyncio.gather(
        tdx_get(f"/v2/Bus/RealTimeByFrequency/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=6),
        tdx_get(f"/v2/Bus/StopOfRoute/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=3600),
    )

    buses = []
    for p in pos_data:
        if p.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        b_pos = p.get("BusPosition", {})
        lat = b_pos.get("PositionLat")
        lon = b_pos.get("PositionLon")
        if lat is None or lon is None:
            continue
        buses.append({
            "plate": p.get("PlateNumb", ""),
            "direction": p.get("Direction", 0),
            "lat": lat,
            "lon": lon,
            "speed": p.get("Speed", 0),
            "azimuth": p.get("Azimuth"),
        })

    stops_by_dir: Dict[str, List[Dict[str, Any]]] = {"0": [], "1": []}
    for r in stops_data:
        if r.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        d = str(r.get("Direction", 0))
        if stops_by_dir.get(d):
            continue
        for idx, s in enumerate(r.get("Stops", [])):
            sp = s.get("StopPosition", {})
            stops_by_dir[d].append({
                "seq": s.get("StopSequence", idx + 1),
                "name": s.get("StopName", {}).get("Zh_tw", ""),
                "lat": sp.get("PositionLat"),
                "lon": sp.get("PositionLon"),
            })

    return {"buses": buses, "stops": stops_by_dir}


@app.get("/api/bus/timetable/{city}/{route_name}")
async def get_bus_timetable(city: str, route_name: str):
    safe_route = route_name.replace("'", "''")
    filter_expr = f"RouteName/Zh_tw eq '{safe_route}'"
    raw_tt = await tdx_get(f"/v2/Bus/Schedule/City/{city}/{route_name}", {"$filter": filter_expr}, ttl=3600)

    directions: Dict[str, Dict[str, Dict[str, List[str]]]] = {
        "0": {dk: {"times": [], "frequencies": []} for dk in DAY_KEYS},
        "1": {dk: {"times": [], "frequencies": []} for dk in DAY_KEYS},
    }

    for item in raw_tt:
        if item.get("RouteName", {}).get("Zh_tw") != route_name:
            continue
        d = str(item.get("Direction", 0))
        if d not in directions:
            continue
        for tt in item.get("Timetables", []):
            service_day = tt.get("ServiceDay", {})
            stop_times = tt.get("StopTimes", [])
            if not stop_times:
                continue
            dep = stop_times[0].get("DepartureTime", "")[:5]
            if not dep:
                continue
            for dk in DAY_KEYS:
                if service_day.get(dk) == 1 and dep not in directions[d][dk]["times"]:
                    directions[d][dk]["times"].append(dep)

        for fq in item.get("Frequencies", []):
            service_day = fq.get("ServiceDay", {})
            st_time = fq.get("StartTime", "")[:5]
            ed_time = fq.get("EndTime", "")[:5]
            min_h = fq.get("MinHeadwayMins", "")
            max_h = fq.get("MaxHeadwayMins", "")
            label = f"{st_time}~{ed_time} 約 {min_h}-{max_h} 分一班"
            for dk in DAY_KEYS:
                if service_day.get(dk) == 1 and label not in directions[d][dk]["frequencies"]:
                    directions[d][dk]["frequencies"].append(label)

    for d in ("0", "1"):
        for dk in DAY_KEYS:
            directions[d][dk]["times"].sort()

    return {"route": route_name, "directions": directions}


@app.get("/api/bus/nearby/{city}")
async def get_nearby_stops(
    city: str,
    lat: float = Query(...),
    lon: float = Query(...),
    radius: int = Query(450),
):
    spatial_filter = f"nearby(StopPosition, {lat}, {lon}, {radius})"
    raw_stops = await tdx_get(f"/v2/Bus/Stop/City/{city}", {"$filter": spatial_filter, "$top": 60}, ttl=120)

    seen_names = set()
    results = []
    for s in raw_stops:
        name = s.get("StopName", {}).get("Zh_tw", "")
        if not name or name in seen_names:
            continue
        sp = s.get("StopPosition", {})
        s_lat = sp.get("PositionLat")
        s_lon = sp.get("PositionLon")
        if s_lat is None or s_lon is None:
            continue
        dist = _haversine(lat, lon, float(s_lat), float(s_lon))
        if dist <= radius:
            seen_names.add(name)
            results.append({"name": name, "distanceMeters": dist})

    results.sort(key=lambda x: x["distanceMeters"])
    return {"stops": results[:20]}


@app.get("/api/bus/stop/{city}/{stop_name}")
async def get_stop_routes(city: str, stop_name: str):
    safe_stop = stop_name.replace("'", "''")
    eta_list = await tdx_get(
        f"/v2/Bus/EstimatedTimeOfArrival/City/{city}",
        {"$filter": f"StopName/Zh_tw eq '{safe_stop}'", "$top": 120},
        ttl=15,
    )

    seen = set()
    routes = []
    for e in eta_list:
        r_name = e.get("RouteName", {}).get("Zh_tw", "")
        d = e.get("Direction", 0)
        key = (r_name, d)
        if not r_name or key in seen:
            continue
        seen.add(key)
        status = e.get("StopStatus", 1)
        est = e.get("EstimateTime")
        routes.append({
            "routeName": r_name,
            "direction": d,
            "status": status,
            "remSec": int(est) if (status == 0 and est is not None) else None,
        })

    routes.sort(key=lambda x: (0 if x["status"] == 0 and x["remSec"] is not None else 1, x["remSec"] or 99999))
    return {"stopName": stop_name, "routes": routes}


@app.get("/api/bus/transfer/{city}")
async def plan_bus_transfer(
    city: str,
    origin: str = Query(...),
    destination: str = Query(...),
    exclude_route: str = Query(""),
):
    base_orig = _normalize_stop(origin)
    base_dest = _normalize_stop(destination)

    # 1. 先從伺服器快取池掃描可達路線（零 TDX API 額度消耗）
    direct_results: List[Dict[str, Any]] = []
    seen_direct = set()

    for ck, payload in list(_ROUTE_CACHE_POOL.items()):
        if not ck.startswith(f"{city}_"):
            continue
        r_name = ck[len(city) + 1 :]
        if exclude_route and r_name == exclude_route:
            continue
        for dir_key in ("0", "1"):
            s_list = payload.get(dir_key, [])
            orig_idx = -1
            dest_idx = -1
            orig_stop_obj = None
            dest_stop_name = destination
            for idx, st in enumerate(s_list):
                s_name = st.get("name", "")
                if orig_idx == -1 and (origin in s_name or (base_orig and base_orig in s_name)):
                    orig_idx = idx
                    orig_stop_obj = st
                if orig_idx >= 0 and idx > orig_idx and (destination in s_name or (base_dest and base_dest in s_name)):
                    dest_idx = idx
                    dest_stop_name = s_name
                    break
            if orig_idx >= 0 and dest_idx > orig_idx and orig_stop_obj:
                d_int = int(dir_key)
                seen_direct.add((r_name, d_int))
                buses = orig_stop_obj.get("buses", [])
                buses_here = orig_stop_obj.get("busesHere", [])
                rem_sec = 0 if buses_here else (buses[0]["remSec"] if buses else None)
                status = 0 if (buses_here or buses) else orig_stop_obj.get("status", 1)
                direct_results.append({
                    "routeName": r_name,
                    "direction": d_int,
                    "originStop": orig_stop_obj.get("name", origin),
                    "targetStop": dest_stop_name,
                    "stopsCount": dest_idx - orig_idx,
                    "status": status,
                    "remSec": rem_sec,
                })

    # 2. 僅使用 2 筆高效率關鍵字過濾請求向 TDX 查詢經過起點與終點的班次，避免觸發 429 限流
    try:
        safe_o = base_orig.replace("'", "''")
        safe_d = base_dest.replace("'", "''")
        orig_etas, dest_etas = await asyncio.gather(
            tdx_get(
                f"/v2/Bus/EstimatedTimeOfArrival/City/{city}",
                {"$filter": f"contains(StopName/Zh_tw,'{safe_o}')", "$top": 80},
                ttl=20,
            ),
            tdx_get(
                f"/v2/Bus/EstimatedTimeOfArrival/City/{city}",
                {"$filter": f"contains(StopName/Zh_tw,'{safe_d}')", "$top": 80},
                ttl=20,
            ),
        )

        orig_by_route: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for e in orig_etas:
            r_name = e.get("RouteName", {}).get("Zh_tw", "")
            d = int(e.get("Direction", 0))
            if not r_name or (exclude_route and r_name == exclude_route):
                continue
            orig_by_route[(r_name, d)] = e

        dest_by_route: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for e in dest_etas:
            r_name = e.get("RouteName", {}).get("Zh_tw", "")
            d = int(e.get("Direction", 0))
            if r_name:
                dest_by_route[(r_name, d)] = e

        for key, o_e in orig_by_route.items():
            if key in dest_by_route and key not in seen_direct:
                d_e = dest_by_route[key]
                o_seq = o_e.get("StopSequence")
                d_seq = d_e.get("StopSequence")
                if o_seq is not None and d_seq is not None and int(d_seq) <= int(o_seq):
                    continue
                stops_cnt = (int(d_seq) - int(o_seq)) if (o_seq is not None and d_seq is not None) else 6
                status = o_e.get("StopStatus", 1)
                est = o_e.get("EstimateTime")
                direct_results.append({
                    "routeName": key[0],
                    "direction": key[1],
                    "originStop": o_e.get("StopName", {}).get("Zh_tw", origin),
                    "targetStop": d_e.get("StopName", {}).get("Zh_tw", destination),
                    "stopsCount": max(1, stops_cnt),
                    "status": status,
                    "remSec": int(est) if (status == 0 and est is not None) else None,
                })
                seen_direct.add(key)
    except HTTPException as exc:
        if exc.status_code == 429 and not direct_results:
            raise exc

    direct_results.sort(
        key=lambda x: (0 if x["status"] == 0 and x["remSec"] is not None else 1, x["remSec"] or 99999)
    )
    return {"direct": direct_results, "transfers": []}
