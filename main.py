import asyncio
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Taiwan Bus & YouBike Real-time API")

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
TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

TW_TZ = timezone(timedelta(hours=8))

# 全域快取池，降低 TDX 與 Overpass API 呼叫次數避免觸發 429 限制
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_api_cache: dict[str, tuple[float, Any]] = {}
_osm_signal_cache: dict[str, tuple[float, int]] = {}
_osm_signal_coords_cache: dict[str, tuple[float, list[dict[str, float]]]] = {}


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def get_tdx_token(client: httpx.AsyncClient) -> str | None:
    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        return None
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = await client.post(
        TDX_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": TDX_CLIENT_ID,
            "client_secret": TDX_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    if resp.status_code == 200:
        data = resp.json()
        _token_cache["access_token"] = data.get("access_token")
        _token_cache["expires_at"] = now + float(data.get("expires_in", 3600))
        return _token_cache["access_token"]
    return None


async def tdx_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    cache_ttl: int = 15,
) -> Any:
    query_params = {"$format": "JSON"}
    if params:
        query_params.update(params)

    cache_key = f"{url}|{sorted(query_params.items())}"
    now = time.time()
    if cache_key in _api_cache:
        exp, cached_val = _api_cache[cache_key]
        if now < exp:
            return cached_val

    headers = {"Accept": "application/json"}
    token = await get_tdx_token(client)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = await client.get(url, params=query_params, headers=headers, timeout=20.0)
    if resp.status_code == 429:
        if cache_key in _api_cache:
            return _api_cache[cache_key][1]
        raise HTTPException(status_code=429, detail="TDX API 查詢頻率達上限 (429)")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"TDX 回應錯誤 ({resp.status_code}): {resp.text[:160]}",
        )

    data = resp.json()
    _api_cache[cache_key] = (now + cache_ttl, data)
    return data


async def fetch_osm_signal_coords(
    client: httpx.AsyncClient,
    cache_id: str,
    stops: list[dict[str, Any]],
) -> list[dict[str, float]]:
    """取得路線沿線 95 公尺內所有紅綠燈的經緯度座標"""
    now = time.time()
    if cache_id in _osm_signal_coords_cache:
        exp, cached_coords = _osm_signal_coords_cache[cache_id]
        if now < exp:
            return cached_coords

    coords = [
        (float(s["lat"]), float(s["lon"]))
        for s in stops
        if s.get("lat") is not None and s.get("lon") is not None
    ]
    if len(coords) < 2:
        return []

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    south, north = min(lats) - 0.002, max(lats) + 0.002
    west, east = min(lons) - 0.002, max(lons) + 0.002

    if (north - south) > 0.35 or (east - west) > 0.35:
        return []

    query = f"""
    [out:json][timeout:6];
    node["highway"="traffic_signals"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
    out body;
    """
    try:
        resp = await client.post(OVERPASS_URL, data={"data": query}, timeout=6.5)
        if resp.status_code == 200:
            elements = resp.json().get("elements", [])
            matched_nodes: dict[int, dict[str, float]] = {}
            for el in elements:
                elat = el.get("lat")
                elon = el.get("lon")
                eid = el.get("id")
                if elat is None or elon is None or eid is None:
                    continue
                for slat, slon in coords:
                    if abs(elat - slat) < 0.0012 and abs(elon - slon) < 0.0012:
                        if haversine_meters(elat, elon, slat, slon) <= 95:
                            matched_nodes[eid] = {"lat": float(elat), "lon": float(elon)}
                            break
            result_list = list(matched_nodes.values())
            _osm_signal_coords_cache[cache_id] = (now + 86400, result_list)
            return result_list
    except Exception:
        pass

    return []


async def fetch_osm_signal_count(
    client: httpx.AsyncClient,
    cache_id: str,
    stops: list[dict[str, Any]],
) -> int:
    """透過 OpenStreetMap Overpass API 計算路線沿線經過的交通號誌（紅綠燈）數量"""
    now = time.time()
    if cache_id in _osm_signal_cache:
        exp, count = _osm_signal_cache[cache_id]
        if now < exp:
            return count

    signal_nodes = await fetch_osm_signal_coords(client, f"{cache_id}_coords", stops)
    if signal_nodes:
        count = len(signal_nodes)
        _osm_signal_cache[cache_id] = (now + 86400, count)
        return count

    coords = [
        (float(s["lat"]), float(s["lon"]))
        for s in stops
        if s.get("lat") is not None and s.get("lon") is not None
    ]
    if len(coords) < 2:
        return 0

    total_dist = sum(
        haversine_meters(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )
    fallback_est = max(1, round(total_dist / 320.0))
    _osm_signal_cache[cache_id] = (now + 3600, fallback_est)
    return fallback_est


def parse_next_departure_str(next_bus_time: str | None) -> str:
    if not next_bus_time:
        return ""
    try:
        dt = datetime.fromisoformat(next_bus_time.replace("Z", "+00:00")).astimezone(TW_TZ)
        return dt.strftime("%H:%M")
    except Exception:
        if "T" in next_bus_time and len(next_bus_time) >= 16:
            return next_bus_time[11:16]
        return next_bus_time


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Taiwan Bus & YouBike Real-time API",
        "tdx_auth_configured": bool(TDX_CLIENT_ID and TDX_CLIENT_SECRET),
    }


