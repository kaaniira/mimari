import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Chrono-Build API")

# CORS Ayarları (CORS hatasını çözer)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Veri Modeli
class AnalyzeRequest(BaseModel):
    lat: Optional[float] = 41.01
    lng: Optional[float] = 28.97
    taban_alani: float
    kat_sayisi: int
    senaryo: Optional[str] = "ssp245"

@app.post("/analyze")
async def analyze(request: AnalyzeRequest):
    try:
        # Giriş verilerini al
        area = request.taban_alani
        floors = request.kat_sayisi
        scenario = request.senaryo
        
        # --- BASİT MİMARİ ANALİZ MOTORU ---
        total_m2 = area * floors
        
        # Senaryoya göre iklim katsayısı (Örnek mantık)
        climate_multiplier = 1.0
        if scenario == 'ssp126': climate_multiplier = 0.9
        elif scenario == 'ssp585': climate_multiplier = 1.4
        
        # Mevcut durum maliyet ve karbon hesaplama
        # toLocaleString hatası almamak için değerlerin sayı olduğundan emin oluyoruz
        current_maliyet = total_m2 * 45 * climate_multiplier
        current_karbon = total_m2 * 12 * climate_multiplier
        
        # AI Optimizasyon Önerisi
        ai_maliyet = current_maliyet * 0.65 # %35 tasarruf
        ai_karbon = current_karbon * 0.55  # %45 karbon tasarrufu
        
        # Geri dönüş süreleri (Payback)
        pb_eco = round(25000 / (current_maliyet - ai_maliyet), 1) if current_maliyet > ai_maliyet else 10.0
        pb_carb = 2.4
        
        # Su ve Güneş potansiyeli (Mimari hesaplamalar)
        su_hasadi = round(area * 0.75 * 0.8, 1) # m3
        pv_potansiyeli = int(area * 0.6 * 165)  # kWh
        
        # Frontend'in beklediği tam yapı
        return {
            "mevcut": {
                "maliyet": int(current_maliyet),
                "karbon": int(current_karbon)
            },
            "ai_onerisi": {
                "yalitim": "Taş Yünü (Sert Levha)",
                "kalinlik": 8,
                "pencere": "Üçlü Low-E Cam",
                "maliyet": int(ai_maliyet),
                "karbon": int(ai_karbon),
                "pb_eco": str(pb_eco),
                "pb_carb": str(pb_carb),
                "su_hasadi": str(su_hasadi),
                "pv_potansiyeli": pv_potansiyeli
            }
        }

    except Exception as e:
        # Hata durumunda 500 dön ve güvenli yapı paylaş
        raise HTTPException(status_code=500, detail={
            "error": str(e),
            "mevcut": {"maliyet": 0, "karbon": 0},
            "ai_onerisi": {
                "yalitim": "Hata",
                "kalinlik": 0,
                "maliyet": 0,
                "karbon": 0,
                "pb_eco": "0",
                "pb_carb": "0",
                "su_hasadi": "0",
                "pv_potansiyeli": 0
            }
        })

if __name__ == "__main__":
    import uvicorn
    # Local testler için PORT ayarı
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
