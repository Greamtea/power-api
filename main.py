import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime, timedelta
import pytz
import re

# --- Створення "мозку" ---
app = FastAPI()

# --- Глобальні константи ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'
AJAX_URL = DTEK_URL + '/ua/ajax'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'

# --- Словник станів ---
# "Світла немає" або "Може не бути" (з будь-якого графіка)
OUTAGE_STATES = ("no", "first", "second", "mfirst", "msecond", "maybe")
POWER_ON_STATES = ("yes", "cell-non-scheduled")

# --- Допоміжні функції ---

def is_off_now(status: str, minute: int) -> bool:
    """Перевіряє, чи вимкнено світло в поточну хвилину."""
    if status in ('no', 'maybe'):
        return True
    if (status in ('first', 'mfirst')) and minute < 30:
        return True
    if (status in ('second', 'msecond')) and minute >= 30:
        return True
    return False

def get_time_range(hour_key: str, status: str, time_zone_map: dict) -> (str, str):
    """Повертає точний час початку та кінця для 30-хв блоків."""
    hour_range = time_zone_map.get(hour_key)
    if not hour_range: return None, None
        
    start_time = hour_range[1]
    end_time = hour_range[2]

    if status in ("first", "mfirst"):
        end_time = start_time[:3] + "30"
    elif status in ("second", "msecond"):
        start_time = start_time[:3] + "30"
    
    return start_time, end_time

def expand_schedule(raw_schedule: dict, time_zone_map: dict) -> dict:
    """
    Перетворює погодинний графік у 30-хвилинний формат.
    Формат: "00:00": "off", "00:30": "off", "01:00": "on", "01:30": "on"
    """
    expanded_schedule = {}
    for hour_key in range(1, 25):
        h_key = str(hour_key)
        status = raw_schedule.get(h_key)
        
        if not status or h_key not in time_zone_map:
            continue
            
        start_hour = time_zone_map[h_key][1][:2]
        
        time_00 = f"{start_hour}:00"
        time_30 = f"{start_hour}:30"
        
        if status == 'yes' or status == 'cell-non-scheduled':
            expanded_schedule[time_00] = "on"
            expanded_schedule[time_30] = "on"
        elif status == 'no' or status == 'maybe':
            expanded_schedule[time_00] = "off"
            expanded_schedule[time_30] = "off"
        elif status == 'first' or status == 'mfirst':
            # Світла немає перші 30 хв (off-on)
            expanded_schedule[time_00] = "off"
            expanded_schedule[time_30] = "on"
        elif status == 'second' or status == 'msecond':
            # Світла немає другі 30 хв (on-off)
            expanded_schedule[time_00] = "on"
            expanded_schedule[time_30] = "off"

    return expanded_schedule

def find_block_end(schedule_current: dict, schedule_next_day: dict, start_hour_index: int, time_zone_map: dict) -> (str, str):
    """
    Знаходить час закінчення поточного блоку відключень, враховуючи 30-хвилинні інтервали.
    start_hour_index: час, з якого почалося відключення (0-23).
    """
    
    # 1. Шукаємо кінець сьогодні
    for i in range(start_hour_index, 24):
        hour_key = str(i + 1)
        status = schedule_current.get(hour_key)
        
        if status in POWER_ON_STATES or status is None:
            # Знайшли годину, де світло є. Визначаємо точний час закінчення відключення.
            
            if status == 'yes' or status == 'cell-non-scheduled':
                _start_on, _ = get_time_range(hour_key, status, time_zone_map)
                return _start_on, ""
            
            if status in ("first", "mfirst"):
                _start_on, _ = get_time_range(hour_key, status, time_zone_map)
                return _start_on, ""
            
            if status in ("second", "msecond"):
                _start, end_time = get_time_range(hour_key, status, time_zone_map) # end_time = XX:30
                return end_time, ""
            
    
    # 2. Якщо дійшли до 24:00, перевіряємо завтра
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_next_day.get(hour_key)
        
        if status in POWER_ON_STATES or status is None:
            # Знайшли годину, де світло є.
            _start_on, _ = get_time_range(hour_key, status, time_zone_map)
            
            if status == 'yes' or status == 'cell-non-scheduled' or status in ("first", "mfirst"):
                return _start_on, " (наступного дня)"
                
            if status in ("second", "msecond"):
                 _start, end_time = get_time_range(hour_key, status, time_zone_map)
                 return end_time, " (наступного дня)"

    # Якщо відключення йде до кінця графіка
    return "24:00", " (наступного дня)"

