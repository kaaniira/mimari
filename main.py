import os
import time
import hmac
import hashlib
import base64
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import date, timedelta
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from google.cloud import firestore

# -------------------- CONFIG --------------------
PROJECT_ID = (
    os.environ.get("PROJECT_ID", "").strip()
    or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    or os.environ.get("GCP_PROJECT", "").strip()
    or os.environ.get("GCLOUD_PROJECT", "").strip()
    or None
)

DETECTED_PROJECT_ID: Optional[str] = None

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


DEFAULT_CONFIG = {
    "default_gas_tl_m3": 6.0,
    "pv_eff": 0.22,
    "rain_eff": 0.90,
    "roof_ratio": 0.50,
}

DEFAULT_WINDOW = {
    "name": "Çift Cam (Isıcam S)",
    "u_value": 2.8,
    "price_m2": 3200,
    "carbon_m2": 25,
    "active": True,
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- HEALTH --------------------


@app.get("/", response_class=HTMLResponse)
def frontend_page():
    fp = Path(__file__).with_name("frontend.html")
    if not fp.exists():
        raise HTTPException(404, "frontend.html bulunamadı")
    return HTMLResponse(fp.read_text(encoding="utf-8"))

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
            "lambda_value": 0.035,
            "price_m3": 2800,
            "carbon_m3": 150,
            "active": True
        })

    if not list(_col("windows").limit(1).stream()):
        set_doc("windows", "cift_cam", {
            "name": "Çift Cam (Isıcam S)",
            "u_value": 2.8,
            "price_m2": 3200,
            "carbon_m2": 25,
            "active": True
        })

def load_catalog() -> Dict[str, Any]:
    ensure_defaults_once()

    cfg = get_config()
    ins = [x for x in get_all("insulations") if x.get("active", True)]
    win = [x for x in get_all("windows") if x.get("active", True)]
    gas = [x for x in get_all("gas_tariffs") if x.get("active", True)]

    ins_map = {x["name"]: x for x in ins if x.get("name") and x.get("lambda_value")}
    win_map = {x["name"]: x for x in win if x.get("name") and x.get("u_value")}
    gas_map = {str(x.get("province", "")).strip().upper(): float(x.get("price_tl_m3", 0)) for x in gas}

    return {
        "config": cfg,
        "insulations": ins_map,
        "windows": win_map,
        "gas_by_province": gas_map,
    }


def fallback_catalog() -> Dict[str, Any]:
    return {
        "config": dict(DEFAULT_CONFIG),
        "insulations": {},
        "windows": {DEFAULT_WINDOW["name"]: dict(DEFAULT_WINDOW)},
        "gas_by_province": {},
    }


def require_firestore():
    try:
        _ = db()
    except Exception as exc:
        raise HTTPException(503, f"Firestore bağlantısı yok: {exc}")

# -------------------- SESSION TOKEN (stdlib) --------------------
def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def sign_token(user: str, exp_ts: int) -> str:
    """
    token = base64url("user|exp|sighex")
    sig = HMAC_SHA256(secret, "user|exp")
    """
    if not ADMIN_SECRET:
        # SECRET yoksa güvenli değil; yine de çalışsın diye exception yerine engelleyelim:
        raise HTTPException(500, "ADMIN_SECRET env eksik. Cloud Run env'e ekleyin.")
    msg = f"{user}|{exp_ts}".encode("utf-8")
    sig = hmac.new(ADMIN_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    raw = f"{user}|{exp_ts}|{sig}".encode("utf-8")
    return _b64u(raw)

def verify_token(token: str) -> Optional[str]:
    try:
        raw = _b64u_decode(token).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 3:
            return None
        user, exp_s, sig = parts
        exp = int(exp_s)
        if exp < int(time.time()):
            return None
        msg = f"{user}|{exp}".encode("utf-8")
        expected = hmac.new(ADMIN_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return user
    except Exception:
        return None

def require_login(request: Request):
    tok = request.cookies.get(COOKIE_NAME, "")
    if not tok:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = verify_token(tok)
    if not user:
        raise HTTPException(status_code=401, detail="Session invalid/expired")
    return True

# -------------------- ADMIN PAGES --------------------
LOGIN_HTML = """
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>ChronoBuild Admin Login</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen flex items-center justify-center p-6">
  <div class="w-full max-w-md bg-white border rounded-2xl p-6 shadow-sm">
    <div class="text-xl font-black">ChronoBuild Admin</div>
    <div class="text-xs text-slate-500 mt-1">Giriş yap</div>

    {error_block}

    <form method="post" action="/admin/login" class="mt-4 space-y-3">
      <label class="block text-sm font-bold text-slate-600">Kullanıcı</label>
      <input name="username" class="w-full border rounded-xl p-3" placeholder="admin" required />

      <label class="block text-sm font-bold text-slate-600">Şifre</label>
      <input name="password" type="password" class="w-full border rounded-xl p-3" placeholder="••••••••" required />

      <button class="w-full bg-slate-900 text-white rounded-xl p-3 font-bold">Giriş</button>
    </form>
  </div>
</body>
</html>
"""

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
        key=COOKIE_NAME,
        value=tok,
        max_age=COOKIE_MAX_AGE_SEC,
        httponly=True,
        secure=True,        # Cloud Run HTTPS
        samesite="lax",
        path="/",
    )
    return resp

@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: bool = Depends(require_login)):
    return ADMIN_HTML

