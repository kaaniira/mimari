from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import math
import random
import numpy as np
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Standart sunucu ortamı için uygulama başlatılıyor
app = FastAPI(
    title="Chrono-Build API",
    description="Geleceğin Bina Analiz ve İklim Adaptasyon Sistemi API Servisi",
    version="2.0.0"
)

# CORS Ayarları: Sunucunun farklı domainlerden gelen isteklere cevap verebilmesi için
# Üretim aşamasında allow_origins=["*"] yerine sitenizin domainini yazmanız önerilir.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MALZEME VERİTABANI ---
MALZEME_DB = {
    "yalitımlar": {
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

# --- VERİ MODELLERİ ---
class BinaInput(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    yonelim: int
    senaryo: str  # ssp126, ssp245, ssp585
    mevcut_yalitim: str
    mevcut_pencere: str

# --- HESAPLAMA FONKSİYONLARI ---
def bina_analiz_motoru(data: BinaInput, yalitim_tipi: str, yalitim_kalinlik_cm: int, pencere_tipi: str):
    # Bina Geometrisi Hesaplama
    kenar = math.sqrt(data.taban_alani)
    duvar_alani = (kenar * 4) * (data.kat_sayisi * 3)
    pencere_alani = duvar_alani * 0.15
    net_duvar = duvar_alani - pencere_alani
    
    # Isıl Performans (U-Ortalama)
    # R_toplam = R_ic + R_tugla + R_yalitim + R_dis
    R_duvar = 0.13 + (0.19 / 0.45) + (yalitim_kalinlik_cm / 100 / MALZEME_DB["yalitımlar"][yalitim_tipi]["lambda"]) + 0.04
    U_duvar = 1 / R_duvar
    U_pencere = MALZEME_DB["pencereler"][pencere_tipi]["u"]
    U_ort = (U_duvar * net_duvar + U_pencere * pencere_alani) / duvar_alani
    
    # Karbon Ayak İzi ve Maliyet Analizi
    karbon = (net_duvar * (yalitim_kalinlik_cm/100 * 100 * MALZEME_DB["yalitımlar"][yalitim_tipi]["karbon"])) + (pencere_alani * MALZEME_DB["pencereler"][pencere_tipi]["karbon"])
    maliyet = (net_duvar * (yalitim_kalinlik_cm/100 * 100 * MALZEME_DB["yalitımlar"][yalitim_tipi]["maliyet"])) + (pencere_alani * MALZEME_DB["pencereler"][pencere_tipi]["maliyet"])
    
    return {"U_ort": U_ort, "karbon": karbon, "maliyet": maliyet}

@app.get("/")
async def root():
    return {"message": "Chrono-Build API Sunucusu Aktif", "status": "online"}

@app.post("/analyze")
async def analyze(input_data: BinaInput):
    # Malzeme kontrolü
    if input_data.mevcut_yalitim not in MALZEME_DB["yalitımlar"] or input_data.mevcut_pencere not in MALZEME_DB["pencereler"]:
        raise HTTPException(status_code=400, detail="Geçersiz malzeme veya pencere tipi seçildi.")

    # 1. Mevcut Tasarım Analizi (Varsayılan 8cm üzerinden)
    mevcut = bina_analiz_motoru(input_data, input_data.mevcut_yalitim, 8, input_data.mevcut_pencere)
    
    # 2. AI Optimizasyon (Bütünleşik Verimlilik Arama)
    best_option = None
    min_score = float('inf')
    
    for y_ad in MALZEME_DB["yalitımlar"].keys():
        for p_ad in MALZEME_DB["pencereler"].keys():
            # Farklı kalınlık senaryolarını test et
            for kalinlik in [10, 12, 15, 20]:
                res = bina_analiz_motoru(input_data, y_ad, kalinlik, p_ad)
                # Fitness Fonksiyonu: U Değeri (Isı Kaybı) ve Karbon dengesi
                score = (res["U_ort"] * 1000) + (res["karbon"] * 0.1)
                if score < min_score:
                    min_score = score
                    best_option = {
                        "yalitim": y_ad, 
                        "kalinlik": kalinlik, 
                        "pencere": p_ad, 
                        "u": round(res["U_ort"], 3),
                        "karbon": round(res["karbon"], 2)
                    }

    return {
        "status": "success",
        "mevcut": mevcut,
        "ai_onerisi": best_option,
        "lokasyon": {"lat": input_data.lat, "lng": input_data.lng}
    }

# Sunucu üzerinde çalıştırmak için (Örn: python backend.py)
if __name__ == "__main__":
    # 0.0.0.0 adresi sunucunun dış dünyaya açık olmasını sağlar
    uvicorn.run("backend:app", host="0.0.0.0", port=8000, reload=False)
