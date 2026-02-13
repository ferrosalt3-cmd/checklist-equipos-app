import base64
import io
import json
import os
import re
import hashlib
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm


# =========================================================
# Streamlit config
# =========================================================
st.set_page_config(page_title="Checklist Equipos", page_icon="✅", layout="wide")


# =========================================================
# Helpers
# =========================================================
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _today_iso() -> str:
    return date.today().isoformat()

def _safe_b64(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def b64_to_bytes(b64str: str) -> bytes:
    return base64.b64decode(b64str.encode("utf-8"))


# =========================================================
# CSS / Branding (incluye arreglo franja blanca)
# =========================================================
def inject_css(is_login: bool):
    fondo_b64 = _safe_b64("fondo.png")

    # Nota: is_login permite esconder elementos cuando toca login
    st.markdown(f"""
    <style>
      .block-container {{
        padding-top: 1.2rem;
        padding-bottom: 2.2rem;
        max-width: 1200px;
        position: relative;
        z-index: 1;
      }}

      /* Sidebar */
      [data-testid="stSidebar"] {{
        background: rgba(255,255,255,0.92);
        position: relative;
        z-index: 1;
      }}

      /* Hide Streamlit chrome */
      #MainMenu {{visibility: hidden;}}
      footer {{visibility: hidden;}}
      header {{visibility: hidden;}}

      /* Inputs */
      .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] {{
        border-radius: 12px !important;
      }}
      .stButton > button {{
        border-radius: 12px;
        padding: 0.65rem 1rem;
        font-weight: 650;
      }}

      /* Cards */
      .card {{
        background: rgba(255,255,255,0.94);
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 18px;
        padding: 18px;
        box-shadow: 0 12px 34px rgba(0,0,0,0.10);
      }}

      .title {{
        font-size: 1.35rem;
        font-weight: 900;
        margin: 0;
      }}
      .muted {{
        color: rgba(0,0,0,0.62);
        font-size: 0.92rem;
        margin: 0.25rem 0 0.1rem 0;
      }}
      .hr {{
        height: 1px;
        background: rgba(0,0,0,0.10);
        margin: 14px 0;
      }}
      .pill {{
        display: inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(0,0,0,0.05);
        font-size: 0.85rem;
        margin-right: 6px;
      }}

      /* Signature box */
      .sigbox {{
        background: rgba(255,255,255,0.98);
        border: 1px dashed rgba(0,0,0,0.25);
        border-radius: 14px;
        padding: 12px;
      }}

      /* LOGIN overlay (centro real + no franja) */
      .login-overlay {{
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.38);
        z-index: 9998;
        pointer-events: none;
      }}

      .login-wrap {{
        position: fixed;
        inset: 0;
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 18px;
        z-index: 9999;
      }}

      .login-card {{
        width: min(560px, 94vw);
        background: rgba(255,255,255,0.96);
        border: 1px solid rgba(0,0,0,0.10);
        border-radius: 20px;
        padding: 22px;
        box-shadow: 0 18px 45px rgba(0,0,0,0.25);
      }}

      .brand-row {{
        display: flex;
        gap: 12px;
        align-items: center;
        margin-bottom: 10px;
      }}
      .brand-title {{
        font-size: 1.45rem;
        font-weight: 950;
        margin: 0;
      }}
      .brand-sub {{
        margin: 0;
        color: rgba(0,0,0,0.62);
        font-size: 0.95rem;
      }}
      @media (max-width: 640px) {{
        .login-card {{
          padding: 16px;
          border-radius: 16px;
        }}
        .brand-title {{
          font-size: 1.25rem;
        }}
      }}

      /* Esto evita “bloques fantasmas” debajo en login */
      {"[data-testid='stAppViewContainer']{overflow:hidden !important;}" if is_login else ""}

      /* Fondo */
    </style>
    """, unsafe_allow_html=True)

    if fondo_b64:
        st.markdown(f"""
        <style>
          .stApp {{
            background-image: url("data:image/png;base64,{fondo_b64}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
          }}
          .stApp::before {{
            content: "";
            position: fixed;
            inset: 0;
            background: rgba(255,255,255,0.30);
            pointer-events: none;
            z-index: 0;
          }}
        </style>
        """, unsafe_allow_html=True)


def show_top_header():
    col1, col2 = st.columns([1, 6])
    with col1:
        if os.path.exists("logo.png"):
            st.image("logo.png", width=120)
    with col2:
        st.markdown('<p class="title">Checklist de Equipos</p>', unsafe_allow_html=True)
        st.markdown('<p class="muted">Operador llena • Supervisor revisa y aprueba • PDF final con firmas</p>', unsafe_allow_html=True)


# =========================================================
# Google Sheets
# =========================================================
def get_gsheet_client() -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    else:
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not gac:
            raise RuntimeError("Falta Secrets gcp_service_account (Streamlit Cloud) o GOOGLE_APPLICATION_CREDENTIALS (local).")
        creds = Credentials.from_service_account_file(gac, scopes=scopes)
    return gspread.authorize(creds)

def get_sheet_id() -> str:
    sid = st.secrets.get("GSHEET_ID") if hasattr(st, "secrets") else None
    if not sid:
        sid = os.environ.get("GSHEET_ID")
    if not sid:
        raise RuntimeError("Falta GSHEET_ID en Secrets o variable de entorno.")
    return sid

SHEETS = {
    "users": "users",
    "submissions": "submissions",
    "submission_items": "submission_items",
    "approvals": "approvals",
    "photos": "photos",
}

def ensure_worksheets(sh: gspread.Spreadsheet):
    def _ensure(name: str, headers: List[str]):
        try:
            ws = sh.worksheet(name)
        except Exception:
            ws = sh.add_worksheet(title=name, rows="2000", cols=str(max(12, len(headers) + 5)))
            ws.append_row(headers)
            return
        values = ws.get_all_values()
        if not values:
            ws.append_row(headers)

    _ensure(SHEETS["users"], ["username", "password_hash", "role", "full_name", "is_active", "created_at"])
    _ensure(SHEETS["submissions"], [
        "submission_id", "date", "created_at", "equipo", "operador_username", "operador_full_name",
        "estado_general", "nota", "firma_operador_b64", "status", "updated_at"
    ])
    _ensure(SHEETS["submission_items"], ["submission_id", "item_id", "item_text", "estado", "comentario"])
    _ensure(SHEETS["photos"], ["submission_id", "item_id", "filename", "photo_b64"])
    _ensure(SHEETS["approvals"], [
        "submission_id", "approved_at", "supervisor_username", "supervisor_full_name",
        "conforme", "observaciones", "firma_supervisor_b64", "pdf_b64"
    ])

@st.cache_resource(show_spinner=False)
def get_db() -> Tuple[gspread.Spreadsheet, Dict[str, gspread.Worksheet]]:
    gc = get_gsheet_client()
    sh = gc.open_by_key(get_sheet_id())
    ensure_worksheets(sh)
    wss = {k: sh.worksheet(v) for k, v in SHEETS.items()}
    return sh, wss

def ws_all_records(ws: gspread.Worksheet) -> List[Dict[str, Any]]:
    return ws.get_all_records()

def ws_append(ws: gspread.Worksheet, row: List[Any]):
    ws.append_row(row, value_input_option="USER_ENTERED")

def ws_update_row_by_key(ws: gspread.Worksheet, key_col: str, key_val: str, updates: Dict[str, Any]) -> bool:
    data = ws.get_all_values()
    if len(data) < 2:
        return False
    headers = data[0]
    if key_col not in headers:
        return False
    key_idx = headers.index(key_col)

    row_idx = None
    for i in range(1, len(data)):
        if str(data[i][key_idx]).strip().lower() == str(key_val).strip().lower():
            row_idx = i + 1
            break
    if not row_idx:
        return False

    for col_name, new_val in updates.items():
        if col_name not in headers:
            continue
        col_idx = headers.index(col_name) + 1
        ws.update_cell(row_idx, col_idx, new_val)
    return True

def ws_delete_row_by_key(ws: gspread.Worksheet, key_col: str, key_val: str) -> bool:
    data = ws.get_all_values()
    if len(data) < 2:
        return False
    headers = data[0]
    if key_col not in headers:
        return False
    key_idx = headers.index(key_col)
    for i in range(1, len(data)):
        if str(data[i][key_idx]).strip().lower() == str(key_val).strip().lower():
            ws.delete_rows(i + 1)
            return True
    return False


# =========================================================
# Auth (con recuperación admin opcional)
# =========================================================
def get_user(username: str) -> Optional[Dict[str, Any]]:
    _, wss = get_db()
    for u in ws_all_records(wss["users"]):
        if str(u.get("username", "")).strip().lower() == username.strip().lower():
            return u
    return None

def ensure_admin_seed_and_optional_reset():
    """
    - Si users está vacío: crea admin/admin123.
    - Si pones en Secrets: ADMIN_RESET_PASSWORD = "algo"
      entonces fuerza el password de admin a ese valor (sin borrar usuarios).
    """
    _, wss = get_db()
    users = ws_all_records(wss["users"])

    # Seed si vacío
    if not users:
        ws_append(wss["users"], ["admin", hash_password("admin123"), "supervisor", "Administrador", True, _now_iso()])
        return

    # Reset opcional por secrets
    reset_pw = None
    try:
        reset_pw = st.secrets.get("ADMIN_RESET_PASSWORD")
    except Exception:
        reset_pw = None

    if reset_pw:
        # actualiza admin (si existe)
        ok = ws_update_row_by_key(wss["users"], "username", "admin", {"password_hash": hash_password(reset_pw), "is_active": True})
        if not ok:
            # si no existiera admin, lo creamos
            ws_append(wss["users"], ["admin", hash_password(reset_pw), "supervisor", "Administrador", True, _now_iso()])

def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    u = get_user(username)
    if not u:
        return None
    if not bool(u.get("is_active", True)):
        return None
    if u.get("password_hash") != hash_password(password):
        return None
    return u


# =========================================================
# Config JSON (equipos/preguntas) - robusto
# =========================================================
def load_json_any(path: str) -> Any:
    if not os.path.exists(path):
        raise RuntimeError(f"No encuentro {path} en la raíz del repo.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

RAW_CONFIG = load_json_any("checklist_config.json")

def _as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def _first_key(d: dict, keys: List[str]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None

def normalize_config(raw: Any) -> Dict[str, Any]:
    """
    Normaliza a:
    { "equipos": [ {"nombre": "...", "items": [{"id":"I1","texto":"..."}, ...]}, ... ] }
    """
    # Caso ideal
    if isinstance(raw, dict) and isinstance(raw.get("equipos"), list):
        equipos_out = []
        for e in raw["equipos"]:
            if not isinstance(e, dict):
                continue
            nombre = _first_key(e, ["nombre","name","equipo","unidad","codigo"]) or "SIN_NOMBRE"
            items_raw = _first_key(e, ["items","checklist","preguntas","criterios","lista","campos"]) or []
            items_out = []
            for idx, it in enumerate(_as_list(items_raw)):
                if isinstance(it, dict):
                    texto = _first_key(it, ["texto","item","pregunta","descripcion","name","label"]) or f"Item {idx+1}"
                    iid = _first_key(it, ["id","item_id","codigo"]) or f"I{idx+1}"
                else:
                    texto = str(it)
                    iid = f"I{idx+1}"
                items_out.append({"id": str(iid), "texto": str(texto)})
            equipos_out.append({"nombre": str(nombre), "items": items_out})
        return {"equipos": equipos_out}

    # Caso dict con otra llave para equipos
    if isinstance(raw, dict):
        equipos_raw = _first_key(raw, ["equipos","equipment","unidades","maquinas","activos"])
        if equipos_raw is not None:
            equipos_out = []
            for e in _as_list(equipos_raw):
                if isinstance(e, dict):
                    nombre = _first_key(e, ["nombre","name","equipo","unidad","codigo"]) or "SIN_NOMBRE"
                    items_raw = _first_key(e, ["items","checklist","preguntas","criterios","lista","campos"]) or []
                else:
                    nombre = str(e)
                    items_raw = []
                items_out = []
                for idx, it in enumerate(_as_list(items_raw)):
                    if isinstance(it, dict):
                        texto = _first_key(it, ["texto","item","pregunta","descripcion","name","label"]) or f"Item {idx+1}"
                        iid = _first_key(it, ["id","item_id","codigo"]) or f"I{idx+1}"
                    else:
                        texto = str(it)
                        iid = f"I{idx+1}"
                    items_out.append({"id": str(iid), "texto": str(texto)})
                equipos_out.append({"nombre": str(nombre), "items": items_out})
            return {"equipos": equipos_out}

        # Caso: {"Apilador 1":[...preguntas...], "Apilador 2":[...]}
        if all(isinstance(v, (list, dict)) for v in raw.values()) and len(raw.keys()) > 0:
            equipos_out = []
            for k, v in raw.items():
                nombre = str(k)
                items_raw = v
                if isinstance(v, dict):
                    items_raw = _first_key(v, ["items","checklist","preguntas","criterios","lista","campos"]) or v
                items_out = []
                for idx, it in enumerate(_as_list(items_raw)):
                    if isinstance(it, dict):
                        texto = _first_key(it, ["texto","item","pregunta","descripcion","name","label"]) or f"Item {idx+1}"
                        iid = _first_key(it, ["id","item_id","codigo"]) or f"I{idx+1}"
                    else:
                        texto = str(it)
                        iid = f"I{idx+1}"
                    items_out.append({"id": str(iid), "texto": str(texto)})
                equipos_out.append({"nombre": nombre, "items": items_out})
            return {"equipos": equipos_out}

        return {"equipos": []}

    # Caso lista de equipos
    if isinstance(raw, list):
        equipos_out = []
        for e in raw:
            if isinstance(e, dict):
                nombre = _first_key(e, ["nombre","name","equipo","unidad","codigo"]) or "SIN_NOMBRE"
                items_raw = _first_key(e, ["items","checklist","preguntas","criterios","lista","campos"]) or []
                items_out = []
                for idx, it in enumerate(_as_list(items_raw)):
                    if isinstance(it, dict):
                        texto = _first_key(it, ["texto","item","pregunta","descripcion","name","label"]) or f"Item {idx+1}"
                        iid = _first_key(it, ["id","item_id","codigo"]) or f"I{idx+1}"
                    else:
                        texto = str(it)
                        iid = f"I{idx+1}"
                    items_out.append({"id": str(iid), "texto": str(texto)})
                equipos_out.append({"nombre": str(nombre), "items": items_out})
        return {"equipos": equipos_out}

    return {"equipos": []}

CONFIG = normalize_config(RAW_CONFIG)

def list_equipos() -> List[str]:
    return [e.get("nombre","SIN_NOMBRE") for e in CONFIG.get("equipos", [])]

def get_items_for_equipo(nombre_equipo: str) -> List[Dict[str, Any]]:
    for e in CONFIG.get("equipos", []):
        if e.get("nombre") == nombre_equipo:
            return e.get("items", [])
    return []


# =========================================================
# Signature component
# =========================================================
try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:
    CANVAS_AVAILABLE = False

def signature_input(label: str, key_prefix: str) -> Optional[str]:
    st.markdown(f"**{label}**")
    st.markdown('<div class="sigbox">', unsafe_allow_html=True)

    if CANVAS_AVAILABLE:
        canvas_res = st_canvas(
            fill_color="rgba(255,255,255,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="rgba(255,255,255,1)",
            height=170,
            width=520,
            drawing_mode="freedraw",
            key=f"{key_prefix}_canvas",
        )
        st.caption("Firma con el mouse o el dedo (celular).")
        st.markdown("</div>", unsafe_allow_html=True)

        if canvas_res.image_data is not None:
            import numpy as np
            from PIL import Image
            img = Image.fromarray(canvas_res.image_data.astype(np.uint8))
            gray = img.convert("L")
            arr = np.array(gray)
            nonwhite = (arr < 250).sum()
            if nonwhite < 220:
                return None
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            return base64.b64encode(bio.getvalue()).decode("utf-8")
        return None
    else:
        up = st.file_uploader("Sube una imagen con tu firma (PNG/JPG)", type=["png","jpg","jpeg"], key=f"{key_prefix}_upload")
        st.markdown("</div>", unsafe_allow_html=True)
        if up:
            return base64.b64encode(up.read()).decode("utf-8")
        return None


# =========================================================
# Submissions / PDF / Export
# =========================================================
def make_submission_id() -> str:
    import random, string
    return f"S{datetime.now().strftime('%Y%m%d%H%M%S')}{''.join(random.choices(string.ascii_uppercase+string.digits,k=4))}"

def create_submission(equipo: str, operador: Dict[str, Any], estado_general: str, nota: str, firma_b64: str) -> str:
    _, wss = get_db()
    sid = make_submission_id()
    ws_append(wss["submissions"], [
        sid, _today_iso(), _now_iso(), equipo,
        operador["username"], operador.get("full_name",""),
        estado_general, nota, firma_b64, "PENDIENTE", _now_iso()
    ])
    return sid

def upsert_submission_items(submission_id: str, items_rows: List[List[Any]]):
    _, wss = get_db()
    ws = wss["submission_items"]
    data = ws.get_all_values()
    if len(data) >= 2:
        headers = data[0]
        sid_idx = headers.index("submission_id")
        rows_to_delete = [i+1 for i in range(1, len(data)) if str(data[i][sid_idx]).strip() == submission_id]
        for r in reversed(rows_to_delete):
            ws.delete_rows(r)
    for row in items_rows:
        ws_append(ws, row)

def upsert_photos(submission_id: str, photos_rows: List[List[Any]]):
    _, wss = get_db()
    ws = wss["photos"]
    data = ws.get_all_values()
    if len(data) >= 2:
        headers = data[0]
        sid_idx = headers.index("submission_id")
        rows_to_delete = [i+1 for i in range(1, len(data)) if str(data[i][sid_idx]).strip() == submission_id]
        for r in reversed(rows_to_delete):
            ws.delete_rows(r)
    for row in photos_rows:
        ws_append(ws, row)

def list_all_submissions() -> pd.DataFrame:
    _, wss = get_db()
    df = pd.DataFrame(ws_all_records(wss["submissions"]))
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df.sort_values("created_at", ascending=False)

def list_pending_submissions() -> pd.DataFrame:
    df = list_all_submissions()
    if df.empty:
        return df
    return df[df["status"].astype(str).str.upper() == "PENDIENTE"]

def get_submission_detail(submission_id: str):
    _, wss = get_db()
    subs = ws_all_records(wss["submissions"])
    sub = next((s for s in subs if str(s.get("submission_id")) == submission_id), None)

    items = ws_all_records(wss["submission_items"])
    df_items = pd.DataFrame([i for i in items if str(i.get("submission_id")) == submission_id])

    photos = ws_all_records(wss["photos"])
    df_photos = pd.DataFrame([p for p in photos if str(p.get("submission_id")) == submission_id])

    approvals = ws_all_records(wss["approvals"])
    appr = next((a for a in approvals if str(a.get("submission_id")) == submission_id), None)

    return sub, df_items, df_photos, appr

def make_pdf_bytes(sub: Dict[str, Any], df_items: pd.DataFrame, df_photos: pd.DataFrame, appr: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 2.0*cm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(2.0*cm, y, "Checklist de Equipos - Reporte Aprobado")
    y -= 0.8*cm

    c.setFont("Helvetica", 10)
    c.drawString(2.0*cm, y, f"Equipo: {sub.get('equipo','')}")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Fecha: {sub.get('date','')}  |  Creado: {sub.get('created_at','')}")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Operador: {sub.get('operador_full_name','')} ({sub.get('operador_username','')})")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Estado general: {sub.get('estado_general','')}")
    y -= 0.7*cm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2.0*cm, y, "Detalle de ítems")
    y -= 0.5*cm
    c.setFont("Helvetica", 9)

    for _, row in df_items.fillna("").iterrows():
        line = f"- {row.get('item_text','')} | Estado: {row.get('estado','')} | Coment: {str(row.get('comentario',''))[:60]}"
        if y < 4.0*cm:
            c.showPage()
            y = height - 2.0*cm
            c.setFont("Helvetica", 9)
        c.drawString(2.0*cm, y, line[:140])
        y -= 0.35*cm

    if y < 8.0*cm:
        c.showPage()
        y = height - 2.0*cm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2.0*cm, y, "Aprobación Supervisor")
    y -= 0.5*cm
    c.setFont("Helvetica", 10)
    c.drawString(2.0*cm, y, f"Supervisor: {appr.get('supervisor_full_name','')} ({appr.get('supervisor_username','')})")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Aprobado: {appr.get('approved_at','')}  |  Conforme: {appr.get('conforme','')}")
    y -= 0.6*cm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(2.0*cm, y, "Firma Operador")
    c.drawString(11.0*cm, y, "Firma Supervisor")
    y -= 3.2*cm

    try:
        op_b64 = sub.get("firma_operador_b64") or ""
        sup_b64 = appr.get("firma_supervisor_b64") or ""
        if op_b64:
            op_img = io.BytesIO(b64_to_bytes(op_b64))
            c.drawImage(op_img, 2.0*cm, y, width=7.5*cm, height=3.0*cm, preserveAspectRatio=True, mask="auto")
        if sup_b64:
            sup_img = io.BytesIO(b64_to_bytes(sup_b64))
            c.drawImage(sup_img, 11.0*cm, y, width=7.5*cm, height=3.0*cm, preserveAspectRatio=True, mask="auto")
    except Exception:
        pass

    c.showPage()
    c.save()
    return buf.getvalue()

def approve_submission(submission_id: str, supervisor: Dict[str, Any], conforme: str, observaciones: str, firma_supervisor_b64: str):
    _, wss = get_db()
    ok = ws_update_row_by_key(wss["submissions"], "submission_id", submission_id, {"status": "APROBADO", "updated_at": _now_iso()})
    if not ok:
        raise RuntimeError("No pude actualizar status del submission.")

    sub, df_items, df_photos, _appr = get_submission_detail(submission_id)
    if not sub:
        raise RuntimeError("No encontré submission.")

    appr_row = {
        "submission_id": submission_id,
        "approved_at": _now_iso(),
        "supervisor_username": supervisor["username"],
        "supervisor_full_name": supervisor.get("full_name",""),
        "conforme": conforme,
        "observaciones": observaciones,
        "firma_supervisor_b64": firma_supervisor_b64,
    }
    pdf_bytes = make_pdf_bytes(sub, df_items, df_photos, appr_row)
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    # Upsert approvals
    ws = wss["approvals"]
    data = ws.get_all_values()
    if len(data) >= 2:
        headers = data[0]
        sid_idx = headers.index("submission_id")
        rows_to_delete = [i+1 for i in range(1, len(data)) if str(data[i][sid_idx]).strip() == submission_id]
        for r in reversed(rows_to_delete):
            ws.delete_rows(r)

    ws_append(ws, [
        submission_id, appr_row["approved_at"],
        appr_row["supervisor_username"], appr_row["supervisor_full_name"],
        conforme, observaciones, firma_supervisor_b64, pdf_b64
    ])

def export_weekly_xlsx(start_date: date, end_date: date) -> bytes:
    df = list_all_submissions()
    _, wss = get_db()
    items = pd.DataFrame(ws_all_records(wss["submission_items"]))
    approvals = pd.DataFrame(ws_all_records(wss["approvals"]))

    if df.empty:
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            pd.DataFrame().to_excel(writer, index=False, sheet_name="submissions")
        return out.getvalue()

    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    dfw = df[(df["date_dt"] >= start_date) & (df["date_dt"] <= end_date)].drop(columns=["date_dt"])

    if not items.empty and not dfw.empty:
        items = items[items["submission_id"].isin(dfw["submission_id"].tolist())]
    else:
        items = pd.DataFrame()

    if not approvals.empty and not dfw.empty:
        approvals = approvals[approvals["submission_id"].isin(dfw["submission_id"].tolist())]
    else:
        approvals = pd.DataFrame()

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dfw.to_excel(writer, index=False, sheet_name="submissions")
        items.to_excel(writer, index=False, sheet_name="items")
        approvals.to_excel(writer, index=False, sheet_name="approvals")
    return out.getvalue()


# =========================================================
# UI helpers
# =========================================================
def role_badge(role: str) -> str:
    role = (role or "").lower()
    return "🛡️ Supervisor" if role == "supervisor" else "👷 Operador"

def card_open():
    st.markdown('<div class="card">', unsafe_allow_html=True)

def card_close():
    st.markdown("</div>", unsafe_allow_html=True)

def logout():
    st.session_state.user = None
    st.session_state.pop("selected_submission", None)


# =========================================================
# INIT session
# =========================================================
if "user" not in st.session_state:
    st.session_state.user = None

# Admin seed/reset (antes de login)
ensure_admin_seed_and_optional_reset()


# =========================================================
# LOGIN (IMPORTANTE: se renderiza ANTES de todo lo demás)
# =========================================================
if not st.session_state.user:
    inject_css(is_login=True)

    st.markdown('<div class="login-overlay"></div>', unsafe_allow_html=True)
    st.markdown('<div class="login-wrap"><div class="login-card">', unsafe_allow_html=True)

    st.markdown('<div class="brand-row">', unsafe_allow_html=True)
    if os.path.exists("logo.png"):
        st.image("logo.png", width=72)
    st.markdown("""
      <div>
        <p class="brand-title">Checklist de Equipos</p>
        <p class="brand-sub">Inicia sesión para continuar</p>
      </div>
    """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

    username = st.text_input("Usuario", placeholder="Ej: operador1")
    password = st.text_input("Contraseña", type="password", placeholder="********")

    if st.button("Entrar", use_container_width=True):
        user = authenticate(username, password)
        if not user:
            st.error("Usuario o contraseña incorrecta.")
        else:
            st.session_state.user = user
            st.rerun()

    st.caption("Si olvidaste tu acceso, el supervisor debe crear tu usuario.")

    st.markdown("</div></div>", unsafe_allow_html=True)

    # CLAVE: no renderizar nada más (evita franja blanca)
    st.stop()


# =========================================================
# APP (post-login)
# =========================================================
inject_css(is_login=False)

user = st.session_state.user
role = (user.get("role") or "operador").lower()

show_top_header()

topc1, topc2 = st.columns([6, 1])
with topc2:
    if st.button("Cerrar sesión"):
        logout()
        st.rerun()

equipos_list = list_equipos()

with st.sidebar:
    st.markdown("### Menú")
    st.markdown(f"**Usuario:** {user.get('full_name','') or user.get('username')}")
    st.markdown(f"**Rol:** {role_badge(role)}")
    st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
    if role == "operador":
        page = st.radio("Secciones", ["Llenar checklist", "Mis envíos"], index=0)
    else:
        page = st.radio("Secciones", ["Pendientes", "Reportes", "Usuarios", "Export semanal"], index=0)


# =========================================================
# OPERADOR
# =========================================================
if role == "operador":
    if not equipos_list:
        st.error("No hay equipos cargados. Revisa checklist_config.json (estructura o contenido).")
        if isinstance(RAW_CONFIG, dict):
            st.info(f"Claves encontradas en tu JSON: {list(RAW_CONFIG.keys())[:30]}")
        st.stop()

    if page == "Llenar checklist":
        card_open()
        st.markdown("### Llenar checklist")

        equipo = st.selectbox("Selecciona equipo", options=equipos_list)
        items = get_items_for_equipo(equipo)

        st.markdown(f'<span class="pill">Equipo: {equipo}</span><span class="pill">Ítems: {len(items)}</span>', unsafe_allow_html=True)
        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

        estado_general = st.selectbox("Estado general del equipo", ["Operativo", "Operativo con falla", "Inoperativo"], index=0)
        nota = st.text_area("Observaciones del operador (opcional)")

        st.markdown("#### Checklist por ítem")
        estados_opciones = ["Operativo", "Operativo con falla", "Inoperativo"]

        items_rows = []
        photos_rows = []
        missing_photo = False

        for idx, it in enumerate(items):
            item_id = it.get("id") or f"I{idx+1}"
            text = it.get("texto") or f"Item {idx+1}"

            st.markdown(f"**{idx+1}. {text}**")
            cA, cB, cC = st.columns([1.2, 2.2, 2.2])

            with cA:
                estado = st.selectbox("Estado", estados_opciones, key=f"estado_{equipo}_{item_id}", label_visibility="collapsed")
            with cB:
                comentario = st.text_input("Comentario", key=f"coment_{equipo}_{item_id}", placeholder="(Opcional)")
            with cC:
                up = None
                if estado == "Operativo con falla":
                    up = st.file_uploader("Foto evidencia (obligatoria)", type=["png", "jpg", "jpeg"], key=f"foto_{equipo}_{item_id}")
                    if not up:
                        missing_photo = True
                if up:
                    photos_rows.append([None, item_id, up.name, base64.b64encode(up.read()).decode("utf-8")])

            items_rows.append([None, item_id, text, estado, comentario])
            st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

        st.markdown("#### Firma del operador")
        firma_b64 = signature_input("Firma digital", "firma_operador")

        if st.button("Enviar a supervisor"):
            if missing_photo:
                st.error("Falta foto en ítems con 'Operativo con falla'.")
                st.stop()
            if not firma_b64:
                st.error("La firma del operador es obligatoria.")
                st.stop()

            sid = create_submission(equipo, user, estado_general, nota, firma_b64)
            items_rows2 = [[sid, r[1], r[2], r[3], r[4]] for r in items_rows]
            photos_rows2 = [[sid, r[1], r[2], r[3]] for r in photos_rows]

            upsert_submission_items(sid, items_rows2)
            upsert_photos(sid, photos_rows2)

            st.success(f"Enviado ✅ ID: {sid} (pendiente de revisión)")

        card_close()

    elif page == "Mis envíos":
        card_open()
        st.markdown("### Mis envíos")

        df = list_all_submissions()
        dfo = df[df["operador_username"].astype(str).str.lower() == user["username"].lower()] if not df.empty else pd.DataFrame()

        if dfo.empty:
            st.info("Aún no tienes envíos.")
            card_close()
            st.stop()

        st.dataframe(dfo[["submission_id", "date", "equipo", "estado_general", "status", "updated_at"]],
                     use_container_width=True, hide_index=True)

        st.markdown("#### Descargar PDF (solo aprobados)")
        sid = st.text_input("ID aprobado", placeholder="Ej: S2026...")
        if st.button("Buscar PDF"):
            sub, df_items, df_photos, appr = get_submission_detail(sid)
            if not sub:
                st.error("No existe ese ID.")
            elif str(sub.get("status", "")).upper() != "APROBADO":
                st.warning("Aún no está aprobado.")
            elif not appr or not appr.get("pdf_b64"):
                st.error("No hay PDF.")
            else:
                st.download_button("Descargar PDF", data=b64_to_bytes(appr["pdf_b64"]),
                                   file_name=f"checklist_{sid}.pdf", mime="application/pdf")

        card_close()


# =========================================================
# SUPERVISOR
# =========================================================
else:
    if page == "Usuarios":
        card_open()
        st.markdown("### Gestión de usuarios (Supervisor)")

        _, wss = get_db()
        users_df = pd.DataFrame(ws_all_records(wss["users"]))
        if not users_df.empty:
            st.dataframe(users_df[["username", "role", "full_name", "is_active", "created_at"]],
                         use_container_width=True, hide_index=True)
        else:
            st.info("No hay usuarios.")

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.markdown("#### Crear / actualizar usuario")

        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            username = st.text_input("Username", placeholder="ej: operador1").strip()
        with c2:
            role_new = st.selectbox("Rol", ["operador", "supervisor"], index=0)
        with c3:
            full_name = st.text_input("Nombre completo", placeholder="Ej: Juan Pérez")

        pw = st.text_input("Contraseña (obligatoria al crear)", type="password")
        active = st.checkbox("Activo", value=True)

        if st.button("Guardar usuario"):
            if not username or not re.match(r"^[a-zA-Z0-9_.-]{3,}$", username):
                st.error("Username inválido (mín 3 caracteres).")
                st.stop()

            existing = next((u for u in ws_all_records(wss["users"])
                             if str(u.get("username","")).strip().lower() == username.lower()), None)

            if not existing:
                if not pw:
                    st.error("Para crear usuario, la contraseña es obligatoria.")
                    st.stop()
                ws_append(wss["users"], [username, hash_password(pw), role_new, full_name, active, _now_iso()])
                st.success("Usuario creado ✅")
            else:
                updates = {"role": role_new, "full_name": full_name, "is_active": active}
                if pw:
                    updates["password_hash"] = hash_password(pw)
                ok = ws_update_row_by_key(wss["users"], "username", username, updates)
                if ok:
                    st.success("Usuario actualizado ✅")
                else:
                    st.error("No pude actualizar (revisa la hoja users).")
            st.rerun()

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.markdown("#### Eliminar usuario")

        del_user = st.text_input("Username a eliminar", placeholder="ej: operador2").strip()
        if st.button("Eliminar"):
            if del_user.lower() == "admin":
                st.error("No se permite borrar admin.")
            else:
                ok = ws_delete_row_by_key(wss["users"], "username", del_user)
                if ok:
                    st.success("Eliminado ✅")
                    st.rerun()
                else:
                    st.warning("No existe ese usuario.")

        card_close()

    elif page == "Pendientes":
        card_open()
        st.markdown("### Pendientes de aprobación")

        dfp = list_pending_submissions()
        if dfp.empty:
            st.success("No hay pendientes 🎉")
            card_close()
            st.stop()

        st.dataframe(dfp[["submission_id", "created_at", "date", "equipo", "operador_full_name", "estado_general", "status"]],
                     use_container_width=True, hide_index=True)

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        sid = st.text_input("ID de submission", value=st.session_state.get("selected_submission", ""), placeholder="Ej: S2026...")
        if st.button("Cargar detalle"):
            st.session_state.selected_submission = sid.strip()
            st.rerun()

        sid = st.session_state.get("selected_submission", "").strip()
        if sid:
            sub, df_items, df_photos, appr = get_submission_detail(sid)
            if not sub:
                st.error("No existe ese submission.")
                card_close()
                st.stop()

            st.markdown(f"**Equipo:** {sub.get('equipo','')}  •  **Operador:** {sub.get('operador_full_name','')}  •  **Estado:** {sub.get('estado_general','')}")
            if sub.get("nota"):
                st.caption(f"Nota operador: {sub.get('nota')}")

            st.markdown("##### Ítems")
            st.dataframe(df_items[["item_id", "item_text", "estado", "comentario"]] if not df_items.empty else pd.DataFrame(),
                         use_container_width=True, hide_index=True)

            st.markdown("##### Evidencias")
            if df_photos.empty:
                st.info("No hay fotos.")
            else:
                for _, prow in df_photos.iterrows():
                    st.markdown(f"- **Item {prow.get('item_id')}** • {prow.get('filename')}")
                    try:
                        st.image(b64_to_bytes(prow.get("photo_b64","")), width=420)
                    except Exception:
                        st.warning("No pude mostrar una foto.")

            st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
            conforme = st.selectbox("Conformidad", ["Conforme", "No conforme"], index=0)
            observ = st.text_area("Observaciones (opcional)")
            firma_sup_b64 = signature_input("Firma del supervisor", "firma_supervisor")

            if st.button("Aprobar y generar PDF"):
                if str(sub.get("status","")).upper() != "PENDIENTE":
                    st.warning("Ya no está pendiente.")
                    st.stop()
                if not firma_sup_b64:
                    st.error("Firma supervisor obligatoria.")
                    st.stop()
                approve_submission(sid, user, conforme, observ, firma_sup_b64)
                st.success("Aprobado ✅ PDF generado.")
                st.session_state.selected_submission = ""
                st.rerun()

        card_close()

    elif page == "Reportes":
        card_open()
        st.markdown("### Reportes y Dashboard")

        df = list_all_submissions()
        if df.empty:
            st.info("No hay datos aún.")
            card_close()
            st.stop()

        c1, c2 = st.columns([1, 1])
        with c1:
            equipo = st.selectbox("Equipo", ["(Todos)"] + sorted(df["equipo"].dropna().unique().tolist()))
        with c2:
            status = st.selectbox("Estado del flujo", ["(Todos)", "PENDIENTE", "APROBADO"])

        dff = df.copy()
        if equipo != "(Todos)":
            dff = dff[dff["equipo"] == equipo]
        if status != "(Todos)":
            dff = dff[dff["status"].astype(str).str.upper() == status]

        total = len(dff)
        aprob = int((dff["status"].astype(str).str.upper() == "APROBADO").sum())
        pend = int((dff["status"].astype(str).str.upper() == "PENDIENTE").sum())
        fallas = int(dff["estado_general"].astype(str).str.lower().str.contains("falla").sum())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total", total)
        k2.metric("Aprobados", aprob)
        k3.metric("Pendientes", pend)
        k4.metric("Con falla (general)", fallas)

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.dataframe(dff[["submission_id", "date", "equipo", "operador_full_name", "estado_general", "status", "updated_at"]].head(300),
                     use_container_width=True, hide_index=True)

        card_close()

    elif page == "Export semanal":
        card_open()
        st.markdown("### Export semanal")

        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            start = st.date_input("Desde", value=date.today() - timedelta(days=7))
        with c2:
            end = st.date_input("Hasta", value=date.today())
        with c3:
            st.caption("Descarga XLSX (submissions + items + approvals) del rango.")

        if st.button("Generar XLSX"):
            if end < start:
                st.error("La fecha final no puede ser menor.")
                st.stop()
            data = export_weekly_xlsx(start, end)
            st.download_button(
                "Descargar Excel",
                data=data,
                file_name=f"reporte_{start.isoformat()}_a_{end.isoformat()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        card_close()
