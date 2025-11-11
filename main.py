import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime, timedelta
import pytz # Потрібен для роботи з часовими зонами
import json
import re

# --- Створюємо "мозок" ---
app = FastAPI()

# --- Глобальні константи ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'
AJAX_URL = DTEK_URL + '/ua/ajax'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'

# --- Словник станів ---
# "Світла немає" або "Може не бути"
OUTAGE_STATES = ("no", "first", "second", "mfirst", "msecond", "maybe")

# --- Допоміжні функції ---

def is_off_now(status: str, minute: int) -> bool:
    """Перевіряє, чи вимкнено світло в поточну хвилину."""
    if status == 'no' or status == 'maybe':
        return True
    if (status == 'first' or status == 'mfirst') and minute < 30:
        return True
    if (status == 'second' or status == 'msecond') and minute >= 30:
        return True
    return False

def get_time_range(hour_key: str, status: str, time_zone_map: dict) -> (str, str):
    """Повертає точний час початку та кінця для 30-хв блоків."""
    hour_range = time_zone_map.get(hour_key) # ["19-20", "19:00", "20:00"]
    if not hour_range:
        return None, None
        
    start_time = hour_range[1]
    end_time = hour_range[2]

    if status in ("first", "mfirst"):
        end_time = start_time[:3] + "30" # "19:30"
    elif status in ("second", "msecond"):
        start_time = start_time[:3] + "30" # "19:30"
        
    return start_time, end_time

def find_block_end(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str):
    """
    Знаходить час закінчення поточного блоку відключень.
    start_hour_index: 0-23 (поточна година)
    """
    
    # 1. Шукаємо кінець сьогодні (починаючи з НАСТУПНОЇ години)
    for i in range(start_hour_index + 1, 24): # 0-23 -> 1-24
        hour_key = str(i + 1)
        status = schedule_today.get(hour_key)
        
        if status not in OUTAGE_STATES:
            # Знайшли кінець! Це початок "світлої" зони
            _start, end_time = get_time_range(hour_key, "yes", time_zone_map)
            return end_time, "" # Повертаємо час початку "yes"

    # 2. Якщо о 24:00 (індекс 23) світла ще немає, шукаємо кінець завтра
    for i in range(0, 24): # 0-23
        hour_key = str(i + 1)
        status = schedule_tomorrow.get(hour_key)
        
        if status not in OUTAGE_STATES:
            # Знайшли кінець!
            _start, end_time = get_time_range(hour_key, "yes", time_zone_map)
            return end_time, " (наступного дня)"

    return "24:00", " (наступного дня)" # Якщо відключення триває весь завтрашній день

def find_next_block(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str, str):
    """
    Знаходить час початку та кінця НАСТУПНОГО блоку.
    start_hour_index: 0-23 (поточна година)
    """
    
    start_time = None
    end_time = None
    end_day_suffix = ""

    # 1. Шукаємо ПОЧАТОК наступного блоку СЬОГОДНІ
    # (починаючи з НАСТУПНОЇ години)
    for i in range(start_hour_index + 1, 24): 
        hour_key = str(i + 1) # 1-24
        status = schedule_today.get(hour_key)
        
        if status in OUTAGE_STATES:
            # Знайшли початок!
            start_time, _end = get_time_range(hour_key, status, time_zone_map)
            
            # 2. Тепер шукаємо КІНЕЦЬ цього блоку
            end_time, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, i, time_zone_map)
            
            return start_time, end_time, end_day_suffix

    # 3. Якщо СЬОГОДНІ більше нічого немає, шукаємо ПОЧАТОК блоку ЗАВТРА
    for i in range(0, 24):
        hour_key = str(i + 1) # 1-24
        status = schedule_tomorrow.get(hour_key)
        
        if status in OUTAGE_STATES:
            # Знайшли початок!
            start_time, _end = get_time_range(hour_key, status, time_zone_map)
            
            # 4. Шукаємо КІНЕЦЬ цього завтрашнього блоку
            end_time, _suffix = find_block_end(schedule_tomorrow, {}, i, time_zone_map) # Pass empty dict for "day after tomorrow"
            
            return start_time, end_time, " (наступного дня)"

    return None, None, None # Більше відключень немає


