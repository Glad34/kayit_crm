import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta

import vertexai
from vertexai.generative_models import GenerativeModel
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

PROJECT_ID = "masaustuotomasyon"
LOCATION = "us-central1"
SPREADSHEET_ID = "1xjdxkMXKe3iQjD9rosNb69CIo36JhHUCPM-4kYzzRBM"
CALENDAR_ID = 'onurglad34@gmail.com' 

try:
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Vertex AI başarıyla başlatıldı.")
except Exception as e:
    print(f"HATA: Vertex AI başlatılamadı: {e}")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/calendar"]
creds = None
worksheet = None
calendar_service = None
try:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    sheets_client = gspread.authorize(creds)
    spreadsheet = sheets_client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.sheet1
    print(f"ID'si belirtilen Google E-Tablosuna başarıyla bağlanıldı.")
    calendar_service = build('calendar', 'v3', credentials=creds)
    print("Google Calendar servisine başarıyla bağlanıldı.")
except Exception as e:
    print(f"--- GOOGLE SERVİS BAĞLANTI HATASI ---\nHATA: {e}\n---------------------------------")

app = Flask(__name__)

# !!! DEĞİŞİKLİK BURADA: EN KAPSAMLI VE KURALLI PROMPT !!!
def get_gemini_prompt(transcript):
    return f"""
    SENARYO: Sen bir emlak danışmanı için veri yapılandırma asistanısın. Görevin, sana verilen serbest metni analiz ederek aşağıdaki kurallara harfiyen uyarak bir JSON formatında cevap vermektir.

    ## KESİN KURALLAR ##
    1.  **Kaynak Tespiti:** Metindeki 'sahibinden', 'reklam', 'branda', 'fsbo', 'etki çevresi', 'web sitesi', 'sosyal medya', 'google işletme', 'direk temas' gibi anahtar kelimelerden birini bularak 'Kaynak' alanına yaz. Eğer hiçbiri yoksa "Belirtilmedi" yaz.
    2.  **Telefon Formatı:** Telefon numarasını bul ve SADECE rakamlardan oluşan '5414746388' formatında yaz. Başka hiçbir format kabul edilmez.
    3.  **Bütçe Standardı:** Eğer "4-5 milyon arası" gibi bir aralık belirtilirse, her zaman YÜKSEK olan rakamı al ve sayı olarak yaz (örn: 5000000).
    4.  **Sayısal Alanlar:** 'Oda_Sayisi' ("2+1" gibi), 'MetreKare', 'Bina_Yasi', 'Kat' alanlarına SADECE sayı veya standart format yaz. "en az", "yaklaşık" gibi ifadeleri at.
    5.  **Var/Yok Alanları:** 'Balkon', 'Asansor', 'Havuz', 'Manzara' (deniz manzarası vb. varsa) alanlarına SADECE "Var" veya "Yok" yaz.
    6.  **Konum Ayrımı:** Metindeki ilçe isimlerini 'Konum' alanına, mahalle isimlerini 'Mahalle' alanına yaz. Birden fazla varsa aralarına virgül koy.
    7.  **Konut Tipi Standardı:** 'Konut_Tipi' alanına SADECE "Daire", "Rezidans", "Müstakil Ev", "Yazlık" seçeneklerinden birini yaz.
    8.  **Cevap Formatı:** Cevabın SADECE geçerli bir JSON formatında olmalı. Başka hiçbir açıklama ekleme.

    İŞLENECEK METİN:
    "{transcript}"

    İSTENEN JSON FORMATI (ÖRNEK):
    {{
      "Kaynak": "Sahibinden",
      "Müşteri_Adı": "Sercan Bey",
      "Telefon": "5414746388",
      "Oturum_mu_Yatirim_mi": "Oturum Amaçlı",
      "Taraf": "Alıcı",
      "Butce": 8000000,
      "Oda_Sayisi": "2+1",
      "MetreKare": "Belirtilmedi",
      "Bina_Yasi": 20,
      "Kat": "Belirtilmedi",
      "Balkon": "Var",
      "Asansor": "Yok",
      "Konum": "Konak",
      "Mahalle": "Göztepe,Alsancak",
      "Havuz": "Yok",
      "Manzara": "Var",
      "Notlar": "Ek notlar.",
      "Konut_Tipi": "Daire",
      "Aksiyonlar": "Sercan Bey'i ara",
      "Hatırlatma_Tarihi_Metni": "yarın",
      "Hatırlatma_Saati_Metni": "17:00"
    }}
    """

# ... (Tarih işleme ve diğer Flask rotaları aynı kalıyor, sadece 'row_to_insert' güncellenecek) ...

