import os
import json
import re # Telefon numarasını temizlemek için eklendi
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta

# Kullanıcı Girişi için yeni kütüphaneler
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth

# Google Kütüphaneleri
import vertexai
from vertexai.generative_models import GenerativeModel
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- UYGULAMA KURULUMU ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'yerel-test-icin-cok-gizli-bir-anahtar')
# Oturum çerezi ayarlarını canlı sunucu (HTTPS) için güvenli hale getir
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='None')

# --- KULLANICI GİRİŞ SİSTEMİ (LOGIN MANAGER) KURULUMU ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

# Basit bir kullanıcı sınıfı
class User(UserMixin):
    def __init__(self, id, name, email):
        self.id = id
        self.name = name
        self.email = email
users = {}

@login_manager.user_loader
def load_user(user_id):
    return users.get(user_id)


# --- OAUTH (GOOGLE İLE GİRİŞ) KURULUMU ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    # 'server_metadata_url' yerine tüm bilgileri manuel olarak veriyoruz.
    # Bu, 'invalid_claim' hatasını kesin olarak çözer.
    access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    userinfo_endpoint='https://openidconnect.googleapis.com/v1.0/userinfo',
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs", # Bu satır kritik
    client_kwargs={'scope': 'openid email profile'},
)

# --- GOOGLE SERVİSLERİNİ BAŞLATMA ---
PROJECT_ID = "masaustuotomasyon"
LOCATION = "us-central1"
SPREADSHEET_ID = "1xjdxkMXKe3iQjD9rosNb69CIo36JhHUCPM-4kYzzRBM"
CALENDAR_ID = 'onurglad34@gmail.com' 
creds = None
worksheet = None
calendar_service = None
try:
    google_creds_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/cloud-platform"]
    if google_creds_json_str:
        google_creds_dict = json.loads(google_creds_json_str)
        creds = Credentials.from_service_account_info(google_creds_dict, scopes=SCOPES)
        vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=creds)
        sheets_client = gspread.authorize(creds)
        spreadsheet = sheets_client.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.sheet1
        calendar_service = build('calendar', 'v3', credentials=creds)
        print("Tüm Google Servisleri (Render Modu) başarıyla başlatıldı.")
    else:
        print("UYARI: Ortam değişkeni bulunamadı. Yerel modda çalışılıyor.")
        creds_file = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        sheets_client = gspread.authorize(creds_file)
        spreadsheet = sheets_client.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.sheet1
        calendar_service = build('calendar', 'v3', credentials=creds_file)
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        print("Tüm servisler (Yerel Mod) başarıyla başlatıldı.")
except Exception as e:
    print(f"--- KRİTİK HATA: GOOGLE SERVİSLERİ BAŞLATILAMADI ---\nHATA: {e}")

