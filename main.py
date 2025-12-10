import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime, timedelta
import pytz
import re

# --- Создаем "мозг" ---
app = FastAPI()

# --- Глобальные константы ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'
AJAX_URL = DTEK_URL + '/ua/ajax'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'

# --- Словарь состояний ---
# "Света нет" или "Может не быть" (из любого графика)
OUTAGE_STATES = ("no", "first", "second", "mfirst", "msecond", "maybe")
POWER_ON_STATES = ("yes", "cell-non-scheduled")

# --- Дополнительные функции ---

def is_off_now(status: str, minute: int) -> bool:
    """Проверяет, отключен ли свет в текущую минуту."""
    if status in ('no', 'maybe'):
        return True
    if (status in ('first', 'mfirst')) and minute < 30:
        return True
    if (status in ('second', 'msecond')) and minute >= 30:
        return True
    return False

def get_time_range(hour_key: str, status: str, time_zone_map: dict) -> (str, str):
    """Возвращает точное время начала и конца для 30-мин блоков."""
    hour_range = time_zone_map.get(hour_key)
    if not hour_range: return None, None
        
    start_time = hour_range[1]
    end_time = hour_range[2]

    if status in ("first", "mfirst"):
        end_time = start_time[:3] + "30"
    elif status in ("second", "msecond"):
        start_time = start_time[:3] + "30"
    
    return start_time, end_time

# ✅ ИСПРАВЛЕННАЯ ФУНКЦИЯ find_block_end
def find_block_end(schedule_current: dict, schedule_next_day: dict, start_hour_index: int, time_zone_map: dict) -> (str, str):
    """
    Находит время окончания текущего блока отключений, склеивая последовательные часы.
    schedule_current: график текущего дня.
    schedule_next_day: график следующего дня (для проверки перехода через полночь).
    start_hour_index: час, с которого нужно начать проверку (0-23).
    """
    
    # 1. Сначала ищем в текущем дне
    for i in range(start_hour_index, 24):
        hour_key = str(i + 1)
        status = schedule_current.get(hour_key)
        
        # Если час отсутствует, или свет включен (POWER_ON_STATES)
        if status in POWER_ON_STATES or status is None:
            # ✅ Найдено Включение!
            _start, end_time = get_time_range(hour_key, status, time_zone_map)
            
            # Если найдена "светлая" зона, нужно определить точное время окончания отключения.
            # Если это 'yes' или 'cell-non-scheduled', отключение закончилось в начале этой часа.
            # Но если предыдущий час был 30-мин блоком (second), конец должен быть в :00
            
            # Проверяем, был ли предыдущий час (i) последним блоком отключения
            prev_hour_key = str(i)
            prev_status = schedule_current.get(prev_hour_key)
            
            if prev_status in ("second", "msecond"):
                # Отключение закончилось в начале часа i (т.е. в :00)
                return _start[:3] + "00", "" 

            # Если предыдущего часа не было (т.е. это 00-01), или он был полным отключением,
            # то берем начало этой "светлой" зоны.
            return _start, "" 
            
    
    # 2. Если дошли до конца сегодня (24:00), проверяем следующий день
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_next_day.get(hour_key)
        
        if status in POWER_ON_STATES or status is None:
            # ✅ Найдено Включение!
            _start, end_time = get_time_range(hour_key, status, time_zone_map)
            
            # Проверяем, был ли предыдущий час (i) последним блоком отключения
            # (Берем статус из schedule_next_day, кроме i=0, где смотрим в schedule_current[24])
            if i == 0:
                 prev_status = schedule_current.get("24")
            else:
                prev_hour_key = str(i)
                prev_status = schedule_next_day.get(prev_hour_key)
            
            if prev_status in ("second", "msecond"):
                 # Отключение закончилось в начале часа i (т.е. в :00)
                 return _start[:3] + "00", " (наступного дня)"
                 
            # Иначе берем начало этой "светлой" зоны.
            return _start, " (наступного дня)"

    # Если отключение идет до конца графика
    return "24:00", " (наступного дня)"