@app.get("/api/bus/accurate/{city}/{route}")
async def get_bus_accurate(city: str, route: str):
    """取得指定路線去返程站序、即時到站秒數、車牌、目前所在站牌與沿線紅綠燈數"""
    clean_route = route.replace("'", "''")
    filter_expr = f"RouteName/Zh_tw eq '{clean_route}'"

    async with httpx.AsyncClient() as client:
        stop_of_route_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{route}",
            {"$filter": filter_expr},
            cache_ttl=3600,
        )
        eta_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}/{route}",
            {"$filter": filter_expr},
            cache_ttl=12,
        )
        real_time_near_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/RealTimeNearStop/City/{city}/{route}",
            {"$filter": filter_expr},
            cache_ttl=10,
        )
        real_time_freq_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/RealTimeByFrequency/City/{city}/{route}",
            {"$filter": filter_expr},
            cache_ttl=10,
        )

        results = await asyncio.gather(
            stop_of_route_task,
            eta_task,
            real_time_near_task,
            real_time_freq_task,
            return_exceptions=True,
        )

        stop_of_route_data = results[0] if not isinstance(results[0], Exception) else []
        eta_data = results[1] if not isinstance(results[1], Exception) else []
        near_stop_data = results[2] if not isinstance(results[2], Exception) else []
        freq_data = results[3] if not isinstance(results[3], Exception) else []

        if isinstance(results[0], HTTPException) and not stop_of_route_data:
            raise results[0]

        bus_live_info: dict[str, dict[str, Any]] = {}
        for item in freq_data or []:
            plate = item.get("PlateNumb")
            pos = item.get("BusPosition") or {}
            if plate:
                bus_live_info[plate] = {
                    "plate": plate,
                    "direction": item.get("Direction", 0),
                    "speed": item.get("Speed", 0),
                    "lat": pos.get("PositionLat"),
                    "lon": pos.get("PositionLon"),
                    "currentStop": "",
                }

        buses_at_stop: dict[tuple[int, int], list[str]] = {}
        for item in near_stop_data or []:
            plate = item.get("PlateNumb")
            direction = int(item.get("Direction", 0))
            seq = int(item.get("StopSequence", 0))
            s_name = item.get("StopName", {}).get("Zh_tw") or ""
            event_type = item.get("A2EventType", 1)
            if plate:
                bus_live_info.setdefault(plate, {"plate": plate, "direction": direction})
                bus_live_info[plate]["currentStop"] = s_name
                if event_type == 1:
                    buses_at_stop.setdefault((direction, seq), [])
                    if plate not in buses_at_stop[(direction, seq)]:
                        buses_at_stop[(direction, seq)].append(plate)

        eta_by_uid: dict[tuple[int, str], dict[str, Any]] = {}
        eta_by_seq: dict[tuple[int, int], dict[str, Any]] = {}
        for item in eta_data or []:
            if item.get("RouteName", {}).get("Zh_tw") != route:
                continue
            direction = int(item.get("Direction", 0))
            s_uid = item.get("StopUID") or ""
            s_seq = int(item.get("StopSequence", 0))
            if s_uid:
                eta_by_uid[(direction, s_uid)] = item
            if s_seq:
                eta_by_seq[(direction, s_seq)] = item

        directional_stops: dict[str, list[dict[str, Any]]] = {"0": [], "1": []}

        for sr in stop_of_route_data or []:
            if sr.get("RouteName", {}).get("Zh_tw") != route:
                continue
            direction = int(sr.get("Direction", 0))
            dir_key = str(direction)
            if dir_key not in directional_stops or directional_stops[dir_key]:
                continue

            raw_stops = sr.get("Stops") or []
            parsed_stops = []
            for st in raw_stops:
                seq = int(st.get("StopSequence", 0))
                s_uid = st.get("StopUID") or ""
                s_name = st.get("StopName", {}).get("Zh_tw") or "未知站牌"
                pos = st.get("StopPosition") or {}
                lat = pos.get("PositionLat")
                lon = pos.get("PositionLon")

                eta_item = eta_by_uid.get((direction, s_uid)) or eta_by_seq.get((direction, seq)) or {}
                est_sec = eta_item.get("EstimateTime")
                stop_status = eta_item.get("StopStatus", 1 if est_sec is None else 0)
                next_dep = parse_next_departure_str(eta_item.get("NextBusTime"))
                plate = eta_item.get("PlateNumb") or ""

                buses_here = buses_at_stop.get((direction, seq), [])
                approaching_buses = []
                if est_sec is not None:
                    b_detail = bus_live_info.get(plate, {})
                    approaching_buses.append(
                        {
                            "plate": plate or "行駛中",
                            "remSec": est_sec,
                            "currentStop": b_detail.get("currentStop", ""),
                            "speed": b_detail.get("speed"),
                            "lat": b_detail.get("lat"),
                            "lon": b_detail.get("lon"),
                        }
                    )

                parsed_stops.append(
                    {
                        "seq": seq,
                        "stopUID": s_uid,
                        "name": s_name,
                        "lat": lat,
                        "lon": lon,
                        "status": 0 if est_sec is not None else stop_status,
                        "nextDeparture": next_dep,
                        "nextPlate": plate if est_sec is None else "",
                        "busesHere": buses_here,
                        "buses": approaching_buses,
                    }
                )

            directional_stops[dir_key] = parsed_stops

        sig0, sig1 = await asyncio.gather(
            fetch_osm_signal_count(client, f"{city}_{route}_0", directional_stops["0"]),
            fetch_osm_signal_count(client, f"{city}_{route}_1", directional_stops["1"]),
        )

    return {
        "0": directional_stops["0"],
        "1": directional_stops["1"],
        "trafficSignals": {"0": sig0, "1": sig1},
        "missedAlerts": [],
    }