# -------------------- ADMIN API --------------------
class ConfigItem(BaseModel):
    default_gas_tl_m3: float = Field(6.0, gt=0)
    pv_eff: float = Field(0.22, gt=0, le=1)
    rain_eff: float = Field(0.90, gt=0, le=1)
    roof_ratio: float = Field(0.50, gt=0, le=1)

class InsulationItem(BaseModel):
    name: str
    lambda_value: float = Field(..., gt=0)
    price_m3: float = Field(..., ge=0)
    carbon_m3: float = Field(..., ge=0)
    active: bool = True

class WindowItem(BaseModel):
    name: str
    u_value: float = Field(..., gt=0)
    price_m2: float = Field(0, ge=0)
    carbon_m2: float = Field(0, ge=0)
    active: bool = True

class GasTariffItem(BaseModel):
    province: str
    price_tl_m3: float = Field(..., gt=0)
    active: bool = True

@app.get("/admin/api/catalog")
def admin_get_catalog(_: bool = Depends(require_login)):
    require_firestore()
    return load_catalog()

@app.put("/admin/api/config")
def admin_put_config(item: ConfigItem, _: bool = Depends(require_login)):
    require_firestore()
    set_doc("config", "main", item.model_dump())
    return {"ok": True}

@app.put("/admin/api/insulations/{doc_id}")
def admin_put_insulation(doc_id: str, item: InsulationItem, _: bool = Depends(require_login)):
    require_firestore()
    set_doc("insulations", doc_id, item.model_dump())
    return {"ok": True}

@app.put("/admin/api/windows/{doc_id}")
def admin_put_window(doc_id: str, item: WindowItem, _: bool = Depends(require_login)):
    require_firestore()
    set_doc("windows", doc_id, item.model_dump())
    return {"ok": True}

@app.delete("/admin/api/windows/{doc_id}")
def admin_delete_window(doc_id: str, _: bool = Depends(require_login)):
    require_firestore()
    delete_doc("windows", doc_id)
    return {"ok": True}

@app.put("/admin/api/gas_tariffs/{doc_id}")
def admin_put_gas(doc_id: str, item: GasTariffItem, _: bool = Depends(require_login)):
    require_firestore()
    payload = item.model_dump()
    payload["province"] = payload["province"].strip().upper()
    set_doc("gas_tariffs", doc_id, payload)
    return {"ok": True}

@app.delete("/admin/api/insulations/{doc_id}")
def admin_delete_insulation(doc_id: str, _: bool = Depends(require_login)):
    require_firestore()
    delete_doc("insulations", doc_id)
    return {"ok": True}

@app.delete("/admin/api/gas_tariffs/{doc_id}")
def admin_delete_gas(doc_id: str, _: bool = Depends(require_login)):
    require_firestore()
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


TS825_U_WALL_MAX_BY_ZONE = {
    1: 0.70,
    2: 0.60,
    3: 0.50,
    4: 0.40,
}

