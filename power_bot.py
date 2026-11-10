"""
Telegram-бот: уведомляет, есть ли дома свет, и раз в неделю присылает сводку.
Логика: если хоть одно устройство из Mi Home в сети — свет есть;
если все офлайн OFF_CONFIRM проверок подряд — света нет.
Запускать на внешнем сервере (не дома!).

pip install micloud requests
python power_bot.py            — обычный режим
python power_bot.py --summary  — вывести сводку за текущую неделю (для проверки)
python power_bot.py --once     — одна проверка и выход (для GitHub Actions)
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


def load_env_file(path: str = "settings.txt") -> None:
    """Подхватывает настройки из settings.txt рядом со скриптом."""
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env_file()

TZ = ZoneInfo("Europe/Simferopol")
STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"   # журнал: каждая строка — {"ts": ..., "power": true/false}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# ---------- хранение ----------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"power": None, "since": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def log_event(ts: float, power: bool) -> None:
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "power": power}) + "\n")


def load_events() -> list:
    events = []
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return sorted(events, key=lambda e: e["ts"])


# ---------- форматирование ----------

def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def fmt_dur(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


# ---------- недельная сводка ----------

def on_intervals(events: list, until: float) -> list:
    """Список отрезков (начало, конец), когда свет был."""
    result, start = [], None
    for e in events:
        if e["power"] and start is None:
            start = e["ts"]
        elif not e["power"] and start is not None:
            result.append((start, e["ts"]))
            start = None
    if start is not None:
        result.append((start, until))
    return result


def build_week_summary(week_start: datetime) -> str:
    events = load_events()
    week_end = week_start + timedelta(days=7)
    intervals = on_intervals(events, min(time.time(), week_end.timestamp()))
    first_ts = events[0]["ts"] if events else None

    lines = [f"📊 Свет за неделю {week_start:%d.%m}–{week_end - timedelta(days=1):%d.%m}\n"]
    week_total = 0.0

    for i in range(7):
        ds = week_start + timedelta(days=i)
        de = ds + timedelta(days=1)
        ds_ts, de_ts = ds.timestamp(), de.timestamp()
        head = f"{WEEKDAYS[i]} {ds:%d.%m}"

        if first_ts is None or de_ts <= first_ts or ds_ts > time.time():
            lines.append(f"{head}: нет данных")
            continue

        parts, day_total = [], 0.0
        for s, e in intervals:
            s2, e2 = max(s, ds_ts), min(e, de_ts)
            if e2 > s2:
                start_txt = datetime.fromtimestamp(s2, TZ).strftime("%H:%M")
                end_txt = "24:00" if e2 >= de_ts else datetime.fromtimestamp(e2, TZ).strftime("%H:%M")
                parts.append(f"с {start_txt} до {end_txt}")
                day_total += e2 - s2
        week_total += day_total

        note = ""
        if ds_ts < first_ts:
            note = f" (данные с {datetime.fromtimestamp(first_ts, TZ):%H:%M})"
        if parts:
            lines.append(f"{head}: свет был {' и '.join(parts)} — всего {fmt_dur(day_total)}{note}")
        else:
            lines.append(f"{head}: света не было{note}")

    lines.append(f"\nИтого за неделю: {fmt_dur(week_total)}")
    return "\n".join(lines)


def current_monday() -> datetime:
    now = datetime.now(TZ)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- основной цикл ----------

def main() -> None:
    from micloud import MiCloud

    tg_token = os.environ["TG_TOKEN"]
    tg_chat_id = os.environ["TG_CHAT_ID"]
    mi_user = os.environ["MI_USER"]
    mi_pass = os.environ["MI_PASS"]
    mi_country = (os.getenv("MI_COUNTRY") or "ru")
    interval = int(os.getenv("INTERVAL", "60"))
    off_confirm = int(os.getenv("OFF_CONFIRM", "3"))

    def send(text: str) -> None:
        requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat_id, "text": text},
            timeout=15,
        )

    state = load_state()
    # при первом запуске не шлём сводку за неделю, когда бот ещё не работал
    if "last_summary" not in state:
        state["last_summary"] = (current_monday() - timedelta(days=7)).strftime("%Y-%m-%d")
        save_state(state)

    mc = None
    off_count = 0
    first_off_ts = None

    while True:
        # --- недельная сводка: в понедельник после 00:05 за прошлую неделю ---
        try:
            this_monday = current_monday()
            prev_monday = this_monday - timedelta(days=7)
            key = prev_monday.strftime("%Y-%m-%d")
            if state.get("last_summary") != key and datetime.now(TZ) >= this_monday + timedelta(minutes=5):
                send(build_week_summary(prev_monday))
                state["last_summary"] = key
                save_state(state)
                logging.info("Сводка за неделю %s отправлена", key)
        except Exception as e:
            logging.warning("Ошибка сводки: %s", e)

        # --- проверка устройств ---
        try:
            if mc is None:
                mc = MiCloud(mi_user, mi_pass)
                mc.login()
                logging.info("Вход в Xiaomi выполнен")

            devices = mc.get_devices(country=mi_country)
            if devices is None:
                raise RuntimeError("облако Xiaomi не вернуло список устройств")

            online = [d.get("name", "?") for d in devices if d.get("isOnline")]
            now = time.time()
            logging.info("Онлайн: %s", online or "никого")

            if online:
                power = True
                off_count, first_off_ts = 0, None
                event_ts = now
            else:
                off_count += 1
                first_off_ts = first_off_ts or now
                if off_count < off_confirm:
                    time.sleep(interval)
                    continue
                power = False
                event_ts = first_off_ts

            if power != state["power"]:
                prev = state["since"]
                if power:
                    msg = f"💡 Свет дали — {fmt_time(event_ts)}\nВ сети: {', '.join(online)}"
                    if prev and state["power"] is False:
                        msg += f"\nСвета не было: {fmt_dur(event_ts - prev)}"
                else:
                    msg = f"🔌 Света нет — с ~{fmt_time(event_ts)}"
                    if prev and state["power"] is True:
                        msg += f"\nСвет был: {fmt_dur(event_ts - prev)}"
                send(msg)
                log_event(event_ts, power)
                state.update({"power": power, "since": event_ts})
                save_state(state)

        except Exception as e:
            logging.warning("Ошибка: %s — переподключусь", e)
            mc = None

        time.sleep(interval)


# ---------- вход в Xiaomi ----------

def xiaomi_login():
    """Возвращает залогиненный MiCloud.
    Если задан MI_PASS_TOKEN — входит по ключу сессии (без пароля и без подтверждения входа).
    Иначе — по логину и паролю."""
    import json as _json
    from micloud import MiCloud

    user = os.environ["MI_USER"].strip()
    token = (os.getenv("MI_PASS_TOKEN") or "").strip()
    mc = MiCloud(user, os.getenv("MI_PASS") or "-")

    if not token:
        if not mc.login():
            raise SystemExit("Не удалось войти в Xiaomi по паролю")
        return mc

    mc._init_session()
    for domain in ("account.xiaomi.com", ".account.xiaomi.com"):
        mc.session.cookies.set("passToken", token, domain=domain)
        mc.session.cookies.set("userId", user, domain=domain)
    r = mc.session.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true")
    j = _json.loads(r.text.replace("&&&START&&&", ""))
    if not j.get("ssecurity") or not j.get("location"):
        raise SystemExit(
            "Ключ сессии Xiaomi не подошёл или устарел (code="
            f"{j.get('code')}). Обнови секрет MI_PASS_TOKEN.")
    mc.user_id = j.get("userId") or user
    mc.ssecurity = j["ssecurity"]
    mc._login_step3(j["location"])
    if not mc.service_token:
        raise SystemExit("Xiaomi не выдал serviceToken по ключу сессии")
    logging.info("Вход в Xiaomi по ключу сессии выполнен")
    return mc


# ---------- режим GitHub Actions: один короткий запуск ----------

def run_once() -> None:
    """Проверяет свет и завершается. Запускается по расписанию каждые 5 минут.
    Свет есть — если хоть одно устройство онлайн.
    Света нет — если все устройства офлайн CHECKS проверок подряд с паузой PAUSE сек."""

    tg_token = os.environ["TG_TOKEN"]
    tg_chat_id = os.environ["TG_CHAT_ID"]
    checks = int(os.getenv("CHECKS", "3"))
    pause = int(os.getenv("PAUSE", "60"))

    def send(text: str) -> None:
        r = requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat_id, "text": text},
            timeout=15,
        )
        if not r.ok:
            raise RuntimeError(f"Telegram ответил ошибкой {r.status_code}: {r.text}")
        state["last_msg"] = time.time()

    status_every = int(os.getenv("STATUS_EVERY_MIN", "60")) * 60
    state = load_state()
    state.setdefault("last_msg", time.time())

    # недельная сводка: первый запуск после 00:05 понедельника — за прошлую неделю
    this_monday = current_monday()
    prev_key = (this_monday - timedelta(days=7)).strftime("%Y-%m-%d")
    if "last_summary" not in state:
        state["last_summary"] = prev_key   # первый запуск — сводку за пустую неделю не шлём
    elif state["last_summary"] != prev_key and datetime.now(TZ) >= this_monday + timedelta(minutes=5):
        send(build_week_summary(this_monday - timedelta(days=7)))
        state["last_summary"] = prev_key
        logging.info("Недельная сводка отправлена")

    try:
        mc = xiaomi_login()
    finally:
        save_state(state)

    power, online, first_off = None, [], None
    for i in range(checks):
        devices = mc.get_devices(country=(os.getenv("MI_COUNTRY") or "ru"))
        if devices is None:
            raise SystemExit("Облако Xiaomi не вернуло список устройств")
        online = [d.get("name", "?") for d in devices if d.get("isOnline")]
        logging.info("Проверка %d: онлайн %d из %d", i + 1, len(online), len(devices))
        if online:
            power = True
            break
        first_off = first_off or time.time()
        if state.get("power") is False:      # уже знаем, что света нет — подтверждать не нужно
            break
        if i < checks - 1:
            time.sleep(pause)
    if power is None:
        power = False

    if power != state.get("power"):
        event_ts = time.time() if power else first_off
        prev = state.get("since")
        if power:
            msg = f"💡 Свет дали — {fmt_time(event_ts)}\nВ сети: {', '.join(online)}"
            if prev and state.get("power") is False:
                msg += f"\nСвета не было: {fmt_dur(event_ts - prev)}"
        else:
            msg = f"🔌 Света нет — с ~{fmt_time(event_ts)}"
            if prev and state.get("power") is True:
                msg += f"\nСвет был: {fmt_dur(event_ts - prev)}"
        send(msg)
        log_event(event_ts, power)
        state.update({"power": power, "since": event_ts})
        logging.info("Состояние изменилось: %s", "свет есть" if power else "света нет")

    # обычный режим: статус не чаще раза в час (с запасом 5 мин на задержки GitHub)
    elif time.time() - state["last_msg"] >= status_every - 300:
        since = state.get("since")
        dur = f" уже {fmt_dur(time.time() - since)} (с {fmt_time(since)})" if since else ""
        send(("💡 Свет есть" if power else "🔌 Света нет") + dur)
        logging.info("Отправлен ежечасный статус")

    save_state(state)


if __name__ == "__main__":
    if "--summary" in sys.argv:
        print(build_week_summary(current_monday()))
    elif "--once" in sys.argv:
        run_once()
    else:
        main()
