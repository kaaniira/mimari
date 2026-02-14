import os
import time
import hmac
import hashlib
import base64
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

app.add_middleware(
    CORSMiddleware,
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
    # Firestore’a dokunur; izin yoksa burada 500 alırsın (ama deploy’u bozmaz)
    _ = load_catalog()
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

@app.put("/admin/api/gas_tariffs/{doc_id}")
def admin_put_gas(doc_id: str, item: GasTariffItem, _: bool = Depends(require_login)):
    payload = item.model_dump()
    payload["province"] = payload["province"].strip().upper()
    set_doc("gas_tariffs", doc_id, payload)
    return {"ok": True}

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

    return {
        "mevcut": {"pencere": win["name"], "u_pencere": win["u_value"], "gaz_fiyat": gas_price},
        "note": "Login + Firestore altyapısı hazır. TS825+2050 bloğunu bu analyze'e entegre edeceğiz."
    }

# -------------------- RUN --------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
