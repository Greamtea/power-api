import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime, timedelta
import pytz
import re

# --- Створюємо "мозок" ---
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
    if status == 'no' or status == 'maybe':
        return True
    if (status == 'first' or status == 'mfirst') and minute < 30:
        return True
    if (status == 'second' or status == 'msecond') and minute >= 30:
        return True
    return False

def get_time_range(hour_key: str, status: str, time_zone_map: dict) -> (str, str):
    """Повертає точний час початку та кінця для 30-хв блоків."""
    hour_range = time_zone_map.get(hour_key)
    if not hour_range: return None, None
        
    start_time = hour_range[1]
    end_time = hour_range[2]

    # Якщо статус вказує на 30-хвилинний блок (first/second)
    if status in ("first", "mfirst"):
        end_time = start_time[:3] + "30"
    elif status in ("second", "msecond"):
        start_time = start_time[:3] + "30"
    
    # Якщо статус - "yes", то час - це повна година
    elif status in POWER_ON_STATES:
        pass
        
    return start_time, end_time

# ✅ ВИПРАВЛЕННЯ ЛОГІКИ: Тепер коректно обробляємо 30-хвилинні переходи та перехід через північ
def find_block_end(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str):
    """Знаходить час закінчення поточного блоку відключень."""
    
    # 1. Шукаємо кінець сьогодні (починаючи з години, в якій почалося відключення)
    for i in range(start_hour_index, 24):
        hour_key = str(i + 1)
        status = schedule_today.get(hour_key)
        
        # Якщо ми знаходимо годину, в якій світло Є (або частково є)
        if status in POWER_ON_STATES:
            _start, end_time = get_time_range(hour_key, status, time_zone_map)
            
            # Якщо поточна "світла" година має статус "second" або "msecond", це означає, 
            # що світло дадуть о :30, а відключення закінчується о :00
            if status in ("second", "msecond"):
                return _start[:3] + "00", ""
            
            return _start, "" # Це час початку "світлої" години
    
    # 2. Якщо дійшли до 24:00, перевіряємо завтра
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_tomorrow.get(hour_key)
        
        if status in POWER_ON_STATES:
            _start, end_time = get_time_range(hour_key, status, time_zone_map)
            
            # Якщо поточна "світла" година має статус "second" або "msecond", це означає, 
            # що світло дадуть о :30, а відключення закінчується о :00
            if status in ("second", "msecond"):
                return _start[:3] + "00", " (наступного дня)"
                
            return _start, " (наступного дня)"

    return "24:00", " (наступного дня)"

def find_next_block(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str, str):
    """Знаходить час початку та кінця НАСТУПНОГО блоку."""
    
    # 1. Шукаємо ПОЧАТОК сьогодні
    for i in range(start_hour_index + 1, 24): 
        hour_key = str(i + 1)
        status = schedule_today.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            end_time, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, i, time_zone_map)
            return start_time, end_time, end_day_suffix

    # 2. Шукаємо ПОЧАТОК завтра
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_tomorrow.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            end_time, _suffix = find_block_end(schedule_tomorrow, {}, i, time_zone_map)
            return start_time, end_time, " (наступного дня)"

    return None, None, None

