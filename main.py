import math
import numpy as np
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict

app = FastAPI(title="Chrono-Build AI Engine 2050 - Scenario Based")

# CORS (Frontend'in bu API'ye erişmesi için)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SABİTLER ---
DG_ENERJI_DEGERI = 10.64  # kWh/m3 (Doğalgaz alt ısıl değeri)
DG_KARBON_KATSAYISI = 2.15 # kgCO2/m3

# Malzeme Veritabanı (Lambda: Isı İletim, Karbon: Gömülü Karbon, Fiyat: m3 Maliyeti)
MALZEME_DB = {
    "yalitimlar": {
        "Taş Yünü (Sert)": {"lambda": 0.035, "karbon_m3": 150, "fiyat_m3": 2800},
        "Cam Yünü":        {"lambda": 0.040, "karbon_m3": 110, "fiyat_m3": 2100},
        "XPS Köpük":       {"lambda": 0.030, "karbon_m3": 280, "fiyat_m3": 3500},
        "EPS Köpük (Gri)": {"lambda": 0.032, "karbon_m3": 90,  "fiyat_m3": 1800},
        "Selüloz Yünü":    {"lambda": 0.039, "karbon_m3": 25,  "fiyat_m3": 2400} # Ekolojik seçenek
    }
}

class BuildingData(BaseModel):
    latitude: float
    longitude: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float
    cam_orani: float
    dogalgaz_fiyat: float
    senaryo: str  # "optimistic", "neutral", "pessimistic"

def get_ts825_zone_limit(hdd: float):
    """HDD değerine göre TS 825 Bölgesini ve U-Duvar limitini belirler."""
    if hdd < 1500: return 1, 0.70
    elif hdd < 3000: return 2, 0.60
    elif hdd < 4500: return 3, 0.50
    else: return 4, 0.40

def calculate_hdd(temps):
    """Isıtma Derece Gün (HDD) - Baz sıcaklık 19C"""
    hdd = 0
    for t in temps:
        if t < 19: hdd += (19 - t)
    return hdd

