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
    cihaz_turu: Optional[str] = "mobil"


class AbonelikSilIstegi(BaseModel):
    dosya_no: str
    endpoint: str


def _dosya_varyasyonlari(temiz_no: str) -> list:
    temiz_no = temiz_no.strip().upper()
    varyasyonlar = {temiz_no}

    m1 = re.match(r"^(\d+)\s*/\s*(\d{4})$", temiz_no)
    if m1:
        varyasyonlar.add(f"{m1.group(1)}/RD/{m1.group(2)}")

    m2 = re.match(r"^(\d+)\s*/\s*RD\s*/\s*(\d{4})$", temiz_no)
    if m2:
        varyasyonlar.add(f"{m2.group(1)}/{m2.group(2)}")

    return list(varyasyonlar)


def _dinamik_sorgu_olustur(koleksiyon_adi: str, aday_numaralar: list) -> dict:
    ornek = db[koleksiyon_adi].find_one({}, {"_id": 0})
    if not ornek:
        return {"dosya_no": {"$in": aday_numaralar}}

    alanlar = list(ornek.keys())
    return {"$or": [{alan: {"$in": aday_numaralar}} for alan in alanlar]}


def _alan_degeri_oku(kayit: dict, anahtar_kelimeler: list, haric_kelimeler: list = None) -> str:
    """
    Kayıt içerisindeki sütun isimlerine bakarak eşleşen ilk dolu değeri döndürür.
    """
    if not kayit:
        return ""
    haric_kelimeler = haric_kelimeler or []
    for k, v in kayit.items():
        k_low = str(k).lower()
        if any(h in k_low for h in haric_kelimeler):
            continue
        if any(a in k_low for a in anahtar_kelimeler):
            deger = str(v).strip()
            if deger and deger.lower() not in ["nan", "none", "null", ""]:
                return deger
    return ""


def _stadiu_detay_analiz_et(temiz_no: str) -> dict:
    """
    stadiu_dosar ve stadiu_dosar_eski tablolarından tüm dosya detaylarını ve Termen değişimini çıkarır.
    """
    sonuc = {
        "kayit_var": False,
        "resmi_no": temiz_no.upper(),
        "madde": "Otomatik",
        "basvuru_tarihi": "-",
        "guncel_termen": "-",
        "eski_termen": "",
        "termen_degisti": False,
        "solutie": "-",
        "kaynak_pdf": "-"
    }
    if db is None:
        return sonuc

    aday_numaralar = _dosya_varyasyonlari(temiz_no)
    koleksiyonlar = db.list_collection_names()

    guncel_kayit = None
    eski_kayit = None

    if "stadiu_dosar" in koleksiyonlar:
        guncel_kayit = db["stadiu_dosar"].find_one(
            _dinamik_sorgu_olustur("stadiu_dosar", aday_numaralar), {"_id": 0}
        )

    if "stadiu_dosar_eski" in koleksiyonlar:
        eski_kayit = db["stadiu_dosar_eski"].find_one(
            _dinamik_sorgu_olustur("stadiu_dosar_eski", aday_numaralar), {"_id": 0}
        )

    aktif_kayit = guncel_kayit or eski_kayit
    if not aktif_kayit:
        return sonuc

    sonuc["kayit_var"] = True

    # Tablodaki tam dosya numarasını bul (Örn: 23805/RD/2025)
    for k, v in aktif_kayit.items():
        v_str = str(v).strip().upper()
        if v_str in aday_numaralar:
            sonuc["resmi_no"] = v_str
            break

    # Madde tespiti
    tum_degerler = " ".join(str(v) for v in aktif_kayit.values()).lower()
    if "art-11" in tum_degerler or "articolul 11" in tum_degerler or "madde 11" in tum_degerler:
        sonuc["madde"] = "Madde 11"
    elif "art-10" in tum_degerler or "articolul 10" in tum_degerler or "madde 10" in tum_degerler:
        sonuc["madde"] = "Madde 10"

    # Başvuru Tarihi (Data Înregistrării / Başvuru / Data)
    basvuru = _alan_degeri_oku(
        aktif_kayit,
        ["basvuru", "başvuru", "inregistr", "înregistr", "data", "tarih"],
        haric_kelimeler=["termen", "solutie", "soluție", "update", "guncel"]
    )
    if basvuru:
        sonuc["basvuru_tarihi"] = basvuru

    # Termen
    guncel_termen = _alan_degeri_oku(guncel_kayit, ["termen"])
    eski_termen = _alan_degeri_oku(eski_kayit, ["termen"])

    if guncel_termen:
        sonuc["guncel_termen"] = guncel_termen
    elif eski_termen:
        sonuc["guncel_termen"] = eski_termen

    if eski_termen:
        sonuc["eski_termen"] = eski_termen

    if guncel_termen and eski_termen and guncel_termen != eski_termen and guncel_termen != "-":
        sonuc["termen_degisti"] = True

    # Solutie (Kurum Notu)
    solutie = _alan_degeri_oku(aktif_kayit, ["solutie", "soluție", "not", "durum", "stadiu"])
    if solutie:
        sonuc["solutie"] = solutie

    # Kaynak PDF
    kaynak = _alan_degeri_oku(aktif_kayit, ["kaynak", "pdf", "dosya_adi", "dosya adi", "source", "belge"])
    if not kaynak:
        # Sütun adı farklıysa içinde .pdf geçen değeri yakala
        for v in aktif_kayit.values():
            v_str = str(v).strip()
            if ".pdf" in v_str.lower():
                kaynak = v_str
                break
    if kaynak:
        sonuc["kaynak_pdf"] = kaynak

    return sonuc


