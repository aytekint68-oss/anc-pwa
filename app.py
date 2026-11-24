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
    madde_secimi: Optional[str] = ""


class AbonelikSilIstegi(BaseModel):
    dosya_no: str
    endpoint: str


def _dosya_formati_dogrula(temiz_no: str) -> int:
    """
    Dosya numarasinin 'dosya no/yıl' kuralina uygunlugunu denetler ve yil bilgisini dondurur:
    - Dosya no 1-5 basamakli olmali ve 0 olamaz
    - Arada '/' karakteri bulunmali
    - Yil 4 basamakli olmali ve 2010 ile guncel yil arasinda olmali
    """
    guncel_yil = datetime.now(TSI).year
    eslesme = re.match(r"^(\d{1,5})/(\d{4})$", temiz_no)
    if not eslesme:
        raise HTTPException(
            status_code=400,
            detail=f"Geçersiz dosya numarası formatı! Doğru format: dosya no/yıl (Örn: 12345/{guncel_yil}). Dosya no 1-5 basamaklı, yıl 4 basamaklı olmalıdır."
        )

    dosya_sayi = int(eslesme.group(1))
    yil_sayi = int(eslesme.group(2))

    if dosya_sayi <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya numarası 0 olamaz! Doğru format: dosya no/yıl (Örn: 12345/{guncel_yil})."
        )

    if yil_sayi < 2010 or yil_sayi > guncel_yil:
        raise HTTPException(
            status_code=400,
            detail=f"Yıl bilgisi 2010 ile {guncel_yil} arasında olmalıdır! Doğru format: dosya no/yıl (Örn: 12345/{guncel_yil})."
        )

    return yil_sayi


def _dosya_varyasyonlari(temiz_no: str) -> list:
    temiz_no = temiz_no.strip().upper()
    varyasyonlar = {temiz_no}

    m1 = re.match(r"^(\d+)\s*/\s*(\d{4})$", temiz_no)
    if m1:
        varyasyonlar.add(f"{int(m1.group(1))}/{m1.group(2)}")
        varyasyonlar.add(f"{int(m1.group(1))}/RD/{m1.group(2)}")

    m2 = re.match(r"^(\d+)\s*/\s*RD\s*/\s*(\d{4})$", temiz_no)
    if m2:
        varyasyonlar.add(f"{int(m2.group(1))}/{m2.group(2)}")
        varyasyonlar.add(f"{int(m2.group(1))}/RD/{m2.group(2)}")

    return list(varyasyonlar)


def _dinamik_sorgu_olustur(koleksiyon_adi: str, aday_numaralar: list) -> dict:
    ornek = db[koleksiyon_adi].find_one({}, {"_id": 0})
    if not ornek:
        return {"dosya_no": {"$in": aday_numaralar}}

    alanlar = list(ornek.keys())
    return {"$or": [{alan: {"$in": aday_numaralar}} for alan in alanlar]}


def _alan_degeri_oku(kayit: dict, anahtar_kelimeler: list, haric_kelimeler: list = None) -> str:
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

    for k, v in aktif_kayit.items():
        v_str = str(v).strip().upper()
        if v_str in aday_numaralar:
            sonuc["resmi_no"] = v_str
            break

    tum_degerler = " ".join(str(v) for v in aktif_kayit.values()).lower()
    if "art-11" in tum_degerler or "articolul 11" in tum_degerler or "madde 11" in tum_degerler:
        sonuc["madde"] = "Madde 11"
    elif "art-10" in tum_degerler or "articolul 10" in tum_degerler or "madde 10" in tum_degerler:
        sonuc["madde"] = "Madde 10"

    basvuru = _alan_degeri_oku(
        aktif_kayit,
        ["basvuru", "başvuru", "inregistr", "înregistr", "data", "tarih"],
        haric_kelimeler=["termen", "solutie", "soluție", "update", "guncel"]
    )
    if basvuru:
        sonuc["basvuru_tarihi"] = basvuru

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

    solutie = _alan_degeri_oku(aktif_kayit, ["solutie", "soluție", "not", "durum", "stadiu"])
    if solutie:
        sonuc["solutie"] = solutie

    kaynak = _alan_degeri_oku(aktif_kayit, ["kaynak", "pdf", "dosya_adi", "dosya adi", "source", "belge"])
    if not kaynak:
        for v in aktif_kayit.values():
            v_str = str(v).strip()
            if ".pdf" in v_str.lower():
                kaynak = v_str
                break
    if kaynak:
        sonuc["kaynak_pdf"] = kaynak

    return sonuc


