import os
import math
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, Optional


app = FastAPI(title="ChronoBuild AI Engine (WP Catalog + TS825 + ROI10)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_PB_ECO_YIL = 10.0

WP_CATALOG_URL = os.environ.get("WP_CATALOG_URL", "").strip()
WP_API_KEY = os.environ.get("WP_API_KEY", "").strip()
NOMINATIM_EMAIL = os.environ.get("NOMINATIM_EMAIL", "").strip()

# TS825 Ek A.2 (Duvar Umax - derece gün bölgesine göre)
TS825_UWALL_MAX = {1: 0.45, 2: 0.40, 3: 0.40, 4: 0.35, 5: 0.25, 6: 0.25}

# TS825 Ek D - iller -> derece-gün bölgeleri (özet; genişletilebilir)
DG_ZONE_PROVINCES = {
    1: {"ADANA", "ANTALYA", "MERSİN"},
    2: {"ADIYAMAN", "AYDIN", "BATMAN", "DENİZLİ", "GAZİANTEP", "HATAY", "İZMIR", "KAHRAMANMARAŞ",
        "KİLİS", "MANİSA", "MARDİN", "OSMANİYE", "SİİRT", "ŞANLIURFA"},
    3: {"BALIKESİR", "BURSA", "ÇANAKKALE", "GİRESUN", "İSTANBUL", "KOCAELİ", "MUĞLA", "ORDU",
        "RİZE", "SAKARYA", "SAMSUN", "SİNOP", "TEKİRDAĞ", "TRABZON", "YALOVA", "ZONGULDAK"},
    4: {"AFYON", "AMASYA", "AKSARAY", "ANKARA", "ARTVİN", "BARTIN", "BİLECİK", "BİNGÖL", "BOLU",
        "BURDUR", "ÇANKIRI", "ÇORUM", "DÜZCE", "DİYARBAKIR", "EDİRNE", "ELAZIĞ", "ERZİNCAN",
        "ESKİŞEHİR", "IĞDIR", "ISPARTA", "KARABÜK", "KARAMAN", "KAYSERİ", "KIRIKKALE", "KIRKLARELİ",
        "KIRŞEHİR", "KONYA", "KÜTAHYA", "MALATYA", "NEVŞEHİR", "NİĞDE", "ŞIRNAK", "TOKAT",
        "TUNCELİ", "UŞAK"},
    5: {"BAYBURT", "BİTLİS", "GÜMÜŞHANE", "HAKKARİ", "KASTAMONU", "MUŞ", "SİVAS", "VAN", "YOZGAT"},
    6: {"AĞRI", "ARDAHAN", "ERZURUM", "KARS"},
}


class BuildingData(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float
    dogalgaz_fiyat: float = 0.0  # kullanıcı girerse öncelik verilecek (istersen 0 bırak)
    yonelim: int = 180
    senaryo: str
    mevcut_pencere: str

    # opsiyonel ayarlar
    pencere_orani: float = 0.15
    cati_orani: float = 0.5
    su_verimi: float = 0.9
    pv_verim: float = 0.22

    # TS825 hesap varsayımları
    r_base_layers: float = 0.50  # yalıtım harici duvar R toplamı

    # Doğalgaz katsayıları
    gaz_kwh_m3: float = 10.64
    gaz_co2_kg_m3: float = 2.15

    class Config:
        extra = "ignore"


# ---------------- WP CATALOG ----------------
def fetch_wp_catalog() -> Dict[str, Any]:
    if not WP_CATALOG_URL:
        raise RuntimeError("WP_CATALOG_URL env yok. Cloud Run env'e ekleyin.")

    headers = {"X-CHRONO-KEY": WP_API_KEY} if WP_API_KEY else {}
    r = requests.get(WP_CATALOG_URL, headers=headers, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"WP catalog HTTP {r.status_code}")

    data = r.json()
    if "yalitimlar" not in data or "pencereler" not in data:
        raise RuntimeError("WP catalog formatı hatalı (yalitimlar/pencereler yok).")

    yal = {}
    for x in data.get("yalitimlar", []):
        if x.get("active", True) and x.get("name") and x.get("lambda") and x.get("fiyat_m3") is not None and x.get("karbon_m3") is not None:
            yal[str(x["name"])] = {
                "lambda": float(x["lambda"]),
                "fiyat_m3": float(x["fiyat_m3"]),
                "karbon_m3": float(x["karbon_m3"]),
            }

    pen = {}
    for x in data.get("pencereler", []):
        if x.get("active", True) and x.get("name") and x.get("u") is not None:
            pen[str(x["name"])] = {
                "u": float(x["u"]),
                "fiyat_m2": float(x.get("fiyat_m2", 0.0)),
                "karbon_m2": float(x.get("karbon_m2", 0.0)),
            }

    tarifeler = data.get("tarifeler", {}) or {}
    config = data.get("config", {}) or {}

    if not yal:
        raise RuntimeError("WP catalog: aktif yalıtım bulunamadı.")
    if not pen:
        raise RuntimeError("WP catalog: aktif pencere bulunamadı.")

    return {"yalitimlar": yal, "pencereler": pen, "tarifeler": tarifeler, "config": config}


# ---------------- TS825 mantolama formülleri ----------------
def ts825_u_from_R(R_total: float) -> float:
    return 1.0 / max(1e-9, R_total)

def ts825_required_insulation_thickness_cm(
    U_target: float, lambda_ins: float, Rsi: float, Rse: float, R_base_layers: float
) -> int:
    # 1/U = Rsi + R_base + R_ins + Rse  =>  R_ins = 1/U - (Rsi+R_base+Rse)
    needed_R_ins = (1.0 / max(1e-9, U_target)) - (Rsi + R_base_layers + Rse)
    d_m = max(0.0, needed_R_ins) * lambda_ins
    cm = int(math.ceil(d_m * 100))
    if cm % 2 != 0:
        cm += 1
    return max(0, cm)


# ---------------- Geometri ----------------
def geometry(data: BuildingData):
    kenar = math.sqrt(max(1e-6, data.taban_alani))
    cevre = 4 * kenar
    brut_cephe = cevre * data.kat_yuksekligi * data.kat_sayisi
    cam = brut_cephe * data.pencere_orani
    duvar = brut_cephe - cam
    cati = data.taban_alani
    return duvar, cam, cati


# ---------------- Climate (Open-Meteo) ----------------
def calculate_hdd(temps, base=19.0):
    return sum(max(0.0, base - t) for t in temps if t is not None)

def climate_year(lat: float, lng: float, year: int, scenario: str) -> Dict[str, Any]:
    climate_url = "https://climate-api.open-meteo.com/v1/climate"

    # basit senaryo düzeltmeleri (API senaryo parametresi yoksa bile “oynatmak” için)
    if scenario == "ssp126":
        temp_adj, precip_adj = -0.3, 1.05
    elif scenario == "ssp585":
        temp_adj, precip_adj = +1.8, 0.85
    else:
        temp_adj, precip_adj = 0.0, 1.0

    params = {
        "latitude": lat,
        "longitude": lng,
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "models": "EC_Earth3P_HR",
        "daily": ["temperature_2m_mean", "precipitation_sum", "shortwave_radiation_sum"],
        "disable_bias_correction": "true",
    }

    safe = {"hdd": 2200.0, "yagis_mm": 450.0, "gunes_kwh_m2": 1550.0, "is_real": False}

    try:
        r = requests.get(climate_url, params=params, timeout=10)
        if r.status_code != 200:
            return safe
        j = r.json()
        if "daily" not in j:
            return safe

        ts = j["daily"].get("temperature_2m_mean", []) or []
        ps = j["daily"].get("precipitation_sum", []) or []
        ss = j["daily"].get("shortwave_radiation_sum", []) or []
        units = j.get("daily_units", {}) or {}

        temps = [(t + temp_adj) for t in ts if t is not None]
        if len(temps) < 50:
            return safe

        hdd = calculate_hdd(temps, base=19.0)
        if hdd < 500:
            return safe

        yagis = sum(p for p in ps if p is not None) * precip_adj

        sun_sum = sum(s for s in ss if s is not None)
        sun_unit = units.get("shortwave_radiation_sum", "")
        if "MJ" in sun_unit:
            gunes_kwh_m2 = sun_sum / 3.6
        elif "Wh" in sun_unit:
            gunes_kwh_m2 = sun_sum / 1000.0
        else:
            gunes_kwh_m2 = sun_sum / 3.6

        return {"hdd": float(hdd), "yagis_mm": float(yagis), "gunes_kwh_m2": float(gunes_kwh_m2), "is_real": True}
    except:
        return safe


# ---------------- Province / TS825 zone ----------------
def reverse_geocode_province(lat: float, lng: float) -> Optional[str]:
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 10, "addressdetails": 1}
        ua = f"ChronoBuild/1.0 ({NOMINATIM_EMAIL})" if NOMINATIM_EMAIL else "ChronoBuild/1.0 (educational)"
        headers = {"User-Agent": ua}
        r = requests.get(url, params=params, headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        j = r.json()
        addr = j.get("address", {}) or {}
        cand = addr.get("province") or addr.get("state") or addr.get("county")
        if not cand:
            return None
        return cand.strip().upper().replace("İ", "İ")
    except:
        return None

def degree_day_zone_from_province(prov: Optional[str]) -> int:
    if not prov:
        return 3
    for zone, provs in DG_ZONE_PROVINCES.items():
        if prov in provs:
            return zone
    return 3


# ---------------- Energy + investment ----------------
def annual_energy_from_U(data: BuildingData, hdd: float, u_wall: float, u_win: float) -> Dict[str, float]:
    duvar, cam, _ = geometry(data)
    enerji_kwh = ((u_wall * duvar) + (u_win * cam)) * hdd * 24.0 / 1000.0
    gaz_m3 = enerji_kwh / max(1e-9, data.gaz_kwh_m3)
    gaz_fiyat = data.dogalgaz_fiyat if data.dogalgaz_fiyat > 0 else 6.0
    tutar = gaz_m3 * gaz_fiyat
    co2 = gaz_m3 * data.gaz_co2_kg_m3
    return {"enerji_kwh": enerji_kwh, "gaz_m3": gaz_m3, "tutar_tl": tutar, "co2_kg": co2}

def investment_insulation(duvar_alan: float, kal_cm: int, mat: dict) -> Dict[str, float]:
    vol = duvar_alan * (kal_cm / 100.0)
    cost = vol * mat["fiyat_m3"]
    emb = vol * mat["karbon_m3"]
    return {"vol_m3": vol, "cost_tl": cost, "emb_kg": emb}

def investment_window(cam_alan: float, win: dict) -> Dict[str, float]:
    cost = cam_alan * float(win.get("fiyat_m2", 0.0))
    emb = cam_alan * float(win.get("karbon_m2", 0.0))
    return {"cost_tl": cost, "emb_kg": emb}


@app.post("/analyze")
async def analyze_building(data: BuildingData):
    try:
        catalog = fetch_wp_catalog()
        cfg = catalog.get("config", {}) or {}
        tarifeler = catalog.get("tarifeler", {}) or {}

        # config override (WP’den)
        data.pv_verim = float(cfg.get("pv_verim", data.pv_verim))
        data.su_verimi = float(cfg.get("su_verimi", data.su_verimi))
        data.cati_orani = float(cfg.get("cati_orani", data.cati_orani))

        # il & TS825 zone
        prov = reverse_geocode_province(data.lat, data.lng)
        zone = degree_day_zone_from_province(prov)
        u_wall_max = TS825_UWALL_MAX[zone]

        # gaz fiyatı (WP -> il bazlı -> default) / kullanıcı girişi varsa üstün
        default_gas = float(tarifeler.get("default_gas_tl_m3", 6.0))
        by_prov = tarifeler.get("by_province", {}) or {}
        wp_gas = default_gas
        if prov and prov in by_prov:
            try:
                wp_gas = float(by_prov[prov])
            except:
                wp_gas = default_gas
        if data.dogalgaz_fiyat and data.dogalgaz_fiyat > 0:
            pass  # kullanıcı üstün
        else:
            data.dogalgaz_fiyat = wp_gas

        # iklim
        clim_now = climate_year(data.lat, data.lng, 2020, data.senaryo)
        clim_2050 = climate_year(data.lat, data.lng, 2050, data.senaryo)

        # geometri
        duvar, cam, _ = geometry(data)

        # pencere (mevcut)
        p_db = catalog["pencereler"]
        win_mevcut = p_db.get(data.mevcut_pencere) or p_db.get("Çift Cam (Isıcam S)") or next(iter(p_db.values()))
        u_win_mevcut = float(win_mevcut["u"])

        # TS825 yüzey dirençleri (standart kullanım)
        Rsi, Rse = 0.13, 0.04

        # --- TS825 Baz: Umax sağlayan en düşük yatırım maliyetli yalıtımı seç ---
        y_db = catalog["yalitimlar"]
        best_ts = None
        best_ts_cost = 1e30

        for y_name, mat in y_db.items():
            kal_cm = ts825_required_insulation_thickness_cm(
                U_target=u_wall_max,
                lambda_ins=float(mat["lambda"]),
                Rsi=Rsi,
                Rse=Rse,
                R_base_layers=float(data.r_base_layers),
            )
            R_total = Rsi + float(data.r_base_layers) + (kal_cm / 100.0) / max(1e-9, float(mat["lambda"])) + Rse
            u_wall = ts825_u_from_R(R_total)

            inv = investment_insulation(duvar, kal_cm, mat)
            if inv["cost_tl"] < best_ts_cost:
                best_ts_cost = inv["cost_tl"]
                best_ts = {"yalitim": y_name, "kalinlik_cm": int(kal_cm), "u_wall": float(u_wall)}

        base_now = annual_energy_from_U(data, clim_now["hdd"], best_ts["u_wall"], u_win_mevcut)
        base_2050 = annual_energy_from_U(data, clim_2050["hdd"], best_ts["u_wall"], u_win_mevcut)

        mevcut = {
            "province": prov or "Bilinmiyor",
            "ts825_zone": zone,
            "u_wall_max": u_wall_max,
            "yalitim": best_ts["yalitim"],
            "kalinlik_cm": best_ts["kalinlik_cm"],
            "u_wall": round(best_ts["u_wall"], 3),
            "pencere": data.mevcut_pencere,
            "gaz_fiyat_tl_m3": round(float(data.dogalgaz_fiyat), 3),
            "today": {
                "hdd": int(round(clim_now["hdd"])),
                "yillik_gaz_m3": int(round(base_now["gaz_m3"])),
                "yillik_tutar_tl": int(round(base_now["tutar_tl"])),
                "yillik_co2_kg": int(round(base_now["co2_kg"])),
            },
            "y2050": {
                "hdd": int(round(clim_2050["hdd"])),
                "yillik_gaz_m3": int(round(base_2050["gaz_m3"])),
                "yillik_tutar_tl": int(round(base_2050["tutar_tl"])),
                "yillik_co2_kg": int(round(base_2050["co2_kg"])),
            },
        }

        # --- AI tarama: 10 yıl altını öncelikle seç ---
        best_under_10 = None
        best_under_10_score = 1e30
        best_any = None
        best_any_score = 1e30

        def build_candidate(y_name, mat, kal_cm, u_wall, p_name, win) -> Dict[str, Any]:
            u_win = float(win["u"])
            ai_2050 = annual_energy_from_U(data, clim_2050["hdd"], u_wall, u_win)

            tasarruf_tl = base_2050["tutar_tl"] - ai_2050["tutar_tl"]
            tasarruf_co2 = base_2050["co2_kg"] - ai_2050["co2_kg"]
            tasarruf_gaz = base_2050["gaz_m3"] - ai_2050["gaz_m3"]

            inv_ins = investment_insulation(duvar, kal_cm, mat)
            inv_win = investment_window(cam, win)
            yatirim = inv_ins["cost_tl"] + inv_win["cost_tl"]
            embodied = inv_ins["emb_kg"] + inv_win["emb_kg"]

            pb_eco = (yatirim / tasarruf_tl) if tasarruf_tl > 0 else 99.0
            pb_carb = (embodied / tasarruf_co2) if tasarruf_co2 > 0 else 99.0

            return {
                "yalitim": y_name,
                "kalinlik_cm": int(kal_cm),
                "u_wall": round(float(u_wall), 3),
                "pencere": p_name,
                "u_pencere": round(float(u_win), 2),
                "y2050": {
                    "yillik_gaz_m3": int(round(ai_2050["gaz_m3"])),
                    "yillik_tutar_tl": int(round(ai_2050["tutar_tl"])),
                    "yillik_co2_kg": int(round(ai_2050["co2_kg"])),
                },
                "tasarruf": {
                    "yillik_tasarruf_tl": int(round(tasarruf_tl)),
                    "yillik_gaz_tasarruf_m3": int(round(tasarruf_gaz)),
                    "yillik_co2_tasarruf_kg": int(round(tasarruf_co2)),
                },
                "yatirim": {
                    "yatirim_tl": int(round(yatirim)),
                    "embodied_co2_kg": int(round(embodied)),
                },
                "pb_eco_yil": round(pb_eco, 1),
                "pb_carb_yil": round(pb_carb, 1),
                "max_pb_eco_yil": MAX_PB_ECO_YIL,
            }

        for y_name, mat in y_db.items():
            kal_cm = ts825_required_insulation_thickness_cm(
                U_target=u_wall_max,
                lambda_ins=float(mat["lambda"]),
                Rsi=Rsi,
                Rse=Rse,
                R_base_layers=float(data.r_base_layers),
            )
            R_total = Rsi + float(data.r_base_layers) + (kal_cm / 100.0) / max(1e-9, float(mat["lambda"])) + Rse
            u_wall = ts825_u_from_R(R_total)

            for p_name, win in p_db.items():
                cand = build_candidate(y_name, mat, kal_cm, u_wall, p_name, win)
                score = float(cand["pb_eco_yil"]) + float(cand["pb_carb_yil"])

                if score < best_any_score:
                    best_any_score = score
                    best_any = cand

                if cand["pb_eco_yil"] <= MAX_PB_ECO_YIL and score < best_under_10_score:
                    best_under_10_score = score
                    best_under_10 = cand

        best_ai = best_under_10 if best_under_10 else best_any

        # --- 10 yıl altı yoksa alternatif yöntem öner ---
        alternatif = None
        uyari = None

        if best_under_10 is None:
            uyari = f"Ekonomik geri ödeme hedefi (≤{MAX_PB_ECO_YIL} yıl) için uygun kombinasyon bulunamadı. Alternatif yöntem öneriliyor."

            # A) sadece pencere yükseltmesi (TS825 baz yalıtım sabit)
            best_win = None
            best_win_pb = 1e30
            for p_name, win in p_db.items():
                u_win = float(win["u"])
                ai_2050 = annual_energy_from_U(data, clim_2050["hdd"], best_ts["u_wall"], u_win)

                tasarruf_tl = base_2050["tutar_tl"] - ai_2050["tutar_tl"]
                tasarruf_co2 = base_2050["co2_kg"] - ai_2050["co2_kg"]

                inv_win = investment_window(cam, win)
                yatirim = inv_win["cost_tl"]
                embodied = inv_win["emb_kg"]

                pb_eco = (yatirim / tasarruf_tl) if tasarruf_tl > 0 else 99.0
                pb_carb = (embodied / tasarruf_co2) if tasarruf_co2 > 0 else 99.0

                if pb_eco < best_win_pb:
                    best_win_pb = pb_eco
                    best_win = {
                        "tip": "Sadece Pencere Yükseltmesi",
                        "yalitim": best_ts["yalitim"],
                        "kalinlik_cm": best_ts["kalinlik_cm"],
                        "pencere": p_name,
                        "pb_eco_yil": round(pb_eco, 1),
                        "pb_carb_yil": round(pb_carb, 1),
                        "yatirim_tl": int(round(yatirim)),
                        "embodied_co2_kg": int(round(embodied)),
                        "yillik_tasarruf_tl": int(round(tasarruf_tl)),
                    }

            # B) sadece yalıtım (mevcut pencere sabit)
            best_ins = None
            best_ins_pb = 1e30
            for y_name, mat in y_db.items():
                kal_cm = ts825_required_insulation_thickness_cm(
                    U_target=u_wall_max,
                    lambda_ins=float(mat["lambda"]),
                    Rsi=Rsi,
                    Rse=Rse,
                    R_base_layers=float(data.r_base_layers),
                )
                R_total = Rsi + float(data.r_base_layers) + (kal_cm / 100.0) / max(1e-9, float(mat["lambda"])) + Rse
                u_wall = ts825_u_from_R(R_total)

                ai_2050 = annual_energy_from_U(data, clim_2050["hdd"], u_wall, u_win_mevcut)

                tasarruf_tl = base_2050["tutar_tl"] - ai_2050["tutar_tl"]
                tasarruf_co2 = base_2050["co2_kg"] - ai_2050["co2_kg"]

                inv_ins = investment_insulation(duvar, kal_cm, mat)
                yatirim = inv_ins["cost_tl"]
                embodied = inv_ins["emb_kg"]

                pb_eco = (yatirim / tasarruf_tl) if tasarruf_tl > 0 else 99.0
                pb_carb = (embodied / tasarruf_co2) if tasarruf_co2 > 0 else 99.0

                if pb_eco < best_ins_pb:
                    best_ins_pb = pb_eco
                    best_ins = {
                        "tip": "Sadece Yalıtım Optimizasyonu",
                        "yalitim": y_name,
                        "kalinlik_cm": int(kal_cm),
                        "pencere": data.mevcut_pencere,
                        "pb_eco_yil": round(pb_eco, 1),
                        "pb_carb_yil": round(pb_carb, 1),
                        "yatirim_tl": int(round(yatirim)),
                        "embodied_co2_kg": int(round(embodied)),
                        "yillik_tasarruf_tl": int(round(tasarruf_tl)),
                    }

            candidates = [c for c in [best_win, best_ins] if c is not None]
            under10 = [c for c in candidates if c["pb_eco_yil"] <= MAX_PB_ECO_YIL]
            if under10:
                alternatif = min(under10, key=lambda x: x["pb_eco_yil"])
            elif candidates:
                alternatif = min(candidates, key=lambda x: x["pb_eco_yil"])

        # Su & PV (2050)
        su_m3 = data.taban_alani * (clim_2050["yagis_mm"] / 1000.0) * data.su_verimi
        pv_kwh = (data.taban_alani * data.cati_orani) * clim_2050["gunes_kwh_m2"] * data.pv_verim

        best_ai["su_hasadi_m3_yil"] = round(su_m3, 1)
        best_ai["pv_kwh_yil"] = int(round(pv_kwh))
        best_ai["uyari"] = uyari
        best_ai["alternatif_oneri"] = alternatif

        return {
            "iklim_info": {
                "today": {
                    "hdd": int(round(clim_now["hdd"])),
                    "yagis_mm": int(round(clim_now["yagis_mm"])),
                },
                "y2050": {
                    "hdd": int(round(clim_2050["hdd"])),
                    "yagis_mm": int(round(clim_2050["yagis_mm"])),
                    "gunes_kwh_m2": int(round(clim_2050["gunes_kwh_m2"])),
                    "is_real": bool(clim_2050["is_real"]),
                },
            },
            "mevcut": mevcut,
            "ai_onerisi": best_ai,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
