import os
import math
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from google.cloud import firestore

# -------------------- CONFIG --------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "").strip() or None
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

if not ADMIN_PASS:
    # Cloud Run’da mutlaka env set et
    print("WARNING: ADMIN_PASS env is empty. Set it for /admin security!")

db = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()

app = FastAPI(title="ChronoBuild Engine + Admin (Firestore)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # WordPress’ten çağıracaksan kalsın; istersen domain bazlı daraltırız.
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- MODELS --------------------
class InsulationItem(BaseModel):
    name: str
    lambda_value: float = Field(..., gt=0)     # W/mK
    price_m3: float = Field(..., ge=0)         # TL/m3
    carbon_m3: float = Field(..., ge=0)        # kgCO2/m3
    active: bool = True

class WindowItem(BaseModel):
    name: str
    u_value: float = Field(..., gt=0)          # W/m2K
    price_m2: float = Field(0, ge=0)           # TL/m2
    carbon_m2: float = Field(0, ge=0)          # kgCO2/m2
    active: bool = True

class GasTariffItem(BaseModel):
    province: str                               # "İSTANBUL"
    price_tl_m3: float = Field(..., gt=0)
    active: bool = True

class ConfigItem(BaseModel):
    default_gas_tl_m3: float = Field(6.0, gt=0)
    pv_eff: float = Field(0.22, gt=0, le=1)
    rain_eff: float = Field(0.90, gt=0, le=1)
    roof_ratio: float = Field(0.50, gt=0, le=1)

# Analyze input (WordPress frontend’den gelen)
class AnalyzeInput(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float = 2.8
    dogalgaz_fiyat: float = 0.0  # 0 ise Firestore tarifesinden alınır
    senaryo: str
    mevcut_pencere: str

    pencere_orani: float = 0.15

    class Config:
        extra = "ignore"


# -------------------- AUTH (simple) --------------------
def require_admin(request: Request):
    """
    Basit BasicAuth:
    Header: Authorization: Basic base64(user:pass)
    Cloud Run için pratik. Daha güvenlisi: IAP / Cloud Armor / Identity Platform.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise HTTPException(status_code=401, detail="Auth required")

    import base64
    try:
        b64 = auth.split(" ", 1)[1].strip()
        userpass = base64.b64decode(b64).decode("utf-8")
        user, pwd = userpass.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Bad auth")

    if user != ADMIN_USER or pwd != ADMIN_PASS:
        raise HTTPException(status_code=403, detail="Forbidden")

    return True


# -------------------- FIRESTORE HELPERS --------------------
def _col(name: str):
    return db.collection(name)

def set_doc(collection: str, doc_id: str, payload: Dict[str, Any]):
    _col(collection).document(doc_id).set(payload, merge=True)

def get_all_active(collection: str) -> List[Dict[str, Any]]:
    docs = _col(collection).stream()
    out = []
    for d in docs:
        obj = d.to_dict() or {}
        obj["id"] = d.id
        out.append(obj)
    return out

def get_config() -> Dict[str, Any]:
    doc = _col("config").document("main").get()
    if doc.exists:
        return doc.to_dict() or {}
    return {}

def ensure_defaults():
    # İlk kurulumda boşsa varsayılan config ve örnekler
    cfg = get_config()
    if not cfg:
        set_doc("config", "main", ConfigItem().model_dump())

    # Koleksiyonlar boşsa örnek ekle (isteğe bağlı)
    if not list(_col("insulations").limit(1).stream()):
        set_doc("insulations", "tas_yunu", InsulationItem(
            name="Taş Yünü (Sert)", lambda_value=0.035, price_m3=2800, carbon_m3=150, active=True
        ).model_dump())
    if not list(_col("windows").limit(1).stream()):
        set_doc("windows", "cift_cam", WindowItem(
            name="Çift Cam (Isıcam S)", u_value=2.8, price_m2=3200, carbon_m2=25, active=True
        ).model_dump())

ensure_defaults()


def load_catalog() -> Dict[str, Any]:
    cfg = get_config()
    ins = [x for x in get_all_active("insulations") if x.get("active", True)]
    win = [x for x in get_all_active("windows") if x.get("active", True)]
    gas = [x for x in get_all_active("gas_tariffs") if x.get("active", True)]

    ins_map = {x["name"]: x for x in ins if x.get("name") and x.get("lambda_value")}
    win_map = {x["name"]: x for x in win if x.get("name") and x.get("u_value")}
    gas_map = {str(x.get("province", "")).strip().upper(): float(x.get("price_tl_m3", 0)) for x in gas}

    return {
        "config": cfg,
        "insulations": ins_map,
        "windows": win_map,
        "gas_by_province": gas_map,
    }


# -------------------- ADMIN UI --------------------
ADMIN_HTML = """
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>ChronoBuild Admin</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-900">
  <div class="max-w-6xl mx-auto p-6 space-y-6">
    <div class="bg-white border rounded-2xl p-6 flex items-center justify-between">
      <div>
        <div class="text-xl font-black">ChronoBuild Admin</div>
        <div class="text-xs text-slate-500">Firestore katalog yönetimi</div>
      </div>
      <div class="text-xs text-slate-500">Auth: Basic</div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="bg-white border rounded-2xl p-6">
        <div class="font-bold mb-3">Config</div>
        <div class="grid grid-cols-2 gap-3 text-sm">
          <label class="block">Default gaz (TL/m³)
            <input id="cfg_default_gas" type="number" step="0.01" class="mt-1 w-full border rounded-xl p-2"/>
          </label>
          <label class="block">PV verim (0-1)
            <input id="cfg_pv" type="number" step="0.01" class="mt-1 w-full border rounded-xl p-2"/>
          </label>
          <label class="block">Yağmur verim (0-1)
            <input id="cfg_rain" type="number" step="0.01" class="mt-1 w-full border rounded-xl p-2"/>
          </label>
          <label class="block">Çatı oranı (0-1)
            <input id="cfg_roof" type="number" step="0.01" class="mt-1 w-full border rounded-xl p-2"/>
          </label>
        </div>
        <button onclick="saveConfig()" class="mt-4 w-full bg-slate-900 text-white rounded-xl p-2 font-bold">Kaydet</button>
      </div>

      <div class="bg-white border rounded-2xl p-6">
        <div class="font-bold mb-3">Yalıtım Ekle/Güncelle</div>
        <div class="space-y-2 text-sm">
          <input id="ins_id" placeholder="id (örn tas_yunu)" class="w-full border rounded-xl p-2"/>
          <input id="ins_name" placeholder="ad" class="w-full border rounded-xl p-2"/>
          <input id="ins_lambda" type="number" step="0.001" placeholder="lambda" class="w-full border rounded-xl p-2"/>
          <input id="ins_price" type="number" step="1" placeholder="fiyat TL/m3" class="w-full border rounded-xl p-2"/>
          <input id="ins_carbon" type="number" step="1" placeholder="karbon kgCO2/m3" class="w-full border rounded-xl p-2"/>
          <label class="flex items-center gap-2"><input id="ins_active" type="checkbox" checked/> Aktif</label>
        </div>
        <button onclick="upsertInsulation()" class="mt-4 w-full bg-indigo-600 text-white rounded-xl p-2 font-bold">Kaydet</button>
      </div>

      <div class="bg-white border rounded-2xl p-6">
        <div class="font-bold mb-3">Pencere Ekle/Güncelle</div>
        <div class="space-y-2 text-sm">
          <input id="win_id" placeholder="id (örn cift_cam)" class="w-full border rounded-xl p-2"/>
          <input id="win_name" placeholder="ad" class="w-full border rounded-xl p-2"/>
          <input id="win_u" type="number" step="0.01" placeholder="U değeri" class="w-full border rounded-xl p-2"/>
          <input id="win_price" type="number" step="1" placeholder="fiyat TL/m2" class="w-full border rounded-xl p-2"/>
          <input id="win_carbon" type="number" step="1" placeholder="karbon kgCO2/m2" class="w-full border rounded-xl p-2"/>
          <label class="flex items-center gap-2"><input id="win_active" type="checkbox" checked/> Aktif</label>
        </div>
        <button onclick="upsertWindow()" class="mt-4 w-full bg-emerald-600 text-white rounded-xl p-2 font-bold">Kaydet</button>
      </div>
    </div>

    <div class="bg-white border rounded-2xl p-6">
      <div class="font-bold mb-3">Doğalgaz Tarifesi (İl bazlı)</div>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
        <input id="gas_id" placeholder="id (örn istanbul)" class="border rounded-xl p-2"/>
        <input id="gas_prov" placeholder="İL (İSTANBUL)" class="border rounded-xl p-2"/>
        <input id="gas_price" type="number" step="0.01" placeholder="TL/m3" class="border rounded-xl p-2"/>
        <label class="flex items-center gap-2 border rounded-xl p-2 justify-center"><input id="gas_active" type="checkbox" checked/> Aktif</label>
      </div>
      <button onclick="upsertGas()" class="mt-4 w-full bg-amber-500 text-white rounded-xl p-2 font-bold">Kaydet</button>
    </div>

    <div class="bg-white border rounded-2xl p-6">
      <div class="font-bold mb-3">Mevcut Katalog</div>
      <pre id="catalog" class="text-xs bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-[420px]"></pre>
      <button onclick="loadCatalog()" class="mt-4 w-full bg-slate-700 text-white rounded-xl p-2 font-bold">Yenile</button>
    </div>
  </div>

<script>
  async function api(path, method="GET", body=null){
    const res = await fetch(path, {
      method,
      headers: {"Content-Type":"application/json"},
      body: body ? JSON.stringify(body) : null
    });
    if(!res.ok){
      const t = await res.text();
      throw new Error(res.status + " " + t);
    }
    return res.json();
  }

  async function loadCatalog(){
    const data = await api("/admin/api/catalog");
    document.getElementById("catalog").textContent = JSON.stringify(data, null, 2);

    const cfg = data.config || {};
    document.getElementById("cfg_default_gas").value = cfg.default_gas_tl_m3 ?? 6.0;
    document.getElementById("cfg_pv").value = cfg.pv_eff ?? 0.22;
    document.getElementById("cfg_rain").value = cfg.rain_eff ?? 0.90;
    document.getElementById("cfg_roof").value = cfg.roof_ratio ?? 0.50;
  }

  async function saveConfig(){
    const body = {
      default_gas_tl_m3: parseFloat(document.getElementById("cfg_default_gas").value || "6"),
      pv_eff: parseFloat(document.getElementById("cfg_pv").value || "0.22"),
      rain_eff: parseFloat(document.getElementById("cfg_rain").value || "0.90"),
      roof_ratio: parseFloat(document.getElementById("cfg_roof").value || "0.50")
    };
    await api("/admin/api/config", "PUT", body);
    await loadCatalog();
    alert("Config kaydedildi");
  }

  async function upsertInsulation(){
    const id = document.getElementById("ins_id").value.trim();
    const body = {
      name: document.getElementById("ins_name").value.trim(),
      lambda_value: parseFloat(document.getElementById("ins_lambda").value),
      price_m3: parseFloat(document.getElementById("ins_price").value),
      carbon_m3: parseFloat(document.getElementById("ins_carbon").value),
      active: document.getElementById("ins_active").checked
    };
    await api("/admin/api/insulations/" + encodeURIComponent(id), "PUT", body);
    await loadCatalog();
    alert("Yalıtım kaydedildi");
  }

  async function upsertWindow(){
    const id = document.getElementById("win_id").value.trim();
    const body = {
      name: document.getElementById("win_name").value.trim(),
      u_value: parseFloat(document.getElementById("win_u").value),
      price_m2: parseFloat(document.getElementById("win_price").value || "0"),
      carbon_m2: parseFloat(document.getElementById("win_carbon").value || "0"),
      active: document.getElementById("win_active").checked
    };
    await api("/admin/api/windows/" + encodeURIComponent(id), "PUT", body);
    await loadCatalog();
    alert("Pencere kaydedildi");
  }

  async function upsertGas(){
    const id = document.getElementById("gas_id").value.trim();
    const body = {
      province: document.getElementById("gas_prov").value.trim(),
      price_tl_m3: parseFloat(document.getElementById("gas_price").value),
      active: document.getElementById("gas_active").checked
    };
    await api("/admin/api/gas_tariffs/" + encodeURIComponent(id), "PUT", body);
    await loadCatalog();
    alert("Tarife kaydedildi");
  }

  loadCatalog();
</script>
</body>
</html>
"""

@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: bool = Depends(require_admin)):
    return ADMIN_HTML


# -------------------- ADMIN API --------------------
@app.get("/admin/api/catalog")
def admin_get_catalog(_: bool = Depends(require_admin)):
    return load_catalog()

@app.put("/admin/api/config")
def admin_put_config(item: ConfigItem, _: bool = Depends(require_admin)):
    set_doc("config", "main", item.model_dump())
    return {"ok": True}

@app.put("/admin/api/insulations/{doc_id}")
def admin_put_insulation(doc_id: str, item: InsulationItem, _: bool = Depends(require_admin)):
    set_doc("insulations", doc_id, item.model_dump())
    return {"ok": True, "id": doc_id}

@app.put("/admin/api/windows/{doc_id}")
def admin_put_window(doc_id: str, item: WindowItem, _: bool = Depends(require_admin)):
    set_doc("windows", doc_id, item.model_dump())
    return {"ok": True, "id": doc_id}

@app.put("/admin/api/gas_tariffs/{doc_id}")
def admin_put_gas(doc_id: str, item: GasTariffItem, _: bool = Depends(require_admin)):
    payload = item.model_dump()
    payload["province"] = payload["province"].strip().upper()
    set_doc("gas_tariffs", doc_id, payload)
    return {"ok": True, "id": doc_id}


# -------------------- ANALYZE (placeholder) --------------------
# Burada sadece katalog Firestore’dan geliyor. TS825 kısmını bir sonraki adımda PDF’ye göre “birebir” bağlayacağız.

@app.post("/analyze")
def analyze(inp: AnalyzeInput):
    try:
        cat = load_catalog()
        cfg = cat.get("config", {})
        ins_db = cat.get("insulations", {})
        win_db = cat.get("windows", {})
        gas_by = cat.get("gas_by_province", {})

        if not ins_db or not win_db:
            raise HTTPException(500, "Katalog boş. /admin'den ürün ekleyin.")

        # Gaz fiyatı: kullanıcı 0 ise default
        gas_price = float(inp.dogalgaz_fiyat) if inp.dogalgaz_fiyat and inp.dogalgaz_fiyat > 0 else float(cfg.get("default_gas_tl_m3", 6.0))

        # Mevcut pencere eşleştirme
        win_mevcut = win_db.get(inp.mevcut_pencere) or next(iter(win_db.values()))
        u_mevcut = float(win_mevcut["u_value"])

        # Şimdilik demo çıktı (TS825 hesaplarını sonraki mesajda ekleyeceğiz)
        return {
            "config_used": {
                "default_gas_tl_m3": cfg.get("default_gas_tl_m3", 6.0),
                "pv_eff": cfg.get("pv_eff", 0.22),
                "rain_eff": cfg.get("rain_eff", 0.90),
                "roof_ratio": cfg.get("roof_ratio", 0.50),
            },
            "mevcut": {
                "pencere": inp.mevcut_pencere,
                "u_pencere": u_mevcut,
                "gaz_fiyat_tl_m3": gas_price
            },
            "ai_onerisi": {
                "note": "DB/admin altyapısı aktif. TS825+2050 hesap bloğunu bir sonraki adımda PDF’ye göre entegre edeceğiz.",
                "ornek_yalitim_sayisi": len(ins_db),
                "ornek_pencere_sayisi": len(win_db),
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# -------------------- RUN --------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