def find_next_block(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str, str):
    """Знаходить час початку та кінця НАСТУПНОГО блоку."""
    
    # 1. Шукаємо ПОЧАТОК наступного блоку СЬОГОДНІ
    for i in range(start_hour_index + 1, 24): 
        hour_key = str(i + 1)
        status = schedule_today.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            
            # Находимо кінець цього блоку, використовуючи schedule_tomorrow
            end_time, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, i, time_zone_map)
            return start_time, end_time, end_day_suffix

    # 2. Якщо СЬОГОДНІ більше нічого немає, шукаємо НАЧАЛО блока ЗАВТРА
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_tomorrow.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            
            # Находимо кінець цього блоку (завтрашній графік)
            end_time, _suffix = find_block_end(schedule_tomorrow, {}, i, time_zone_map) 
            
            return start_time, end_time, " (наступного дня)"

    return None, None, None

def parse_yellow_info(html_content: str):
    """Парсить інформацію з жовтої рамки (активне відключення)."""
    soup = BeautifulSoup(html_content, 'html.parser')
    # ✅ ВИПРАВЛЕНО: Шукаємо "discon-current-outage" - це і є жовта рамка
    yellow_div = soup.find('div', {'class': 'discon-current-outage'})
    
    if not yellow_div: return None

    # Причина
    # Ми беремо текст до "Час початку" або "Орієнтовний час"
    reason_match = re.search(r'Причина:\s*(.*?)\s*Час початку', yellow_div.text, re.DOTALL)
    if not reason_match:
        reason_match = re.search(r'Причина:\s*(.*?)Орієнтовний час', yellow_div.text, re.DOTALL)

    reason = reason_match.group(1).strip() if reason_match else "Невідома причина"
    
    # Час початку (Час початку)
    start_time_match = re.search(r'Час початку –\s*(\d{2}:\d{2})\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    start_time = start_time_match.group(1).strip() if start_time_match else None
    
    # Час відновлення (Орієнтовний час відновлення)
    end_time_match = re.search(r'Орієнтовний час відновлення електроенергії –\s*до\s*(\d{2}:\d{2})\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    end_time = end_time_match.group(1).strip() if end_time_match else None

    # Проверяємо, чи переходить через північ 
    end_time_suffix = ""
    if start_time and end_time:
        try:
            start_dt = datetime.strptime(start_time, '%H:%M')
            end_dt = datetime.strptime(end_time, '%H:%M')
            if end_dt < start_dt:
                 end_time_suffix = " (наступного дня)"
        except ValueError:
            pass
            
    return {
        "is_active_outage": True,
        "reason": reason,
        "start_time": start_time, 
        "end_time": end_time,
        "end_time_suffix": end_time_suffix
    }

def get_day_schedules(fact_data: dict, group_name: str, time_zone_map: dict, now: datetime) -> (dict, dict):
    """Витягує та форматує повні графіки на сьогодні та завтра."""
    
    today_timestamp_key = str(fact_data.get("today", 0))
    schedule_today_all_groups = fact_data.get("data", {}).get(today_timestamp_key, {})
    
    tomorrow_timestamp_key = None
    all_fact_keys = list(fact_data.get("data", {}).keys())
    for key in all_fact_keys:
        if key != today_timestamp_key:
            tomorrow_timestamp_key = key
            break
            
    schedule_tomorrow_all_groups = fact_data.get("data", {}).get(tomorrow_timestamp_key, {})

    schedule_today_raw = schedule_today_all_groups.get(group_name, {})
    schedule_tomorrow_raw = schedule_tomorrow_all_groups.get(group_name, {})

    # Форматування в 30-хвилинний формат для C#
    today_schedule = expand_schedule(schedule_today_raw, time_zone_map)
    tomorrow_schedule = expand_schedule(schedule_tomorrow_raw, time_zone_map)

    return today_schedule, tomorrow_schedule

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
            return {"status": "error", "message": "Не можу отримати токен (CSRS)."}

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
        group_name = group_name_list[0] if group_name_list else "Невідомо"
        
        # 2. Знаходимо графіки
        fact = json_data.get("fact")
        preset = json_data.get("preset")
        time_zone_map = preset.get("time_zone", {}) if preset else {}
        
        # 3. Отримуємо повні графіки на сьогодні та завтра
        schedule_today, schedule_tomorrow = get_day_schedules(fact, group_name, time_zone_map, now)
        
        # ---
        # Визначаємо поточний статус, перевіряючи, чи активне відключення
        # ---
        
        current_hour_index = now.hour
        current_minute = now.minute
        
        # Поточний статус з графіку на сьогодні
        raw_schedule_today = fact.get("data", {}).get(str(fact.get("today", 0)), {}).get(group_name, {})
        current_status_raw = raw_schedule_today.get(str(current_hour_index + 1))
        
        is_currently_off_by_schedule = False
        if current_status_raw:
             is_currently_off_by_schedule = is_off_now(current_status_raw, current_minute)
        
        
        # --- ✅ ПАРСИНГ ЖОВТОЇ РАМКИ (Пріоритет №1 - Фактичні відключення) ---
        yellow_data = parse_yellow_info(json_data.get("content", ""))
        
        # ✅ ВИПРАВЛЕННЯ: Якщо жовта рамка є, повертаємо її дані, ІГНОРУЮЧИ графік!
        if yellow_data and yellow_data['is_active_outage']:
             return {
                "status": "warning",
                "group": group_name,
                "today_schedule": schedule_today,
                "tomorrow_schedule": schedule_tomorrow,
                "active_outage_info": { 
                    "reason": yellow_data['reason'],
                    "start_time": yellow_data['start_time'],
                    "end_time": yellow_data['end_time'],
                    "end_time_suffix": yellow_data['end_time_suffix'],
                    "type": "Фактичне відключення"
                }
            }
        
        # --- Крок 4: АНАЛІЗУЄМО ГРАФІК (Якщо немає жовтої рамки, але є графік) ---
        
        if not schedule_today:
             return {"status": "ok", "group": group_name, "today_schedule": {}, "tomorrow_schedule": {}}


        # 5. Перевіряємо, чи світло вимкнене ЗАРАЗ (за графіком)
        if is_currently_off_by_schedule:
            
            start_time, _ = get_time_range(str(current_hour_index + 1), current_status_raw, time_zone_map)
            block_end, end_day_suffix = find_block_end(raw_schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)

            return {
                "status": "warning",
                "group": group_name,
                "today_schedule": schedule_today,
                "tomorrow_schedule": schedule_tomorrow,
                "active_outage_info": { # ✅ Інформація про поточне планове відключення
                    "reason": "Планове (Згідно з графіком)",
                    "start_time": start_time,
                    "end_time": block_end,
                    "end_time_suffix": end_day_suffix,
                    "type": "Планове"
                }
            }

        # 6. Якщо світло ЗАРАЗ є, шукаємо НАСТУПНЕ відключення
        next_start, next_end, end_day_suffix = find_next_block(raw_schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)
        
        if next_start and next_end:
             return {
                "status": "ok",
                "group": group_name,
                "today_schedule": schedule_today,
                "tomorrow_schedule": schedule_tomorrow,
                "next_outage_info": { # ✅ Додаємо інформацію про наступне планове відключення
                    "start_time": next_start,
                    "end_time": next_end,
                    "end_time_suffix": end_day_suffix,
                    "type": "Планове"
                }
            }

        # 7. Якщо сьогодні і завтра нічого немає
        return {
            "status": "ok",
            "group": group_name,
            "today_schedule": schedule_today,
            "tomorrow_schedule": schedule_tomorrow
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутрішня помилка API: {str(e)}"}

# --- Команда для запуску локально (для тестів) ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
