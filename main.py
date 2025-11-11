import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime
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

# --- Допоміжна функція: Пошук наступного відключення ---
def find_next_outage(schedule: dict, current_hour_key: str, time_zone_map: dict):
    """
    Шукає наступне відключення, починаючи з поточної години.
    """
    current_hour_index = int(current_hour_key) # Поточна година (напр., 19 для 19:30)

    # Шукаємо з поточної години до кінця дня
    for i in range(current_hour_index, 25): # 25, тому що range не включає останнє
        hour_key = str(i)
        status = schedule.get(hour_key)
        
        # 'maybe' також вважаємо можливим відключенням
        if status in ("no", "first", "second", "mfirst", "msecond", "maybe"):
            # Знайшли!
            hour_range = time_zone_map.get(hour_key)
            if not hour_range:
                continue # Дивна година, пропускаємо

            start_time = hour_range[1] # "19:00"
            end_time = hour_range[2]   # "20:00"
            
            # Уточнюємо час для 30-хвилинних блоків
            if status in ("first", "mfirst"):
                end_time = start_time[:3] + "30" # "19:30"
            elif status in ("second", "msecond"):
                start_time = start_time[:3] + "30" # "19:30"
                
            return start_time, end_time
            
    return None, None # Більше відключень сьогодні немає

# --- Головний метод (ендпоінт) ---
@app.get("/check")
async def check_power_outage(city: str = "", street: str = "", house: str = ""):
    
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        # --- Крок 1: Отримуємо "Печатку" (CSRF-токен) ---
        response_main = session.get(SHUTDOWNS_PAGE)
        if response_main.status_code != 200:
            return {"status": "error", "message": "Сайт ДТЕК не відповідає (головна)."}

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
        
        # Отримуємо поточний час для Києва
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
        
        if response_ajax.status_code != 200:
            return {"status": "error", "message": f"Помилка AJAX-запиту ({response_ajax.status_code})"}

        json_data = response_ajax.json()

        # Якщо дім не вказано (запит списку вулиць/міст)
        if not house:
            list_data = json_data.get("data")
            if isinstance(list_data, list): return {"available_list": list_data}
            elif isinstance(list_data, dict): return {"available_list": list(list_data.keys())}
            else: return {"status": "error", "message": "Адресу не знайдено."}

        # ---
        # ✅ Крок 3: АНАЛІЗУЄМО ВІДПОВІДЬ (Нова логіка)
        # ---
        
        # 1. Знаходимо "ГРУПУ" будинку (напр. "GPV6.1")
        house_info = json_data.get("data", {}).get(house)
        if not house_info:
            return {"status": "error", "message": "Дім не знайдено."}

        group_name_list = house_info.get("sub_type_reason")
        if not group_name_list or len(group_name_list) == 0:
             # Обробляємо аварійне (старий метод)
            outage_type = house_info.get("type")
            if outage_type and outage_type != "":
                 return { "status": "warning", "message": "Світла немає (Аварійно)", "start_time": "", "end_time": "", "type": "Аварійне" }
            return {"status": "error", "message": "Дім знайдено, але не знайдено групу (sub_type_reason)."}
        
        group_name = group_name_list[0] # "GPV6.1"

        # 2. Знаходимо "сховані" графіки
        preset = json_data.get("preset")
        fact = json_data.get("fact")
        
        if not preset or not fact:
            return {"status": "error", "message": "Не можу знайти 'preset' або 'fact' у JSON."}

        time_zone_map = preset.get("time_zone", {})
        today_timestamp_str = str(fact.get("today", 0))
        todays_schedule_all_groups = fact.get("data", {}).get(today_timestamp_str)
        
        if not todays_schedule_all_groups:
             return {"status": "error", "message": f"Не можу знайти графік на сьогодні ({today_timestamp_str})."}

        schedule = todays_schedule_all_groups.get(group_name)
        if not schedule:
             return {"status": "error", "message": f"Не можу знайти групу {group_name} у графіку на сьогодні."}

        # --- Аналіз графіка ---
        current_hour_index = now.hour # Наприклад, 19 (для 19:30)
        current_hour_key = str(current_hour_index + 1) # Ключ для 19:00 - "20"
        current_minute = now.minute # 30

        current_status = schedule.get(current_hour_key)
        if not current_status or not time_zone_map.get(current_hour_key):
             return {"status": "error", "message": f"Не можу знайти поточну годину {current_hour_key} у графіку."}

        current_hour_range = time_zone_map.get(current_hour_key) # ["19-20", "19:00", "20:00"]
        start_time = current_hour_range[1]
        end_time = current_hour_range[2]

        # 3. Перевіряємо, чи світло вимкнене ЗАРАЗ
        is_off_now = False
        if current_status == 'no':
            is_off_now = True
        elif current_status == 'first' and current_minute < 30:
            is_off_now = True
            end_time = start_time[:3] + "30" # "19:30"
        elif current_status == 'second' and current_minute >= 30:
            is_off_now = True
            start_time = start_time[:3] + "30" # "19:30"
        
        # 'maybe' також означає, що світла *може* не бути (вважаємо, що немає)
        elif current_status == 'maybe':
             is_off_now = True
        elif current_status == 'mfirst' and current_minute < 30:
             is_off_now = True
             end_time = start_time[:3] + "30"
        elif current_status == 'msecond' and current_minute >= 30:
             is_off_now = True
             start_time = start_time[:3] + "30"

        if is_off_now:
            return {
                "status": "warning", # "warning" = світла немає ЗАРАЗ
                "message": f"Світла немає (до {end_time})",
                "start_time": start_time,
                "end_time": end_time,
                "type": "Планове"
            }

        # 4. Якщо світло ЗАРАЗ є, шукаємо НАСТУПНЕ відключення
        next_start, next_end = find_next_outage(schedule, int(current_hour_key), time_zone_map)
        
        if next_start:
             return {
                "status": "ok", # Світло є
                "message": f"Відключення о {next_start}", # <-- ✅ ЦЕ ВАША МЕТА
                "start_time": next_start,
                "end_time": next_end,
                "type": "Планове"
            }

        # 5. Якщо сьогодні більше нічого немає
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
