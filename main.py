import os
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# Firestore import (paket yoksa burada crash eder -> requirements şart)
from google.cloud import firestore


PROJECT_ID = os.environ.get("PROJECT_ID", "").strip() or None
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

app = FastAPI(title="ChronoBuild Engine + Admin (Firestore)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- HEALTH (Firestore’a dokunmaz) ----------
@app.get("/health")
def health():
    return {"ok": True}

# ---------- AUTH ----------
def require_admin(request: Request):
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

# ---------- Firestore Lazy Client ----------
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
    # Firestore erişim problemi varsa burada exception olur (ama artık health check’i bozmaz)
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

# ---------- Admin UI ----------
ADMIN_HTML = "<html><body><h1>ChronoBuild Admin</h1><p>OK</p></body></html>"

@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: bool = Depends(require_admin)):
    # katalogu load edebilmek için Firestore’a dokunur; sorun varsa burada görürsün.
    _ = load_catalog()
    return ADMIN_HTML

# ---------- Admin API ----------
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
def admin_get_catalog(_: bool = Depends(require_admin)):
    return load_catalog()

@app.put("/admin/api/config")
def admin_put_config(item: ConfigItem, _: bool = Depends(require_admin)):
    set_doc("config", "main", item.model_dump())
    return {"ok": True}

@app.put("/admin/api/insulations/{doc_id}")
def admin_put_insulation(doc_id: str, item: InsulationItem, _: bool = Depends(require_admin)):
    set_doc("insulations", doc_id, item.model_dump())
    return {"ok": True}

@app.put("/admin/api/windows/{doc_id}")
def admin_put_window(doc_id: str, item: WindowItem, _: bool = Depends(require_admin)):
    set_doc("windows", doc_id, item.model_dump())
    return {"ok": True}

@app.put("/admin/api/gas_tariffs/{doc_id}")
def admin_put_gas(doc_id: str, item: GasTariffItem, _: bool = Depends(require_admin)):
    payload = item.model_dump()
    payload["province"] = payload["province"].strip().upper()
    set_doc("gas_tariffs", doc_id, payload)
    return {"ok": True}

# ---------- Analyze (şimdilik basit) ----------
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
        "note": "Deploy test sürümü. TS825+2050 bloğunu buraya entegre edeceğiz."
    }