def _karar_maddesini_bul(temiz_no: str) -> str:
    if db is None:
        return "Otomatik"
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

    temiz_no = dosya_no.strip().upper()
    if not temiz_no:
        raise HTTPException(status_code=400, detail="Lütfen geçerli bir dosya numarası girin (dosya no/yıl).")

    yil_sayi = _dosya_formati_dogrula(temiz_no)
    guncel_yil = datetime.now(TSI).year

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
                "dosya_no": stadiu_detay["resmi_no"] if stadiu_detay["kayit_var"] else temiz_no,
                "madde": madde_adi,
                "pdf_adi": pdf_adi,
                "tarih": tarih,
                "pdf_url": pdf_url
            })

    if not bulunanlar:
        gecmis_yil_hatali = (yil_sayi < guncel_yil and not stadiu_detay["kayit_var"])
        return {
            "bulundu": False,
            "gecmis_yil_hatali": gecmis_yil_hatali,
            "detay": stadiu_detay,
            "mesaj": "Dosya numaranız veya yılı hatalıdır, lütfen kontrol ediniz." if gecmis_yil_hatali else "Dosyanız henüz resmi Karar (Ordin) listelerinde yayımlanmamıştır."
        }

    ilk = bulunanlar[0]
    return {
        "bulundu": True,
        "gecmis_yil_hatali": False,
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
    bu_cihazda = "bu bilgisayarda" if is_pc else "bu telefonda"

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
        return {"status": "ok", "mesaj": f"✅ Cihazınız Yönetici (Admin) olarak kaydedildi! Her akşam 19:00 raporu {bu_cihaz} gelecek."}

    if not temiz_no:
        raise HTTPException(status_code=400, detail="Lütfen takip edilecek dosya numarasını girin (dosya no/yıl).")

    yil_sayi = _dosya_formati_dogrula(temiz_no)
    guncel_yil = datetime.now(TSI).year

    aday_numaralar = _dosya_varyasyonlari(temiz_no)

    # Mükerrer takip kontrolü (12345/2025 veya 12345/RD/2025 varyasyonları dahil)
    mevcut_kayit = db["pwa_abonelikleri"].find_one({
        "subscription.endpoint": endpoint,
        "$or": [
            {"dosya_no": {"$in": aday_numaralar}},
            {"dosya_varyasyonlari": {"$in": aday_numaralar}}
        ]
    })

    if mevcut_kayit:
        if is_admin and not mevcut_kayit.get("is_admin"):
            db["pwa_abonelikleri"].update_one(
                {"_id": mevcut_kayit["_id"]},
                {"$set": {"is_admin": True, "subscription": veri.subscription}}
            )
            return {
                "status": "ok",
                "zaten_kayitli": True,
                "mesaj": f"ℹ️ {mevcut_kayit.get('dosya_no', temiz_no)} numaralı dosya zaten {bu_cihazda} takip listesinde kayıtlı! [Admin Yetkisi Eklendi]"
            }

        return {
            "status": "ok",
            "zaten_kayitli": True,
            "mesaj": f"ℹ️ {mevcut_kayit.get('dosya_no', temiz_no)} numaralı dosya zaten {bu_cihazda} bildirim takibinde! Karar çıktığında {cihaz_ek} anında bildirim gelecek."
        }

    stadiu_analiz = _stadiu_detay_analiz_et(temiz_no)
    tespit_edilen_madde = stadiu_analiz["madde"]
    if tespit_edilen_madde == "Otomatik":
        tespit_edilen_madde = _karar_maddesini_bul(temiz_no)

    # Güncel yıl hariç geçmiş yıllara ait dosya Stadiu Dosar'da (ve kararlarda) bulunmuyorsa uyar
    if yil_sayi < guncel_yil and not stadiu_analiz["kayit_var"] and tespit_edilen_madde == "Otomatik":
        raise HTTPException(
            status_code=400,
            detail="Dosya numaranız veya yılı hatalıdır, lütfen kontrol ediniz."
        )

    kullanici_secimi = (veri.madde_secimi or "").strip()

    # Güncel yıla ait olup sistemde henüz kaydı yoksa ve kullanıcı henüz seçim yapmadıysa madde sor
    if tespit_edilen_madde == "Otomatik" and kullanici_secimi not in ["Madde 10", "Madde 11", "Otomatik"]:
        return {
            "status": "madde_sor",
            "dosya_no": temiz_no
        }

    if tespit_edilen_madde != "Otomatik":
        son_madde = tespit_edilen_madde
        madde_bilgi = f" ({son_madde} olarak tespit edildi)"
    else:
        son_madde = kullanici_secimi
        if son_madde in ["Madde 10", "Madde 11"]:
            madde_bilgi = f" ({son_madde} olarak seçildi)"
        else:
            madde_bilgi = " (Madde 10 & 11 Otomatik Tarama)"

    yeni_belge = {
        "dosya_no": temiz_no,
        "dosya_varyasyonlari": aday_numaralar,
        "madde": son_madde,
        "subscription": veri.subscription,
        "is_admin": is_admin,
        "genel_bildirim": True,
        "cihaz_turu": veri.cihaz_turu,
        "onaylandi": False,
        "kayit_tarihi": datetime.now(TSI).strftime("%d.%m.%Y %H:%M")
    }
    db["pwa_abonelikleri"].insert_one(yeni_belge)

    ek_not = " [Admin Aktif]" if is_admin else ""
    return {
        "status": "ok",
        "zaten_kayitli": False,
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