def parse_date(date_text, time_text):
    if not date_text or date_text == "belirtilmedi":
        return None
    now = datetime.now()
    base_date = None
    if "yarın" in date_text: base_date = now + timedelta(days=1)
    elif "bugün" in date_text: base_date = now
    elif "gün sonra" in date_text:
        try: base_date = now + timedelta(days=int(''.join(filter(str.isdigit, date_text))))
        except ValueError: base_date = now
    elif "haftaya" in date_text:
        try:
            date_text_en = date_text.replace("haftaya", "next")
            turkish_replacements = {"pazartesi": "monday", "salı": "tuesday", "çarşamba": "wednesday", "perşembe": "thursday", "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}
            for tr, en in turkish_replacements.items(): date_text_en = date_text_en.replace(tr, en)
            base_date = parse(date_text_en, default=now)
        except ValueError: base_date = now + timedelta(weeks=1)
    elif "hafta sonra" in date_text:
        try: base_date = now + timedelta(weeks=int(''.join(filter(str.isdigit, date_text))))
        except ValueError: base_date = now
    elif "ay sonra" in date_text:
        try: base_date = now + relativedelta(months=int(''.join(filter(str.isdigit, date_text))))
        except ValueError: base_date = now
    else:
        try:
            turkish_replacements = {"pazartesi": "monday", "salı": "tuesday", "çarşamba": "wednesday", "perşembe": "thursday", "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}
            for tr, en in turkish_replacements.items(): date_text = date_text.replace(tr, en)
            base_date = parse(date_text, default=now)
        except ValueError: base_date = now
    
    hour, minute = 10, 0
    if time_text and time_text != "belirtilmedi":
        try:
            time_parts = time_text.split(':')
            hour = int(time_parts[0]); minute = int(time_parts[1]) if len(time_parts) > 1 else 0
        except (ValueError, IndexError): hour, minute = 10, 0
    return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

@app.route('/')
def index():
    records = []
    try:
        all_data = worksheet.get_all_records()
        records = list(reversed(all_data))
    except Exception as e:
        print(f"E-Tablodan veri çekerken hata oluştu: {e}")
    return render_template('index.html', records=records)

@app.route('/process', methods=['POST'])
def process_transcript():
    try:
        data = request.get_json()
        transcript = data.get('transcript')

        model = GenerativeModel("gemini-2.5-pro")
        prompt = get_gemini_prompt(transcript)
        response = model.generate_content(prompt)
        
        cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip()
        structured_data = json.loads(cleaned_response_text)
        
        reminder_datetime_obj = parse_date(
            structured_data.get("Hatırlatma_Tarihi_Metni", "").lower(),
            structured_data.get("Hatırlatma_Saati_Metni", "").lower()
        )
        
        if reminder_datetime_obj and calendar_service:
            event_start_time = reminder_datetime_obj
            event_end_time = event_start_time + timedelta(hours=1)
            event = {
                'summary': structured_data.get("Aksiyonlar", "İsimsiz Görev"),
                'description': f"Müşteri: {structured_data.get('Müşteri_Adı', 'Belirtilmedi')}\nTelefon: {structured_data.get('Telefon', 'Belirtilmedi')}\n\nNotlar:\n{transcript}",
                'start': {'dateTime': event_start_time.isoformat(), 'timeZone': 'Europe/Istanbul'},
                'end': {'dateTime': event_end_time.isoformat(), 'timeZone': 'Europe/Istanbul'},
            }
            calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
            print(f"Google Takvim'e ('{CALENDAR_ID}') etkinlik eklendi: {event['summary']}")

        kayit_tarihi = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reminder_for_sheet = reminder_datetime_obj.strftime("%Y-%m-%d %H:%M") if reminder_datetime_obj else "Belirtilmedi"
        
        # !!! DEĞİŞİKLİK BURADA: E-Tablo satırı yeni sütunlara göre güncellendi !!!
        row_to_insert = [
            structured_data.get("Kaynak", "Belirtilmedi"),
            structured_data.get("Müşteri_Adı", "Belirtilmedi"),
            structured_data.get("Telefon", "Belirtilmedi"),
            structured_data.get("Oturum_mu_Yatirim_mi", "Belirtilmedi"),
            structured_data.get("Taraf", "Belirtilmedi"),
            structured_data.get("Butce", "Belirtilmedi"),
            structured_data.get("Oda_Sayisi", "Belirtilmedi"),
            structured_data.get("MetreKare", "Belirtilmedi"),
            structured_data.get("Bina_Yasi", "Belirtilmedi"),
            structured_data.get("Kat", "Belirtilmedi"),
            structured_data.get("Balkon", "Belirtilmedi"),
            structured_data.get("Asansor", "Belirtilmedi"),
            structured_data.get("Konum", "Belirtilmedi"),
            structured_data.get("Mahalle", "Belirtilmedi"), # Yeni Mahalle sütunu
            structured_data.get("Havuz", "Belirtilmedi"),
            structured_data.get("Otopark", "Belirtilmedi"),
            structured_data.get("Manzara", "Belirtilmedi"),
            structured_data.get("Notlar", "Belirtilmedi"),
            structured_data.get("Konut_Tipi", "Belirtilmedi"),
            kayit_tarihi,
            structured_data.get("Aksiyonlar", "Belirtilmedi"),
            reminder_for_sheet
        ]
        worksheet.append_row(row_to_insert, value_input_option='USER_ENTERED')
        
        return jsonify({"status": "success", "data": structured_data})
    except Exception as e:
        print(f"\n!!!! HATA !!!!\n{e}\n!!!!!!!!!!!!!!")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)