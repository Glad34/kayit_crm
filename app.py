import os
import json
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
from googleapiclient.errors import HttpError

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
    access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    userinfo_endpoint='https://openidconnect.googleapis.com/v1.0/userinfo',
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
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
    calendar_events = []
    if worksheet:
        try:
            all_data = worksheet.get_all_records()
            user_email = current_user.email
            user_records = [rec for rec in all_data if rec.get('Danışman_Eposta') == user_email]
            records = list(reversed(user_records))

            for record in records:
                try:
                    hatirlatma_tarihi_str = record.get('Hatırlatma_Tarihi')
                    aksiyon = record.get('Aksiyonlar')
                    
                    if hatirlatma_tarihi_str and hatirlatma_tarihi_str != 'Belirtilmedi' and aksiyon and aksiyon != 'Belirtilmedi':
                        event = {
                            'title': aksiyon,
                            'start': hatirlatma_tarihi_str.replace(' ', 'T'),
                            'extendedProps': {
                                'musteri': record.get('Müşteri_Adı', 'N/A'),
                                'telefon': record.get('Telefon', 'N/A'),
                                'notlar': record.get('Notlar', 'N/A'),
                                'taraf': record.get('Taraf', 'N/A'),
                                'butce': record.get('Butce', 'N/A')
                            }
                        }
                        calendar_events.append(event)
                except Exception as e:
                    print(f"Takvim etkinliği oluşturulurken bir kayıtta hata oluştu: {record}. Hata: {e}")

        except gspread.exceptions.APIError as e:
            print(f"Google Sheets API Hatası: {e}")
        except Exception as e:
            print(f"E-Tablodan veri çekerken hata oluştu: {e}")
            
    return render_template('index.html', records=records, calendar_events_json=json.dumps(calendar_events))

# --- VERİ İŞLEME ROTASI ---
@app.route('/process', methods=['POST'])
@login_required
def process_transcript():
    try:
        data = request.get_json()
        transcript = data.get('transcript')
        if not transcript:
            return jsonify({"status": "error", "message": "Boş metin gönderildi."}), 400

        model = GenerativeModel("gemini-1.5-pro-001") # Modeli güncelledim
        prompt = get_gemini_prompt(transcript)
        response = model.generate_content(prompt)
        
        # Yanıtın temizlenmesi
        cleaned_response_text = response.text.strip()
        if cleaned_response_text.startswith("```json"):
            cleaned_response_text = cleaned_response_text[7:-3].strip()
        
        structured_data = json.loads(cleaned_response_text)
        
        # Tarih ve saat işleme
        reminder_datetime_obj = None
        try:
            reminder_date_text = structured_data.get("Hatırlatma_Tarihi_Metni", "").lower()
            reminder_time_text = structured_data.get("Hatırlatma_Saati_Metni", "").lower()

            if reminder_date_text and reminder_date_text != "belirtilmedi":
                now = datetime.now()
                base_date = now

                if "yarın" in reminder_date_text: base_date = now + timedelta(days=1)
                elif "gün sonra" in reminder_date_text:
                    days = int(''.join(filter(str.isdigit, reminder_date_text)) or 1)
                    base_date = now + timedelta(days=days)
                elif "hafta sonra" in reminder_date_text:
                    weeks = int(''.join(filter(str.isdigit, reminder_date_text)) or 1)
                    base_date = now + timedelta(weeks=weeks)
                elif "ay sonra" in reminder_date_text:
                    months = int(''.join(filter(str.isdigit, reminder_date_text)) or 1)
                    base_date = now + relativedelta(months=months)
                else:
                    try: base_date = parse(reminder_date_text, default=now)
                    except: pass
                
                hour, minute = 10, 0 # Varsayılan saat
                if reminder_time_text and reminder_time_text != "belirtilmedi":
                    try:
                        parsed_time = parse(reminder_time_text)
                        hour, minute = parsed_time.hour, parsed_time.minute
                    except: pass
                
                reminder_datetime_obj = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception as e:
            print(f"Tarih/saat ayrıştırma hatası: {e}")

        # Google Takvim'e ekleme
        if reminder_datetime_obj and calendar_service:
            try:
                event = {
                    'summary': structured_data.get("Aksiyonlar", "İsimsiz Görev"),
                    'description': f"Müşteri: {structured_data.get('Müşteri_Adı', 'Belirtilmedi')}\nTelefon: {structured_data.get('Telefon', 'Belirtilmedi')}\n\nNotlar:\n{transcript}",
                    'start': {'dateTime': reminder_datetime_obj.isoformat(), 'timeZone': 'Europe/Istanbul'},
                    'end': {'dateTime': (reminder_datetime_obj + timedelta(hours=1)).isoformat(), 'timeZone': 'Europe/Istanbul'},
                }
                calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                print("Google Takvim etkinliği oluşturuldu.")
            except HttpError as e:
                print(f"Google Takvim'e etkinlik eklenirken HATA oluştu: {e}")

        # Google E-Tablolar'a ekleme
        kayit_tarihi = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reminder_for_sheet = reminder_datetime_obj.strftime("%Y-%m-%d %H:%M") if reminder_datetime_obj else "Belirtilmedi"
        
        # Sütun başlıklarının tam olarak E-Tablonuzdaki gibi olduğundan emin olun!
        row_to_insert = [
            current_user.email,
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
            structured_data.get("Mahalle", "Belirtilmedi"),
            structured_data.get("Havuz", "Belirtilmedi"),
            structured_data.get("Otopark", "Belirtilmedi"),
            structured_data.get("Manzara", "Belirtilmedi"),
            structured_data.get("Notlar", "Belirtilmedi"),
            structured_data.get("Konut_Tipi", "Belirtilmedi"),
            kayit_tarihi,
            structured_data.get("Aksiyonlar", "Belirtilmedi"),
            reminder_for_sheet
        ]
        if worksheet:
            worksheet.append_row(row_to_insert, value_input_option='USER_ENTERED')
            print("Google E-Tablolar'a yeni kayıt eklendi.")
        
        return jsonify({"status": "success", "data": structured_data})

    except json.JSONDecodeError as e:
        print(f"JSON Ayrıştırma Hatası: {e}\nGelen Ham Metin: {response.text}")
        return jsonify({"status": "error", "message": "Yapay zekadan geçersiz formatta yanıt alındı."}), 500
    except Exception as e:
        import traceback
        print(f"\n!!!! GENEL HATA !!!!\n{traceback.format_exc()}\n!!!!!!!!!!!!!!")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Yerel test için debug=True, canlı ortam için False olmalı
    # ve host='0.0.0.0' ile dışarıya açılabilir.
    app.run(debug=True)