def _dosya_maddesini_tespit_et(temiz_no: str) -> str:
    analiz = _stadiu_detay_analiz_et(temiz_no)
    if analiz["madde"] != "Otomatik":
        return analiz["madde"]

    if db is not None:
        aday_numaralar = _dosya_varyasyonlari(temiz_no)
        if db["kararlar_m11"].find_one(_dinamik_sorgu_olustur("kararlar_m11", aday_numaralar)):
            return "Madde 11"
        if db["kararlar_m10"].find_one(_dinamik_sorgu_olustur("kararlar_m10", aday_numaralar)):
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
    stadiu_detay = _stadiu_detay_analiz_et(temiz_no)

    bulunanlar = []
    for madde_adi, kol_adi in [("Madde 11", "kararlar_m11"), ("Madde 10", "kararlar_m10")]:
        sorgu_filtresi = _dinamik_sorgu_olustur(kol_adi, aday_numaralar)
        kayitlar = list(db[kol_adi].find(sorgu_filtresi, {"_id": 0}))
        for kayit in kayitlar:
            pdf_adi = "-"
            tarih = "-"
            pdf_url = ""
            for k, v in kayit.items():
                k_low = k.lower()
                v_str = str(v).strip()
                if not v_str:
                    continue
                if any(x in k_low for x in ["pdf", "ordin", "belge", "dosya_adi", "dosya adi", "kaynak"]):
                    if not v_str.startswith("http"):
                        pdf_adi = v_str
                if "tarih" in k_low or "date" in k_low:
                    tarih = v_str
                if v_str.startswith("http"):
                    pdf_url = v_str

            bulunanlar.append({
                "dosya_no": stadiu_detay["resmi_no"] if stadiu_detay["kayit_var"] else temiz_no.upper(),
                "madde": madde_adi,
                "pdf_adi": pdf_adi,
                "tarih": tarih,
                "pdf_url": pdf_url
            })

    if not bulunanlar:
        return {
            "bulundu": False,
            "detay": stadiu_detay,
            "mesaj": "Dosyanız henüz resmi Karar (Ordin) listelerinde yayımlanmamıştır."
        }

    ilk = bulunanlar[0]
    return {
        "bulundu": True,
        "dosya_no": ilk["dosya_no"],
        "madde": ilk["madde"],
        "pdf_adi": ilk["pdf_adi"],
        "tarih": ilk["tarih"],
        "pdf_url": ilk["pdf_url"],
        "detay": stadiu_detay,
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

    is_pc = (veri.cihaz_turu == "pc")
    cihaz_ek = "bilgisayarınıza" if is_pc else "telefonunuza"
    bu_cihaz = "bu bilgisayara" if is_pc else "bu telefona"

    is_admin = (veri.admin_kodu == ADMIN_SECRET) and (ADMIN_SECRET != "")

    if is_admin and not temiz_no:
        db["pwa_abonelikleri"].update_one(
            {"subscription.endpoint": endpoint, "is_admin": True},
            {"$set": {
                "subscription": veri.subscription,
                "is_admin": True,
                "genel_bildirim": True,
                "cihaz_turu": veri.cihaz_turu,
                "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
            }},
            upsert=True
        )
        return {"status": "ok", "mesaj": f"✅ Cihazınız Yönetici (Admin) olarak kaydedildi! Her akşam 20:00 raporu {bu_cihaz} gelecek."}

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
            "cihaz_turu": veri.cihaz_turu,
            "onaylandi": False,
            "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
        }
    }
    db["pwa_abonelikleri"].update_one(filtre, guncelleme, upsert=True)

    madde_bilgi = f" ({tespit_edilen_madde} olarak tespit edildi)" if tespit_edilen_madde != "Otomatik" else " (Madde 10 & 11 Otomatik Tarama)"
    ek_not = " [Admin Aktif]" if is_admin else ""
    return {
        "status": "ok",
        "mesaj": f"✅ {temiz_no}{madde_bilgi} takibe alındı! Karar çıktığında {cihaz_ek} anında bildirim gelecek.{ek_not}"
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