import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient

app = FastAPI(title="ANC Dosya Takip PWA")

MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

TSI = timezone(timedelta(hours=3))

client = MongoClient(MONGO_URI) if MONGO_URI else None
db = client[DB_NAME] if (client and DB_NAME) else None

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


class AbonelikIstegi(BaseModel):
    dosya_no: str
    subscription: dict
    admin_kodu: Optional[str] = ""


class AbonelikSilIstegi(BaseModel):
    dosya_no: str
    endpoint: str


def _dosya_varyasyonlari(temiz_no: str) -> list:
    """
    Kullanıcı '11470/2026' yazsa da '11470/RD/2026' yazsa da her iki formatı üretir.
    """
    temiz_no = temiz_no.strip().upper()
    varyasyonlar = {temiz_no}

    # Örn: 11470/2026 -> 11470/RD/2026
    m1 = re.match(r"^(\d+)\s*/\s*(\d{4})$", temiz_no)
    if m1:
        varyasyonlar.add(f"{m1.group(1)}/RD/{m1.group(2)}")

    # Örn: 11470/RD/2026 -> 11470/2026
    m2 = re.match(r"^(\d+)\s*/\s*RD\s*/\s*(\d{4})$", temiz_no)
    if m2:
        varyasyonlar.add(f"{m2.group(1)}/{m2.group(2)}")

    return list(varyasyonlar)


def _dosya_maddesini_tespit_et(temiz_no: str) -> str:
    """
    stadiu_dosar ve karar tablolarına bakarak dosyanın Madde 10 mu Madde 11 mi olduğunu kesin tespit eder.
    """
    if db is None:
        return "Otomatik"

    aday_numaralar = _dosya_varyasyonlari(temiz_no)

    # 1. Önce 518K kayıtlı stadiu_dosar koleksiyonunda ara
    if "stadiu_dosar" in db.list_collection_names():
        # İlk kayıttan alan adlarını dinamik oku (dosya no ve pdf sütunu)
        ornek = db["stadiu_dosar"].find_one({}, {"_id": 0})
        if ornek:
            alanlar = list(ornek.keys())
            sorgu_or = [{alan: {"$in": aday_numaralar}} for alan in alanlar]
            kayit = db["stadiu_dosar"].find_one({"$or": sorgu_or}, {"_id": 0})
            if kayit:
                tum_degerler = " ".join(str(v) for v in kayit.values()).lower()
                if "art-11" in tum_degerler or "articolul 11" in tum_degerler or "madde 11" in tum_degerler:
                    return "Madde 11"
                if "art-10" in tum_degerler or "articolul 10" in tum_degerler or "madde 10" in tum_degerler:
                    return "Madde 10"

    # 2. Onaylanan karar tablolarında zaten varsa oradan tespit et
    karar_sorgu = {
        "$or": [
            {"dosya_no": {"$in": aday_numaralar}},
            {"Dosya_No": {"$in": aday_numaralar}},
            {"dosya": {"$in": aday_numaralar}}
        ]
    }
    if db["kararlar_m11"].find_one(karar_sorgu):
        return "Madde 11"
    if db["kararlar_m10"].find_one(karar_sorgu):
        return "Madde 10"

    return "Otomatik"


@app.get("/")
async def ana_sayfa():
    return FileResponse("static/index.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/ping")
async def ping():
    return {"status": "ok", "time": datetime.now(TSI).strftime("%H:%M:%S")}


@app.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.get("/api/sorgula")
async def dosya_sorgula(dosya_no: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Veritabanı bağlantısı yapılandırılmamış.")

    temiz_no = dosya_no.strip()
    if not temiz_no:
        raise HTTPException(status_code=400, detail="Lütfen geçerli bir dosya numarası girin.")

    aday_numaralar = _dosya_varyasyonlari(temiz_no)
    sorgu_filtresi = {
        "$or": [
            {"dosya_no": {"$in": aday_numaralar}},
            {"Dosya_No": {"$in": aday_numaralar}},
            {"dosya": {"$in": aday_numaralar}}
        ]
    }

    bulunanlar = []
    for madde_adi, kol_adi in [("Madde 11", "kararlar_m11"), ("Madde 10", "kararlar_m10")]:
        kayitlar = list(db[kol_adi].find(sorgu_filtresi, {"_id": 0}))
        for kayit in kayitlar:
            bulunanlar.append({
                "dosya_no": temiz_no,
                "madde": madde_adi,
                "pdf_adi": kayit.get("pdf_adi") or kayit.get("dosya_adi") or kayit.get("Ordin") or "Karar PDF",
                "tarih": kayit.get("tarih") or kayit.get("Tarih") or kayit.get("karar_tarihi") or "-",
                "pdf_url": kayit.get("pdf_url") or kayit.get("link") or kayit.get("url") or ""
            })

    if not bulunanlar:
        tespit_madde = _dosya_maddesini_tespit_et(temiz_no)
        madde_notu = f" ({tespit_madde} kapsamında işlemde)" if tespit_madde != "Otomatik" else ""
        return {
            "bulundu": False,
            "mesaj": f"{temiz_no}{madde_notu} numaralı dosya henüz onaylanan karar listelerinde yer almıyor."
        }

    ilk = bulunanlar[0]
    return {
        "bulundu": True,
        "dosya_no": ilk["dosya_no"],
        "madde": ilk["madde"],
        "pdf_adi": ilk["pdf_adi"],
        "tarih": ilk["tarih"],
        "pdf_url": ilk["pdf_url"],
        "tum_sonuclar": bulunanlar
    }


@app.post("/api/abone-ol")
async def abone_ol(veri: AbonelikIstegi):
    if db is None:
        raise HTTPException(status_code=500, detail="Veritabanı bağlantısı yok.")

    temiz_no = veri.dosya_no.strip().upper()
    endpoint = veri.subscription.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Geçersiz bildirim aboneliği.")

    is_admin = (veri.admin_kodu == ADMIN_SECRET) and (ADMIN_SECRET != "")

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

    tespit_edilen_madde = _dosya_maddesini_tespit_et(temiz_no)

    filtre = {
        "subscription.endpoint": endpoint,
        "dosya_no": temiz_no
    }
    guncelleme = {
        "$set": {
            "dosya_no": temiz_no,
            "dosya_varyasyonlari": _dosya_varyasyonlari(temiz_no),
            "madde": tespit_edilen_madde,
            "subscription": veri.subscription,
            "is_admin": is_admin,
            "genel_bildirim": True,
            "onaylandi": False,
            "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
        }
    }
    db["pwa_abonelikleri"].update_one(filtre, guncelleme, upsert=True)

    madde_bilgi = f" ({tespit_edilen_madde} olarak tespit edildi)" if tespit_edilen_madde != "Otomatik" else " (Madde 10 & 11 Otomatik Tarama)"
    ek_not = " [Admin Aktif]" if is_admin else ""
    return {
        "status": "ok",
        "mesaj": f"✅ {temiz_no}{madde_bilgi} takibe alındı! Karar çıktığında telefonunuza anında bildirim gelecek.{ek_not}"
    }


@app.post("/api/takip-listem")
async def takip_listem(veri: dict):
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
        "dosya_no": veri.dosya_no
    })
    return {"status": "ok"}