# --- Головний метод (ендпоіінт) ---
@app.get("/check")
async def check_power_outage(city: str = "", street: str = "", house: str = ""):
    
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        # --- Крок 1: Отримуємо "Печатку" (CSRF-токен) ---
        response_main = session.get(SHUTDOWNS_PAGE)
        soup = BeautifulSoup(response_main.text, 'html.parser')
        token_tag = soup.find('meta', {'name': 'csrf-token'})
        if not token_tag:
            return {"status": "error", "message": "Не можу отримати токен (CSRF)."}

        fresh_token = token_tag['content']
        session.headers.update({
            'x-csrf-token': fresh_token,
            'x-requested-with': 'XMLHttpRequest',
            'Referer': SHUTDOWNS_PAGE
        })

        # --- Крок 2: Робимо AJAX-запит ---
        tz_kyiv = pytz.timezone('Europe/Kiev')
        now = datetime.now(tz_kyiv)
        current_time_kyiv_str = now.strftime("%d.%m.%Y %H:%M")

        payload = {
            'method': 'getHomeNum',
            'data[0][name]': 'city', 'data[0][value]': city,
            'data[1][name]': 'street', 'data[1][value]': street,
            'data[2][name]': 'updateFact', 'data[2][value]': current_time_kyiv_str
        }
        
        response_ajax = session.post(AJAX_URL, data=payload)
        json_data = response_ajax.json()

        if not house:
             # (Логіка для списків)
            list_data = json_data.get("data")
            if isinstance(list_data, list): return {"available_list": list_data}
            elif isinstance(list_data, dict): return {"available_list": list(list_data.keys())}
            else: return {"status": "error", "message": "Адресу не знайдено."}

        # --- Крок 3: АНАЛІЗУЄМО ВІДПОВІДЬ ---
        
        # 1. Знаходимо "ГРУПУ" будинку (напр. "GPV6.1")
        house_info = json_data.get("data", {}).get(house)
        if not house_info:
            return {"status": "error", "message": "Дім не знайдено."}

        group_name_list = house_info.get("sub_type_reason")
        if not group_name_list or len(group_name_list) == 0:
            outage_type = house_info.get("type")
            if outage_type and outage_type != "":
                 return { "status": "warning", "message": "Світла немає (Аварійно)", "start_time": "", "end_time": "", "type": "Аварійне" }
            return {"status": "error", "message": "Дім знайдено, але не знайдено групу (sub_type_reason)."}
        
        group_name = group_name_list[0] # "GPV6.1"

        # 2. Знаходимо "сховані" графіки
        preset = json_data.get("preset")
        if not preset:
            return {"status": "error", "message": "Не можу знайти 'preset' (тижневий графік) у JSON."}

        time_zone_map = preset.get("time_zone", {})
        
        # (Понеділок = 0, ... Неділя = 6). Сайт ДТЕК: Понеділок = 1.
        today_index = now.weekday() + 1
        current_day_of_week_key = str(today_index) 
        
        # ✅ Отримуємо ключ на завтра (1->2, 6->7, 7->1)
        tomorrow_index = (today_index % 7) + 1
        tomorrow_day_of_week_key = str(tomorrow_index)
        
        schedule_all_groups = preset.get("data", {}).get(group_name)
        if not schedule_all_groups:
             return {"status": "error", "message": f"Не можу знайти групу {group_name} у 'preset'."}

        schedule_today = schedule_all_groups.get(current_day_of_week_key)
        schedule_tomorrow = schedule_all_groups.get(tomorrow_day_of_week_key, {}) # {} = порожній графік
        
        if not schedule_today:
             return {"status": "error", "message": f"Не можу знайти графік на сьогодні (день {current_day_of_week_key})."}

        # --- Аналіз графіка ---
        current_hour_index = now.hour # 0-23
        current_hour_key = str(current_hour_index + 1) # 1-24
        current_minute = now.minute 

        current_status = schedule_today.get(current_hour_key)
        if not current_status or not time_zone_map.get(current_hour_key):
             return {"status": "error", "message": f"Не можу знайти поточну годину {current_hour_key} у графіку."}

        # 3. Перевіряємо, чи світло вимкнене ЗАРАЗ
        if is_off_now(current_status, current_minute):
            
            start_time, _ = get_time_range(current_hour_key, current_status, time_zone_map)
            
            # Шукаємо, коли цей блок закінчиться (можливо, завтра)
            block_end, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)

            return {
                "status": "warning", # "warning" = світла немає ЗАРАЗ
                "message": f"Світла немає (до {block_end}{end_day_suffix})",
                "start_time": start_time,
                "end_time": block_end,
                "type": "Планове"
            }

        # 4. Якщо світло ЗАРАЗ є, шукаємо НАСТУПНЕ відключення
        next_start, next_end, end_day_suffix = find_next_block(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)
        
        if next_start and next_end:
             return {
                "status": "ok", # Світло є
                "message": f"Відключення з {next_start} до {next_end}{end_day_suffix}",
                "start_time": next_start,
                "end_time": next_end,
                "type": "Планове"
            }

        # 5. Якщо сьогодні і завтра нічого немає
        return {
            "status": "ok",
            "message": "Відключень не заплановано",
            "start_time": "",
            "end_time": "",
            "type": ""
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутрішня помилка API: {str(e)}"}

# --- Команда для запуску локально (для тестів) ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