TS825_ZONE_BY_PROVINCE = {
    "ADANA": 1, "ANTALYA": 1, "MERSIN": 1, "HATAY": 1, "MUĞLA": 1, "MUGLA": 1, "AYDIN": 1, "İZMİR": 1, "IZMIR": 1,
    "TEKİRDAĞ": 2, "TEKIRDAG": 2, "BALIKESİR": 2, "BALIKESIR": 2, "BURSA": 2, "İSTANBUL": 2, "ISTANBUL": 2,
    "ANKARA": 3, "ESKİŞEHİR": 3, "ESKISEHIR": 3, "KONYA": 3, "SAMSUN": 3, "TRABZON": 3,
    "ERZURUM": 4, "KARS": 4, "ARDAHAN": 4, "AĞRI": 4, "AGRI": 4, "SİVAS": 4, "SIVAS": 4,
}

DEFAULT_INSULATIONS = [
    {"name": "Taş Yünü (Sert)", "lambda_value": 0.035, "price_m3": 2800.0, "carbon_m3": 150.0, "active": True},
    {"name": "EPS", "lambda_value": 0.038, "price_m3": 2100.0, "carbon_m3": 95.0, "active": True},
    {"name": "XPS", "lambda_value": 0.032, "price_m3": 3300.0, "carbon_m3": 180.0, "active": True},
]

DEFAULT_WINDOWS = [
    {"name": "Tek Cam (Standart)", "u_value": 5.8, "price_m2": 1200.0, "carbon_m2": 18.0, "active": True},
    {"name": "Çift Cam (Isıcam S)", "u_value": 2.8, "price_m2": 3200.0, "carbon_m2": 25.0, "active": True},
    {"name": "Üçlü Cam (Isıcam K)", "u_value": 1.6, "price_m2": 4600.0, "carbon_m2": 34.0, "active": True},
]


def ts825_zone_for_province(province: Optional[str]) -> int:
    if not province:
        return 3
    return TS825_ZONE_BY_PROVINCE.get(province.upper(), 3)


def round_up_5(value_cm: float) -> int:
    """Round up thickness (cm) to nearest 5 cm step."""
    v = float(value_cm or 0)
    if v <= 0:
        return 5
    return int(math.ceil(v / 5.0) * 5)