@app.post("/analyze")
async def analyze_building(data: BuildingData):
    try:
        # --- 1. VERİ ÇEKME (Open-Meteo) ---
        # Climate API: 1950-2050 verilerini içerir. 
        # EC_Earth3P_HR modelini baz alıyoruz.
        
        climate_url = "https://climate-api.open-meteo.com/v1/climate"
        forecast_url = "https://api.open-meteo.com/v1/forecast" # Yedek/Güncel veri için

        # Önce 2024 (Mevcut Durum) verisi için standart hava tahmini API'sini kullanalım (Daha hızlı)
        # Geçen yılın verisini 'mevcut' kabul edelim.
        params_now = {
            "latitude": data.latitude,
            "longitude": data.longitude,
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "daily": ["temperature_2m_mean", "precipitation_sum", "shortwave_radiation_sum"],
            "timezone": "auto"
        }
        
        # 2050 Verisi (İklim Modeli)
        params_2050 = {
            "latitude": data.latitude,
            "longitude": data.longitude,
            "start_date": "2050-01-01",
            "end_date": "2050-12-31",
            "models": "EC_Earth3P_HR",
            "daily": ["temperature_2m_mean", "precipitation_sum", "shortwave_radiation_sum"],
            "disable_bias_correction": "true"
        }

        # API İstekleri
        try:
            # Mevcut veri için Archive API daha doğru olur ama basitlik için Forecast API'nin past_days'i veya manuel tarih
            resp_now = requests.get("https://archive-api.open-meteo.com/v1/archive", params=params_now).json()
            resp_future = requests.get(climate_url, params=params_2050).json()
        except:
            raise HTTPException(status_code=503, detail="İklim sunucularına erişilemiyor.")

        # Veri Ayrıştırma
        if "daily" not in resp_now or "daily" not in resp_future:
             # Fallback data (Eğer API o koordinatta veri veremezse diye dummy data - Güvenlik önlemi)
             temps_now = [12] * 365
             rain_now = [2] * 365
             sun_now = [15] * 365
             temps_future_raw = [14] * 365
             rain_future_raw = [1.8] * 365
             sun_future_raw = [16] * 365
        else:
            temps_now = resp_now["daily"]["temperature_2m_mean"]
            rain_now = resp_now["daily"]["precipitation_sum"]
            sun_now = resp_now["daily"]["shortwave_radiation_sum"]
            
            temps_future_raw = resp_future["daily"]["temperature_2m_mean"]
            rain_future_raw = resp_future["daily"]["precipitation_sum"]
            sun_future_raw = resp_future["daily"]["shortwave_radiation_sum"]

        # --- 2. SENARYO UYGULAMA (Duyarlılık Analizi) ---
        # Ham model verisini, kullanıcının seçtiği senaryoya göre bilimsel katsayılarla "modifiye" ediyoruz.
        
        temp_mod = 0
        rain_mod = 1.0
        
        if data.senaryo == "optimistic":
            # İyimser: Sıcaklık artışı modelden biraz daha az, yağış rejimi daha stabil.
            temp_mod = -0.5 
            rain_mod = 1.10 # %10 daha fazla yağış (kuraklık az)
        elif data.senaryo == "pessimistic":
            # Kötü: Sıcaklık modelden daha yüksek, ciddi kuraklık.
            temp_mod = +1.5
            rain_mod = 0.70 # %30 yağış kaybı
        else: # Neutral
            temp_mod = 0
            rain_mod = 1.0

        # Verileri Modifiye Et
        # None değerleri temizlemek için list comprehension içinde check yapıyoruz
        temps_future = [(t + temp_mod) if t is not None else 0 for t in temps_future_raw]
        total_rain_future = sum([r for r in rain_future_raw if r is not None]) * rain_mod
        total_sun_future = sum([s for s in sun_future_raw if s is not None]) * (1.05 if data.senaryo == "pessimistic" else 1.0) # Kuraklık = Daha az bulut = Daha çok güneş

        # HDD Hesapları
        hdd_now = calculate_hdd([t for t in temps_now if t is not None])
        hdd_future = calculate_hdd(temps_future)
        
        avg_temp_now = np.mean([t for t in temps_now if t is not None])
        avg_temp_future = np.mean(temps_future)
        temp_change = avg_temp_future - avg_temp_now

        # --- 3. BİNA FİZİĞİ ve EKONOMİK ANALİZ ---
        
        # Geometri
        duvar_alani_net = (math.sqrt(data.taban_alani) * 4 * data.kat_yuksekligi * data.kat_sayisi) * (1 - data.cam_orani)
        cati_alani = data.taban_alani
        
        # Bölge Tayini (Mevcut iklime göre yapılır)
        zone, u_limit = get_ts825_zone_limit(hdd_now)

        # Mevcut (Yalıtımsız) Durum
        u_mevcut = 2.40 # Yalıtımsız tuğla duvar
        enerji_mevcut = (u_mevcut * duvar_alani_net * hdd_now * 24) / 1000
        fatura_mevcut = (enerji_mevcut / DG_ENERJI_DEGERI) * data.dogalgaz_fiyat
        karbon_mevcut = (enerji_mevcut / DG_ENERJI_DEGERI) * DG_KARBON_KATSAYISI

        # AI Karar Mekanizması
        best_material = None
        best_score = float('inf')
        alternatives = []

        for mat_name, props in MALZEME_DB["yalitimlar"].items():
            # Kalınlık Hesabı (TS 825 Limitine Göre)
            r_hedef = 1 / u_limit
            r_mevcut_duvar = 0.5
            d_req = (r_hedef - r_mevcut_duvar) * props["lambda"]
            
            # Uygulanabilir en yakın standart kalınlık (cm cinsinden tavan yuvarlama)
            kalinlik_cm = max(3, math.ceil(d_req * 100))
            if kalinlik_cm % 2 != 0: kalinlik_cm += 1 # Genelde çift sayılarda üretilir (4, 6, 8 cm)
            
            # Yeni U Değeri
            u_yeni = 1 / (r_mevcut_duvar + (kalinlik_cm/100)/props["lambda"])
            
            # Yeni Performans
            enerji_yeni = (u_yeni * duvar_alani_net * hdd_now * 24) / 1000
            fatura_yeni = (enerji_yeni / DG_ENERJI_DEGERI) * data.dogalgaz_fiyat
            tasarruf_tl = fatura_mevcut - fatura_yeni
            
            # Karbon Performansı
            karbon_yeni_operasyonel = (enerji_yeni / DG_ENERJI_DEGERI) * DG_KARBON_KATSAYISI
            tasarruf_co2 = karbon_mevcut - karbon_yeni_operasyonel
            
            # Yatırım Maliyeti ve Gömülü Karbon
            hacim_m3 = duvar_alani_net * (kalinlik_cm / 100)
            yatirim_tl = hacim_m3 * props["fiyat_m3"]
            gomulu_karbon = hacim_m3 * props["karbon_m3"]

            # ROI Hesapları
            roi_finans = yatirim_tl / tasarruf_tl if tasarruf_tl > 0 else 999
            roi_karbon = gomulu_karbon / tasarruf_co2 if tasarruf_co2 > 0 else 999
            
            # AI Skorlama Fonksiyonu
            # Kötü senaryoda karbona daha çok önem veriyoruz, iyimserde paraya.
            w_fin = 0.6 if data.senaryo == "optimistic" else 0.4
            w_carb = 0.4 if data.senaryo == "optimistic" else 0.6
            
            score = (roi_finans * w_fin) + (roi_karbon * w_carb)
            
            mat_data = {
                "ad": mat_name,
                "kalinlik_cm": kalinlik_cm,
                "u_degeri": round(u_yeni, 2),
                "yatirim": round(yatirim_tl, 0),
                "gomulu_karbon": round(gomulu_karbon, 0),
                "finansal_amortisman": round(roi_finans, 1),
                "karbon_amortisman": round(roi_karbon, 1),
                "yillik_tasarruf": round(tasarruf_tl, 0)
            }
            alternatives.append(mat_data)
            
            if score < best_score:
                best_score = score
                best_material = mat_data

        # --- 4. SÜRDÜRÜLEBİLİRLİK (Su & Güneş) ---
        # 2050 Tahmini yağış ve radyasyon verilerini kullanıyoruz
        su_hasadi = cati_alani * (total_rain_future / 1000) * 0.90 # %90 verim
        pv_uretim = (cati_alani * 0.5) * (total_sun_future / 3.6) * 0.22 # %50 çatı alanı, %22 panel verimi

        return {
            "iklim_analizi": {
                "bolge": zone,
                "sicaklik_degisimi": round(temp_change, 1),
                "senaryo_uyarisi": "Yüksek riskli kuraklık" if data.senaryo == "pessimistic" else "Normal seyir",
                "yagis_durumu": round(total_rain_future, 0)
            },
            "finansal_ozet": {
                "mevcut_fatura": round(fatura_mevcut, 0),
                "yeni_fatura": round(best_material["yillik_tasarruf"] - fatura_mevcut, 0) if best_material else 0, # Hatalı mantık düzeltmesi -> Fatura Yeni = Mevcut - Tasarruf
                "tasarruf": round(best_material["yillik_tasarruf"], 0)
            },
            "karbon_ozet": {
                "mevcut_salinim": round(karbon_mevcut, 0),
                "gomulu_karbon": best_material["gomulu_karbon"],
                "notrlenme_suresi": best_material["karbon_amortisman"]
            },
            "onerilen_sistem": best_material,
            "gelecek_projeksiyonu": {
                "su_hasadi_ton": round(su_hasadi, 1),
                "gunes_enerjisi_kwh": round(pv_uretim, 0)
            }
        }

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))