# --- GEMINI PROMPT FONKSİYONU ---
def get_gemini_prompt(transcript):
    return f"""
    SENARYO: Sen bir emlak danışmanı için veri yapılandırma asistanısın. Görevin, sana verilen serbest metni analiz ederek aşağıdaki kurallara harfiyen uyarak bir JSON formatında cevap vermektir.
    ## KESİN KURALLAR ##
    1.  **Kaynak Tespiti:** Metindeki 'sahibinden', 'reklam', 'branda', 'fsbo', 'etki çevresi', 'web sitesi', 'sosyal medya', 'google işletme', 'direk temas' gibi anahtar kelimelerden birini bularak 'Kaynak' alanına yaz. Eğer hiçbiri yoksa "Belirtilmedi" yaz.
    2.  **Telefon Formatı:** Telefon numarasını bul ve SADECE rakamlardan oluşan '5414746388' formatında yaz. Başka hiçbir format kabul edilmez.
    3.  **Bütçe Standardı:** Eğer "4-5 milyon arası" gibi bir aralık belirtilirse, her zaman YÜKSEK olan rakamı al ve sayı olarak yaz (örn: 5000000).
    4.  **Sayısal Alanlar:** 'Oda_Sayisi' ("2+1" gibi), 'MetreKare', 'Bina_Yasi', 'Kat' alanlarına SADECE sayı veya standart format yaz. "en az", "yaklaşık" gibi ifadeleri at.
    5.  **Var/Yok Alanları:** 'Balkon', 'Asansor', 'Havuz', 'Otopark', 'Manzara' (deniz manzarası vb. varsa) alanlarına SADECE "Var" veya "Yok" yaz.
    6.  **Konum Ayrımı:** Metindeki ilçe isimlerini 'Konum' alanına, mahalle isimlerini 'Mahalle' alanına yaz. Birden fazla varsa aralarına virgül koy.
    7.  **Konut Tipi Standardı:** 'Konut_Tipi' alanına SADECE "Daire", "Rezidans", "Müstakil Ev", "Yazlık" seçeneklerinden birini yaz.
    8.  **Aksiyon ve Zaman Tespiti:** Metindeki görevi 'Aksiyonlar' olarak, zaman ifadesini ('yarın', '2 hafta sonra' vb.) 'Hatırlatma_Tarihi_Metni' olarak, saat ifadesini ('14:00', 'saat 2'de' vb.) 'Hatırlatma_Saati_Metni' olarak al.
    9.  **Cevap Formatı:** Cevabın SADECE geçerli bir JSON formatında olmalı. Başka hiçbir açıklama ekleme.
    İŞLENECEK METİN:
    "{transcript}"
    İSTENEN JSON FORMATI (ÖRNEK):
    {{
      "Kaynak": "Sahibinden", "Müşteri_Adı": "Sercan Bey", "Telefon": "5414746388", "Oturum_mu_Yatirim_mi": "Oturum Amaçlı",
      "Taraf": "Alıcı", "Butce": 8000000, "Oda_Sayisi": "2+1", "MetreKare": "Belirtilmedi", "Bina_Yasi": 20, "Kat": "Belirtilmedi",
      "Balkon": "Var", "Asansor": "Yok", "Konum": "Konak", "Mahalle": "Göztepe,Alsancak", "Havuz": "Yok", "Otopark": "Var", "Manzara": "Var",
      "Notlar": "Ek notlar.", "Konut_Tipi": "Daire", "Aksiyonlar": "Sercan Bey'i ara", "Hatırlatma_Tarihi_Metni": "yarın", "Hatırlatma_Saati_Metni": "17:00"
    }}
    """

# --- GİRİŞ / ÇIKIŞ SAYFALARI (ROUTES) ---
@app.route('/login_page')
def login_page():
    return render_template('login.html')

