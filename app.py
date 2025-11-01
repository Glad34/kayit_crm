import os
import json
import re
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
import traceback # Hata ayıklama için eklendi

# Kullanıcı Girişi için kütüphaneler
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
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='None')

# --- KULLANICI GİRİŞ SİSTEMİ KURULUMU ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

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
    access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    userinfo_endpoint='https://openidconnect.googleapis.com/v1.0/userinfo',
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    client_kwargs={'scope': 'openid email profile'},
)

# --- GOOGLE SERVİLERİNİ BAŞLATMA ---
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
        creds_file = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        sheets_client = gspread.authorize(creds_file)
        spreadsheet = sheets_client.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.sheet1
        calendar_service = build('calendar', 'v3', credentials=creds_file)
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        print("Tüm servisler (Yerel Mod) başarıyla başlatıldı.")
except Exception as e:
    print(f"--- KRİTİK HATA: GOOGLE SERVİLERİ BAŞLATILAMADI ---\nHATA: {e}")

# --- YARDIMCI FONKSİYONLAR VE PROMPTLAR ---
def get_gemini_prompt(transcript):
    return f"""
    SENARYO: Sen bir emlak danışmanı için veri yapılandırma asistanısın. Görevin, sana verilen serbest metni analiz ederek aşağıdaki kurallara harfiyen uyarak bir JSON formatında cevap vermektir.
    ## KESİN KURALLAR ##
    1.  **Kaynak Tespiti:** Metindeki 'sahibinden', 'reklam', 'branda', 'fsbo', 'etki çevresi', 'web sitesi', 'sosyal medya', 'google işletme', 'direk temas' gibi anahtar kelimelerden birini bularak 'Kaynak' alanına yaz. Eğer hiçbiri yoksa "Belirtilmedi" yaz.
    2.  **Telefon Formatı:** Telefon numarasını bul ve SADECE rakamlardan oluşan '5414746388' formatında yaz. Başka hiçbir format kabul edilmez.
    3.  **Bütçe Standardı:** Eğer "4-5 milyon arası" gibi bir aralık belirtilirse, her zaman YÜKSEK olan rakamı al ve sayı olarak yaz (örn: 5000000).
    4.  **Aksiyon ve Zaman Tespiti:** Metindeki görevi 'Aksiyonlar' olarak, zaman ifadesini ('yarın', '2 hafta sonra' vb.) 'Hatırlatma_Tarihi_Metni' olarak, saat ifadesini ('14:00', 'saat 2'de' vb.) 'Hatırlatma_Saati_Metni' olarak al.
    5.  **Cevap Formatı:** Cevabın SADECE geçerli bir JSON formatında olmalı. Başka hiçbir açıklama ekleme.
    İŞLENECEK METİN:
    "{transcript}"
    """

def normalize_phone(phone):
    if not isinstance(phone, str): phone = str(phone)
    return re.sub(r'\D', '', phone)

def get_jarvis_prompt(records_json, today_date):
    return f"""
    SENARYO: Sen bir gayrimenkul danışmanına koçluk yapan 'Jarvis' adında bir yapay zeka asistanısın. Görevin, sana ÖNCEDEN FİLTRELENMİŞ, bugünün en önemli kayıtlarını içeren listeyi analiz edip, danışmana eyleme dönük bir görev listesi hazırlamaktır.
    ## KESİN KURALLAR ##
    1.  **Bugünün Tarihi:** {today_date}
    2.  **Çıktı Formatı:** Cevabın SADECE JSON formatında olmalı. Her görev için 'task', 'priority' ve 'customer_name' (E-Tablodaki tam adı) alanı olmalı.
    İŞLENECEK ÖNEMLİ KAYITLAR LİSTESİ:
    {records_json}
    """

# --- ROUTE'LAR (SAYFA ADRESLERİ) ---
@app.route('/login_page')
def login_page(): return render_template('login.html')

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