def find_next_block(schedule_today: dict, schedule_tomorrow: dict, start_hour_index: int, time_zone_map: dict) -> (str, str, str):
    """Находит время начала и конца СЛЕДУЮЩЕГО блока."""
    
    # 1. Ищем НАЧАЛО следующего блока СЕГОДНЯ
    for i in range(start_hour_index + 1, 24): 
        hour_key = str(i + 1)
        status = schedule_today.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            
            # Находим конец этого блока, используя schedule_tomorrow
            end_time, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, i, time_zone_map)
            return start_time, end_time, end_day_suffix

    # 2. Если СЕГОДНЯ больше ничего нет, ищем НАЧАЛО блока ЗАВТРА
    for i in range(0, 24):
        hour_key = str(i + 1)
        status = schedule_tomorrow.get(hour_key)
        
        if status in OUTAGE_STATES:
            start_time, _ = get_time_range(hour_key, status, time_zone_map)
            
            # Находим конец этого блока (завтрашний график)
            end_time, _suffix = find_block_end(schedule_tomorrow, {}, i, time_zone_map) 
            
            return start_time, end_time, " (наступного дня)"

    return None, None, None

def parse_yellow_info(html_content: str):
    """Парсит информацию из желтой рамки (активное отключение)."""
    soup = BeautifulSoup(html_content, 'html.parser')
    yellow_div = soup.find('div', {'class': 'discon-current-outage'})
    
    if not yellow_div: return None

    # Причина
    reason_match = re.search(r'Причина:\s*(.*?)\s*Час початку', yellow_div.text, re.DOTALL)
    if not reason_match:
        reason_match = re.search(r'Причина:\s*(.*?)Орієнтовний час', yellow_div.text, re.DOTALL)

    reason = reason_match.group(1).strip() if reason_match else "Неизвестна причина"
    
    # Время начала (Час початку)
    start_time_match = re.search(r'Час початку –\s*(.*?)\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    start_time = start_time_match.group(1).strip() if start_time_match else None
    
    # Время восстановления (Орієнтовний час відновлення)
    end_time_match = re.search(r'Орієнтовний час відновлення електроенергії –\s*до\s*(.*?)\s*\d{2}\.\d{2}\.\d{4}', yellow_div.text)
    end_time = end_time_match.group(1).strip() if end_time_match else None

    # Проверяем, переходит ли через полночь (для сообщения)
    end_time_suffix = ""
    if start_time and end_time:
        try:
            start_dt = datetime.strptime(start_time, '%H:%M')
            end_dt = datetime.strptime(end_time, '%H:%M')
            if end_dt < start_dt:
                 end_time_suffix = " (наступного дня)"
        except ValueError:
            pass # Если формат времени неверный, игнорируем суффикс
            
    return {
        "is_active_outage": True,
        "reason": reason,
        "start_time": start_time,
        "end_time": end_time,
        "end_time_suffix": end_time_suffix
    }

def get_day_schedules(fact_data: dict, group_name: str, time_zone_map: dict, now: datetime) -> (dict, dict):
    """Извлекает и форматирует полные графики на сегодня и завтра."""
    
    today_timestamp_key = str(fact_data.get("today", 0))
    schedule_today_all_groups = fact_data.get("data", {}).get(today_timestamp_key, {})
    
    # Находим ключ "завтра"
    tomorrow_timestamp_key = None
    all_fact_keys = list(fact_data.get("data", {}).keys())
    for key in all_fact_keys:
        if key != today_timestamp_key:
            tomorrow_timestamp_key = key
            break
            
    schedule_tomorrow_all_groups = fact_data.get("data", {}).get(tomorrow_timestamp_key, {})

    schedule_today_raw = schedule_today_all_groups.get(group_name, {})
    schedule_tomorrow_raw = schedule_tomorrow_all_groups.get(group_name, {})

    # Форматируем в 24-часовой список {час: статус} для C#
    def format_schedule(raw_schedule: dict):
        formatted = {}
        for hour_key, status in raw_schedule.items():
            if hour_key in time_zone_map:
                formatted[hour_key] = status
        return formatted
        
    return format_schedule(schedule_today_raw), format_schedule(schedule_tomorrow_raw)

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
            return {"status": "error", "message": "Не могу получить токен (CSRF)."}

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
            else: return {"status": "error", "message": "Адрес не найден."}

        # --- Крок 3: АНАЛИЗИРУЕМ ОТВЕТ ---
        
        # 1. Находим "ГРУППУ" дома (напр. "GPV6.1")
        house_info = json_data.get("data", {}).get(house)
        if not house_info:
            return {"status": "error", "message": "Дом не найден."}

        group_name_list = house_info.get("sub_type_reason")
        group_name = group_name_list[0] if group_name_list else "Неизвестно"
        
        # 2. Находим графики
        fact = json_data.get("fact")
        preset = json_data.get("preset")
        time_zone_map = preset.get("time_zone", {}) if preset else {}
        
        # 3. Получаем полные графики на сегодня и завтра
        today_schedule_full, tomorrow_schedule_full = get_day_schedules(fact, group_name, time_zone_map, now)
        
        # --- ✅ ПАРСИНГ ЖЕЛТОЙ РАМКИ (Приоритет №1) ---
        yellow_data = parse_yellow_info(json_data.get("content", ""))
        
        if yellow_data and yellow_data['is_active_outage']:
             return {
                "status": "warning",
                "message": f"Света нет. Причина: {yellow_data['reason']}",
                "start_time": yellow_data['start_time'],
                "end_time": yellow_data['end_time'] + yellow_data['end_time_suffix'],
                "type": "Фактическое отключение",
                "group": group_name,
                "today_schedule": today_schedule_full,
                "tomorrow_schedule": tomorrow_schedule_full
            }
        
        # --- Крок 4: АНАЛИЗИРУЕМ ГРАФИК (Если нет желтой рамки) ---
        
        schedule_today = today_schedule_full
        schedule_tomorrow = tomorrow_schedule_full

        if not schedule_today:
             return {"status": "ok", "message": "Отключений не запланировано.", "start_time": "", "end_time": "", "type": "Плановое", "group": group_name, "today_schedule": {}, "tomorrow_schedule": {}}


        # --- Анализ графика ---
        current_hour_index = now.hour # 0-23
        current_hour_key = str(current_hour_index + 1) # 1-24
        current_minute = now.minute 

        current_status = schedule_today.get(current_hour_key)
        
        # 5. Проверяем, отключен ли свет СЕЙЧАС (по графику)
        if is_off_now(current_status, current_minute):
            
            start_time, _ = get_time_range(current_hour_key, current_status, time_zone_map)
            block_end, end_day_suffix = find_block_end(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)

            return {
                "status": "warning",
                "message": f"Света нет (до {block_end}{end_day_suffix})",
                "start_time": start_time,
                "end_time": block_end,
                "type": "Плановое (Согласно графику)",
                "group": group_name,
                "today_schedule": today_schedule_full,
                "tomorrow_schedule": tomorrow_schedule_full
            }

        # 6. Если свет СЕЙЧАС есть, ищем СЛЕДУЮЩЕЕ отключение
        next_start, next_end, end_day_suffix = find_next_block(schedule_today, schedule_tomorrow, current_hour_index, time_zone_map)
        
        if next_start and next_end:
             return {
                "status": "ok",
                "message": f"Отключение с {next_start} до {next_end}{end_day_suffix}",
                "start_time": next_start,
                "end_time": next_end,
                "type": "Плановое (Согласно графику)",
                "group": group_name,
                "today_schedule": today_schedule_full,
                "tomorrow_schedule": tomorrow_schedule_full
            }

        # 7. Если сегодня и завтра ничего нет
        return {
            "status": "ok",
            "message": "Отключений не запланировано",
            "start_time": "",
            "end_time": "",
            "type": "Плановое (Согласно графику)",
            "group": group_name,
            "today_schedule": today_schedule_full,
            "tomorrow_schedule": tomorrow_schedule_full
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутренняя ошибка API: {str(e)}"}

# --- Команда для запуску локально (для тестів) ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
