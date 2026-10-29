#!/usr/bin/env python3
"""Бот Медведовки: запускается GitHub Actions по расписанию.

Каждый запуск (раз в полчаса):
  1. забирает новые события из Telegram (добавление в чат, голоса в опросах);
  2. раз в час считает прогноз на 14 вечеров для обеих точек и кладёт его в data/fc_*.json для сайта;
  3. раз в день, в обед, пишет прогноз в журнал;
  4. объявляет новые сборы, в обед и вечером присылает опрос, если вечером мокро;
  5. после вечера с опросом спрашивает «как было на споте»;
  6. сверяет старые прогнозы с ответами с места и реанализом ERA5, обновляет data/calibration.json;
  7. раз в 4 часа днём присылает анекдот.

Секреты (Settings → Secrets and variables → Actions):
  TELEGRAM_BOT_TOKEN — токен от @BotFather (без него Telegram-часть пропускается)
  CREW_BOT_CODE      — код бота для закрытых данных (без него нет сборов и статистики)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "bot"))
import engine as E  # noqa: E402

STATE = ROOT / "bot" / "state.json"
CAL = ROOT / "data" / "calibration.json"
TRUTH = ROOT / "data" / "ground_truth.json"   # ответы «как было на споте» (только счётчики, без имён)
SPOT_IDS = ["medvedovka", "krasnaya"]
LOG_PATH = {"medvedovka": ROOT / "data" / "forecast_log.csv", "krasnaya": ROOT / "data" / "forecast_log_krasnaya.csv"}
VERIFY_PATH = {"medvedovka": ROOT / "data" / "verify.csv", "krasnaya": ROOT / "data" / "verify_krasnaya.csv"}
FC_PATH = {sp: ROOT / "data" / f"fc_{sp}.json" for sp in SPOT_IDS}   # готовый прогноз для сайта
TRUTH_OPTS = ["Мокро", "Сухо", "Не ездили"]


def spot_cal(cal: dict, sp: str) -> dict:
    """Калибровка и журнал точки: Медведовка лежит в корне calibration.json, остальные в cal["spots"][id]."""
    return cal if sp == "medvedovka" else cal.setdefault("spots", {}).setdefault(sp, {})


def spot_loc(sp: str) -> str:
    return "Медведовке" if sp == "medvedovka" else "KAPANY"
SITE = "https://kilfaaa.github.io/drift/"
LOG_FIELDS = ["issued", "date", "lead", "pct", "p_det", "p_ens", "d_wet", "d_all", "e_wet", "e_all", "status"]
RU_WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
RU_MON = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def ru_date(d: str) -> str:
    x = datetime.strptime(d, "%Y-%m-%d")
    return f"{RU_WD[x.weekday()]}, {x.day} {RU_MON[x.month - 1]}"


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---------- Telegram ----------
class TG:
    def __init__(self, token: str | None):
        self.token = token

    def call(self, method: str, **params):
        if not self.token:
            return None
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode())
            return j.get("result") if j.get("ok") else None
        except Exception as e:
            print(f"telegram {method} failed: {e}")
            return None


# ---------- закрытые данные ----------
class Vault:
    def __init__(self, code: str | None):
        self.ok = False
        self.data, self.stats, self.K = {}, {"people": {}}, None
        if not code:
            return
        try:
            import crew  # tools/crew.py
            self.crew = crew
            self.K, self.data = crew.read_vault(code)
            self.stats = crew.read_stats(self.K)
            self.stats.setdefault("people", {})
            self.ok = True
        except SystemExit:
            print("CREW_BOT_CODE не подошёл")
        except Exception as e:
            print("vault error:", e)

    def save_stats(self):
        if self.ok:
            self.crew.write_stats(self.K, self.stats)


# ---------- шаги ----------
def collect_updates(tg: TG, st: dict, vault: Vault):
    if not tg.token:
        return
    ups = tg.call("getUpdates", offset=st.get("offset", 0), timeout=0,
                  allowed_updates=["message", "edited_message", "my_chat_member", "poll_answer"]) or []
    changed_stats = False
    seen = st.setdefault("_seen", [])
    for u in ups:
        st["offset"] = u["update_id"] + 1
        kind = next((k for k in u if k != "update_id"), "?")
        obj = u.get(kind) if isinstance(u.get(kind), dict) else {}
        seen.append(f"{kind}:{(obj.get('chat') or {}).get('type', '-')}:{((obj.get('new_chat_member') or {}).get('status', ''))}")
        mcm = u.get("my_chat_member")
        if mcm and mcm["chat"]["type"] in ("group", "supergroup") and mcm["new_chat_member"]["status"] in ("member", "administrator", "restricted"):
            if not st.get("chat_id"):
                st["chat_id"] = mcm["chat"]["id"]
                st["hello"] = False
        msg = u.get("message") or u.get("edited_message")
        if msg and msg.get("chat", {}).get("type") in ("group", "supergroup") and not st.get("chat_id"):
            st["chat_id"] = msg["chat"]["id"]; st["hello"] = False
        if msg and msg.get("migrate_to_chat_id") and msg["chat"]["id"] == st.get("chat_id"):
            st["chat_id"] = msg["migrate_to_chat_id"]
        pa = u.get("poll_answer")
        if pa:
            poll = st.get("polls", {}).get(pa["poll_id"])
            if poll and poll.get("kind") == "truth" and pa.get("user"):
                import hashlib
                who = hashlib.sha256(f"drift:{pa['user']['id']}".encode()).hexdigest()[:10]
                votes = st.setdefault("truth_votes", {}).setdefault(pa["poll_id"], {})
                ids = pa.get("option_ids", [])
                if ids:
                    votes[who] = ids[0]
                else:
                    votes.pop(who, None)
                continue
        if pa and vault.ok:
            poll = st.get("polls", {}).get(pa["poll_id"])
            user = pa.get("user")
            if poll and user and poll.get("kind") in ("weather", "meet"):
                uid = str(user["id"])
                name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Без имени"
                p = vault.stats["people"].setdefault(uid, {"name": name, "dates": []})
                p["name"] = name
                d = poll["date"]
                going = 0 in pa.get("option_ids", [])
                if going and d not in p["dates"]:
                    p["dates"].append(d); p["dates"].sort()
                if not going and d in p["dates"]:
                    p["dates"].remove(d)
                changed_stats = True
    if changed_stats:
        vault.save_stats()


def with_joke(st: dict, vault: "Vault", text: str) -> str:
    """Добавить анекдот: по очереди про каждого из экипажа, без повторов, пока не кончатся."""
    jokes = (vault.data.get("jokes") or {}) if vault.ok else {}
    people = sorted(k for k, v in jokes.items() if v)
    if not people:
        return text
    n = st.get("joke_n", 0); st["joke_n"] = n + 1
    who = people[n % len(people)]
    return f"{text}\n\n🖤 {next_joke(st, jokes, who)}"


def next_joke(st: dict, jokes: dict, who: str) -> str:
    """Следующий анекдот про человека: общая очередь для всех сообщений, без повторов до конца списка."""
    used = st.setdefault("joke_pick", {})
    i = used.get(who, 0); used[who] = i + 1
    lst = jokes[who]
    return lst[i % len(lst)]


def send_poll(tg: TG, st: dict, text: str, question: str, date: str, kind: str, vault: "Vault | None" = None, spot: str = "medvedovka"):
    chat = st.get("chat_id")
    if not chat:
        return False
    if vault is not None:
        text = with_joke(st, vault, text)
    tg.call("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)
    m = tg.call("sendPoll", chat_id=chat, question=question, options=[{"text": "Еду"}, {"text": "Не еду"}], is_anonymous=False)
    if m and m.get("poll"):
        st.setdefault("polls", {})[m["poll"]["id"]] = {"date": date, "kind": kind, "spot": spot}
        return True
    return False


def evening_line(e) -> str:
    s = f"мокро {e['pct']}%"
    if e["d_all"]:
        s += f" (модели {e['d_wet']}/{e['d_all']}"
        s += f", ансамбль {e['e_wet']}/{e['e_all']})" if e["e_all"] else ")"
    if e.get("snow"):
        s += ", но это снег"
    elif e.get("ice"):
        s += ", но дорога подмерзает — гололёд"
    return s


def notify(tg: TG, st: dict, vault: Vault, evs: dict, slot: str):
    chat = st.get("chat_id")
    if not tg.token or not chat:
        return
    if not st.get("hello"):
        tg.call("sendMessage", chat_id=chat, text="Я на месте. Буду писать сюда, когда на Медведовке после 20:00 будет мокро (или на KAPANY, если на Медведовке сухо), и присылать опросы «Еду / Не еду».\n" + SITE)
        st["hello"] = True
    today = E.now_msk().strftime("%Y-%m-%d")
    tomorrow = (E.now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    by = {sp: {e["date"]: e for e in ev} for sp, ev in evs.items()}
    M, K = by.get("medvedovka", {}), by.get("krasnaya", {})
    polls = st.get("polls", {})
    polled = {p["date"] for p in polls.values() if p.get("kind") in ("weather", "meet")}

    def pick(d):
        """Куда ехать в этот вечер: Медведовка, если там мокро, иначе запасные KAPANY."""
        m, k = M.get(d), K.get(d)
        if m and m["status"] == "go":
            return "medvedovka", m, k
        if k and k["status"] == "go":
            return "krasnaya", k, m
        return None, m, k

    # новые сборы
    ann = st.setdefault("announced", [])
    for m in (vault.data.get("meets") or []) if vault.ok else []:
        key = f"{m.get('date')} {m.get('time', '20:00')}"
        if not m.get("date") or m["date"] < today or key in ann:
            continue
        sp = m.get("spot") or "medvedovka"
        e = by.get(sp, {}).get(m["date"])
        text = f"Сбор: {ru_date(m['date'])}, {m.get('time', '20:00')}, {E.SPOTS[sp][0]}."
        if m.get("note"):
            text += f"\n{m['note']}"
        if e:
            text += f"\nПрогноз на вечер: {evening_line(e)}."
        text += f"\n{SITE}"
        if send_poll(tg, st, text, f"Едешь на сбор {ru_date(m['date'])}?", m["date"], "meet", vault, sp):
            ann.append(key); polled.add(m["date"])

    sp, e, other = pick(today)
    if slot in ("noon", "evening") and sp and today not in polled:
        pre = "" if slot == "noon" else "Прогноз улучшился. "
        if sp == "medvedovka":
            text = f"{pre}Сегодня после 20:00 на Медведовке {evening_line(e)}."
            if other and other["status"] == "go":
                text += f"\nНа KAPANY тоже мокро ({other['pct']}%)."
        else:
            text = (f"{pre}Запасной вариант: на Медведовке сегодня сухо ({other['pct'] if other else 0}%), "
                    f"а на KAPANY {evening_line(e)}.")
        send_poll(tg, st, f"{text}\n{SITE}", f"Сегодня 20:00, {E.SPOTS[sp][0]}, мокро {e['pct']}%. Едешь?", today, "weather", vault, sp)
    elif slot == "evening" and today in polled and st.get("cancelled") != today:
        last = [p for p in polls.values() if p["date"] == today and p.get("kind") in ("weather", "meet")]
        psp = (last[-1].get("spot") if last else None) or "medvedovka"
        cur = by.get(psp, {}).get(today)
        if cur and cur["status"] in ("dry", "snow", "ice"):
            alt_sp, alt_e, _ = pick(today)
            why = {"snow": "к вечеру пойдёт снег", "ice": "к вечеру дорога подмёрзнет, будет гололёд"}.get(cur["status"], f"к вечеру мокро всего {cur['pct']}%")
            text = f"Отбой: на {spot_loc(psp)} {why}."
            if alt_sp and alt_sp != psp:
                text += f" Зато на {'Медведовке' if alt_sp == 'medvedovka' else 'KAPANY'} мокро {alt_e['pct']}%."
            tg.call("sendMessage", chat_id=chat, text=with_joke(st, vault, text))
            st["cancelled"] = today
    # снег или гололёд сегодня: опрос не шлём, но предупреждаем один раз
    for wsp in SPOT_IDS:
        we = by.get(wsp, {}).get(today)
        if slot == "noon" and we and we["status"] in ("snow", "ice") and we["pct"] >= 60 and today not in polled \
                and st.get("winter_note") != f"{today}-{wsp}" and wsp == "medvedovka":
            what = "снег" if we["status"] == "snow" else "гололёд: асфальт будет мокрый и подмёрзший"
            tg.call("sendMessage", chat_id=chat, text=with_joke(st, vault, f"Сегодня после 20:00 на Медведовке {what}. Это не обычный мокрый вечер, опрос не присылаю — решайте сами.\n{SITE}"))
            st["winter_note"] = f"{today}-{wsp}"
    tsp, t, tother = pick(tomorrow)
    if slot == "noon" and tsp and st.get("heads_up") != tomorrow and tomorrow not in polled:
        where = "на Медведовке" if tsp == "medvedovka" else "на KAPANY (на Медведовке сухо)"
        tg.call("sendMessage", chat_id=chat, text=with_joke(st, vault, f"Завтра ({ru_date(tomorrow)}) после 20:00 {where} {evening_line(t)}. Опрос пришлю завтра в обед, если прогноз устоит."))
        st["heads_up"] = tomorrow


def log_forecast(st: dict, sp: str, ev: list):
    """Раз в день, в обед (тот прогноз, по которому бот присылает опрос), пишем прогноз точки в журнал."""
    now = E.now_msk(); today = now.strftime("%Y-%m-%d")
    logged = st.setdefault("logged_spots", {})
    if not ev or now.hour < 11 or logged.get(sp) == today:
        return
    path = LOG_PATH[sp]; new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        for e in ev:
            lead = (datetime.strptime(e["date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
            r = lambda x: "" if x is None else f"{x:.3f}"
            w.writerow({"issued": now.strftime("%Y-%m-%dT%H:%M"), "date": e["date"], "lead": lead, "pct": e["pct"],
                        "p_det": r(e["p_det"]), "p_ens": r(e["p_ens"]), "d_wet": e["d_wet"], "d_all": e["d_all"],
                        "e_wet": e["e_wet"], "e_all": e["e_all"], "status": e["status"]})
    logged[sp] = today


def ask_truth(tg: TG, st: dict):
    """После вечера, на который был опрос, спросить в чате, как было на самом деле."""
    chat = st.get("chat_id")
    if not tg.token or not chat:
        return
    now = E.now_msk()
    if now.hour == 23 and now.minute >= 25:
        day = now.strftime("%Y-%m-%d")
    elif now.hour < 2:
        day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        return
    asked = st.setdefault("truth_asked", [])
    real = [p for p in st.get("polls", {}).values() if p["date"] == day and p.get("kind") in ("weather", "meet")]
    if not real or day in asked:
        return
    sp = real[-1].get("spot") or "medvedovka"
    m = tg.call("sendPoll", chat_id=chat, question=f"Как было на {spot_loc(sp)} {ru_date(day)}? Это для проверки прогноза",
                options=[{"text": o} for o in TRUTH_OPTS], is_anonymous=False)
    if m and m.get("poll"):
        st.setdefault("polls", {})[m["poll"]["id"]] = {"date": day, "kind": "truth", "spot": sp}
        asked.append(day); del asked[:-30]


def aggregate_truth(st: dict) -> dict:
    """Счётчики ответов «как было» по точкам и датам → data/ground_truth.json."""
    out = {}
    for pid, votes in st.get("truth_votes", {}).items():
        poll = st.get("polls", {}).get(pid)
        if not poll:
            continue
        c = out.setdefault(poll.get("spot") or "medvedovka", {}).setdefault(poll["date"], {"wet": 0, "dry": 0, "none": 0})
        for opt in votes.values():
            c[["wet", "dry", "none"][opt] if opt in (0, 1, 2) else "none"] += 1
    if out or TRUTH.exists():
        save_json(TRUTH, out)
    return out


def verify(cal_sp: dict, sp: str, truth: dict):
    """Сверить журнал точки: ответы с места важнее реанализа ERA5 (он появляется через ~6 дней)."""
    path, vpath = LOG_PATH[sp], VERIFY_PATH[sp]
    if not path.exists():
        return
    _, lat, lon = E.SPOTS[sp]
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    done = {r["date"]: r for r in csv.DictReader(vpath.open(encoding="utf-8"))} if vpath.exists() else {}
    cutoff = (E.now_msk() - timedelta(days=6)).strftime("%Y-%m-%d")
    todo = sorted({r["date"] for r in rows if r["date"] <= cutoff and r["date"] not in done})
    if todo:
        start = (datetime.strptime(todo[0], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        j = E.era5(start, todo[-1], lat, lon)
        if j:
            obs, _, _ = E.observed_evenings(j)
            for d in todo:
                if d in obs:
                    done[d] = {"date": d, "wet": int(obs[d])}
            with vpath.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["date", "wet"]); w.writeheader()
                for d in sorted(done):
                    w.writerow({"date": d, "wet": done[d]["wet"]})
    # факт: сначала ответы с места (большинство «мокро» или «сухо»), иначе ERA5
    fact = {d: (int(v["wet"]), "era5") for d, v in done.items()}
    for d, c in (truth.get(sp) or {}).items():
        if c["wet"] != c["dry"] and (c["wet"] or c["dry"]):
            fact[d] = (1 if c["wet"] > c["dry"] else 0, "spot")
    lead0 = {r["date"]: r for r in rows if r["lead"] == "0"}
    journal, hits, n, pairs = [], 0, 0, []
    for d in sorted(fact):
        r = lead0.get(d)
        if not r:
            continue
        o, src = fact[d]; pct = int(r["pct"])
        said_wet, said_dry = pct >= 60, pct < 30
        if said_wet or said_dry:
            n += 1; hits += 1 if ((said_wet and o) or (said_dry and not o)) else 0
        journal.append({"date": d, "pct": pct, "wet": bool(o), "src": src})
        if r["p_det"] and r["p_ens"]:
            pairs.append((float(r["p_det"]), float(r["p_ens"]), o))
    cal_sp["journal"] = journal[-30:]
    cal_sp["verified"] = {"n": n, "hits": hits, "days": len(journal), "spot": sum(1 for j in journal if j["src"] == "spot")}
    if len(pairs) >= 30 and sum(o for _, _, o in pairs) >= 5:
        best = min((sum((s * pe + (1 - s) * pd - o) ** 2 for pd, pe, o in pairs), s) for s in [x / 20 for x in range(4, 19)])
        cal_sp["ens_share"] = best[1]


ERA5_FAMILY = ("ECMWF", "ECMWF AIFS")   # ERA5 сделан на модели ECMWF, AIFS на нём обучен — им сравнение подыгрывает


def calibrate_models(cal_sp: dict, sp: str, days: int = 365):
    """Веса моделей по прошлому году для точки: архив прогнозов Open-Meteo против ERA5."""
    _, lat, lon = E.SPOTS[sp]
    end = (E.now_msk() - timedelta(days=7)).date()
    start = end - timedelta(days=days)
    obs_j = E.era5(start.isoformat(), end.isoformat(), lat, lon)
    if not obs_j:
        print("ERA5 недоступен — веса не обновлены"); return
    obs, et0, times = E.observed_evenings(obs_j)
    base = sum(obs.values()) / max(1, len(obs))
    stats = {}
    for m, name in [("best_match", "Open-Meteo")] + E.DET_MODELS:
        url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone={E.TZ_Q}"
               f"&start_date={start}&end_date={end}&hourly=precipitation&models={m}")
        j = E.get_json(url, timeout=90)
        a = E.align(j, times)
        if not a:
            continue
        w = E.wet_series(a[0], et0)
        n = bs = hits = 0
        for i, tt in enumerate(times):
            if tt[11:13] != f"{E.W0:02d}" or tt[:10] not in obs:
                continue
            r = E.evening_wet(w, i)
            if r is None:
                continue
            o = obs[tt[:10]]; n += 1; bs += (int(r) - int(o)) ** 2; hits += 1 if r == o else 0
        if n >= 60:
            stats[name] = {"n": n, "acc": round(hits / n, 3), "bs": round(bs / n, 4)}
    ref = base * (1 - base)
    if ref > 0 and sum(obs.values()) >= 8:
        wts = {k: round(min(2.0, max(0.2, 1 + 2 * (1 - v["bs"] / ref))), 2) for k, v in stats.items()}
        others = [v for k, v in wts.items() if k not in ERA5_FAMILY]
        if others:   # не даём ECMWF больше веса, чем лучшей «независимой» модели
            cap = max(others)
            for k in ERA5_FAMILY:
                if k in wts:
                    wts[k] = min(wts[k], cap)
        cal_sp["weights"] = wts
    cal_sp["models"] = stats
    cal_sp["base_rate"] = round(base, 3)
    cal_sp["calibrated"] = E.now_msk().strftime("%Y-%m-%d")
    cal_sp["period"] = [start.isoformat(), end.isoformat()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "noon", "evening", "collect", "calibrate", "test", "testpoll", "joke", "jokebeat", "tomorrow"])
    ap.add_argument("--who", default="")
    a = ap.parse_args()
    st = load_json(STATE, {})
    cal = load_json(CAL, {})
    tg = TG(os.environ.get("TELEGRAM_BOT_TOKEN") or None)
    vault = Vault(os.environ.get("CREW_BOT_CODE") or None)
    now = E.now_msk(); h = now.hour; today = now.strftime("%Y-%m-%d")
    # GitHub запускает расписание с опозданиями и пропусками, поэтому бот запускается часто,
    # а что делать — решает сам по времени и по тому, что уже сделано сегодня (state.json)
    slot = a.mode if a.mode != "auto" else ("noon" if 11 <= h < 16 else "evening" if 16 <= h < 20 else "collect")
    print(f"mode={a.mode} slot={slot} telegram={'yes' if tg.token else 'no'} vault={'yes' if vault.ok else 'no'} chat={'yes' if st.get('chat_id') else 'no'}")

    collect_updates(tg, st, vault)

    def send_beat():
        jokes = (vault.data.get("jokes") or {}) if vault.ok else {}
        people = sorted(k for k, v in jokes.items() if v)
        if people and tg.token and st.get("chat_id"):
            n = st.get("joke_rot", 0); st["joke_rot"] = n + 1
            who = people[n % len(people)]
            tg.call("sendMessage", chat_id=st["chat_id"], text=f"🖤 {next_joke(st, jokes, who)}")
            print("jokebeat sent:", who)
            return True
        return False

    if a.mode == "jokebeat":
        send_beat()
        save_json(STATE, st)
        return

    if a.mode == "joke":
        jokes = (vault.data.get("jokes") or {}) if vault.ok else {}
        key = next((k for k in jokes if a.who and a.who.lower() in k.lower()), None)
        if key and tg.token and st.get("chat_id"):
            tg.call("sendMessage", chat_id=st["chat_id"], text=f"🖤 {next_joke(st, jokes, key)}")
            print("joke sent:", key)
        else:
            print("joke: не найден человек или нет чата/токена")
        save_json(STATE, st)
        return

    # калибровка раз в неделю для каждой точки
    for sp in SPOT_IDS:
        cs = spot_cal(cal, sp)
        stale = not cs.get("calibrated") or cs["calibrated"] < (now - timedelta(days=7)).strftime("%Y-%m-%d")
        if a.mode == "calibrate" or stale:
            calibrate_models(cs, sp)

    # прогноз: полный расчёт не чаще раза в час (бережём бесплатный погодный сервис),
    # результат кладём в data/fc_<точка>.json — сайт показывает ровно те же цифры
    evs, metas = {}, {}
    last = st.get("fc_at")
    fresh = last and (now - datetime.fromisoformat(last)).total_seconds() < 55 * 60
    need = a.mode in ("noon", "evening", "test", "testpoll", "calibrate", "tomorrow") or not fresh or any(not FC_PATH[sp].exists() for sp in SPOT_IDS)
    for sp in SPOT_IDS:
        res = None
        if need:
            b = E.fetch_bundle(sp)
            if b:
                res = E.analyze(b, spot_cal(cal, sp), now)
                save_json(FC_PATH[sp], {"generated": now.isoformat(timespec="minutes"), "spot": sp, **res})
        if res is None:
            res = load_json(FC_PATH[sp], {}) or {}
            if res.get("evenings"):   # из файла: выкинуть уже прошедшие вечера
                res["evenings"] = [e for e in res["evenings"] if e["date"] >= today]
        evs[sp] = res.get("evenings") or []
        metas[sp] = res.get("meta") or {}
    if need and evs["medvedovka"]:
        st["fc_at"] = now.isoformat(timespec="minutes")
    ev, ev_k = evs["medvedovka"], evs["krasnaya"]
    print("forecast:", ", ".join(f"{e['date'][5:]} {e['pct']}% {e['status']}" for e in ev[:7]) or "нет данных",
          f"| recomputed={'yes' if need else 'no'} det={metas['medvedovka'].get('det', 0)} ens={metas['medvedovka'].get('ens', 0)}")
    for sp in SPOT_IDS:
        if need:
            log_forecast(st, sp, evs[sp])
    if ev:
        if a.mode == "testpoll" and tg.token and st.get("chat_id"):
            e = ev[0]
            send_poll(tg, st, f"Тест опроса (голоса в рейтинг не идут). Сегодня после 20:00 на Медведовке {evening_line(e)}.\n{SITE}",
                      "Тест: едешь сегодня?", e["date"], "test", vault)
        elif a.mode == "test" and tg.token and st.get("chat_id"):
            e = ev[0]
            tg.call("sendMessage", chat_id=st["chat_id"], text=with_joke(st, vault, f"Проверка связи. Сегодня после 20:00: {evening_line(e)}.\n{SITE}"))
        elif a.mode == "tomorrow" and tg.token and st.get("chat_id"):
            tmr = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            names = {"medvedovka": "Медведовка", "krasnaya": "KAPANY"}
            verdict = {"go": "едем", "maybe": "может быть", "dry": "сухо", "snow": "снег", "ice": "гололёд"}
            lines = []
            for sp in SPOT_IDS:
                e = next((x for x in evs[sp] if x["date"] == tmr), None)
                if e:
                    extra = f", воздух от {round(e['t_min'])}°" if e.get("t_min") is not None else ""
                    lines.append(f"{names[sp]}: {verdict.get(e['status'], e['status'])} — {evening_line(e)}{extra}")
            if lines:
                d = datetime.strptime(tmr, "%Y-%m-%d")
                dn = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][d.weekday()]
                tg.call("sendMessage", chat_id=st["chat_id"],
                        text=f"Прогноз на завтра ({dn}, {ru_date(tmr)}) после 20:00:\n" + "\n".join(lines) + f"\n{SITE}")
        elif slot != "calibrate":
            notify(tg, st, vault, evs, slot)
    if a.mode == "auto":
        ask_truth(tg, st)
    # анекдот раз в 4 часа днём: окна 10–14, 14–18, 18–22, 22–24
    if a.mode == "auto" and 10 <= h <= 23:
        win = f"{today}-{(h - 10) // 4}"
        if st.get("joke_win") != win and send_beat():
            st["joke_win"] = win
    truth = aggregate_truth(st)
    for sp in SPOT_IDS:
        verify(spot_cal(cal, sp), sp, truth)
    save_json(CAL, cal)
    seen = st.pop("_seen", [])
    save_json(STATE, st)
    diag = {"updates": seen[-20:], "mode": a.mode, "slot": slot,
            "telegram": bool(tg.token), "vault": vault.ok, "chat": bool(st.get("chat_id")),
            "evenings": len(ev), "evenings_krasnaya": len(ev_k), "det": metas["medvedovka"].get("det", 0), "ens": metas["medvedovka"].get("ens", 0),
            "errors": E.ERRORS[:30]}
    lr = ROOT / "data" / "last_run.json"
    old = load_json(lr, {}); old.pop("at", None)
    if old != diag:   # не коммитить каждые полчаса одно и то же
        save_json(lr, {"at": now.strftime("%Y-%m-%dT%H:%M"), **diag})


if __name__ == "__main__":
    main()