def get_insulations(cat: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return active insulation list; fallback to defaults when catalog is empty/broken."""
    items = cat.get("insulations") or {}
    src = items.values() if isinstance(items, dict) else items

    out: List[Dict[str, Any]] = []
    for raw in src:
        if not isinstance(raw, dict) or not raw.get("active", True):
            continue
        name = str(raw.get("name", "")).strip()
        lam = raw.get("lambda_value")
        if not name or lam in (None, ""):
            continue
        out.append({
            "name": name,
            "lambda_value": float(lam),
            "price_m3": float(raw.get("price_m3", 0) or 0),
            "carbon_m3": float(raw.get("carbon_m3", 0) or 0),
        })

    if not out:
        out = [dict(x) for x in DEFAULT_INSULATIONS if x.get("active", True)]

    return out


def get_windows(cat: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return active window list; fallback to defaults when catalog is empty/broken."""
    items = cat.get("windows") or {}
    src = items.values() if isinstance(items, dict) else items

    out: List[Dict[str, Any]] = []
    for raw in src:
        if not isinstance(raw, dict) or not raw.get("active", True):
            continue
        name = str(raw.get("name", "")).strip()
        u_val = raw.get("u_value")
        if not name or u_val in (None, ""):
            continue
        out.append({
            "name": name,
            "u_value": float(u_val),
            "price_m2": float(raw.get("price_m2", 0) or 0),
            "carbon_m2": float(raw.get("carbon_m2", 0) or 0),
        })

    if not out:
        out = [dict(x) for x in DEFAULT_WINDOWS if x.get("active", True)]

    return out


_ee_inited = False
_ee_last_error: Optional[str] = None


def _set_ee_error(exc: Exception):
    global _ee_last_error
    _ee_last_error = f"{exc.__class__.__name__}: {exc}"


def _get_ee_error() -> Optional[str]:
    return _ee_last_error


def _resolve_project_id() -> Optional[str]:
    global DETECTED_PROJECT_ID
    if DETECTED_PROJECT_ID:
        return DETECTED_PROJECT_ID
    if PROJECT_ID:
        DETECTED_PROJECT_ID = PROJECT_ID
        return DETECTED_PROJECT_ID

    # Cloud Run metadata fallback
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            pid = r.read().decode("utf-8").strip()
            if pid:
                DETECTED_PROJECT_ID = pid
                return DETECTED_PROJECT_ID
    except Exception:
        pass

    return None



def _init_earth_engine() -> bool:
    global _ee_inited, _ee_last_error
    if _ee_inited:
        return True
    try:
        import ee  # type: ignore
        pid = _resolve_project_id()
        if pid:
            ee.Initialize(project=pid)
        else:
            ee.Initialize()
        _ee_inited = True
        _ee_last_error = None
        return True
    except Exception as exc:
        _set_ee_error(exc)
        return False


GEE_MODEL_CANDIDATES = [
    "MRI-ESM2-0",
    "IPSL-CM6A-LR",
    "MIROC6",
    "CNRM-CM6-1",
    "GFDL-ESM4",
]


def fetch_gee_cmip6_2050(lat: float, lng: float, scenario: str) -> Optional[Dict[str, float]]:
    if not _init_earth_engine():
        return None
    try:
        import ee  # type: ignore
        point = ee.Geometry.Point([lng, lat])
        scn = (scenario or "ssp245").lower()

        # Önce tek yıl (2050), veri çıkmazsa aynı senaryoda 20 yıllık pencere dene.
        windows = [
            ("2050-01-01", "2050-12-31", "2050"),
            ("2041-01-01", "2060-12-31", "2041-2060"),
        ]

        last_size = None
        for start_date, end_date, window_name in windows:
            base = (
                ee.ImageCollection("NASA/GDDP-CMIP6")
                .filterDate(start_date, end_date)
                .filter(ee.Filter.eq("scenario", scn))
            )
            # Tanı için koleksiyon boyutu
            try:
                last_size = base.size().getInfo()
            except Exception:
                last_size = None

            for model in GEE_MODEL_CANDIDATES + [None]:
                coll = base.filter(ee.Filter.eq("model", model)) if model else base
                img = coll.select(["tas", "pr", "rsds"]).mean()
                vals = img.reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=point, scale=27830, maxPixels=1e9
                ).getInfo() or {}

                tas_k = vals.get("tas")
                if tas_k is None:
                    continue

                pr_kg_m2_s = vals.get("pr")
                rsds_w_m2 = vals.get("rsds")
                temp_mean_c = float(tas_k) - 273.15
                hdd = max(0.0, (18.0 - temp_mean_c) * 365.0)
                return {
                    "hdd": round(hdd, 3),
                    "yagis_mm": None if pr_kg_m2_s is None else round(float(pr_kg_m2_s) * 86400 * 365, 3),
                    "gunes_kwh_m2": None if rsds_w_m2 is None else round(float(rsds_w_m2) * 24 * 365 / 1000, 3),
                    "temp_mean_c": round(temp_mean_c, 3),
                    "kaynak": "gee-cmip6",
                    "senaryo": scn,
                    "model": model or "ENSEMBLE_MEAN",
                    "zaman_penceresi": window_name,
                }

        _set_ee_error(RuntimeError(
            f"GEE reduceRegion sonucu bos (tas yok), scenario={scn}, collection_size={last_size}"
        ))
        return None
    except Exception as exc:
        _set_ee_error(exc)
        return None


def fetch_gee_2050(lat: float, lng: float, scenario: str, zone: int) -> Dict[str, float]:
    # Open-Meteo tamamen kaldırıldı; 2050 verisi yalnızca GEE CMIP6'dan alınır.
    gee_data = fetch_gee_cmip6_2050(lat, lng, scenario)
    if gee_data:
        return gee_data

    return {
        "hdd": None,
        "yagis_mm": None,
        "gunes_kwh_m2": None,
        "temp_mean_c": None,
        "kaynak": "veri yok",
        "hata": _get_ee_error(),
        "project_id": _resolve_project_id(),
        "senaryo": scenario,
        "model": None,
    }


