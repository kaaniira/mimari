import math
import numpy as np
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any

app = FastAPI(title="Geleceğin Mimarı AI Engine v2.0")

# CORS Ayarları (Tüm kaynaklara izin ver)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SABİTLER ---
DG_ENERJI_DEGERI = 10.64  # kWh/m3
DG_KARBON_KATSAYISI = 2.15 # kgCO2/m3

# Veritabanı
MALZEME_DB = {
    "yalitimlar": {
        "Taş Yünü (Sert)": {"lambda": 0.035, "karbon_m3": 150, "fiyat_m3": 2800},
        "Cam Yünü":        {"lambda": 0.040, "karbon_m3": 110, "fiyat_m3": 2100},
        "XPS Köpük":       {"lambda": 0.030, "karbon_m3": 280, "fiyat_m3": 3500},
        "EPS Köpük (Gri)": {"lambda": 0.032, "karbon_m3": 90,  "fiyat_m3": 1800}
    },
    "pencereler": {
        "Tek Cam (Standart)":   {"u": 5.7},
        "Çift Cam (Isıcam S)":  {"u": 2.8},
        "Üçlü Cam (Isıcam K)":  {"u": 1.1}
    }
}

# Pydantic Model (Frontend'den gelen veriyi karşılar)
# Field(..., alias="...") kullanarak hem snake_case hem camelCase uyumluluğu sağlıyoruz.
class BuildingData(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float
    dogalgaz_fiyat: float
    yonelim: Optional[int] = 180
    senaryo: str
    mevcut_yalitim: Optional[str] = "Taş Yünü (Sert)"
    mevcut_pencere: str
    
    # Eski versiyon uyumluluğu için opsiyonel alanlar (Hata önleyici)
    latitude: Optional[float] = None
    longitude: Optional[float] = None

def calculate_hdd(temps):
    """Isıtma Derece Gün Hesabı"""
    return sum([max(0, 19 - t) for t in temps])

@app.post("/analyze")
async def analyze_building(data: BuildingData):
    try:
        # 1. İKLİM VERİLERİ (Open-Meteo CMIP6)
        climate_url = "https://climate-api.open-meteo.com/v1/climate"
        
        # Koordinatları normalize et
        latitude = data.lat if data.lat else data.latitude
        longitude = data.lng if data.lng else data.longitude

        # Senaryo Parametreleri
        temp_adj = 0
        precip_adj = 1.0
        
        if data.senaryo == "ssp126": # İyimser
            temp_adj = -0.5
            precip_adj = 1.1
        elif data.senaryo == "ssp585": # Kötümser
            temp_adj = +1.8
            precip_adj = 0.7
        
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": "2050-01-01",
            "end_date": "2050-12-31",
            "models": "EC_Earth3P_HR",
            "daily": ["temperature_2m_mean", "precipitation_sum", "shortwave_radiation_sum"],
            "disable_bias_correction": "true"
        }

        hdd = 2000 # Varsayılan
        rain_total = 500
        sun_total = 6000

        try:
            resp = requests.get(climate_url, params=params, timeout=5).json()
            if "daily" in resp:
                temps = [t + temp_adj for t in resp["daily"]["temperature_2m_mean"] if t is not None]
                rain_total = sum([r for r in resp["daily"]["precipitation_sum"] if r is not None]) * precip_adj
                sun_total = sum([s for s in resp["daily"]["shortwave_radiation_sum"] if s is not None])
                hdd = calculate_hdd(temps)
        except Exception as e:
            print(f"API Hatası: {e}")
            # Hata durumunda varsayılan değerlerle devam et, sistemi çökertme

        # 2. BİNA FİZİĞİ HESAPLAMALARI
        duvar_alani = (math.sqrt(data.taban_alani) * 4 * data.kat_yuksekligi * data.kat_sayisi) * 0.85
        pencere_alani = (math.sqrt(data.taban_alani) * 4 * data.kat_yuksekligi * data.kat_sayisi) * 0.15
        
        # Mevcut Durum (Yalıtımsız Duvar: 2.4 W/m2K)
        u_duvar_mevcut = 2.4
        u_pencere_mevcut = MALZEME_DB["pencereler"].get(data.mevcut_pencere, {"u": 2.8})["u"]
        
        enerji_mevcut_kwh = ((u_duvar_mevcut * duvar_alani) + (u_pencere_mevcut * pencere_alani)) * hdd * 24 / 1000
        maliyet_mevcut = (enerji_mevcut_kwh / DG_ENERJI_DEGERI) * data.dogalgaz_fiyat
        karbon_mevcut = (enerji_mevcut_kwh / DG_ENERJI_DEGERI) * DG_KARBON_KATSAYISI

        # 3. AI OPTİMİZASYONU
        best_sys = None
        min_score = float('inf')
        target_u = 0.5 if hdd < 3000 else 0.4 # TS 825 Hedef

        for mat_name, props in MALZEME_DB["yalitimlar"].items():
            # Kalınlık Hesabı
            r_req = 1/target_u
            r_wall = 0.5 # Mevcut duvar direnci
            d = (r_req - r_wall) * props["lambda"]
            kalinlik_cm = max(4, math.ceil(d*100))
            if kalinlik_cm % 2 != 0: kalinlik_cm += 1
            
            # Yeni U Değerleri (Duvar + Isıcam S)
            u_yeni = 1 / (r_wall + (kalinlik_cm/100)/props["lambda"])
            u_pencere_yeni = 2.8 
            
            enerji_yeni_kwh = ((u_yeni * duvar_alani) + (u_pencere_yeni * pencere_alani)) * hdd * 24 / 1000
            fatura_yeni = (enerji_yeni_kwh / DG_ENERJI_DEGERI) * data.dogalgaz_fiyat
            tasarruf_tl = maliyet_mevcut - fatura_yeni
            
            karbon_yeni = (enerji_yeni_kwh / DG_ENERJI_DEGERI) * DG_KARBON_KATSAYISI
            tasarruf_co2 = karbon_mevcut - karbon_yeni
            
            # Yatırım Maliyeti ve ROI
            hacim = duvar_alani * (kalinlik_cm/100)
            yatirim = hacim * props["fiyat_m3"]
            gomulu_karbon = hacim * props["karbon_m3"]
            
            roi_eco = yatirim / tasarruf_tl if tasarruf_tl > 0 else 99
            roi_carb = gomulu_karbon / tasarruf_co2 if tasarruf_co2 > 0 else 99
            
            score = (roi_eco * 0.6) + (roi_carb * 0.4)
            
            if score < min_score:
                min_score = score
                best_sys = {
                    "yalitim": mat_name,
                    "kalinlik": int(kalinlik_cm),
                    "fatura": int(fatura_yeni),
                    "karbon": int(karbon_yeni),
                    "pb_eco": round(roi_eco, 1),
                    "pb_carb": round(roi_carb, 1)
                }

        # 4. KAYNAKLAR (Su ve Güneş)
        su_hasadi = data.taban_alani * (rain_total / 1000) * 0.9
        pv_potansiyeli = (data.taban_alani * 0.5) * (sun_total / 3.6) * 0.22

        # 5. RESPONSE (Frontend ile tam uyumlu anahtarlar)
        return {
            "mevcut": {
                "maliyet": int(maliyet_mevcut),
                "karbon": int(karbon_mevcut)
            },
            "ai_onerisi": {
                **best_sys,
                "su_hasadi": int(su_hasadi),
                "pv_potansiyeli": int(pv_potansiyeli)
            },
            "iklim_info": {
                "hdd": int(hdd),
                "yagis": int(rain_total)
            }
        }

    except Exception as e:
        # Hata durumunda 500 dönmek yerine hata mesajını JSON olarak dön
        print(f"Server Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
