import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from datetime import datetime
import pytz # <-- ✅ НОВЕ: Для роботи з часовими зонами

# --- Створюємо "мозок" ---
app = FastAPI()

# --- Глобальні константи ---
DTEK_URL = 'https://www.dtek-dnem.com.ua'
SHUTDOWNS_PAGE = DTEK_URL + '/ua/shutdowns'
AJAX_URL = DTEK_URL + '/ua/ajax'

# "Паспорт" браузера
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'

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

        # ---
        # ✅ Крок 2: ОНОВЛЕННЯ ЛОГІКИ ЗАПИТУ
        # ---
        
        # ✅ 1. Отримуємо поточний час для Києва (це виправляє баг)
        tz_kyiv = pytz.timezone('Europe/Kiev')
        current_time_kyiv = datetime.now(tz_kyiv).strftime("%d.%m.%Y %H:%M")

        # 2. Формуємо "записку" (payload)
        payload = {
            'method': 'getHomeNum',
            'data[0][name]': 'city',
            'data[0][value]': city,
            'data[1][name]': 'street',
            'data[1][value]': street,
            'data[2][name]': 'updateFact', # <-- ✅ Це поле виправляє баг
            'data[2][value]': current_time_kyiv
        }
        
        response_ajax = session.post(AJAX_URL, data=payload)
        
        if response_ajax.status_code != 200:
            return {"status": "error", "message": f"Помилка AJAX-запиту ({response_ajax.status_code})"}

        json_data = response_ajax.json()

        # 3. Якщо дім не вказано, повертаємо список (якщо він є)
        if not house:
            # Логіка для повернення списків (міст або вулиць)
            list_data = json_data.get("data")
            if isinstance(list_data, list):
                return {"available_list": list_data}
            elif isinstance(list_data, dict):
                 return {"available_list": list(list_data.keys())}
            else:
                return {"status": "error", "message": "Адресу не знайдено."}

        # 4. Якщо дім вказано, шукаємо його
        house_info = json_data.get("data", {}).get(house)
        
        if not house_info:
            return {"status": "error", "message": "Дім не знайдено."}

        # ---
        # ✅ Крок 3: АНАЛІЗУЄМО ВІДПОВІДЬ
        # ---
        
        planned_start_full = house_info.get("start_date") # "2025-11-01 14:00:00"
        planned_end_full = house_info.get("end_date")
        outage_type = house_info.get("type") # "GPV", "SAV" etc.
        
        # 1. Перевіряємо ЗАПЛАНОВАНЕ відключення
        if planned_start_full and planned_end_full:
            # Витягуємо лише час
            start_time_str = planned_start_full.split(' ')[1][:5] # "14:00"
            end_time_str = planned_end_full.split(' ')[1][:5] # "16:00"
            
            return {
                "status": "ok", # Статус "ok", бо світло ЗАРАЗ є
                "message": f"Відключення о {start_time_str}",
                "start_time": start_time_str, # C# код побачить це
                "end_time": end_time_str,
                "type": "Планове"
            }
        
        # 2. Перевіряємо АКТИВНЕ (Аварійне) відключення
        if outage_type and outage_type != "":
            return {
                "status": "warning", # Статус "warning", бо світла ЗАРАЗ немає
                "message": "Світла немає (Аварійно)",
                "start_time": "", 
                "end_time": "",
                "type": "Аварійне"
            }

        # 3. Якщо нічого не знайдено
        return {
            "status": "ok",
            "message": "Відключень не заплановано",
            "start_time": "",
            "end_time": "",
            "type": ""
        }

    except Exception as e:
        return {"status": "error", "message": f"Внутрішня помилка API: {str(e)}"}