@app.route('/login')
def login():
    redirect_uri = url_for('authorize', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def authorize():
    try:
        token = google.authorize_access_token()
        session['user_token'] = token
        user_info = google.get('userinfo').json()
        user_id = user_info['id']
        user = User(id=user_id, name=user_info.get('name', 'İsimsiz'), email=user_info['email'])
        users[user_id] = user
        login_user(user)
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Giriş sırasında hata: {e}")
        return redirect(url_for('login_page'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login_page'))

# --- ANA UYGULAMA SAYFASI ---
@app.route('/')
@login_required
def index():
    records = []
    if worksheet:
        try:
            all_data = worksheet.get_all_records()
            user_email = current_user.email
            user_records = [rec for rec in all_data if rec.get('Danışman_Eposta') == user_email]
            records = list(reversed(user_records))
        except Exception as e:
            print(f"E-Tablodan veri çekerken hata oluştu: {e}")
    return render_template('index.html', records=records)

@app.route('/service-worker.js')
def service_worker():
    return app.send_static_file('service-worker.js')

# YENİ: Telefon numarasını temizleyen yardımcı fonksiyon
def normalize_phone(phone):
    if not isinstance(phone, str):
        phone = str(phone)
    # Sadece rakamları al
    return re.sub(r'\D', '', phone)

# --- VERİ İŞLEME ROTASI (TAMAMEN YENİLENDİ) ---
@app.route('/process', methods=['POST'])
@login_required
def process_transcript():
    try:
        data = request.get_json()
        transcript = data.get('transcript')
        model = GenerativeModel("gemini-1.5-pro-preview-0409")
        prompt = get_gemini_prompt(transcript)
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip()
        new_data = json.loads(cleaned_response_text)
        
        structured_data = new_data # Başlangıç değeri olarak ata
        
        # Telefon numarasını al ve temizle
        new_phone = normalize_phone(new_data.get("Telefon", ""))

        # Mevcut kayıtları kontrol et ve güncelleme mantığını uygula
        record_updated = False
        if new_phone and worksheet:
            all_records = worksheet.get_all_records()
            # E-tabloda arama yaparken sadece danışmana ait kayıtları dikkate al
            user_records_with_index = [
                (i + 2, record) for i, record in enumerate(all_records) 
                if record.get('Danışman_Eposta') == current_user.email
            ]

            for row_index, existing_record in user_records_with_index:
                existing_phone = normalize_phone(existing_record.get("Telefon", ""))
                if existing_phone == new_phone:
                    # Eşleşme bulundu! Mevcut kaydı yeni verilerle güncelle
                    
                    # Notları ve Aksiyonları eskiyi silmeden ekle
                    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M")
                    old_notes = existing_record.get("Notlar", "")
                    new_note = new_data.get("Notlar", "")
                    if new_note and new_note != "Belirtilmedi":
                        if old_notes and old_notes != "Belirtilmedi":
                            existing_record["Notlar"] = f"{old_notes}\n---\n[{timestamp}] {new_note}"
                        else:
                            existing_record["Notlar"] = f"[{timestamp}] {new_note}"
                    
                    old_aksiyonlar = existing_record.get("Aksiyonlar", "")
                    new_aksiyon = new_data.get("Aksiyonlar", "")
                    if new_aksiyon and new_aksiyon != "Belirtilmedi":
                        if old_aksiyonlar and old_aksiyonlar != "Belirtilmedi":
                            existing_record["Aksiyonlar"] = f"{old_aksiyonlar}\n---\n[{timestamp}] {new_aksiyon}"
                        else:
                            existing_record["Aksiyonlar"] = f"[{timestamp}] {new_aksiyon}"

                    # Diğer alanları güncelle (yeni veri varsa ve "Belirtilmedi" değilse)
                    for key, value in new_data.items():
                        if key not in ["Notlar", "Aksiyonlar"] and value and str(value).strip() != "Belirtilmedi":
                            existing_record[key] = value
                    
                    # E-Tablodaki satırı güncellemek için başlık sırasına göre liste oluştur
                    headers = worksheet.row_values(1)
                    # Gspread'in update metodu için aralık belirtiyoruz. Örn: 'A2:W2'
                    update_range = f'A{row_index}:{chr(ord("A")+len(headers)-1)}{row_index}'
                    row_to_update = [existing_record.get(h, "") for h in headers]
                    worksheet.update(update_range, [row_to_update])
                    
                    record_updated = True
                    structured_data = existing_record # İstemciye güncellenmiş veriyi gönder
                    break

        # Takvim etkinliği oluşturma
        reminder_date_text = new_data.get("Hatırlatma_Tarihi_Metni", "").lower()
        reminder_time_text = new_data.get("Hatırlatma_Saati_Metni", "").lower()
        reminder_datetime_obj = None
        if reminder_date_text and reminder_date_text != "belirtilmedi":
            now = datetime.now()
            base_date = None
            if "yarın" in reminder_date_text: base_date = now + timedelta(days=1)
            elif "bugün" in reminder_date_text: base_date = now
            elif "gün sonra" in reminder_date_text:
                try: base_date = now + timedelta(days=int(''.join(filter(str.isdigit, reminder_date_text))))
                except: base_date = now
            elif "haftaya" in reminder_date_text:
                try:
                    date_text_en = reminder_date_text.replace("haftaya", "next")
                    turkish_replacements = {"pazartesi": "monday", "salı": "tuesday", "çarşamba": "wednesday", "perşembe": "thursday", "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}
                    for tr, en in turkish_replacements.items(): date_text_en = date_text_en.replace(tr, en)
                    base_date = parse(date_text_en, default=now)
                except: base_date = now + timedelta(weeks=1)
            elif "hafta sonra" in reminder_date_text:
                try: base_date = now + timedelta(weeks=int(''.join(filter(str.isdigit, reminder_date_text))))
                except: base_date = now
            elif "ay sonra" in reminder_date_text:
                try: base_date = now + relativedelta(months=int(''.join(filter(str.isdigit, reminder_date_text))))
                except: base_date = now
            else:
                try:
                    turkish_replacements = {"pazartesi": "monday", "salı": "tuesday", "çarşamba": "wednesday", "perşembe": "thursday", "cuma": "friday", "cumartesi": "saturday", "pazar": "sunday"}
                    for tr, en in turkish_replacements.items(): reminder_date_text = reminder_date_text.replace(tr, en)
                    base_date = parse(reminder_date_text, default=now)
                except: base_date = now
            
            hour, minute = 10, 0
            if reminder_time_text and reminder_time_text != "belirtilmedi":
                try:
                    time_parts = reminder_time_text.split(':')
                    hour = int(time_parts[0]); minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                except: hour, minute = 10, 0
            reminder_datetime_obj = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if reminder_datetime_obj and calendar_service:
            event_start_time = reminder_datetime_obj
            event_end_time = event_start_time + timedelta(hours=1)
            event = {
                'summary': new_data.get("Aksiyonlar", "İsimsiz Görev"),
                'description': f"Müşteri: {new_data.get('Müşteri_Adı', 'Belirtilmedi')}\nTelefon: {new_data.get('Telefon', 'Belirtilmedi')}\n\nNotlar:\n{transcript}",
                'start': {'dateTime': event_start_time.isoformat(), 'timeZone': 'Europe/Istanbul'},
                'end': {'dateTime': event_end_time.isoformat(), 'timeZone': 'Europe/Istanbul'},
            }
            calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        
        # Eğer kayıt güncellenmediyse, yeni bir kayıt olarak ekle
        if not record_updated and worksheet:
            kayit_tarihi = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reminder_for_sheet = reminder_datetime_obj.strftime("%Y-%m-%d %H:%M") if reminder_datetime_obj else "Belirtilmedi"
            
            row_to_insert = [
                current_user.email,
                new_data.get("Kaynak", "Belirtilmedi"),
                new_data.get("Müşteri_Adı", "Belirtilmedi"),
                new_data.get("Telefon", "Belirtilmedi"),
                new_data.get("Oturum_mu_Yatirim_mi", "Belirtilmedi"),
                new_data.get("Taraf", "Belirtilmedi"),
                new_data.get("Butce", "Belirtilmedi"),
                new_data.get("Oda_Sayisi", "Belirtilmedi"),
                new_data.get("MetreKare", "Belirtilmedi"),
                new_data.get("Bina_Yasi", "Belirtilmedi"),
                new_data.get("Kat", "Belirtilmedi"),
                new_data.get("Balkon", "Belirtilmedi"),
                new_data.get("Asansor", "Belirtilmedi"),
                new_data.get("Konum", "Belirtilmedi"),
                new_data.get("Mahalle", "Belirtilmedi"),
                new_data.get("Havuz", "Belirtilmedi"),
                new_data.get("Otopark", "Belirtilmedi"),
                new_data.get("Manzara", "Belirtilmedi"),
                new_data.get("Notlar", "Belirtilmedi"),
                new_data.get("Konut_Tipi", "Belirtilmedi"),
                kayit_tarihi,
                new_data.get("Aksiyonlar", "Belirtilmedi"),
                reminder_for_sheet
            ]
            worksheet.append_row(row_to_insert, value_input_option='USER_ENTERED')
            structured_data = new_data
        
        return jsonify({"status": "success", "data": structured_data})
    except Exception as e:
        print(f"\n!!!! HATA !!!!\n{e}\n!!!!!!!!!!!!!!")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
