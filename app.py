import os
import json
import re
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
import traceback

# Kullanıcı Girişi için kütüphaneler
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth

# Google Kütüphaneleri
import vertexai
from vertexai.generative_models import GenerativeModel
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
        self.id = id; self.name = name; self.email = email
users = {}

@login_manager.user_loader
def load_user(user_id): return users.get(user_id)

# --- OAUTH (GOOGLE İLE GİRİŞ) KURULUMU ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'), client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    access_token_url='https://accounts.google.com/o/oauth2/token', access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth', authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/', userinfo_endpoint='https://openidconnect.googleapis.com/v1.0/userinfo',
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs", client_kwargs={'scope': 'openid email profile'},
)

# --- GOOGLE SERVİLERİNİ BAŞLATMA ---
PROJECT_ID = "masaustuotomasyon"
LOCATION = "us-central1"
SPREADSHEET_ID = "1xjdxkMXKe3iQjD9rosNb69CIo36JhHUCPM-4kYzzRBM"
CALENDAR_ID = 'onurglad34@gmail.com'
creds = None; worksheet = None; calendar_service = None
try:
    google_creds_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/cloud-platform"]
    if google_creds_json_str:
        google_creds_dict = json.loads(google_creds_json_str)
        creds = Credentials.from_service_account_info(google_creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=creds)
    sheets_client = gspread.authorize(creds)
    spreadsheet = sheets_client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.sheet1
    calendar_service = build('calendar', 'v3', credentials=creds)
    print("Tüm Google Servisleri başarıyla başlatıldı.")
except Exception as e:
    print(f"--- KRİTİK HATA: GOOGLE SERVİLERİ BAŞLATILAMADI ---\nHATA: {e}")

# --- YARDIMCI FONKSİYONLAR VE PROMPTLAR ---
def normalize_phone(phone):
    if not isinstance(phone, str): phone = str(phone)
    return re.sub(r'\D', '', phone)

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

