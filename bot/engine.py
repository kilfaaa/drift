"""Прогноз мокрого асфальта — та же логика, что в index.html (держать в синхроне).

Источники (Open-Meteo, без ключа): общий прогноз + отдельные модели + ансамбли.
Асфальт: баланс воды — дождь добавляет, испарение (ET0) убирает.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

LAT, LON = 45.38302, 39.02531   # основной спот (журнал и подстройка считаются по нему)
SPOTS = {"medvedovka": ("Медведовка", 45.383020, 39.025310), "krasnaya": ("KAPANY", 45.102440, 38.991828)}
MSK = timezone(timedelta(hours=3))
TZ_Q = "Europe%2FMoscow"
def om(lat, lon):
    return f"latitude={lat}&longitude={lon}&timezone={TZ_Q}&past_days=1&forecast_days=14"
DET_MODELS = [("ecmwf_ifs025", "ECMWF"), ("ecmwf_aifs025_single", "ECMWF AIFS"), ("icon_seamless", "ICON"), ("icon_eu", "ICON-EU"),
              ("gfs_seamless", "GFS"), ("ukmo_seamless", "UKMO"), ("gem_seamless", "GEM")]
ENS_MODELS = [("ecmwf_ifs025", "ECMWF ENS"), ("gfs_seamless", "GFS ENS"), ("icon_seamless", "ICON ENS"), ("gem_global", "GEM ENS")]
WET_MM, CAP_MM, MIN_DRY = 0.1, 0.8, 0.03
W0, W_LEN = 20, 4
GO, MAYBE = 0.6, 0.3
UA = {"User-Agent": "medvedovka-drift-bot (github.com/kilfaaa/drift)"}


def now_msk() -> datetime:
    return datetime.now(MSK)


ERRORS: list = []   # диагностика последнего запуска


def get_json(url: str, timeout: int = 30, tries: int = 3):
    host = url.split("/")[2]
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode())
            if isinstance(j, dict) and j.get("hourly", {}).get("time"):
                return j
            ERRORS.append(f"{host}: пустой ответ {str(j)[:120]}")
            return None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:160]
            except Exception:
                pass
            ERRORS.append(f"{host}: HTTP {e.code} {body}")
            if e.code in (400, 404):
                return None
        except Exception as e:
            ERRORS.append(f"{host}: {type(e).__name__} {str(e)[:120]}")
        time.sleep(2 * (k + 1))
    return None


def is_rain_key(k: str) -> bool:
    return (k == "precipitation" or k.startswith("precipitation_")) and "probability" not in k


def align(j, times):
    h = (j or {}).get("hourly") or {}
    if not h.get("time"):
        return []
    idx = {t: i for i, t in enumerate(h["time"])}
    out = []
    for k, arr in h.items():
        if not is_rain_key(k):
            continue
        s = []
        for t in times:
            i = idx.get(t)
            v = None if i is None else arr[i]
            s.append(None if v is None else float(v))
        if any(v is not None for v in s):
            out.append(s)
    return out


def wet_series(P, E):
    f, out = 0.0, []
    for i, p in enumerate(P):
        if p is None:
            out.append(None); f = 0.0; continue
        f = min(CAP_MM, f + p)
        f = max(0.0, f - max(MIN_DRY, E[i] or 0.0))
        out.append(p >= WET_MM or f >= WET_MM)
    return out


def film_at(P, E, upto):
    f = 0.0
    for i in range(0, upto + 1):
        f = min(CAP_MM, f + (P[i] or 0.0))
        f = max(0.0, f - max(MIN_DRY, E[i] or 0.0))
    return f


def evening_wet(w, i):
    n = 0
    for k in range(W_LEN):
        if i + k >= len(w) or w[i + k] is None:
            return None
        n += 1 if w[i + k] else 0
    return n >= 2


def fetch_bundle(spot: str = "medvedovka"):
    _, lat, lon = SPOTS[spot]
    OM = om(lat, lon)
    main = get_json("https://api.open-meteo.com/v1/forecast?" + OM +
                    "&hourly=precipitation,precipitation_probability,temperature_2m,wind_speed_10m,et0_fao_evapotranspiration,snowfall,soil_temperature_0cm&wind_speed_unit=ms")
    if not main:
        return None
    times = main["hourly"]["time"]
    det, ens = [], []
    for m, name in DET_MODELS:
        j = get_json(f"https://api.open-meteo.com/v1/forecast?{OM}&hourly=precipitation&models={m}")
        a = align(j, times)
        if a:
            det.append((name, a))
    for m, name in ENS_MODELS:
        url = f"https://ensemble-api.open-meteo.com/v1/ensemble?{OM}&hourly=precipitation&models={m}"
        j = get_json(url, timeout=60) or get_json(url.replace("&past_days=1", ""), timeout=60)
        a = align(j, times)
        if a:
            ens.append((name, a))
    return {"main": main, "det": det, "ens": ens}


SNOW_T = 0.5     # при такой и ниже температуре воздуха осадки считаем снегом
ICE_T = 0.0      # поверхность (почва 0 см) ниже нуля + вода = гололёд
RAIN_HOUR = 0.05 # от скольки мм в час считаем «идёт дождь» для графика по часам


def analyze(b, cal=None, now=None):
    """Полный расчёт: вечера + почасовые данные. Логика 1-в-1 с analyze() в index.html."""
    cal = cal or {}
    weights = cal.get("weights") or {}
    share = float(cal.get("ens_share", 0.65))
    w_of = lambda n: max(0.05, float(weights.get(n, 1.0)))
    blend = lambda pe, pd: (share * pe + (1 - share) * pd) if (pe is not None and pd is not None) else (pe if pe is not None else pd)
    h = b["main"]["hourly"]
    t = h["time"]; n = len(t)
    pick = lambda k: h.get(k) or h.get(k + "_best_match")
    num = lambda arr, i: (None if (arr is None or i < 0 or i >= len(arr) or arr[i] is None) else float(arr[i]))
    P = [None if v is None else float(v) for v in (pick("precipitation") or [None] * n)]
    E = [0.05 if v is None else float(v) for v in (pick("et0_fao_evapotranspiration") or [0.05] * n)]
    temp, wind = pick("temperature_2m"), pick("wind_speed_10m")
    soil, snowf = pick("soil_temperature_0cm"), pick("snowfall")
    det = [{"name": "Open-Meteo", "P": P, "w": wet_series(P, E)}] + \
          [{"name": nm, "P": s[0], "w": wet_series(s[0], E)} for nm, s in b["det"] if s]
    ens = [{"name": nm, "ss": ss, "ws": [wet_series(s, E) for s in ss]} for nm, ss in b["ens"] if len(ss) >= 5]
    # средний дождь по всем расчётам ансамбля (нет ансамблей — основная модель)
    MP = []
    for i in range(n):
        s = c = 0
        for en in ens:
            for ss in en["ss"]:
                if ss[i] is not None:
                    s += ss[i]; c += 1
        MP.append(s / c if c else num(P, i))
    now = now or now_msk()
    today = now.strftime("%Y-%m-%d")
    out = []
    for i, tt in enumerate(t):
        if tt[11:13] != f"{W0:02d}":
            continue
        date = tt[:10]
        if date < today:
            continue
        if i + W_LEN > n:
            break
        d_wet = d_all = 0; ws = wt = 0.0
        for d in det:
            r = evening_wet(d["w"], i)
            if r is None:
                continue
            d_all += 1; wt += w_of(d["name"])
            if r:
                d_wet += 1; ws += w_of(d["name"])
        e_wet = e_all = 0; shares = []
        for en in ens:
            a = w_ = 0
            for w in en["ws"]:
                r = evening_wet(w, i)
                if r is None:
                    continue
                a += 1; w_ += 1 if r else 0
            if a >= 5:
                shares.append(w_ / a); e_wet += w_; e_all += a
        p_det = ws / wt if wt else None
        p_ens = sum(shares) / len(shares) if shares else None
        if p_det is None and p_ens is None:
            continue
        p = blend(p_ens, p_det)
        rain_in = sum((num(MP, i + k) or 0.0) for k in range(W_LEN))
        tv = [x for x in (num(temp, i + k) for k in range(W_LEN)) if x is not None]
        sv = [x for x in (num(soil, i + k) for k in range(W_LEN)) if x is not None]
        wv = [x for x in (num(wind, i + k) for k in range(W_LEN)) if x is not None]
        # осадки с 14:00 до конца окна: какие из них выпали снегом
        snowy = False
        for k in range(-6, W_LEN):
            mp, tk, sf = num(MP, i + k), num(temp, i + k), num(snowf, i + k)
            if (mp or 0) >= RAIN_HOUR and ((tk is not None and tk <= SNOW_T) or (sf or 0) > 0):
                snowy = True; break
        t_min = min(tv) if tv else None
        s_min = min(sv) if sv else t_min
        snow = p >= MAYBE and snowy
        ice = p >= MAYBE and not snow and s_min is not None and s_min <= ICE_T
        status = "snow" if snow else "ice" if ice else ("go" if p >= GO else "maybe" if p >= MAYBE else "dry")
        out.append({
            "date": date, "p": round(p, 4), "pct": round(p * 100), "p_det": p_det, "p_ens": p_ens,
            "d_wet": d_wet, "d_all": d_all, "e_wet": e_wet, "e_all": e_all,
            "water": round(film_at(MP, E, i - 1) + rain_in, 3), "rain_in": round(rain_in, 3),
            "t_min": t_min, "s_min": s_min, "w_max": max(wv) if wv else 0,
            "status": status, "snow": snow, "ice": ice, "windy": (max(wv) if wv else 0) >= 12,
        })
    hourly = []
    for i, tt in enumerate(t):
        shR, shW = [], []; mm = 0.0; mn = 0
        for en in ens:
            a = r = w = 0
            for j, ss in enumerate(en["ss"]):
                x = ss[i]
                if x is None:
                    continue
                a += 1; mm += x; mn += 1
                r += 1 if x >= RAIN_HOUR else 0
                w += 1 if en["ws"][j][i] else 0
            if a >= 5:
                shR.append(r / a); shW.append(w / a)
        rs = wsum = ws2 = 0.0
        for d in det:
            x = d["P"][i]
            if x is None:
                continue
            wgt = w_of(d["name"]); wsum += wgt
            rs += wgt if x >= RAIN_HOUR else 0
            ws2 += wgt if d["w"][i] else 0
        avg = lambda a: sum(a) / len(a) if a else None
        pr = blend(avg(shR), rs / wsum if wsum else None)
        pw = blend(avg(shW), ws2 / wsum if wsum else None)
        hourly.append({"t": tt, "p": round(pr or 0, 3), "wet": round(pw or 0, 3),
                       "mm": round(mm / mn if mn else (num(P, i) or 0), 3)})
    return {"evenings": out, "hourly": hourly,
            "meta": {"det": len(det), "ens": len(ens), "members": sum(len(en["ws"]) for en in ens)}}


# ---------- verification against ERA5 reanalysis ----------
def era5(start: str, end: str, lat: float = LAT, lon: float = LON):
    url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&timezone={TZ_Q}"
           f"&start_date={start}&end_date={end}&hourly=precipitation,et0_fao_evapotranspiration")
    return get_json(url, timeout=60)


def observed_evenings(j):
    """{date: bool} — был ли вечер мокрым по ERA5."""
    h = j["hourly"]; t = h["time"]
    P = [None if v is None else float(v) for v in h["precipitation"]]
    E = [0.05 if v is None else float(v) for v in h["et0_fao_evapotranspiration"]]
    w = wet_series(P, E); out = {}
    for i, tt in enumerate(t):
        if tt[11:13] == f"{W0:02d}":
            r = evening_wet(w, i)
            if r is not None:
                out[tt[:10]] = r
    return out, E, t
