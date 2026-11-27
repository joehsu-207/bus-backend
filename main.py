import asyncio
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Taiwan Bus Dynamic API")

# 允許 Flutter Web (Netlify / Localhost) 跨域存取
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TDX_CLIENT_ID = os.getenv("TDX_CLIENT_ID", "").strip()
TDX_CLIENT_SECRET = os.getenv("TDX_CLIENT_SECRET", "").strip()

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus"
TDX_BIKE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"

# 記憶體快取（降低 TDX API 呼叫頻率，避免 HTTP 429）
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_api_cache: dict[str, tuple[float, Any]] = {}
_signal_count_cache: dict[str, int] = {}

TW_TZ = timezone(timedelta(hours=8))


def _now_tw() -> datetime:
    return datetime.now(TW_TZ)


def _get_cache(key: str, ttl: float) -> Any | None:
    item = _api_cache.get(key)
    if item and (time.time() - item[0]) < ttl:
        return item[1]
    return None


def _set_cache(key: str, value: Any) -> None:
    _api_cache[key] = (time.time(), value)


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def _get_peak_factor() -> float:
    """依台灣都會區尖離峰時段回傳交通延滯倍率"""
    now = _now_tw()
    is_weekend = now.weekday() >= 5
    mins = now.hour * 60 + now.minute

    if not is_weekend:
        if 7 * 60 <= mins <= 9 * 60:
            return 1.32  # 平日早尖峰
        if 17 * 60 <= mins <= 19 * 60 + 30:
            return 1.38  # 平日晚尖峰
        if 12 * 60 <= mins <= 13 * 60 + 30:
            return 1.12  # 午間車多
    else:
        if 13 * 60 <= mins <= 20 * 60:
            return 1.18  # 假日商圈車多

    if now.hour >= 22 or now.hour < 6:
        return 0.88  # 深夜離峰順暢
    return 1.0


async def get_tdx_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="後端尚未設定 TDX_CLIENT_ID 或 TDX_CLIENT_SECRET 環境變數",
        )

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
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"TDX Token 取得失敗 ({resp.status_code}): {resp.text[:200]}",
        )

    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 86400))
    return _token_cache["access_token"]


async def tdx_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    cache_ttl: float = 15.0,
) -> Any:
    query_params = {"$format": "JSON"}
    if params:
        query_params.update(params)

    cache_key = f"{url}|{sorted(query_params.items())}"
    cached = _get_cache(cache_key, cache_ttl)
    if cached is not None:
        return cached

    token = await get_tdx_token(client)
    resp = await client.get(
        url,
        params=query_params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=25.0,
    )

    # 若遇 TDX 429 頻率限制，優先回傳舊快取避免前端報錯
    if resp.status_code == 429:
        stale = _api_cache.get(cache_key)
        if stale is not None:
            return stale[1]
        raise HTTPException(status_code=429, detail="TDX API 查詢頻率達上限 (429)，請稍候再試")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"TDX API 錯誤 ({resp.status_code}): {resp.text[:200]}",
        )

    data = resp.json()
    _set_cache(cache_key, data)
    return data