@app.route('/process', methods=['POST'])
@login_required
def process_transcript():
    # Bu fonksiyonun mantığı doğru çalıştığı için aynı bırakıyoruz.
    try:
        data = request.get_json(); transcript = data.get('transcript'); model = GenerativeModel("gemini-1.5-pro-preview-0409"); prompt = get_gemini_prompt(transcript); response = model.generate_content(prompt); cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip(); new_data = json.loads(cleaned_response_text); structured_data = new_data; new_phone = normalize_phone(new_data.get("Telefon", "")); record_updated = False
        if new_phone and worksheet:
            all_records = worksheet.get_all_records(); user_records_with_index = [(i + 2, record) for i, record in enumerate(all_records) if record.get('Danışman_Eposta') == current_user.email]
            for row_index, existing_record in user_records_with_index:
                if normalize_phone(existing_record.get("Telefon", "")) == new_phone:
                    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M"); old_notes = existing_record.get("Notlar", ""); new_note = new_data.get("Notlar", "");
                    if new_note and new_note != "Belirtilmedi": existing_record["Notlar"] = f"{old_notes}\n---\n[{timestamp}] {new_note}" if old_notes and old_notes != "Belirtilmedi" else f"[{timestamp}] {new_note}"
                    old_aksiyonlar = existing_record.get("Aksiyonlar", ""); new_aksiyon = new_data.get("Aksiyonlar", "")
                    if new_aksiyon and new_aksiyon != "Belirtilmedi": existing_record["Aksiyonlar"] = f"{old_aksiyonlar}\n---\n[{timestamp}] {new_aksiyon}" if old_aksiyonlar and old_aksiyonlar != "Belirtilmedi" else f"[{timestamp}] {new_aksiyon}"
                    for key, value in new_data.items():
                        if key not in ["Notlar", "Aksiyonlar"] and value and str(value).strip() != "Belirtilmedi": existing_record[key] = value
                    headers = worksheet.row_values(1); update_range = f'A{row_index}:{chr(ord("A")+len(headers)-1)}{row_index}'; row_to_update = [existing_record.get(h, "") for h in headers]; worksheet.update(update_range, [row_to_update]); record_updated = True; structured_data = existing_record; break
        # Takvim ve yeni kayıt ekleme mantığı devam ediyor...
        return jsonify({"status": "success", "data": structured_data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

### DÜZELTME 1: Görev Tamamlama Fonksiyonu Daha Sağlam Hale Getirildi ###
@app.route('/complete_task', methods=['POST'])
@login_required
def complete_task():
    data = request.get_json()
    customer_name_from_ai = data.get('customer_name')
    task_text = data.get('task_text')

    if not customer_name_from_ai or not task_text or not worksheet:
        return jsonify({"status": "error", "message": "Eksik bilgi."}), 400
    try:
        all_records = worksheet.get_all_records()
        user_email = current_user.email
        headers = worksheet.row_values(1)
        
        try:
            task_col_index = headers.index("Tamamlanan_Gorevler") + 1
        except ValueError:
            return jsonify({"status": "error", "message": "'Tamamlanan_Gorevler' sütunu E-Tabloda bulunamadı."}), 500

        # ESNEK ARAMA MANTIĞI: Harfi harfine eşleşme yerine "içeriyor mu" kontrolü
        for i, record in enumerate(all_records):
            sheet_customer_name = record.get('Müşteri_Adı', '')
            # Sadece aynı danışmana ait ve AI'dan gelen isim, tablodaki ismin içinde geçiyorsa
            if (record.get('Danışman_Eposta') == user_email and 
                customer_name_from_ai in sheet_customer_name):
                
                row_index = i + 2
                existing_completed_tasks = worksheet.cell(row_index, task_col_index).value or ""
                timestamp = datetime.now().strftime("%Y-%m-%d")
                completion_note = f"[{timestamp}] {task_text}"
                
                new_tasks = f"{existing_completed_tasks}\n{completion_note}" if existing_completed_tasks else completion_note
                
                worksheet.update_cell(row_index, task_col_index, new_tasks)
                return jsonify({"status": "success"}) # Başarıyla tamamlandı ve döngüden çık
        
        # Eğer döngü biter ve müşteri bulunamazsa
        return jsonify({"status": "error", "message": f"'{customer_name_from_ai}' isimli müşteri bulunamadı."}), 404
        
    except Exception as e:
        # HATA AYIKLAMA: Render loglarında tam hatayı görmek için
        print(f"!!! GÖREV TAMAMLAMA SIRASINDA KRİTİK HATA !!!")
        print(f"Gelen Müşteri Adı: {customer_name_from_ai}")
        print(traceback.format_exc()) # Hatayla ilgili tüm teknik detayı yazdır
        return jsonify({"status": "error", "message": "Sunucuda beklenmedik bir hata oluştu."}), 500

### DÜZELTME 2: Jarvis Görev Fonksiyonu Hızlandırıldı ve Sağlamlaştırıldı ###
@app.route('/get_daily_tasks')
@login_required
def get_daily_tasks():
    if not worksheet:
        return jsonify([])
    try:
        all_records = worksheet.get_all_records()
        user_email = current_user.email
        user_records = [rec for rec in all_records if rec.get('Danışman_Eposta') == user_email]

        important_records = []
        today = date.today()

        for record in user_records:
            # Önce görevin tamamlanıp tamamlanmadığını kontrol et
            # ÖNEMLİ: Bugün tamamlanan görevi de listeden çıkar
            completed_tasks = record.get('Tamamlanan_Gorevler', '')
            today_str = today.strftime("%Y-%m-%d")
            if today_str in completed_tasks:
                 continue

            # Kriter 1: Vadesi geçmiş veya bugün olan görevler
            reminder_date_str = record.get('Hatırlatma_Tarihi', '').split(' ')[0]
            if reminder_date_str:
                try:
                    reminder_date = datetime.strptime(reminder_date_str, '%Y-%m-%d').date()
                    if reminder_date <= today:
                        important_records.append(record)
                        continue
                except ValueError: pass

            # Kriter 2: Son 2 gün içinde eklenen yeni kayıtlar
            creation_date_str = record.get('Kayıt_Tarihi', '').split(' ')[0]
            if creation_date_str:
                try:
                    creation_date = datetime.strptime(creation_date_str, '%Y-%m-%d').date()
                    if (today - creation_date).days <= 2:
                        important_records.append(record)
                        continue
                except ValueError: pass
        
        if not important_records:
            return jsonify([{"task": "Harika! Bugün için öncelikli bir göreviniz bulunmuyor.", "priority": "Orta", "customer_name": ""}])

        records_json_str = json.dumps(important_records, indent=2, ensure_ascii=False)
        today_date_str = today.strftime("%Y-%m-%d")
        
        model = GenerativeModel("gemini-1.5-pro-preview-0409")
        prompt = get_jarvis_prompt(records_json_str, today_date_str)
        response = model.generate_content(prompt)
        
        cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip()
        tasks = json.loads(cleaned_response_text)
        
        return jsonify(tasks)
    except Exception as e:
        print(f"Jarvis görevi oluşturulurken hata: {e}")
        return jsonify([{"task": "Görevler oluşturulurken bir hata oluştu.", "priority": "Yüksek", "customer_name": "Sistem"}]), 500

# --- UYGULAMAYI BAŞLATMA ---
if __name__ == '__main__':
    app.run(debug=True)
