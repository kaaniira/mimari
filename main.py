import os
import time
import hmac
import hashlib
import base64
import json
import urllib.parse
import urllib.request
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

from google.cloud import firestore

# -------------------- CONFIG --------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "").strip() or None

ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "").strip()  # MUST set!

COOKIE_NAME = "cb_admin"
COOKIE_MAX_AGE_SEC = 60 * 60 * 8  # 8 saat

if not ADMIN_PASS:
    print("WARNING: ADMIN_PASS is empty (set it in Cloud Run env).")
if not ADMIN_SECRET:
    print("WARNING: ADMIN_SECRET is empty (set it in Cloud Run env).")

app = FastAPI(title="ChronoBuild Engine + Admin Login (Firestore)")
@@ -34,50 +37,53 @@ app.add_middleware(
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- HEALTH --------------------
@app.get("/health")
def health():
    return {"ok": True}

# -------------------- FIRESTORE LAZY CLIENT --------------------
_db_client: Optional[firestore.Client] = None

def db() -> firestore.Client:
    global _db_client
    if _db_client is None:
        _db_client = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()
    return _db_client

def _col(name: str):
    return db().collection(name)

def set_doc(collection: str, doc_id: str, payload: Dict[str, Any]):
    _col(collection).document(doc_id).set(payload, merge=True)

def delete_doc(collection: str, doc_id: str):
    _col(collection).document(doc_id).delete()

def get_all(collection: str) -> List[Dict[str, Any]]:
    out = []
    for d in _col(collection).stream():
        obj = d.to_dict() or {}
        obj["id"] = d.id
        out.append(obj)
    return out

def get_config() -> Dict[str, Any]:
    doc = _col("config").document("main").get()
    return (doc.to_dict() or {}) if doc.exists else {}

def ensure_defaults_once():
    cfg = get_config()
    if not cfg:
        set_doc("config", "main", {
            "default_gas_tl_m3": 6.0,
            "pv_eff": 0.22,
            "rain_eff": 0.90,
            "roof_ratio": 0.50
        })

    if not list(_col("insulations").limit(1).stream()):
        set_doc("insulations", "tas_yunu", {
            "name": "Taş Yünü (Sert)",
@@ -196,71 +202,184 @@ LOGIN_HTML = """

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
      <a href="/admin/logout" class="text-sm font-bold text-slate-700 underline">Çıkış</a>
    </div>

    <div class="bg-white border rounded-2xl p-6">
      <div class="font-bold mb-3">Mevcut Katalog</div>
      <pre id="catalog" class="text-xs bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-[420px]"></pre>
      <button onclick="loadCatalog()" class="mt-4 w-full bg-slate-700 text-white rounded-xl p-2 font-bold">Yenile</button>
    </div>

    <div class="grid md:grid-cols-2 gap-6">
      <div class="bg-white border rounded-2xl p-6 space-y-3">
        <div class="font-bold">Yalıtım Ekle / Güncelle</div>
        <input id="ins_doc" class="w-full border rounded-xl p-2" placeholder="doküman id (örn: tas_yunu)" />
        <input id="ins_name" class="w-full border rounded-xl p-2" placeholder="ad" />
        <input id="ins_lambda" type="number" step="0.001" class="w-full border rounded-xl p-2" placeholder="lambda" />
        <input id="ins_price" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="fiyat m3" />
        <input id="ins_carbon" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="karbon m3" />
        <label class="text-sm"><input id="ins_active" type="checkbox" checked /> aktif</label>
        <div class="grid grid-cols-2 gap-2">
          <button onclick="saveInsulation()" class="bg-slate-900 text-white rounded-xl p-2 font-bold">Kaydet</button>
          <button onclick="deleteInsulation()" class="bg-rose-600 text-white rounded-xl p-2 font-bold">Sil</button>
        </div>
      </div>

      <div class="bg-white border rounded-2xl p-6 space-y-3">
        <div class="font-bold">Pencere Ekle / Güncelle</div>
        <input id="win_doc" class="w-full border rounded-xl p-2" placeholder="doküman id" />
        <input id="win_name" class="w-full border rounded-xl p-2" placeholder="ad" />
        <input id="win_u" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="u değeri" />
        <input id="win_price" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="fiyat m2" />
        <input id="win_carbon" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="karbon m2" />
        <label class="text-sm"><input id="win_active" type="checkbox" checked /> aktif</label>
        <div class="grid grid-cols-2 gap-2">
          <button onclick="saveWindow()" class="bg-slate-900 text-white rounded-xl p-2 font-bold">Kaydet</button>
          <button onclick="deleteWindow()" class="bg-rose-600 text-white rounded-xl p-2 font-bold">Sil</button>
        </div>
      </div>
    </div>

    <div class="grid md:grid-cols-2 gap-6">
      <div class="bg-white border rounded-2xl p-6 space-y-3">
        <div class="font-bold">Doğal Gaz Tarifesi Ekle / Güncelle</div>
        <input id="gas_doc" class="w-full border rounded-xl p-2" placeholder="doküman id" />
        <input id="gas_province" class="w-full border rounded-xl p-2" placeholder="il adı" />
        <input id="gas_price" type="number" step="0.01" class="w-full border rounded-xl p-2" placeholder="TL/m3" />
        <label class="text-sm"><input id="gas_active" type="checkbox" checked /> aktif</label>
        <div class="grid grid-cols-2 gap-2">
          <button onclick="saveGas()" class="bg-slate-900 text-white rounded-xl p-2 font-bold">Kaydet</button>
          <button onclick="deleteGas()" class="bg-rose-600 text-white rounded-xl p-2 font-bold">Sil</button>
        </div>
      </div>

      <div class="bg-white border rounded-2xl p-6 space-y-3">
        <div class="font-bold">Koordinattan İl + Otomatik Gaz Fiyatı</div>
        <input id="lat" type="number" step="0.000001" class="w-full border rounded-xl p-2" placeholder="lat" />
        <input id="lng" type="number" step="0.000001" class="w-full border rounded-xl p-2" placeholder="lng" />
        <button onclick="resolveGas()" class="w-full bg-emerald-700 text-white rounded-xl p-2 font-bold">İli Bul ve Fiyatı Getir</button>
        <pre id="geo_result" class="text-xs bg-slate-100 p-3 rounded-xl overflow-auto"></pre>
      </div>
    </div>

    <div id="msg" class="text-sm font-bold"></div>
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
  }

  function flash(t, ok=true){
    const el = document.getElementById("msg");
    el.textContent = t;
    el.className = ok ? "text-sm font-bold text-emerald-700" : "text-sm font-bold text-rose-700";
  }

  function val(id){ return document.getElementById(id).value.trim(); }
  function boolVal(id){ return document.getElementById(id).checked; }
  function num(id){ return Number(document.getElementById(id).value); }

  async function saveInsulation(){
    try{
      await api(`/admin/api/insulations/${val("ins_doc")}`, "PUT", {
        name: val("ins_name"), lambda_value: num("ins_lambda"), price_m3: num("ins_price"), carbon_m3: num("ins_carbon"), active: boolVal("ins_active")
      });
      flash("Yalıtım kaydedildi"); loadCatalog();
    }catch(e){ flash(e.message, false); }
  }
  async function deleteInsulation(){
    try{ await api(`/admin/api/insulations/${val("ins_doc")}`, "DELETE"); flash("Yalıtım silindi"); loadCatalog(); }
    catch(e){ flash(e.message, false); }
  }
  async function saveWindow(){
    try{
      await api(`/admin/api/windows/${val("win_doc")}`, "PUT", {
        name: val("win_name"), u_value: num("win_u"), price_m2: num("win_price"), carbon_m2: num("win_carbon"), active: boolVal("win_active")
      });
      flash("Pencere kaydedildi"); loadCatalog();
    }catch(e){ flash(e.message, false); }
  }
  async function deleteWindow(){
    try{ await api(`/admin/api/windows/${val("win_doc")}`, "DELETE"); flash("Pencere silindi"); loadCatalog(); }
    catch(e){ flash(e.message, false); }
  }
  async function saveGas(){
    try{
      await api(`/admin/api/gas_tariffs/${val("gas_doc")}`, "PUT", {
        province: val("gas_province"), price_tl_m3: num("gas_price"), active: boolVal("gas_active")
      });
      flash("Gaz tarifesi kaydedildi"); loadCatalog();
    }catch(e){ flash(e.message, false); }
  }
  async function deleteGas(){
    try{ await api(`/admin/api/gas_tariffs/${val("gas_doc")}`, "DELETE"); flash("Gaz tarifesi silindi"); loadCatalog(); }
    catch(e){ flash(e.message, false); }
  }

  async function resolveGas(){
    try{
      const data = await api(`/admin/api/geo-gas?lat=${num("lat")}&lng=${num("lng")}`);
      document.getElementById("geo_result").textContent = JSON.stringify(data, null, 2);
      if(data.gas_price_tl_m3){
        document.getElementById("gas_province").value = data.province || "";
        document.getElementById("gas_price").value = data.gas_price_tl_m3;
      }
      flash("Koordinat sorgulandı");
    }catch(e){ flash(e.message, false); }
  }

  loadCatalog();
</script>
</body>
</html>
"""

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    # zaten login ise /admin'e yönlendir
    tok = request.cookies.get(COOKIE_NAME, "")
    if tok and verify_token(tok):
        return RedirectResponse("/admin", status_code=302)
    return LOGIN_HTML.format(error_block="")

@app.post("/admin/login")
def admin_login_post(username: str = Form(...), password: str = Form(...)):
    if username != ADMIN_USER or password != ADMIN_PASS:
        err = '<div class="mt-4 bg-rose-50 border border-rose-200 text-rose-700 rounded-xl p-3 text-sm font-bold">Hatalı kullanıcı/şifre</div>'
        return HTMLResponse(LOGIN_HTML.format(error_block=err), status_code=401)

    exp = int(time.time()) + COOKIE_MAX_AGE_SEC
    tok = sign_token(username, exp)

    resp = RedirectResponse("/admin", status_code=302)
    resp.set_cookie(
@@ -309,65 +428,122 @@ class WindowItem(BaseModel):

class GasTariffItem(BaseModel):
    province: str
    price_tl_m3: float = Field(..., gt=0)
    active: bool = True

@app.get("/admin/api/catalog")
def admin_get_catalog(_: bool = Depends(require_login)):
    return load_catalog()

@app.put("/admin/api/config")
def admin_put_config(item: ConfigItem, _: bool = Depends(require_login)):
    set_doc("config", "main", item.model_dump())
    return {"ok": True}

@app.put("/admin/api/insulations/{doc_id}")
def admin_put_insulation(doc_id: str, item: InsulationItem, _: bool = Depends(require_login)):
    set_doc("insulations", doc_id, item.model_dump())
    return {"ok": True}

@app.put("/admin/api/windows/{doc_id}")
def admin_put_window(doc_id: str, item: WindowItem, _: bool = Depends(require_login)):
    set_doc("windows", doc_id, item.model_dump())
    return {"ok": True}

@app.delete("/admin/api/windows/{doc_id}")
def admin_delete_window(doc_id: str, _: bool = Depends(require_login)):
    delete_doc("windows", doc_id)
    return {"ok": True}

@app.put("/admin/api/gas_tariffs/{doc_id}")
def admin_put_gas(doc_id: str, item: GasTariffItem, _: bool = Depends(require_login)):
    payload = item.model_dump()
    payload["province"] = payload["province"].strip().upper()
    set_doc("gas_tariffs", doc_id, payload)
    return {"ok": True}

@app.delete("/admin/api/insulations/{doc_id}")
def admin_delete_insulation(doc_id: str, _: bool = Depends(require_login)):
    delete_doc("insulations", doc_id)
    return {"ok": True}

@app.delete("/admin/api/gas_tariffs/{doc_id}")
def admin_delete_gas(doc_id: str, _: bool = Depends(require_login)):
    delete_doc("gas_tariffs", doc_id)
    return {"ok": True}

def province_from_coords(lat: float, lng: float) -> Optional[str]:
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode({
        "lat": lat,
        "lon": lng,
        "format": "jsonv2",
        "accept-language": "tr",
        "zoom": 10,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "chronobuild-admin/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

    addr = data.get("address", {})
    province = addr.get("state") or addr.get("province") or addr.get("city")
    if not province:
        return None
    return str(province).strip().upper()

@app.get("/admin/api/geo-gas")
def admin_geo_gas(lat: float, lng: float, _: bool = Depends(require_login)):
    province = province_from_coords(lat, lng)
    if not province:
        raise HTTPException(404, "Koordinattan il bulunamadı")

    cat = load_catalog()
    price = cat["gas_by_province"].get(province)
    return {
        "province": province,
        "gas_price_tl_m3": price,
        "fallback_default": float(cat["config"].get("default_gas_tl_m3", 6.0)),
    }

# -------------------- ANALYZE (şimdilik test) --------------------
class AnalyzeInput(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float = 2.8
    dogalgaz_fiyat: float = 0.0
    senaryo: str
    mevcut_pencere: str

    class Config:
        extra = "ignore"

@app.post("/analyze")
def analyze(inp: AnalyzeInput):
    cat = load_catalog()
    cfg = cat["config"]
    win_db = cat["windows"]

    win = win_db.get(inp.mevcut_pencere) or next(iter(win_db.values()))
    gas_price = inp.dogalgaz_fiyat if inp.dogalgaz_fiyat > 0 else float(cfg.get("default_gas_tl_m3", 6.0))
    province = province_from_coords(inp.lat, inp.lng)
    gas_by_province = cat["gas_by_province"]

    gas_price = inp.dogalgaz_fiyat
    if gas_price <= 0 and province:
        gas_price = float(gas_by_province.get(province, 0))
    if gas_price <= 0:
        gas_price = float(cfg.get("default_gas_tl_m3", 6.0))

    return {
        "mevcut": {"pencere": win["name"], "u_pencere": win["u_value"], "gaz_fiyat": gas_price},
        "mevcut": {"pencere": win["name"], "u_pencere": win["u_value"], "gaz_fiyat": gas_price, "il": province},
        "note": "Login + Firestore altyapısı hazır. TS825+2050 bloğunu bu analyze'e entegre edeceğiz."
    }

# -------------------- RUN --------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