def get_jarvis_prompt(records_json, today_date):
    return f"""
    SENARYO: Sen bir gayrimenkul danışmanına koçluk yapan 'Jarvis' adında bir yapay zeka asistanısın. Görevin, sana ÖNCEDEN FİLTRELENMİŞ, bugünün en önemli kayıtlarını içeren listeyi analiz etmektir.
    ## ANALİZ KURALLARI ##
    1.  Her müşteri kaydındaki 'Aksiyonlar', 'Notlar' ve özellikle 'Tamamlanan_Aksiyonlar' geçmişini incele.
    2.  Müşterinin sürecindeki **bir sonraki mantıklı adımı** öner. Örneğin, 'emsal analizi yap' görevi tamamlandıysa, bir sonraki görevin 'Müşteriyi arayıp emsal raporunu sun' olmalı.
    3.  Önerdiğin görevin aciliyetine göre ('Hatırlatma_Tarihi' geçmişse 'Yüksek', yeni müşteriyse 'Orta' vb.) önceliklendir.
    ## ÇIKTI KURALLARI ##
    1.  Cevabın SADECE JSON formatında bir liste olmalı.
    2.  Her JSON objesi şunları İÇERMEK ZORUNDA: 'task' (görev metni), 'priority' ('Yüksek', 'Orta', 'Düşük'), 'telefon' (müşterinin telefon numarası) ve 'takvim_etkinlik_id' (eğer varsa, yoksa boş string "").
    İŞLENECEK ÖNEMLİ KAYITLAR LİSTESİ (BUGÜN: {today_date}):
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
        token = google.authorize_access_token(); session['user_token'] = token; user_info = google.get('userinfo').json()
        user_id = user_info['id']; user = User(id=user_id, name=user_info.get('name', 'İsimsiz'), email=user_info['email'])
        users[user_id] = user; login_user(user); return redirect(url_for('index'))
    except Exception as e:
        print(f"Giriş sırasında hata: {e}")
        return redirect(url_for('login_page'))

@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('login_page'))

@app.route('/')
@login_required
def index():
    records = []
    if worksheet:
        try:
            all_data = worksheet.get_all_records()
            user_records = [rec for rec in all_data if rec.get('Danışman_Eposta') == current_user.email]
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
    try:
        data = request.get_json(); transcript = data.get('transcript')
        model = GenerativeModel("gemini-1.5-pro-preview-0409"); prompt = get_gemini_prompt(transcript)
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip()
        new_data = json.loads(cleaned_response_text)
        
        event_id = ""
        # ... (Takvim etkinliği oluşturma mantığınız burada yer alacak ve event_id'yi dolduracak) ...
        
        new_phone = normalize_phone(new_data.get("Telefon", ""))
        # ... (E-Tabloya yeni satır ekleme veya güncelleme mantığınız burada yer alacak) ...
        # Güncelleme veya ekleme yaparken, 'Takvim_Etkinlik_ID' sütununa event_id'yi de yazın.
            
        return jsonify({"status": "success", "data": new_data})
    except Exception as e:
        print(f"HATA (/process): {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/complete_task', methods=['POST'])
@login_required
def complete_task():
    data = request.get_json()
    telefon = data.get('telefon')
    task_text = data.get('task_text')
    takvim_etkinlik_id = data.get('takvim_etkinlik_id')

    if not telefon or not task_text or not worksheet:
        return jsonify({"status": "error", "message": "Eksik bilgi."}), 400
    try:
        all_records = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        
        try:
            task_col_index = headers.index("Tamamlanan_Aksiyonlar") + 1
            event_id_col_index = headers.index("Takvim_Etkinlik_ID") + 1
        except ValueError as e:
            return jsonify({"status": "error", "message": f"E-Tabloda gerekli sütun bulunamadı: {e}"}), 500

        for i, record in enumerate(all_records):
            if (record.get('Danışman_Eposta') == current_user.email and 
                normalize_phone(record.get("Telefon", "")) == normalize_phone(telefon)):
                
                row_index = i + 2
                
                existing_tasks = worksheet.cell(row_index, task_col_index).value or ""
                completion_note = f"[{datetime.now().strftime('%Y-%m-%d')}] {task_text}"
                new_tasks = f"{existing_tasks}\n{completion_note}" if existing_tasks else completion_note
                worksheet.update_cell(row_index, task_col_index, new_tasks)
                
                if takvim_etkinlik_id and calendar_service:
                    try:
                        calendar_service.events().delete(calendarId=CALENDAR_ID, eventId=takvim_etkinlik_id).execute()
                        worksheet.update_cell(row_index, event_id_col_index, "")
                    except HttpError as e:
                        if e.resp.status == 404: print(f"Takvim etkinliği ({takvim_etkinlik_id}) zaten silinmiş.")
                        else: raise e
                
                return jsonify({"status": "success"})
        
        return jsonify({"status": "error", "message": "Müşteri telefon numarası bulunamadı."}), 404
    except Exception as e:
        print(f"HATA (/complete_task): {traceback.format_exc()}")
        return jsonify({"status": "error", "message": "Sunucuda beklenmedik bir hata oluştu."}), 500

@app.route('/get_daily_tasks')
@login_required
def get_daily_tasks():
    if not worksheet: return jsonify([])
    try:
        all_records = worksheet.get_all_records()
        user_records = [rec for rec in all_records if rec.get('Danışman_Eposta') == current_user.email]

        important_records = []
        today = date.today()

        for record in user_records:
            is_important = False
            # Kriter 1: Vadesi geçmiş veya bugün olan görevler
            reminder_date_str = record.get('Hatırlatma_Tarihi', '').split(' ')[0]
            if reminder_date_str:
                try:
                    if datetime.strptime(reminder_date_str, '%Y-%m-%d').date() <= today: is_important = True
                except ValueError: pass
            
            # Kriter 2: Son 3 günde eklenen yeni kayıtlar
            creation_date_str = record.get('Kayıt_Tarihi', '').split(' ')[0]
            if not is_important and creation_date_str:
                try:
                    if (today - datetime.strptime(creation_date_str, '%Y-%m-%d').date()).days <= 3: is_important = True
                except ValueError: pass

            if is_important: important_records.append(record)
        
        if not important_records:
            return jsonify([{"task": "Harika! Bugün için öncelikli bir göreviniz bulunmuyor.", "priority": "Orta", "telefon": "", "takvim_etkinlik_id": ""}])

        records_json_str = json.dumps(important_records, indent=2, ensure_ascii=False)
        today_date_str = today.strftime("%Y-%m-%d")
        
        model = GenerativeModel("gemini-1.5-pro-preview-0409")
        prompt = get_jarvis_prompt(records_json_str, today_date_str)
        response = model.generate_content(prompt)
        
        cleaned_response_text = response.text.replace("```json", "").replace("```", "").strip()
        tasks = json.loads(cleaned_response_text)
        
        return jsonify(tasks)
    except Exception as e:
        print(f"HATA (/get_daily_tasks): {traceback.format_exc()}")
        return jsonify([{"task": "Görevler oluşturulurken bir hata oluştu.", "priority": "Yüksek", "telefon": "", "takvim_etkinlik_id": ""}]), 500

# --- UYGULAMAYI BAŞLATMA ---
if __name__ == '__main__':
    app.run(debug=True)