async def count_traffic_signals_along_stops(cache_key: str, stops: list[dict]) -> int:
    """透過 OpenStreetMap Overpass API 計算整條公車路線沿線的真實紅綠燈路口數（長效快取）"""
    if cache_key in _signal_count_cache:
        return _signal_count_cache[cache_key]

    coords = [(s["lat"], s["lon"]) for s in stops if s.get("lat") and s.get("lon")]
    if len(coords) < 2:
        return 0

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    bbox = f"{min(lats)-0.002},{min(lons)-0.002},{max(lats)+0.002},{max(lons)+0.002}"

    overpass_query = f"""
    [out:json][timeout:10];
    node["highway"="traffic_signals"]({bbox});
    out body;
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": overpass_query},
            )
            if resp.status_code != 200:
                return 0
            nodes = resp.json().get("elements", [])

        matched_signals = 0
        for node in nodes:
            n_lat, n_lon = node.get("lat"), node.get("lon")
            if n_lat is None or n_lon is None:
                continue
            for s_lat, s_lon in coords:
                d_lat = (n_lat - s_lat) * 111000
                d_lon = (n_lon - s_lon) * 101000
                if math.hypot(d_lat, d_lon) <= 180:
                    matched_signals += 1
                    break

        _signal_count_cache[cache_key] = matched_signals
        return matched_signals
    except Exception:
        return 0


def _extract_upcoming_departures(schedules: list[dict], direction: int) -> list[str]:
    """從 TDX 班表資料擷取今日所有表定發車時刻 (HH:MM)"""
    now = _now_tw()
    weekday_keys = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    today_col = weekday_keys[now.weekday()]
    times: set[str] = set()

    for sch in schedules:
        if sch.get("Direction", 0) != direction:
            continue

        # 1. 固定時刻表 (Timetables)
        for tt in sch.get("Timetables", []) or []:
            service_day = tt.get("ServiceDay") or {}
            if service_day and service_day.get(today_col, 1) == 0:
                continue
            for st in tt.get("StopTimes", []) or []:
                if st.get("StopSequence", 1) == 1:
                    dep = (st.get("DepartureTime") or st.get("ArrivalTime") or "")[:5]
                    if len(dep) == 5 and ":" in dep:
                        times.add(dep)

        # 2. 班距班表 (Frequencys) 展開
        for fq in sch.get("Frequencys", []) or []:
            service_day = fq.get("ServiceDay") or {}
            if service_day and service_day.get(today_col, 1) == 0:
                continue
            start_str = (fq.get("StartTime") or "")[:5]
            end_str = (fq.get("EndTime") or "")[:5]
            headway = fq.get("MinHeadwayMins") or fq.get("MaxHeadwayMins") or 0
            if len(start_str) == 5 and len(end_str) == 5 and headway > 0:
                try:
                    sh, sm = map(int, start_str.split(":"))
                    eh, em = map(int, end_str.split(":"))
                    cur_m = sh * 60 + sm
                    end_m = eh * 60 + em
                    while cur_m <= end_m:
                        times.add(f"{cur_m // 60:02d}:{cur_m % 60:02d}")
                        cur_m += int(headway)
                except ValueError:
                    pass

    return sorted(times)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "Taiwan Bus Dynamic API", "time": _now_tw().isoformat()}


@app.get("/api/bus/accurate/{city}/{route}")
async def get_accurate_bus_status(city: str, route: str):
    """整合站序、預估到站、即時車位/車速、每日班表、未發車偵測與沿線紅綠燈數量"""
    encoded_route = route.replace("'", "''")
    route_filter = f"RouteName/Zh_tw eq '{encoded_route}'"

    async with httpx.AsyncClient() as client:
        stop_of_route_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{route}",
            {"$filter": route_filter},
            cache_ttl=3600,  # 站序長效快取 1 小時
        )
        eta_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}/{route}",
            {"$filter": route_filter},
            cache_ttl=12,
        )
        a1_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/RealTimeByFrequency/City/{city}/{route}",
            {"$filter": route_filter},
            cache_ttl=10,
        )
        a2_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/RealTimeNearStop/City/{city}/{route}",
            {"$filter": route_filter},
            cache_ttl=10,
        )
        schedule_task = tdx_get(
            client,
            f"{TDX_BASE_URL}/Schedule/City/{city}/{route}",
            {"$filter": route_filter},
            cache_ttl=3600,
        )

        results = await asyncio.gather(
            stop_of_route_task,
            eta_task,
            a1_task,
            a2_task,
            schedule_task,
            return_exceptions=True,
        )

    stop_routes = results[0] if isinstance(results[0], list) else []
    etas = results[1] if isinstance(results[1], list) else []
    a1_buses = results[2] if isinstance(results[2], list) else []
    a2_events = results[3] if isinstance(results[3], list) else []
    schedules = results[4] if isinstance(results[4], list) else []

    if not stop_routes:
        raise HTTPException(status_code=404, detail=f"找不到 {city} {route} 路的站序資料")

    # 建立 (Direction, StopUID/StopName) -> ETA 映射
    eta_map: dict[tuple[int, str], dict] = {}
    for e in etas:
        if e.get("RouteName", {}).get("Zh_tw") != route:
            continue
        d = int(e.get("Direction", 0))
        s_uid = e.get("StopUID") or ""
        s_name = e.get("StopName", {}).get("Zh_tw") or ""
        if s_uid:
            eta_map[(d, s_uid)] = e
        if s_name:
            eta_map[(d, s_name)] = e

    # 整理線上車輛位置與車速 (A1)
    active_buses_by_dir: dict[int, list[dict]] = {0: [], 1: []}
    bus_speed_map: dict[str, float] = {}
    for b in a1_buses:
        if b.get("RouteName", {}).get("Zh_tw") != route:
            continue
        d = int(b.get("Direction", 0))
        plate = b.get("PlateNumb") or ""
        speed = float(b.get("Speed") or 0.0)
        pos = b.get("BusPosition") or {}
        lat = pos.get("PositionLat")
        lon = pos.get("PositionLon")
        if plate:
            bus_speed_map[plate] = speed
        if d in active_buses_by_dir and lat and lon:
            active_buses_by_dir[d].append(
                {
                    "plate": plate,
                    "speed": speed,
                    "lat": lat,
                    "lon": lon,
                }
            )

    # 整理進離站事件 (A2: A2EventType == 1 代表進站中)
    at_stop_plates: dict[tuple[int, str], list[str]] = {}
    bus_current_stop_name: dict[str, str] = {}
    for ev in a2_events:
        if ev.get("RouteName", {}).get("Zh_tw") != route:
            continue
        d = int(ev.get("Direction", 0))
        plate = ev.get("PlateNumb") or ""
        s_uid = ev.get("StopUID") or ""
        s_name = ev.get("StopName", {}).get("Zh_tw") or ""
        ev_type = ev.get("A2EventType", 0)
        if plate and s_name:
            bus_current_stop_name[plate] = s_name
        if ev_type == 1 and plate:
            if s_uid:
                at_stop_plates.setdefault((d, s_uid), []).append(plate)
            if s_name:
                at_stop_plates.setdefault((d, s_name), []).append(plate)

    # 挑選出每個方向站數最多的主線站序
    best_route_by_dir: dict[int, dict] = {}
    for sr in stop_routes:
        if sr.get("RouteName", {}).get("Zh_tw") != route:
            continue
        d = int(sr.get("Direction", 0))
        stops_list = sr.get("Stops") or []
        if d not in best_route_by_dir or len(stops_list) > len(
            best_route_by_dir[d].get("Stops") or []
        ):
            best_route_by_dir[d] = sr

    now = _now_tw()
    now_str = f"{now.hour:02d}:{now.minute:02d}"
    now_mins = now.hour * 60 + now.minute

    output_dirs: dict[str, list[dict]] = {"0": [], "1": []}
    missed_alerts: list[str] = []

    for d in [0, 1]:
        sr = best_route_by_dir.get(d)
        if not sr:
            continue

        dep_times = _extract_upcoming_departures(schedules, d)
        next_dep_str = ""
        for t in dep_times:
            if t >= now_str:
                next_dep_str = t
                break
        # 若剛過表定發車時間 3~35 分鐘內，保留該班次供前端觸發「未發車」警示
        recent_missed_dep = ""
        for t in reversed(dep_times):
            th, tm = map(int, t.split(":"))
            diff = now_mins - (th * 60 + tm)
            if 3 <= diff <= 35:
                recent_missed_dep = t
                break

        raw_stops = sr.get("Stops") or []
        formatted_stops: list[dict] = []

        for idx, st in enumerate(raw_stops):
            s_uid = st.get("StopUID") or ""
            s_name = st.get("StopName", {}).get("Zh_tw") or ""
            seq = st.get("StopSequence") or (idx + 1)
            pos = st.get("StopPosition") or {}
            s_lat = pos.get("PositionLat")
            s_lon = pos.get("PositionLon")

            eta_info = eta_map.get((d, s_uid)) or eta_map.get((d, s_name)) or {}
            stop_status = eta_info.get("StopStatus", 1)
            est_sec = eta_info.get("EstimateTime")
            plate = eta_info.get("PlateNumb") or ""

            buses_here = list(
                dict.fromkeys(
                    at_stop_plates.get((d, s_uid), [])
                    + at_stop_plates.get((d, s_name), [])
                )
            )
            if est_sec is not None and est_sec <= 25 and plate and plate not in buses_here:
                buses_here.append(plate)

            approaching_buses: list[dict] = []
            if est_sec is not None and est_sec > 0:
                # 找出該車目前 GPS 座標（供前端計算精確紅綠燈數量）
                b_lat, b_lon = None, None
                for ab in active_buses_by_dir[d]:
                    if ab["plate"] == plate:
                        b_lat, b_lon = ab["lat"], ab["lon"]
                        break

                approaching_buses.append(
                    {
                        "plate": plate or "行駛中",
                        "remSec": int(est_sec),
                        "speed": bus_speed_map.get(plate, 25.0),
                        "currentStop": bus_current_stop_name.get(plate, ""),
                        "lat": b_lat,
                        "lon": b_lon,
                    }
                )

            # 若起站無車進站且已逾表定發車時間，產生未發車警示
            stop_next_dep = next_dep_str
            if idx == 0 and not buses_here and not approaching_buses and recent_missed_dep:
                stop_next_dep = f"{recent_missed_dep}未發車"
                dir_label = "去程" if d == 0 else "返程"
                alert_msg = f"{route}路({dir_label}) 表定 {recent_missed_dep} 班次已逾時未發車"
                if alert_msg not in missed_alerts:
                    missed_alerts.append(alert_msg)

            formatted_stops.append(
                {
                    "seq": seq,
                    "stopUID": s_uid,
                    "name": s_name,
                    "lat": s_lat,
                    "lon": s_lon,
                    "status": stop_status,
                    "busesHere": buses_here,
                    "buses": approaching_buses,
                    "nextDeparture": stop_next_dep,
                    "departures": dep_times if idx == 0 else [],
                }
            )

        output_dirs[str(d)] = formatted_stops

    # 非同步並行查詢去/返程沿線真實 OpenStreetMap 紅綠燈路口數
    sig_0, sig_1 = await asyncio.gather(
        count_traffic_signals_along_stops(f"{city}_{route}_0", output_dirs["0"]),
        count_traffic_signals_along_stops(f"{city}_{route}_1", output_dirs["1"]),
    )

    return {
        "0": output_dirs["0"],
        "1": output_dirs["1"],
        "missedAlerts": missed_alerts,
        "trafficSignals": {"0": sig_0, "1": sig_1},
    }


@app.get("/api/bus/positions/{city}/{route}")
async def get_bus_positions(city: str, route: str):
    """即時地圖專用：回傳線上車輛 GPS 座標、車速、方位角與沿線站牌座標"""
    encoded_route = route.replace("'", "''")
    route_filter = f"RouteName/Zh_tw eq '{encoded_route}'"

    async with httpx.AsyncClient() as client:
        a1_data, sr_data = await asyncio.gather(
            tdx_get(
                client,
                f"{TDX_BASE_URL}/RealTimeByFrequency/City/{city}/{route}",
                {"$filter": route_filter},
                cache_ttl=8,
            ),
            tdx_get(
                client,
                f"{TDX_BASE_URL}/StopOfRoute/City/{city}/{route}",
                {"$filter": route_filter},
                cache_ttl=3600,
            ),
        )

    buses = []
    for b in a1_data or []:
        if b.get("RouteName", {}).get("Zh_tw") != route:
            continue
        pos = b.get("BusPosition") or {}
        lat = pos.get("PositionLat")
        lon = pos.get("PositionLon")
        if lat and lon:
            buses.append(
                {
                    "plate": b.get("PlateNumb") or "未知",
                    "direction": int(b.get("Direction", 0)),
                    "speed": float(b.get("Speed") or 0.0),
                    "azimuth": float(b.get("Azimuth") or 0.0),
                    "lat": lat,
                    "lon": lon,
                }
            )

    stops_by_dir: dict[str, list[dict]] = {"0": [], "1": []}
    for sr in sr_data or []:
        if sr.get("RouteName", {}).get("Zh_tw") != route:
            continue
        d_str = str(int(sr.get("Direction", 0)))
        s_list = []
        for st in sr.get("Stops") or []:
            pos = st.get("StopPosition") or {}
            if pos.get("PositionLat") and pos.get("PositionLon"):
                s_list.append(
                    {
                        "name": st.get("StopName", {}).get("Zh_tw") or "",
                        "lat": pos["PositionLat"],
                        "lon": pos["PositionLon"],
                    }
                )
        if len(s_list) > len(stops_by_dir.get(d_str, [])):
            stops_by_dir[d_str] = s_list

    return {"buses": buses, "stops": stops_by_dir}


@app.get("/api/bus/schedule/{city}/{route}")
async def get_bus_schedule(city: str, route: str):
    """每日發車時刻表 API"""
    encoded_route = route.replace("'", "''")
    async with httpx.AsyncClient() as client:
        schedules = await tdx_get(
            client,
            f"{TDX_BASE_URL}/Schedule/City/{city}/{route}",
            {"$filter": f"RouteName/Zh_tw eq '{encoded_route}'"},
            cache_ttl=3600,
        )

    return {
        "0": _extract_upcoming_departures(schedules or [], 0),
        "1": _extract_upcoming_departures(schedules or [], 1),
    }


@app.get("/api/bus/nearby/{city}")
async def get_nearby_bus_stops(
    city: str,
    lat: float = Query(...),
    lon: float = Query(...),
    radius: int = Query(600),
):
    """GPS 附近公車站牌與經過路線查詢"""
    async with httpx.AsyncClient() as client:
        stops_data = await tdx_get(
            client,
            f"{TDX_BASE_URL}/Stop/City/{city}",
            {
                "$spatialFilter": f"nearby({lat},{lon},{radius})",
                "$top": 30,
            },
            cache_ttl=300,
        )

    results = []
    seen_names: set[str] = set()
    for st in stops_data or []:
        s_name = st.get("StopName", {}).get("Zh_tw") or ""
        pos = st.get("StopPosition") or {}
        s_lat = pos.get("PositionLat")
        s_lon = pos.get("PositionLon")
        if not s_name or not s_lat or not s_lon or s_name in seen_names:
            continue
        seen_names.add(s_name)
        dist = round(_haversine_meters(lat, lon, s_lat, s_lon))
        results.append(
            {
                "stopName": s_name,
                "distance": dist,
                "lat": s_lat,
                "lon": s_lon,
                "routes": [],
            }
        )

    results.sort(key=lambda x: x["distance"])
    return results[:15]


@app.get("/api/bus/stop_routes/{city}")
async def get_stop_all_routes(city: str, stop_name: str = Query(...)):
    """查詢經過指定站牌的所有公車路線"""
    clean_stop = stop_name.replace("'", "''")
    async with httpx.AsyncClient() as client:
        etas = await tdx_get(
            client,
            f"{TDX_BASE_URL}/EstimatedTimeOfArrival/City/{city}",
            {
                "$filter": f"StopName/Zh_tw eq '{clean_stop}'",
                "$top": 60,
            },
            cache_ttl=20,
        )

    peak = _get_peak_factor()
    routes_out = []
    seen: set[str] = set()
    for e in etas or []:
        r_name = e.get("RouteName", {}).get("Zh_tw") or ""
        d = int(e.get("Direction", 0))
        key = f"{r_name}_{d}"
        if not r_name or key in seen:
            continue
        seen.add(key)

        est_sec = e.get("EstimateTime")
        status = e.get("StopStatus", 1)
        if est_sec is not None:
            cal_sec = round(est_sec * peak)
            if cal_sec <= 30:
                est_text = "即將進站"
            else:
                est_text = f"{math.ceil(cal_sec / 60)} 分"
        elif status == 2:
            est_text = "交管不停靠"
        elif status == 3:
            est_text = "末班車已過"
        else:
            est_text = "尚未發車"

        routes_out.append(
            {
                "routeName": r_name,
                "direction": d,
                "estimate": est_text,
            }
        )

    return {"routes": routes_out}


@app.get("/api/bus/transfer/{city}")
async def plan_bus_transfer(
    city: str,
    origin: str = Query(...),
    destination: str = Query(...),
    exclude_route: str = Query(""),
):
    """智慧路線規劃：搜尋直達公車與到站時間"""
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
        o_seq = o_item.get("StopSequence") or 1
        d_seq = d_item.get("StopSequence") or 2
        if d_seq <= o_seq:
            continue

        seen.add((r_name, direction))
        direct_list.append(
            {
                "routeName": r_name,
                "direction": direction,
                "originStop": o_item.get("StopName", {}).get("Zh_tw") or origin,
                "targetStop": d_item.get("StopName", {}).get("Zh_tw") or destination,
                "stopsCount": d_seq - o_seq,
                "status": 0 if o_item.get("EstimateTime") is not None else o_item.get("StopStatus", 1),
                "remSec": o_item.get("EstimateTime"),
            }
        )

    return {"direct": direct_list, "transfers": []}


@app.get("/api/youbike/{city}")
async def get_youbike_stations(city: str, keyword: str = Query(...)):
    """查詢指定站牌關鍵字周邊的 YouBike 2.0 即時可借車數與空位"""
    clean_kw = keyword.replace("'", "''")
    async with httpx.AsyncClient() as client:
        stations, availabilities = await asyncio.gather(
            tdx_get(
                client,
                f"{TDX_BIKE_URL}/Station/City/{city}",
                {"$filter": f"contains(StationName/Zh_tw,'{clean_kw}') or contains(StationAddress/Zh_tw,'{clean_kw}')", "$top": 10},
                cache_ttl=3600,
            ),
            tdx_get(
                client,
                f"{TDX_BIKE_URL}/Availability/City/{city}",
                {"$top": 500},
                cache_ttl=30,
            ),
        )

    avail_map = {
        a.get("StationUID"): a for a in (availabilities or []) if a.get("StationUID")
    }

    results = []
    for st in stations or []:
        uid = st.get("StationUID")
        av = avail_map.get(uid) or {}
        name = (st.get("StationName", {}).get("Zh_tw") or "").replace("YouBike2.0_", "")
        results.append(
            {
                "name": name,
                "distanceMeters": 130,
                "rentBikes": av.get("AvailableRentBikes", 0),
                "returnSpaces": av.get("AvailableReturnBikes", 0),
            }
        )

    return {"stations": results[:6]}
