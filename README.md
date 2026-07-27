# 🛡️ AEGIS: Gemini AI Agent & Secure Auth Backend

AEGIS, **CustomTkinter** tabanlı, kod odaklı ve dosya kontrol yeteneklerine sahip masaüstü bir **Gemini AI Ajanıdır**. Bu sürümle birlikte sisteme üretim seviyesinde (production-ready) asimetrik **Google OAuth 2.0 / JWT Kimlik Doğrulama Sunucusu** ve masaüstü güvenli kimlik kasası entegrasyonu eklenmiştir.

---

## 🚀 Öne Çıkan Özellikler

### 🤖 Gemini Yapay Zeka Yetenekleri
- **Kod Odaklı Sistem İstemi**: Dosya oluşturma, düzenleme, silme ve terminal komutlarını doğrudan çalıştırabilme.
- **API Kota Rotasyonu**: Sınırsız sayıda Gemini API anahtarı arasında otomatik kota geçişi ve arka planda saatlik otomatik canlandırma (test çağrıları ile).
- **Yerleşik Tarayıcı Kontrolü (Playwright)**: Sanal imleç ve gerçek zamanlı ekran izleme entegrasyonu ile otonom tarayıcı kontrolü.
- **Git & GitHub Entegrasyonu**: Ajanın kendi kararıyla commit atabilmesi, branch oluşturabilmesi ve GitHub CLI üzerinden PR (Pull Request) açabilmesi.
- **Dosya Seçici & Medya Desteği**: Kod, görsel, PDF, ses ve video dosyalarını doğrudan analiz edebilme.
- **Canlı Türkçe Sesli Sohbet**: Google TTS (`gTTS`) altyapılı yüksek kaliteli ve asenkron oynatılabilir Türkçe sesli asistan modu.

### 🔑 Güvenlik ve Kimlik Doğrulama (Auth Backend)
- **Gerçek Google Sign-In**: Kullanıcılar sadece doğrulanmış gerçek Google hesaplarıyla oturum açabilirler.
- **Asimetrik JWT Güvenliği (RS256)**: Access tokenlar sunucu tarafında RSA Özel Anahtarı ile imzalanır; istemciler doğrulamayı Genel Anahtar ile yapar.
- **Refresh Token Hashing**: Tokenlar veritabanına asla düz metin olarak yazılmaz; **SHA-256** ile şifrelenerek saklanır.
- **Refresh Token Rotation (RTR)**: Her yenileme isteğinde eski token çifti revize edilir. Eski tokenın tekrar kullanımı (replay attack) algılandığı an o kullanıcının tüm oturumları siber güvenlik protokolü gereği kapatılır.
- **Anlık Erişim İptali (Instant Revocation)**: Oturum kapatıldığı veya cihazlar sonlandırıldığı an access token süresi dolmamış olsa dahi veritabanı kontrolüyle anında geçersiz kılınır.
- **Güvenli Kimlik Kasası (Keyring)**: Masaüstü istemci, tokenları düz metin dosyalarında saklamak yerine, işletim sisteminin şifreli kasasında (**Windows Credential Manager / macOS Keychain / Linux Secret Service**) saklar.
- **Slowapi Rate Limiter**: Giriş ve yenileme endpoints için IP/Token bazlı hız sınırları (örn. Giriş: 10 istek/dk).
- **Security Logging**: Şüpheli istekler, replay denemeleri ve yetki yükseltme istekleri özel güvenlik loglarında toplanır.

---

## 📂 Backend Dizin Yapısı (Temiz Mimari & SOLID)

FastAPI backend uygulamamız **Clean Architecture** prensiplerine uygun olarak tasarlanmıştır:
```
backend/
├── app/
│   ├── main.py            # Uygulama girdisi, CORS, Rate Limiting, Exception Handler
│   ├── config.py          # Pydantic BaseSettings ile .env ve RSA anahtar yönetimi
│   ├── database.py        # SQLite/PostgreSQL motoru
│   ├── models/            # SQLAlchemy DB Şemaları (Users, Sessions, RefreshTokens)
│   ├── repositories/      # Repository Pattern arayüz ve somut sınıfları
│   ├── services/          # Token üretimi, RTR, Google doğrulama servis mantığı
│   ├── schemas/           # Pydantic DTO veri modelleri
│   └── routers/           # API Endpoints (/auth/google, /auth/me, /auth/sessions...)
├── migrations/            # Alembic Migration sürümleri
└── tests/                 # pytest birim (unit) ve entegrasyon testleri
```

---

## 🛠 Kurulum Kılavuzu

### 1. Sistem Gereksinimleri
- Python 3.9+ veya Python 3.13+
- Git ve GitHub CLI (opsiyonel, ajan entegrasyonu için)

### 2. Aegis Masaüstü İstemcisi Kurulumu
Proje kök dizinindeyken sanal ortam oluşturup istemci bağımlılıklarını yükleyin:
```bash
# Sanal ortam oluşturma ve aktifleştirme
python -m venv venv
.\venv\Scripts\activate      # Windows için
source venv/bin/activate    # Linux/macOS için

# Bağımlılıkları kurun
pip install -r requirements.txt

# Tarayıcı modülünü kurun (opsiyonel)
playwright install
```

### 3. Backend Servisi Kurulumu
`backend/` klasörüne geçiş yapıp backend bağımlılıklarını kurun ve veritabanı şemasını uygulayın:
```bash
# Backend gereksinimlerini yükleyin
cd backend
pip install -r requirements.txt

# Alembic ile veritabanı şemalarını oluşturun
alembic upgrade head
```

---

## ⚙️ Yapılandırma ve Çalıştırma

### 1. `.env` Ayarları
`backend/.env` dosyasını oluşturun ve yapılandırın:
```env
GOOGLE_CLIENT_ID=sandbox
JWT_ALGORITHM=RS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
DATABASE_URL=sqlite:///./aegis_auth.db
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://localhost:8080,desktop://
```
> [!NOTE]
> `GOOGLE_CLIENT_ID` değeri `sandbox` olarak kaldığında uygulama **Sandbox Test Modu**nda çalışır. Gerçek Google Giriş ekranının açılması için Google Cloud Console'dan aldığınız Web OAuth Client ID değerini buraya girmelisiniz.

### 2. Çalıştırma

**Backend Sunucusunu Başlatın (Port: 8000)**:
```bash
cd backend
uvicorn app.main:app --port 8000
```

**Aegis Arayüzünü Başlatın**:
Kök dizine geri dönüp istemciyi çalıştırın:
```bash
python aegis.py
```

---

## 🧪 Testler ve Doğrulama

### 1. Birim (Unit) Testleri Çalıştırma
Alembic şemaları, şifreleme ve RTR mekanizmaları in-memory veritabanı üzerinden test edilir:
```bash
cd backend
pytest tests/
```

### 2. End-to-End Entegrasyon Testleri Çalıştırma
Aktif çalışan sunucu üzerinden tam oturum açma, profil çekme, rotasyon ve çıkış protokolü doğrulanır:
```bash
python backend/tests/test_integration.py
```

## 📄 Lisans
Bu proje kişisel ve deneysel bir yapay zeka ajanı projesidir. Kullanım şartları ve detaylar için depo sahibi ile iletişime geçin.
