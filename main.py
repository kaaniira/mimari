import os
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
# Tüm kaynaklardan gelen isteklere izin ver (CORS hatasını çözer)
CORS(app)

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.get_json()
        
        # Giriş verilerini al
        lat = data.get('lat', 41.01)
        lng = data.get('lng', 28.97)
        area = float(data.get('taban_alani', 100))
        floors = int(data.get('kat_sayisi', 1))
        scenario = data.get('senaryo', 'ssp245')
        
        # --- BASİT MİMARİ ANALİZ MOTORU ---
        total_m2 = area * floors
        
        # Senaryoya göre iklim katsayısı (Örnek mantık)
        climate_multiplier = 1.0
        if scenario == 'ssp126': climate_multiplier = 0.9
        elif scenario == 'ssp585': climate_multiplier = 1.4
        
        # Mevcut durum maliyet ve karbon hesaplama (Baz değerler)
        # toLocaleString hatası almamak için bu değerlerin mutlaka sayı olması gerekir
        current_maliyet = total_m2 * 45 * climate_multiplier
        current_karbon = total_m2 * 12 * climate_multiplier
        
        # AI Optimizasyon Önerisi
        # Burada genelde genetik algoritma veya ML modeliniz çalışır
        # Şimdilik optimize edilmiş değerleri dönüyoruz
        ai_maliyet = current_maliyet * 0.65 # %35 tasarruf
        ai_karbon = current_karbon * 0.55  # %45 karbon tasarrufu
        
        # Geri dönüş süreleri (Payback)
        pb_eco = round(25000 / (current_maliyet - ai_maliyet), 1) if current_maliyet > ai_maliyet else 10.0
        pb_carb = 2.4
        
        # Su ve Güneş potansiyeli (Mimari hesaplamalar)
        su_hasadi = round(area * 0.75 * 0.8, 1) # m3
        pv_potansiyeli = int(area * 0.6 * 165)  # kWh
        
        # Frontend'in beklediği tam yapı
        result = {
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
        
        return jsonify(result)

    except Exception as e:
        # Hata durumunda boş dönmek yerine anlamlı ama güvenli bir yapı dön
        print(f"Hata oluştu: {e}")
        return jsonify({
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
        }), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