def fetch_gee_current(lat: float, lng: float, zone: int) -> Dict[str, float]:
    # Open-Meteo tamamen kaldırıldı; güncel iklim için GEE CMIP6 historical yaklaşımı.
    if not _init_earth_engine():
        return {
            "hdd": None,
            "yagis_mm": None,
            "gunes_kwh_m2": None,
            "temp_mean_c": None,
            "kaynak": "veri yok",
            "hata": _get_ee_error(),
            "project_id": _resolve_project_id(),
        }

    try:
        import ee  # type: ignore
        point = ee.Geometry.Point([lng, lat])
        base = (
            ee.ImageCollection("NASA/GDDP-CMIP6")
            .filterDate("2005-01-01", "2014-12-31")
            .filter(ee.Filter.eq("scenario", "historical"))
        )

        for model in GEE_MODEL_CANDIDATES + [None]:
            coll = base.filter(ee.Filter.eq("model", model)) if model else base
            img = coll.select(["tas", "pr", "rsds"]).mean()
            vals = img.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=point, scale=27830, maxPixels=1e9
            ).getInfo() or {}

            tas_k = vals.get("tas")
            if tas_k is None:
                continue

            pr_kg_m2_s = vals.get("pr")
            rsds_w_m2 = vals.get("rsds")
            temp_mean_c = float(tas_k) - 273.15
            hdd = max(0.0, (18.0 - temp_mean_c) * 365.0)
            return {
                "hdd": round(hdd, 3),
                "yagis_mm": None if pr_kg_m2_s is None else round(float(pr_kg_m2_s) * 86400 * 365, 3),
                "gunes_kwh_m2": None if rsds_w_m2 is None else round(float(rsds_w_m2) * 24 * 365 / 1000, 3),
                "temp_mean_c": round(temp_mean_c, 3),
                "kaynak": "gee-cmip6-historical",
                "model": model or "ENSEMBLE_MEAN",
            }

        return {
            "hdd": None,
            "yagis_mm": None,
            "gunes_kwh_m2": None,
            "temp_mean_c": None,
            "kaynak": "veri yok",
            "hata": "EEException: GEE reduceRegion sonucu bos (tas yok).",
            "project_id": _resolve_project_id(),
        }
    except Exception as exc:
        _set_ee_error(exc)
        return {
            "hdd": None,
            "yagis_mm": None,
            "gunes_kwh_m2": None,
            "temp_mean_c": None,
            "kaynak": "veri yok",
            "hata": _get_ee_error(),
            "project_id": _resolve_project_id(),
        }


def calc_building_metrics(taban_alani: float, kat_sayisi: int, kat_yuksekligi: float) -> Dict[str, float]:
    """Simple geometric approximation for wall/window/roof areas."""
    base = max(20.0, float(taban_alani or 120.0))
    floors = max(1, int(kat_sayisi or 1))
    h = max(2.4, float(kat_yuksekligi or 2.8))

    side = math.sqrt(base)
    perimeter = 4.0 * side
    gross_wall = perimeter * (floors * h)
    window_ratio = 0.20
    window_area = gross_wall * window_ratio
    net_wall = max(1.0, gross_wall - window_area)

    return {
        "base_area_m2": round(base, 3),
        "total_floor_area_m2": round(base * floors, 3),
        "roof_area_m2": round(base, 3),
        "wall_area_m2": round(net_wall, 3),
        "window_area_m2": round(window_area, 3),
    }