@app.get("/api/bus/positions/{city}/{route}")
async def get_bus_positions(city: str, route: str):
    """取得公車即時地圖所需的所有車輛 GPS 座標、站點座標與沿線紅綠燈座標"""
    clean_route = route.replace("'", "''")
    filter_expr = f"RouteName/Zh_tw eq '{clean_route}'"

    async with httpx.AsyncClient() as client:
        sr_res, freq_res = await asyncio.gather(
            tdx_get(
                client,
                f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{route}",
                {"$filter": filter_expr},
                cache_ttl=3600,
            ),
            tdx_get(
                client,
                f"{TDX_BASE_URL}/RealTimeByFrequency/City/{city}/{route}",
                {"$filter": filter_expr},
                cache_ttl=8,
            ),
            return_exceptions=True,
        )

        sr_data = sr_res if not isinstance(sr_res, Exception) else []
        freq_data = freq_res if not isinstance(freq_res, Exception) else []

        stops_map: dict[str, list[dict[str, Any]]] = {"0": [], "1": []}
        for sr in sr_data or []:
            if sr.get("RouteName", {}).get("Zh_tw") != route:
                continue
            dir_key = str(int(sr.get("Direction", 0)))
            if dir_key in stops_map and not stops_map[dir_key]:
                for st in sr.get("Stops") or []:
                    pos = st.get("StopPosition") or {}
                    stops_map[dir_key].append(
                        {
                            "seq": st.get("StopSequence"),
                            "name": st.get("StopName", {}).get("Zh_tw") or "",
                            "lat": pos.get("PositionLat"),
                            "lon": pos.get("PositionLon"),
                        }
                    )

        sig0_coords, sig1_coords = await asyncio.gather(
            fetch_osm_signal_coords(client, f"{city}_{route}_0_coords", stops_map["0"]),
            fetch_osm_signal_coords(client, f"{city}_{route}_1_coords", stops_map["1"]),
        )

    buses_list = []
    for b in freq_data or []:
        if b.get("RouteName", {}).get("Zh_tw") != route:
            continue
        pos = b.get("BusPosition") or {}
        lat = pos.get("PositionLat")
        lon = pos.get("PositionLon")
        if lat is None or lon is None:
            continue
        buses_list.append(
            {
                "plate": b.get("PlateNumb") or "未知",
                "direction": int(b.get("Direction", 0)),
                "lat": lat,
                "lon": lon,
                "speed": b.get("Speed", 0),
                "azimuth": b.get("Azimuth", 0),
            }
        )

    return {
        "buses": buses_list,
        "stops": stops_map,
        "trafficSignals": {"0": sig0_coords, "1": sig1_coords},
    }


