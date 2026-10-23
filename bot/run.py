#!/usr/bin/env python3
"""Бот Медведовки: запускается GitHub Actions по расписанию.

Каждый запуск:
  1. забирает новые события из Telegram (добавление в чат, голоса в опросах) → статистика выездов;
  2. считает прогноз на 14 вечеров;
  3. раз в день пишет прогноз в журнал data/forecast_log.csv;
  4. объявляет новые сборы, в обед и вечером присылает опрос, если вечером мокро;
  5. сверяет старые прогнозы с реанализом ERA5 и обновляет data/calibration.json.

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
LOG = ROOT / "data" / "forecast_log.csv"
VERIFY = ROOT / "data" / "verify.csv"
CAL = ROOT / "data" / "calibration.json"
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
                  allowed_updates=["message", "my_chat_member", "poll_answer"]) or []
    changed_stats = False
    for u in ups:
        st["offset"] = u["update_id"] + 1
        mcm = u.get("my_chat_member")
        if mcm and mcm["chat"]["type"] in ("group", "supergroup") and mcm["new_chat_member"]["status"] in ("member", "administrator"):
            if not st.get("chat_id"):
                st["chat_id"] = mcm["chat"]["id"]
                st["hello"] = False
        msg = u.get("message")
        if msg and msg.get("chat", {}).get("type") in ("group", "supergroup") and not st.get("chat_id"):
            st["chat_id"] = msg["chat"]["id"]; st["hello"] = False
        if msg and msg.get("migrate_to_chat_id") and msg["chat"]["id"] == st.get("chat_id"):
            st["chat_id"] = msg["migrate_to_chat_id"]
        pa = u.get("poll_answer")
        if pa and vault.ok:
            poll = st.get("polls", {}).get(pa["poll_id"])
            user = pa.get("user")
            if poll and user:
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


def send_poll(tg: TG, st: dict, text: str, question: str, date: str, kind: str):
    chat = st.get("chat_id")
    if not chat:
        return False
    tg.call("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)
    m = tg.call("sendPoll", chat_id=chat, question=question, options=[{"text": "Еду"}, {"text": "Не еду"}], is_anonymous=False)
    if m and m.get("poll"):
        st.setdefault("polls", {})[m["poll"]["id"]] = {"date": date, "kind": kind}
        return True
    return False


def evening_line(e) -> str:
    s = f"мокро {e['pct']}%"
    if e["d_all"]:
        s += f" (модели {e['d_wet']}/{e['d_all']}"
        s += f", ансамбль {e['e_wet']}/{e['e_all']})" if e["e_all"] else ")"
    if e.get("t_min") is not None and e["t_min"] <= 1:
        s += ", возможен гололёд"
    return s


def notify(tg: TG, st: dict, vault: Vault, ev: list, slot: str):
    chat = st.get("chat_id")
    if not tg.token or not chat:
        return
    if not st.get("hello"):
        tg.call("sendMessage", chat_id=chat, text="Я на месте. Буду писать сюда, когда на Медведовке после 20:00 будет мокро, и присылать опросы «Еду / Не еду».\n" + SITE)
        st["hello"] = True
    today = E.now_msk().strftime("%Y-%m-%d")
    tomorrow = (E.now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    by_date = {e["date"]: e for e in ev}
    polled = {p["date"] for p in st.get("polls", {}).values()}

    # новые сборы
    ann = st.setdefault("announced", [])
    for m in (vault.data.get("meets") or []) if vault.ok else []:
        key = f"{m.get('date')} {m.get('time', '20:00')}"
        if not m.get("date") or m["date"] < today or key in ann:
            continue
        e = by_date.get(m["date"])
        text = f"Сбор: {ru_date(m['date'])}, {m.get('time', '20:00')}, Медведовка."
        if m.get("note"):
            text += f"\n{m['note']}"
        if e:
            text += f"\nПрогноз на вечер: {evening_line(e)}."
        text += f"\n{SITE}"
        if send_poll(tg, st, text, f"Едешь на сбор {ru_date(m['date'])}?", m["date"], "meet"):
            ann.append(key); polled.add(m["date"])

    e = by_date.get(today)
    if slot in ("noon", "evening") and e and e["status"] == "go" and today not in polled:
        lead = "Сегодня после 20:00 на Медведовке" if slot == "noon" else "Прогноз улучшился: сегодня после 20:00 на Медведовке"
        send_poll(tg, st, f"{lead} {evening_line(e)}.\n{SITE}", f"Сегодня 20:00, мокро {e['pct']}%. Едешь?", today, "weather")
    elif slot == "evening" and e and today in polled and e["status"] == "dry" and st.get("cancelled") != today:
        tg.call("sendMessage", chat_id=chat, text=f"Отбой: прогноз ухудшился, к вечеру мокро всего {e['pct']}%.")
        st["cancelled"] = today
    t = by_date.get(tomorrow)
    if slot == "noon" and t and t["status"] == "go" and st.get("heads_up") != tomorrow and tomorrow not in polled:
        tg.call("sendMessage", chat_id=chat, text=f"Завтра ({ru_date(tomorrow)}) после 20:00 на Медведовке {evening_line(t)}. Опрос пришлю завтра в обед, если прогноз устоит.")
        st["heads_up"] = tomorrow


def log_forecast(st: dict, ev: list):
    now = E.now_msk(); today = now.strftime("%Y-%m-%d")
    if st.get("logged") == today or not ev:
        return
    new = not LOG.exists()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        for e in ev:
            lead = (datetime.strptime(e["date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
            r = lambda x: "" if x is None else f"{x:.3f}"
            w.writerow({"issued": now.strftime("%Y-%m-%dT%H:%M"), "date": e["date"], "lead": lead, "pct": e["pct"],
                        "p_det": r(e["p_det"]), "p_ens": r(e["p_ens"]), "d_wet": e["d_wet"], "d_all": e["d_all"],
                        "e_wet": e["e_wet"], "e_all": e["e_all"], "status": e["status"]})
    st["logged"] = today


def verify(cal: dict):
    """Сверить журнал с ERA5 (данные появляются с задержкой ~6 дней)."""
    if not LOG.exists():
        return
    rows = list(csv.DictReader(LOG.open(encoding="utf-8")))
    done = {r["date"]: r for r in csv.DictReader(VERIFY.open(encoding="utf-8"))} if VERIFY.exists() else {}
    cutoff = (E.now_msk() - timedelta(days=6)).strftime("%Y-%m-%d")
    todo = sorted({r["date"] for r in rows if r["date"] <= cutoff and r["date"] not in done})
    if todo:
        start = (datetime.strptime(todo[0], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        j = E.era5(start, todo[-1])
        if j:
            obs, _, _ = E.observed_evenings(j)
            for d in todo:
                if d in obs:
                    done[d] = {"date": d, "wet": int(obs[d])}
            with VERIFY.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["date", "wet"]); w.writeheader()
                for d in sorted(done):
                    w.writerow({"date": d, "wet": done[d]["wet"]})
    # журнал «обещали / было»: прогноз, выпущенный в тот же день (lead 0)
    lead0 = {}
    for r in rows:
        if r["lead"] == "0":
            lead0[r["date"]] = r
    journal, hits, n = [], 0, 0
    pairs = []
    for d in sorted(done):
        r = lead0.get(d)
        if not r:
            continue
        o = int(done[d]["wet"]); pct = int(r["pct"])
        said_wet = pct >= 60; said_dry = pct < 30
        ok = (said_wet and o) or (said_dry and not o)
        if said_wet or said_dry:
            n += 1; hits += 1 if ok else 0
        journal.append({"date": d, "pct": pct, "wet": bool(o)})
        if r["p_det"] and r["p_ens"]:
            pairs.append((float(r["p_det"]), float(r["p_ens"]), o))
    cal["journal"] = journal[-30:]
    cal["verified"] = {"n": n, "hits": hits, "days": len(journal)}
    # доля ансамбля: подбираем по журналу, когда набралось достаточно вечеров
    if len(pairs) >= 30 and sum(o for _, _, o in pairs) >= 5:
        best = min((sum((s * pe + (1 - s) * pd - o) ** 2 for pd, pe, o in pairs), s) for s in [x / 20 for x in range(4, 19)])
        cal["ens_share"] = best[1]


def calibrate_models(cal: dict, days: int = 365):
    """Веса моделей по прошлому году: архив прогнозов Open-Meteo против ERA5."""
    end = (E.now_msk() - timedelta(days=7)).date()
    start = end - timedelta(days=days)
    obs_j = E.era5(start.isoformat(), end.isoformat())
    if not obs_j:
        print("ERA5 недоступен — веса не обновлены"); return
    obs, et0, times = E.observed_evenings(obs_j)
    base = sum(obs.values()) / max(1, len(obs))
    stats = {}
    for m, name in [("best_match", "Open-Meteo")] + E.DET_MODELS:
        url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={E.LAT}&longitude={E.LON}&timezone={E.TZ_Q}"
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
        cal["weights"] = {k: round(min(2.0, max(0.2, 1 + 2 * (1 - v["bs"] / ref))), 2) for k, v in stats.items()}
    cal["models"] = stats
    cal["base_rate"] = round(base, 3)
    cal["calibrated"] = E.now_msk().strftime("%Y-%m-%d")
    cal["period"] = [start.isoformat(), end.isoformat()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "noon", "evening", "collect", "calibrate", "test"])
    a = ap.parse_args()
    st = load_json(STATE, {})
    cal = load_json(CAL, {})
    tg = TG(os.environ.get("TELEGRAM_BOT_TOKEN") or None)
    vault = Vault(os.environ.get("CREW_BOT_CODE") or None)
    h = E.now_msk().hour
    slot = a.mode if a.mode != "auto" else ("noon" if 10 <= h < 16 else "evening" if 16 <= h < 20 else "collect")
    print(f"mode={a.mode} slot={slot} telegram={'yes' if tg.token else 'no'} vault={'yes' if vault.ok else 'no'} chat={'yes' if st.get('chat_id') else 'no'}")

    collect_updates(tg, st, vault)

    stale = not cal.get("calibrated") or cal["calibrated"] < (E.now_msk() - timedelta(days=7)).strftime("%Y-%m-%d")
    if a.mode == "calibrate" or stale:
        calibrate_models(cal)

    b = E.fetch_bundle()
    ev = E.analyze(b, cal) if b else []
    print("forecast:", ", ".join(f"{e['date'][5:]} {e['pct']}%" for e in ev[:7]) or "нет данных",
          f"| det={len(b['det']) + 1 if b else 0} ens={len(b['ens']) if b else 0}")
    if ev:
        log_forecast(st, ev)
        if a.mode == "test" and tg.token and st.get("chat_id"):
            e = ev[0]
            tg.call("sendMessage", chat_id=st["chat_id"], text=f"Проверка связи. Сегодня после 20:00: {evening_line(e)}.\n{SITE}")
        elif slot != "calibrate":
            notify(tg, st, vault, ev, slot)
    verify(cal)
    save_json(CAL, cal)
    save_json(STATE, st)
    save_json(ROOT / "data" / "last_run.json", {
        "at": E.now_msk().strftime("%Y-%m-%dT%H:%M"), "mode": a.mode, "slot": slot,
        "telegram": bool(tg.token), "vault": vault.ok, "chat": bool(st.get("chat_id")),
        "evenings": len(ev), "det": (len(b["det"]) + 1) if b else 0, "ens": len(b["ens"]) if b else 0,
        "errors": E.ERRORS[:30]})


if __name__ == "__main__":
    main()