def parse_yellow_info(html_content: str):
    """Парсить інформацію з жовтої рамки, якщо вона є."""
    soup = BeautifulSoup(html_content, 'html.parser')
    # ✅ ВИПРАВЛЕНО: Додана перевірка на class="discon-current-outage"
    yellow_div = soup.find('div', {'class': 'discon-current-outage'})
    
    if not yellow_div:
        return None

    # Причина
    reason_match = re.search(r'Причина:\s*(.*?)Час початку', yellow_div.text, re.DOTALL)
    reason = reason_match.group(1).strip() if reason_match else "Невідома причина"
    
    # Час початку
    start_time_match = re.search(r'Час початку –\s*(.*?)\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    start_time = start_time_match.group(1).strip() if start_time_match else None
    
    # Час відновлення
    end_time_match = re.search(r'Орієнтовний час відновлення електроенергії –\s*до\s*(.*?)\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    end_time = end_time_match.group(1).strip() if end_time_match else None

    # Додаємо суфікс " (наступного дня)" до кінця, якщо він переходить через ніч
    if start_time and end_time and end_time < start_time:
        end_time_suffix = " (наступного дня)"
    else:
        end_time_suffix = ""

    return {
        "is_active_outage": True,
        "reason": reason,
        "start_time": start_time,
        "end_time": end_time,
        "end_time_suffix": end_time_suffix
    }


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
        group_name = group_name_list[0] if group_name_list else None
        
        # 2. Знаходимо "сховані" графіки
        fact = json_data.get("fact")
        preset = json_data.get("preset")
        time_zone_map = preset.get("time_zone", {}) if preset else {}
        
        # --- ✅ ПАРСИНГ ЖОВТОЇ РАМКИ (Пріоритет №1) ---
        yellow_data = parse_yellow_info(json_data.get("content", ""))
        
        if yellow_data and yellow_data['is_active_outage']:
             return {
                "status": "warning",
                "message": f"Світла немає. Причина: {yellow_data['reason']}",
                "start_time": yellow_data['start_time'],
                "end_time": yellow_data['end_time'] + yellow_data['end_time_suffix'],
                "type": "Фактичне відключення",
                "group": group_name
            }
        
        # --- Крок 4: АНАЛІЗУЄМО ГРАФІК (Якщо немає жовтої рамки) ---
        
        if not fact:
            return {"status": "ok", "message": "Інформація про графік відсутня.", "start_time": "", "end_time": "", "type": "", "group": group_name}

        today_timestamp_key = str(fact.get("today", 0))
        fact_data = fact.get("data", {})
        
        all_fact_keys = list(fact_data.keys())
        tomorrow_timestamp_key = None
        for key in all_fact_keys:
            if key != today_timestamp_key:
                tomorrow_timestamp_key = key
                break
        
        schedule_today_all_groups = fact_data.get(today_timestamp_key)
        schedule_tomorrow_all_groups = fact_data.get(tomorrow_timestamp_key, {})
        
        if not schedule_today_all_groups or not group_name:
             return {"status": "ok", "message": "Відключень не заплановано", "start_time": "", "end_time": "", "type": "", "group": group_name}

        schedule_today = schedule_today_all_groups.get(group_name)
        schedule_tomorrow = schedule_tomorrow_all_groups.get(group_name, {})
        
        if not schedule_today:
             return {"status": "ok", "message": "Відключень не заплановано", "start_time": "", "end_time": "", "type": "", "group": group_name}


        # --- Аналіз графіка ---
        current_hour_index = now.hour # 0-23
        current_hour_key = str(current_hour_index + 1) # 1-24
        current_minute = now.minute 

        current_status = schedule_today.get(current_hour_key)
        
        # 5. Перевіряємо, чи світло вимкнене ЗАРАЗ (за графіком)
        if is_off_now(current_status, current_minute):
            
            start_time, _ = get_time_range(current_hour_key, current_status, time_zone_map)
            block_end, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)

            return {
                "status": "warning",
                "message": f"Світла немає (до {block_end}{end_day_suffix})",
                "start_time": start_time,
                "end_time": block_end,
                "type": "Планове (Згідно графіку)",
                "group": group_name
            }

        # 6. Якщо світло ЗАРАЗ є, шукаємо НАСТУПНЕ відключення
        next_start, next_end, end_day_suffix = find_next_block(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)
        
        if next_start and next_end:
             return {
                "status": "ok",
                "message": f"Відключення з {next_start} до {next_end}{end_day_suffix}",
                "start_time": next_start,
                "end_time": next_end,
                "type": "Планове (Згідно графіку)",
                "group": group_name
            }

        # 7. Якщо сьогодні і завтра нічого немає
        return {
            "status": "ok",
            "message": "Відключень не заплановано",
            "start_time": "",
            "end_time": "",
            "type": "",
            "group": group_name
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутрішня помилка API: {str(e)}"}

# --- Команда для запуску локально (для тестів) ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