def evaluate_option(ins: Dict[str, Any], win: Dict[str, Any], metrics: Dict[str, float], climate_2050: Dict[str, float], gas_price: float, r_base: float) -> Dict[str, float]:
    """Evaluate one insulation + window option with coarse engineering assumptions."""
    wall_area = float(metrics["wall_area_m2"])
    window_area = float(metrics["window_area_m2"])
    hdd = float(climate_2050.get("hdd", 0) or 0)

    lam = max(0.01, float(ins.get("lambda_value", 0.04) or 0.04))
    t_cm = max(1.0, float(ins.get("kalinlik_cm", 5) or 5))
    t_m = t_cm / 100.0
    u_wall = 1.0 / max(0.05, float(r_base) + (t_m / lam))

    u_win = max(0.8, float(win.get("u_value", 2.8) or 2.8))

    ua = u_wall * wall_area + u_win * window_area
    annual_heat_kwh = ua * hdd * 24.0 / 1000.0

    system_eff = 0.90
    kwh_per_m3 = 9.5
    yillik_gaz_m3 = max(0.0, annual_heat_kwh / (kwh_per_m3 * system_eff))
    yillik_tutar_tl = yillik_gaz_m3 * max(0.0, float(gas_price or 0))
    yillik_co2_kg = yillik_gaz_m3 * 1.90

    ins_volume_m3 = wall_area * t_m
    yatirim_tl = ins_volume_m3 * float(ins.get("price_m3", 0) or 0) + window_area * float(win.get("price_m2", 0) or 0)
    embodied_co2_kg = ins_volume_m3 * float(ins.get("carbon_m3", 0) or 0) + window_area * float(win.get("carbon_m2", 0) or 0)

    return {
        "yillik_gaz_m3": round(yillik_gaz_m3, 1),
        "yillik_tutar_tl": round(yillik_tutar_tl, 1),
        "yillik_co2_kg": round(yillik_co2_kg, 1),
        "yatirim_tl": round(yatirim_tl, 1),
        "embodied_co2_kg": round(embodied_co2_kg, 1),
    }


@app.get("/climate/current")
def climate_current(lat: float, lng: float):
    province = province_from_coords(lat, lng)
    zone = ts825_zone_for_province(province)
    current = fetch_gee_current(lat, lng, zone)
    return {
        "province": province,
        "zone": zone,
        "current": current,
    }

class AnalyzeInput(BaseModel):
    lat: float
    lng: float
    taban_alani: float
    kat_sayisi: int
    kat_yuksekligi: float = 2.8
    dogalgaz_fiyat: float = 0.0
    senaryo: str = "ssp245"
    mevcut_pencere: str = "Çift Cam (Isıcam S)"
    r_base_layers: float = 0.50

    class Config:
        extra = "ignore"


