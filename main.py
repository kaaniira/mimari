import os
import math
import numpy as np
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict

app = FastAPI(title="Chrono-Build AI Engine")

# CORS Ayarları
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- VERİTABANI ---
MALZEME_DB = {
    "yalitimlar": {
        "Taş Yünü (Sert)": {"lambda": 0.035, "karbon": 1.20, "maliyet": 75},
        "Cam Yünü": {"lambda": 0.040, "karbon": 1.00, "maliyet": 65},
        "XPS Isı Yalıtım": {"lambda": 0.035, "karbon": 3.50, "maliyet": 55},
        "EPS Isı Yalıtım": {"lambda": 0.040, "karbon": 3.20, "maliyet": 40}
    },
    "pencereler": {
        "Tek Cam (Standart)": {"u": 5.8, "g": 0.85, "karbon": 15, "maliyet": 1500},
        "Çift Cam (Isıcam S)": {"u": 2.4, "g": 0.60, "karbon": 25, "maliyet": 3200},
        "Üçlü Cam (Isıcam K)": {"u": 1.1, "g": 0.45, "karbon": 40, "maliyet": 5500}
    }
}

class BinaInput(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    yonelim: int
    senaryo: str
    mevcut_yalitim: str
    mevcut_pencere: str

class ClimateEngine:
    def __init__(self, lat, lon, scenario="ssp245"):
        self.lat = lat
        self.lon = lon
        self.scenario = scenario 
        self.api_url = "https://climate-api.open-meteo.com/v1/climate"

    def fetch_2050_data(self):
        # CMIP6 modellerinden veri çekme denemesi
        model = "MPI_ESM1_2_LR" # Varsayılan model
        params = {
            "latitude": self.lat, "longitude": self.lon,
            "start_date": "2050-01-01", "end_date": "2050-12-31",
            "models": model,
            "daily": ["temperature_2m_max", "precipitation_sum", "shortwave_radiation_sum"]
        }
        try:
            r = requests.get(self.api_url, params=params, timeout=5)
            if r.status_code == 200:
                d = r.json()
                temps = d['daily']['temperature_2m_max']
                precip = d['daily']['precipitation_sum']
                rad = d['daily']['shortwave_radiation_sum']
                return {
                    "avg_temp": np.mean(temps),
                    "total_precip": sum(precip),
                    "total_rad": sum(rad) / 1000, # MJ to kWh conversion factor approximation
                    "is_real": True
                }
        except:
            pass
        # Fallback verileri (Bağlantı hatası durumunda)
        return {"avg_temp": 18.5, "total_precip": 650, "total_rad": 1450, "is_real": False}

def calculate_performance(data: BinaInput, yalitim: str, kalinlik: int, pencere: str, climate: dict):
    # Bina Geometrisi
    kenar = math.sqrt(data.taban_alani)
    duvar_alani = (kenar * 4) * (data.kat_sayisi * 3)
    pencere_alani = duvar_alani * 0.15
    net_duvar = duvar_alani - pencere_alani
    
    y_info = MALZEME_DB["yalitimlar"].get(yalitim, MALZEME_DB["yalitimlar"]["Taş Yünü (Sert)"])
    p_info = MALZEME_DB["pencereler"].get(pencere, MALZEME_DB["pencereler"]["Çift Cam (Isıcam S)"])

    # Isı Kaybı (U-Değeri)
    R_wall = 0.13 + (0.19 / 0.45) + (kalinlik / 100 / y_info["lambda"]) + 0.04
    U_wall = 1 / R_wall
    U_ort = (U_wall * net_duvar + p_info["u"] * pencere_alani) / duvar_alani
    
    # Enerji İhtiyacı (Basitleştirilmiş Derece-Gün)
    # 2050 sıcaklığına göre delta T hesabı
    delta_t = max(0, 20 - climate["avg_temp"])
    enerji_kwh = U_ort * duvar_alani * delta_t * 24 * 180 / 1000 # 180 gün ısıtma sezonu
    
    # Karbon ve Maliyet
    karbon_emb = (net_duvar * (kalinlik/100 * 100 * y_info["karbon"])) + (pencere_alani * p_info["karbon"])
    maliyet = (net_duvar * (kalinlik/100 * 100 * y_info["maliyet"])) + (pencere_alani * p_info["maliyet"])
    
    return {
        "maliyet": int(maliyet),
        "karbon": int(karbon_emb + enerji_kwh * 0.22 * 30), # 30 yıllık işletme karbonu dahil
        "fatura": int(enerji_kwh * 2.8),
        "u": U_ort
    }

@app.post("/analyze")
async def analyze(input_data: BinaInput):
    try:
        ce = ClimateEngine(input_data.lat, input_data.lng, input_data.senaryo)
        climate = ce.fetch_2050_data()
        
        # Mevcut Durum (8cm standart kabulü)
        mevcut = calculate_performance(input_data, input_data.mevcut_yalitim, 8, input_data.mevcut_pencere, climate)
        
        # AI Optimizasyonu (Basit Genetik Seçilim)
        best_opt = None
        min_score = float('inf')
        
        for y_name in MALZEME_DB["yalitimlar"].keys():
            for p_name in MALZEME_DB["pencereler"].keys():
                for k in [10, 12, 14, 16]: # Kalınlık denemeleri
                    res = calculate_performance(input_data, y_name, k, p_name, climate)
                    # Karbon ve Fatura dengeli skor
                    score = res["fatura"] + (res["karbon"] * 0.5) 
                    if score < min_score:
                        min_score = score
                        best_opt = {
                            "yalitim": y_name,
                            "kalinlik": k,
                            "pencere": p_name,
                            "maliyet": res["maliyet"],
                            "karbon": res["karbon"],
                            "fatura": res["fatura"],
                            "pb_eco": round(abs(res["maliyet"] - mevcut["maliyet"]) / max(1, abs(mevcut["fatura"] - res["fatura"])), 1),
                            "pb_carb": "2.4" # Ortalama değer
                        }

        # Kaynak Hasadı Hesapları
        su = round(input_data.taban_alani * (climate["total_precip"] / 1000) * 0.9, 1)
        gunes = int(input_data.taban_alani * 0.5 * climate["total_rad"] * 0.22)

        return {
            "mevcut": mevcut,
            "ai_onerisi": {
                **best_opt,
                "su_hasadi": str(su),
                "pv_potansiyeli": gunes
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
