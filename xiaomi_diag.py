"""Диагностика входа в Xiaomi по ключу сессии. Секретов не печатает."""
import os, logging
logging.basicConfig(level=logging.INFO)
from power_bot import xiaomi_login

mc = xiaomi_login()
devices = mc.get_devices(country=(os.getenv("MI_COUNTRY") or "ru")) or []
print(f"Устройств найдено: {len(devices)}, онлайн: {sum(1 for d in devices if d.get('isOnline'))}")
