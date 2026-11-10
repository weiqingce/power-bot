"""Диагностика входа в Xiaomi: показывает, ПОЧЕМУ Xiaomi отказывает.
Ничего секретного (логин, пароль, токены) не печатает."""
import os, json, hashlib, requests

user = os.environ.get("MI_USER", "").strip()
pwd = os.environ.get("MI_PASS", "")

kind = ("Xiaomi ID (цифры)" if user.isdigit() else
        "телефон с +" if user.startswith("+") else
        "почта" if "@" in user else "другой формат")
print(f"Логин: {kind}, длина {len(user)}; пробелы по краям в секрете: {os.environ.get('MI_USER','') != user}")
print(f"Пароль задан: {bool(pwd)}, длина {len(pwd)}")

s = requests.Session()
s.headers["User-Agent"] = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-APP-ID-xiaomi-home"
r1 = s.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true")
j1 = json.loads(r1.text.replace("&&&START&&&", ""))
print("Шаг 1: HTTP", r1.status_code, "| есть _sign:", "_sign" in j1)

r2 = s.post("https://account.xiaomi.com/pass/serviceLoginAuth2", data={
    "sid": "xiaomiio", "hash": hashlib.md5(pwd.encode()).hexdigest().upper(),
    "callback": "https://sts.api.io.mi.com/sts", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
    "user": user, "_json": "true", "_sign": j1.get("_sign", "")})
j2 = json.loads(r2.text.replace("&&&START&&&", ""))
safe = {k: j2.get(k) for k in ("result", "code", "desc", "description", "reason") if k in j2}
print("Шаг 2:", safe)
print("Нужна капча:", bool(j2.get("captchaUrl")))
print("Нужно подтверждение входа (2FA):", bool(j2.get("notificationUrl")))
print("Вход успешен:", j2.get("result") == "ok" and bool(j2.get("ssecurity")))