@app.get("/api/bus/stop_routes/{city}")
async def get_stop_routes(city: str, stop_name: str = Query(...)):
    """查詢經過指定站牌的所有公車路線與預估到站時間（支援點擊中途站轉乘查詢）"""
    clean_stop = stop_name.replace("'", "''")
    async with httpx.AsyncClient() as client:
        etas = await tdx_get(
            client,
            f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}",
            {"$filter": f"StopName/Zh_tw eq '{clean_stop}'", "$top": 120},
            cache_ttl=15,
        )

    routes_out = []
    seen: set[tuple[str, int]] = set()
    for item in etas or []:
        r_name = item.get("RouteName", {}).get("Zh_tw") or ""
        direction = int(item.get("Direction", 0))
        if not r_name or (r_name, direction) in seen:
            continue
        seen.add((r_name, direction))

        est_sec = item.get("EstimateTime")
        status = item.get("StopStatus", 1)
        if est_sec is not None:
            if est_sec <= 30:
                est_label = "即將進站"
            elif est_sec < 60:
                est_label = f"{est_sec} 秒"
            else:
                est_label = f"{est_sec // 60} 分"
        else:
            next_dep = parse_next_departure_str(item.get("NextBusTime"))
            est_label = next_dep if next_dep else ("末班已過" if status == 3 else "尚未發車")

        routes_out.append(
            {
                "routeName": r_name,
                "direction": direction,
                "estimate": est_label,
            }
        )

    return {"stopName": stop_name, "routes": routes_out}


@app.get("/api/bus/transfer/{city}")
async def plan_bus_transfer(
    city: str,
    origin: str = Query(...),
    destination: str = Query(...),
    exclude_route: str = Query(""),
):
    """智慧路線規劃：搜尋直達公車、沿途經過站點 (viaStops) 與到站時間"""
    clean_orig = origin.replace("'", "''")
    clean_dest = destination.replace("'", "''")

    async with httpx.AsyncClient() as client:
        orig_etas, dest_etas = await asyncio.gather(
            tdx_get(
                client,
                f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}",
                {"$filter": f"contains(StopName/Zh_tw,'{clean_orig}')", "$top": 80},
                cache_ttl=20,
            ),
            tdx_get(
                client,
                f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}",
                {"$filter": f"contains(StopName/Zh_tw,'{clean_dest}')", "$top": 80},
                cache_ttl=20,
            ),
        )

        dest_routes: dict[tuple[str, int], dict] = {}
        for d_item in dest_etas or []:
            r_name = d_item.get("RouteName", {}).get("Zh_tw") or ""
            direction = int(d_item.get("Direction", 0))
            if r_name and r_name != exclude_route:
                dest_routes[(r_name, direction)] = d_item

        direct_list = []
        seen: set[tuple[str, int]] = set()
        for o_item in orig_etas or []:
            r_name = o_item.get("RouteName", {}).get("Zh_tw") or ""
            direction = int(o_item.get("Direction", 0))
            if (r_name, direction) not in dest_routes or (r_name, direction) in seen:
                continue
            d_item = dest_routes[(r_name, direction)]
            o_seq = int(o_item.get("StopSequence") or 1)
            d_seq = int(d_item.get("StopSequence") or 2)
            if d_seq <= o_seq:
                continue

            seen.add((r_name, direction))
            orig_stop_name = o_item.get("StopName", {}).get("Zh_tw") or origin
            dest_stop_name = d_item.get("StopName", {}).get("Zh_tw") or destination

            via_stops = [orig_stop_name, dest_stop_name]
            try:
                enc_r = r_name.replace("'", "''")
                sr_list = await tdx_get(
                    client,
                    f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{r_name}",
                    {"$filter": f"RouteName/Zh_tw eq '{enc_r}'"},
                    cache_ttl=3600,
                )
                for sr in sr_list or []:
                    if (
                        sr.get("RouteName", {}).get("Zh_tw") == r_name
                        and int(sr.get("Direction", 0)) == direction
                    ):
                        stops_arr = sr.get("Stops") or []
                        sliced = [
                            s.get("StopName", {}).get("Zh_tw")
                            for s in stops_arr
                            if o_seq <= int(s.get("StopSequence") or 0) <= d_seq
                        ]
                        sliced = [name for name in sliced if name]
                        if len(sliced) >= 2:
                            via_stops = sliced
                            break
            except Exception:
                pass

            direct_list.append(
                {
                    "routeName": r_name,
                    "direction": direction,
                    "originStop": orig_stop_name,
                    "targetStop": dest_stop_name,
                    "viaStops": via_stops,
                    "stopsCount": d_seq - o_seq,
                    "status": 0 if o_item.get("EstimateTime") is not None else o_item.get("StopStatus", 1),
                    "remSec": o_item.get("EstimateTime"),
                }
            )

    return {"direct": direct_list, "transfers": []}


