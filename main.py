# --- Шаг 0: Импортируем наши "инструменты" ---
import requests
from bs4 import BeautifulSoup
import json
from fastapi import FastAPI # <--- НОВОЕ: импортируем FastAPI
import uvicorn # <--- НОВОЕ: импортируем "движок" для запуска

# --- НОВОЕ: Создаем "приложение" FastAPI ---
# Это будет наш главный "мозг"
app = FastAPI()

# --- НОВОЕ: Создаем "точку входа" (Endpoint) ---
# Мы говорим: "Кто угодно может 'позвонить' по адресу /check
# и передать мне 'city', 'street' и 'house'"
@app.get("/check")
async def check_power_outage(city: str, street: str, house: str):
    
    # Весь наш старый код теперь "живет" внутри этой функции
    
    # --- Шаг 1: "Получаем Печать" (Токен и Cookie) ---
    print(f"ПОЛУЧЕН ЗАПРОС: Город={city}, Улица={street}, Дом={house}")
    print("Шаг 1: Захожу на сайт, чтобы получить 'печать' (CSRF-токен)...")

    session = requests.Session()
    main_page_url = 'https://www.dtek-dnem.com.ua/ua/shutdowns'

    try:
        # 1. "Заходим" на главную страницу
        response_main = session.get(main_page_url)
        soup = BeautifulSoup(response_main.text, 'html.parser')
        
        # 2. "Ищем" на странице нашу "печать" (x-csrf-token).
        token_tag = soup.find('meta', {'name': 'csrf-token'})
        
        if not token_tag:
            print("ОШИБКА: Не могу найти 'печать' (CSRF-токен).")
            # НОВОЕ: Возвращаем ошибку в JSON-формате
            return {"error": "Не могу получить токен с сайта ДТЭК."}

        fresh_token = token_tag['content']
        print(f"Успех! 'Печать' (токен) получена.")

        # --- Шаг 2: "Отправляем Заказ" на "секретный коридор" (/ajax) ---
        print("\nШаг 2: Отправляю 'заказ' на получение графика...")

        ajax_url = 'https://www.dtek-dnem.com.ua/ua/ajax'

        headers = {
            'x-csrf-token': fresh_token,
            'x-requested-with': 'XMLHttpRequest',
            'Referer': main_page_url
        }

        # --- НОВОЕ: Используем данные от пользователя! ---
        # Мы больше не используем "Дніпро", а берем то, что пришло в запросе
        payload = {
            'method': 'getHomeNum',
            'data[0][name]': 'city',
            'data[0][value]': city, # <--- ИСПОЛЬЗУЕМ 'city'
            'data[1][name]': 'street',
            'data[1][value]': street # <--- ИСПОЛЬЗУЕМ 'street'
        }

        response_ajax = session.post(ajax_url, data=payload, headers=headers)

        # --- Шаг 3: "Читаем и Фильтруем Ответ" ---
        print("\nШаг 3: 'Заказ' выполнен! Фильтрую 'ответ'...")

        if response_ajax.status_code == 200:
            json_data = response_ajax.json()
            
            # --- НОВОЕ: Ищем конкретный дом! ---
            # 'house' - это номер дома, который прислал пользователь
            # Мы ищем его в большом словаре 'data'
            
            # .get("data", {}) - безопасно берем "data", или пустой словарь, если "data" нет
            # .get(house) - безопасно берем нужный нам дом
            house_info = json_data.get("data", {}).get(house)
            
            if house_info:
                print(f"Найден дом '{house}'. Анализирую...")
                
                # Проверяем, есть ли дата начала отключения
                start_date = house_info.get("start_date")
                
                if start_date and start_date != "":
                    # Отключение ЕСТЬ!
                    end_date = house_info.get("end_date", "невідомо") # Берем дату конца
                    type_str = house_info.get("type", "Планове") # Берем тип
                    
                    print(f"!!! НАЙДЕНО ОТКЛЮЧЕНИЕ: {start_date} - {end_date}")
                    
                    # Возвращаем чистый JSON для приложения
                    return {
                        "status": "warning",
                        "message": f"{type_str} відключення",
                        "start_time": start_date,
                        "end_time": end_date
                    }
                else:
                    # Отключения НЕТ!
                    print("Отключений для этого дома нет.")
                    return {
                        "status": "ok",
                        "message": "Відключень не заплановано"
                    }
            else:
                # Дом не найден в ответе сервера
                print(f"ОШИБКА: Дом '{house}' не найден в ответе от ДТЭК.")
                return {"error": f"Дім '{house}' не знайдено за цією адресою."}
            
        else:
            print(f"ОШИБКА: Сервер ДТЭК вернул ошибку {response_ajax.status_code}")
            return {"error": "Сервер ДТЭК не отвечает."}

    except Exception as e:
        print(f"\nПроизошла непредвиденная ошибка: {e}")
        return {"error": f"Внутренняя ошибка сервера: {e}"}

# --- НОВОЕ: Команда для запуска "движка" ---
# Эта часть нужна, чтобы мы могли запустить этот файл
if __name__ == "__main__":
    print("--- Запуск локального API-сервера на http://127.0.0.1:8000 ---")
    print("Чтобы остановить, нажми CTRL+C")

    uvicorn.run(app, host="127.0.0.1", port=8000)
