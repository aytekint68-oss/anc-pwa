import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient

app = FastAPI(title="ANC Dosya Takip PWA")

# Ortam Değişkenleri (Render panelinden girilecek)
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "anc_veritabani")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "gizli_admin_sifrem_2026")

TSI = timezone(timedelta(hours=3))

client = MongoClient(MONGO_URI) if MONGO_URI else None
db = client[DB_NAME] if client else None

# Statik PWA dosyalarını (manifest.json, sw.js, ikonlar) sun
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# İstek Modelleri
class AbonelikIstegi(BaseModel):
    dosya_no: str
    madde: str  # "Madde 10" veya "Madde 11"
    subscription: dict
    admin_kodu: Optional[str] = ""


class AbonelikSilIstegi(BaseModel):
    dosya_no: str
    madde: str
    endpoint: str


@app.get("/")
async def ana_sayfa():
    return FileResponse("static/index.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Service Worker'ın tüm siteyi kapsayabilmesi için kök dizinden (/) sunulması zorunludur
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/ping")
async def ping():
    """takipci.py tarafından Render uyku modunu önlemek için çağrılır."""
    return {"status": "ok", "time": datetime.now(TSI).strftime("%H:%M:%S")}


@app.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.get("/api/sorgula")
async def dosya_sorgula(dosya_no: str, madde: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Veritabanı bağlantısı yapılandırılmamış.")

    temiz_no = dosya_no.strip()
    if not temiz_no:
        raise HTTPException(status_code=400, detail="Lütfen geçerli bir dosya numarası girin.")

    koleksiyon_adi = "kararlar_m11" if madde == "Madde 11" else "kararlar_m10"
    col = db[koleksiyon_adi]

    # Veritabanındaki olası alan adlarına göre esnek arama (dosya_no / Dosya_No / dosya)
    sonuclar = list(col.find({
        "$or": [
            {"dosya_no": temiz_no},
            {"Dosya_No": temiz_no},
            {"dosya": temiz_no}
        ]
    }, {"_id": 0}))

    if not sonuclar:
        return {
            "bulundu": False,
            "mesaj": f"{temiz_no} ({madde}) numaralı dosya henüz onaylanan listelerde yer almıyor."
        }

    # İlk eşleşen kaydın detaylarını döndür
    kayit = sonuclar[0]
    pdf_adi = kayit.get("pdf_adi") or kayit.get("dosya_adi") or kayit.get("Ordin") or "Karar PDF"
    tarih = kayit.get("tarih") or kayit.get("Tarih") or kayit.get("karar_tarihi") or "-"
    pdf_url = kayit.get("pdf_url") or kayit.get("link") or kayit.get("url") or ""

    return {
        "bulundu": True,
        "dosya_no": temiz_no,
        "madde": madde,
        "pdf_adi": pdf_adi,
        "tarih": tarih,
        "pdf_url": pdf_url,
        "toplam_eslesme": len(sonuclar)
    }


@app.post("/api/abone-ol")
async def abone_ol(veri: AbonelikIstegi):
    if db is None:
        raise HTTPException(status_code=500, detail="Veritabanı bağlantısı yok.")

    temiz_no = veri.dosya_no.strip()
    endpoint = veri.subscription.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Geçersiz bildirim aboneliği.")

    is_admin = (veri.admin_kodu == ADMIN_SECRET) and (ADMIN_SECRET != "")

    # Eğer sadece admin cihaz kaydı yapılıyorsa (dosya no boşsa)
    if is_admin and not temiz_no:
        db["pwa_abonelikleri"].update_one(
            {"subscription.endpoint": endpoint, "is_admin": True},
            {"$set": {
                "subscription": veri.subscription,
                "is_admin": True,
                "genel_bildirim": True,
                "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
            }},
            upsert=True
        )
        return {"status": "ok", "mesaj": "✅ Cihazınız Yönetici (Admin) olarak kaydedildi! Her akşam 20:00 raporu bu telefona gelecek."}

    if not temiz_no:
        raise HTTPException(status_code=400, detail="Lütfen takip edilecek dosya numarasını girin.")

    # Aynı cihaz + aynı dosya + aynı madde varsa güncelle, yoksa yeni ekle
    filtre = {
        "subscription.endpoint": endpoint,
        "dosya_no": temiz_no,
        "madde": veri.madde
    }
    guncelleme = {
        "$set": {
            "dosya_no": temiz_no,
            "madde": veri.madde,
            "subscription": veri.subscription,
            "is_admin": is_admin,
            "genel_bildirim": True,
            "onaylandi": False,
            "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
        }
    }
    db["pwa_abonelikleri"].update_one(filtre, guncelleme, upsert=True)

    ek_not = " (Admin yetkisi aktif)" if is_admin else ""
    return {
        "status": "ok",
        "mesaj": f"✅ {temiz_no} ({veri.madde}) takibe alındı! Karar çıktığında telefonunuza anında bildirim gelecek.{ek_not}"
    }


@app.post("/api/takip-listem")
async def takip_listem(veri: dict):
    """Kullanıcının kendi telefonundan takip ettiği aktif dosyaları listeler."""
    if db is None:
        return {"kayitlar": []}
    endpoint = veri.get("endpoint")
    if not endpoint:
        return {"kayitlar": []}

    kayitlar = list(db["pwa_abonelikleri"].find(
        {"subscription.endpoint": endpoint, "dosya_no": {"$exists": True, "$ne": ""}},
        {"_id": 0, "dosya_no": 1, "madde": 1, "onaylandi": 1, "onay_pdf": 1}
    ))
    return {"kayitlar": kayitlar}


@app.post("/api/abonelik-sil")
async def abonelik_sil(veri: AbonelikSilIstegi):
    if db is None:
        raise HTTPException(status_code=500, detail="Veritabanı yok.")
    db["pwa_abonelikleri"].delete_one({
        "subscription.endpoint": veri.endpoint,
        "dosya_no": veri.dosya_no,
        "madde": veri.madde
    })
    return {"status": "ok"}