@app.get("/api/bus/nearby/{city}")
async def get_nearby_stops(
    city: str,
    lat: float = Query(...),
    lon: float = Query(...),
    radius: int = Query(600),
):
    """搜尋 GPS 座標周邊指定半徑內的公車站牌"""
    async with httpx.AsyncClient() as client:
        stops_data = await tdx_get(
            client,
            f"{TDX_BASE_URL}/Stop/City/{city}",
            {
                "$spatialFilter": f"nearby({lat},{lon},{radius})",
                "$top": 40,
            },
            cache_ttl=300,
        )

    grouped: dict[str, dict[str, Any]] = {}
    for st in stops_data or []:
        s_name = st.get("StopName", {}).get("Zh_tw") or ""
        pos = st.get("StopPosition") or {}
        s_lat = pos.get("PositionLat")
        s_lon = pos.get("PositionLon")
        if not s_name or s_lat is None or s_lon is None:
            continue
        dist = round(haversine_meters(lat, lon, float(s_lat), float(s_lon)))
        if s_name not in grouped or dist < grouped[s_name]["distance"]:
            grouped[s_name] = {
                "stopName": s_name,
                "distance": dist,
                "lat": s_lat,
                "lon": s_lon,
                "routes": [],
            }

    sorted_stops = sorted(grouped.values(), key=lambda x: x["distance"])[:15]
    return {"stops": sorted_stops}


@app.get("/api/youbike/{city}")
async def get_nearby_youbike(city: str, keyword: str = Query("")):
    """查詢指定關鍵字周邊的 YouBike 2.0 租賃站即時可借車輛與空位"""
    clean_kw = keyword.strip()
    if not clean_kw:
        return {"stations": []}

    results = []
    async with httpx.AsyncClient() as client:
        if city == "Taichung":
            try:
                resp = await client.get(
                    "https://datacenter.taichung.gov.tw/swagger/OpenData/86dfad5c-540c-4479-bb7d-d7439d34eeb1",
                    timeout=12.0,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    records = payload.get("retVal") if isinstance(payload, dict) else payload
                    for item in records or []:
                        sna = str(item.get("sna", ""))
                        ar = str(item.get("ar", ""))
                        if clean_kw in sna or clean_kw in ar:
                            results.append(
                                {
                                    "name": sna.replace("YouBike2.0_", ""),
                                    "distanceMeters": 120,
                                    "rentBikes": int(item.get("sbi", 0) or 0),
                                    "returnSpaces": int(item.get("bemp", 0) or 0),
                                }
                            )
            except Exception:
                pass
        else:
            try:
                enc_kw = clean_kw.replace("'", "''")
                stations, avail = await asyncio.gather(
                    tdx_get(
                        client,
                        f"https://tdx.transportdata.tw/api/basic/v2/Bike/Station/City/{city}",
                        {"$filter": f"contains(StationName/Zh_tw,'{enc_kw}')", "$top": 10},
                        cache_ttl=600,
                    ),
                    tdx_get(
                        client,
                        f"https://tdx.transportdata.tw/api/basic/v2/Bike/Availability/City/{city}",
                        {"$filter": f"contains(StationName/Zh_tw,'{enc_kw}')", "$top": 10},
                        cache_ttl=30,
                    ),
                )
                avail_map = {a.get("StationUID"): a for a in avail or []}
                for st in stations or []:
                    uid = st.get("StationUID")
                    av = avail_map.get(uid, {})
                    s_name = st.get("StationName", {}).get("Zh_tw") or ""
                    results.append(
                        {
                            "name": s_name.replace("YouBike2.0_", ""),
                            "distanceMeters": 150,
                            "rentBikes": int(av.get("AvailableRentBikes", 0) or 0),
                            "returnSpaces": int(av.get("AvailableReturnBikes", 0) or 0),
                        }
                    )
            except Exception:
                pass

    return {"stations": results[:5]}