@app.post("/analyze")
def analyze(inp: AnalyzeInput):
    try:
        cat = load_catalog()
    except Exception:
        cat = fallback_catalog()

    cfg = cat.get("config", DEFAULT_CONFIG)
    province = province_from_coords(inp.lat, inp.lng)
    zone = ts825_zone_for_province(province)
    u_wall_max = TS825_U_WALL_MAX_BY_ZONE[zone]

    gas_by_province = cat.get("gas_by_province", {})
    gas_price = inp.dogalgaz_fiyat
    if gas_price <= 0 and province:
        gas_price = float(gas_by_province.get(province, 0))
    if gas_price <= 0:
        gas_price = float(cfg.get("default_gas_tl_m3", 6.0))

    windows = get_windows(cat)
    win_current = next((w for w in windows if w.get("name") == inp.mevcut_pencere), windows[0])
    win_best = min(windows, key=lambda w: float(w.get("u_value", 99)))

    insulations = get_insulations(cat)
    r_base = max(0.05, inp.r_base_layers)
    for ins in insulations:
        req_t_m = max(0.01, (1.0 / u_wall_max - r_base) * float(ins["lambda_value"]))
        ins["kalinlik_cm"] = round_up_5(req_t_m * 100.0)

    climate_current = fetch_gee_current(inp.lat, inp.lng, zone)
    climate_2050 = fetch_gee_2050(inp.lat, inp.lng, inp.senaryo, zone)

    if climate_2050.get("hdd") is None:
        return {
            "mevcut": {
                "province": province,
                "ts825_zone": zone,
                "u_wall_max": u_wall_max,
                "yalitim": "veri yok",
                "kalinlik_cm": None,
                "pencere": win_current.get("name", "-"),
                "y2050": {
                    "yillik_gaz_m3": None,
                    "yillik_tutar_tl": None,
                    "yillik_co2_kg": None,
                },
            },
            "ai_onerisi": {
                "yalitim": "veri yok",
                "kalinlik_cm": None,
                "pencere": "veri yok",
                "y2050": {
                    "yillik_gaz_m3": None,
                    "yillik_tutar_tl": None,
                    "yillik_co2_kg": None,
                },
                "pb_eco_yil": None,
                "pb_carb_yil": None,
                "max_pb_eco_yil": 10,
                "tasarruf": {
                    "yillik_tasarruf_tl": None,
                    "yillik_gaz_tasarruf_m3": None,
                    "yillik_co2_tasarruf_kg": None,
                },
                "yatirim": {
                    "yatirim_tl": None,
                    "embodied_co2_kg": None,
                },
                "su_hasadi_m3_yil": None,
                "pv_kwh_yil": None,
                "uyari": "2050 iklim verisi alınamadı (veri yok).",
                "alternatif_oneri": None,
            },
            "iklim_info": {
                "senaryo": inp.senaryo,
                "kaynak": {
                    "current": climate_current.get("kaynak", "veri yok"),
                    "y2050": climate_2050.get("kaynak", "veri yok"),
                },
                "istenen_senaryo": inp.senaryo,
                "kullanilan_senaryo": climate_2050.get("senaryo", inp.senaryo),
                "kullanilan_model": climate_2050.get("model"),
                "gee_hata": climate_2050.get("hata") or climate_current.get("hata"),
                "gee_project_id": climate_2050.get("project_id") or climate_current.get("project_id") or _resolve_project_id(),
                "current": climate_current,
                "guncel": climate_current,
                "y2050": climate_2050,
            },
            "ai_notu": "API veri sağlayamadığı alanlarda değerler 'veri yok' olarak döndürüldü.",
        }

    metrics = calc_building_metrics(inp.taban_alani, inp.kat_sayisi, inp.kat_yuksekligi)
    roof_area_eff = metrics["roof_area_m2"] * float(cfg.get("roof_ratio", 0.50))

    # TS825 baz çözüm: en düşük yatırım maliyetli yalıtım + kullanıcının mevcut penceresi
    base_candidates = []
    for ins in insulations:
        calc = evaluate_option(ins, win_current, metrics, climate_2050, gas_price, r_base)
        base_candidates.append((ins, calc))
    base_ins, base_calc = min(base_candidates, key=lambda x: x[1]["yatirim_tl"])

    # AI çözüm: yalıtım + en iyi pencere kombinasyonları içinde hedefleri sağlayan en iyi opsiyon
    ai_candidates = []
    for ins in insulations:
        for w in [win_current, win_best]:
            calc = evaluate_option(ins, w, metrics, climate_2050, gas_price, r_base)
            save_tl = max(0.0, base_calc["yillik_tutar_tl"] - calc["yillik_tutar_tl"])
            save_gas = max(0.0, base_calc["yillik_gaz_m3"] - calc["yillik_gaz_m3"])
            save_co2 = max(0.0, base_calc["yillik_co2_kg"] - calc["yillik_co2_kg"])
            pb_eco = (calc["yatirim_tl"] / save_tl) if save_tl > 0 else 99.0
            pb_carb = (calc["embodied_co2_kg"] / save_co2) if save_co2 > 0 else 99.0
            ai_candidates.append({
                "ins": ins,
                "win": w,
                "calc": calc,
                "save_tl": round(save_tl, 1),
                "save_gas": round(save_gas, 1),
                "save_co2": round(save_co2, 1),
                "pb_eco": round(pb_eco, 1),
                "pb_carb": round(pb_carb, 1),
            })

    feasible = [x for x in ai_candidates if x["pb_eco"] <= 10 and x["pb_carb"] <= 5]
    if feasible:
        ai = max(feasible, key=lambda x: x["save_tl"])
    else:
        ai = min(ai_candidates, key=lambda x: x["pb_eco"])

    alt = None
    if ai["pb_eco"] > 10:
        alt = min(ai_candidates, key=lambda x: (x["pb_eco"], -x["save_tl"]))

    pv_kwh = round(roof_area_eff * climate_2050["gunes_kwh_m2"] * float(cfg.get("pv_eff", 0.22)), 1)
    su_hasadi = round(metrics["roof_area_m2"] * (climate_2050["yagis_mm"] / 1000.0) * float(cfg.get("rain_eff", 0.90)), 1)

    resp = {
        "mevcut": {
            "province": province,
            "ts825_zone": zone,
            "u_wall_max": u_wall_max,
            "yalitim": base_ins["name"],
            "kalinlik_cm": int(base_ins["kalinlik_cm"]),
            "pencere": win_current.get("name", "-"),
            "y2050": {
                "yillik_gaz_m3": base_calc["yillik_gaz_m3"],
                "yillik_tutar_tl": base_calc["yillik_tutar_tl"],
                "yillik_co2_kg": base_calc["yillik_co2_kg"],
            },
        },
        "ai_onerisi": {
            "yalitim": ai["ins"]["name"],
            "kalinlik_cm": int(ai["ins"]["kalinlik_cm"]),
            "pencere": ai["win"].get("name", "-"),
            "y2050": {
                "yillik_gaz_m3": ai["calc"]["yillik_gaz_m3"],
                "yillik_tutar_tl": ai["calc"]["yillik_tutar_tl"],
                "yillik_co2_kg": ai["calc"]["yillik_co2_kg"],
            },
            "pb_eco_yil": ai["pb_eco"],
            "pb_carb_yil": ai["pb_carb"],
            "max_pb_eco_yil": 10,
            "tasarruf": {
                "yillik_tasarruf_tl": ai["save_tl"],
                "yillik_gaz_tasarruf_m3": ai["save_gas"],
                "yillik_co2_tasarruf_kg": ai["save_co2"],
            },
            "yatirim": {
                "yatirim_tl": ai["calc"]["yatirim_tl"],
                "embodied_co2_kg": ai["calc"]["embodied_co2_kg"],
            },
            "su_hasadi_m3_yil": su_hasadi,
            "pv_kwh_yil": pv_kwh,
            "uyari": "10 yıl ekonomik geri ödeme hedefi tutmadı, alternatif gösteriliyor." if ai["pb_eco"] > 10 else None,
            "alternatif_oneri": None,
        },
        "iklim_info": {
            "senaryo": inp.senaryo,
            "senaryo_notu": "Senaryolar arası fark 2050 yılı ham GEE CMIP6 verisinden gelir; veri yoksa değer üretilmez.",
            "kaynak": {
                "current": climate_current.get("kaynak", "veri yok"),
                "y2050": climate_2050.get("kaynak", "veri yok"),
            },
            "gee_hata": climate_2050.get("hata") or climate_current.get("hata"),
            "gee_project_id": climate_2050.get("project_id") or climate_current.get("project_id") or _resolve_project_id(),
            "current": {
                "hdd": climate_current["hdd"],
                "yagis_mm": climate_current["yagis_mm"],
                "gunes_kwh_m2": climate_current["gunes_kwh_m2"],
                "temp_mean_c": climate_current["temp_mean_c"],
            },
            "guncel": {
                "hdd": climate_current["hdd"],
                "yagis_mm": climate_current["yagis_mm"],
                "gunes_kwh_m2": climate_current["gunes_kwh_m2"],
                "temp_mean_c": climate_current["temp_mean_c"],
            },
            "y2050": {
                "hdd": climate_2050["hdd"],
                "yagis_mm": climate_2050["yagis_mm"],
                "gunes_kwh_m2": climate_2050["gunes_kwh_m2"],
                "temp_mean_c": climate_2050["temp_mean_c"],
            },
        },
        "ai_notu": "AI önerisi, TS825 limitini sağlayan seçenekler içinde 2050 senaryosuna göre optimize edilmiştir.",
    }

    if alt is not None:
        resp["ai_onerisi"]["alternatif_oneri"] = {
            "tip": "Maliyet-optimum kombinasyon",
            "yalitim": alt["ins"]["name"],
            "kalinlik_cm": int(alt["ins"]["kalinlik_cm"]),
            "pencere": alt["win"].get("name", "-"),
            "pb_eco_yil": alt["pb_eco"],
            "yatirim_tl": alt["calc"]["yatirim_tl"],
            "yillik_tasarruf_tl": alt["save_tl"],
        }

    return resp

# -------------------- RUN --------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
