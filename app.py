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
# CSS / Branding (FIX: login escribible + sin franja blanca)
# =========================================================
def inject_css(is_login: bool):
    fondo_b64 = _safe_b64("fondo.png")

    st.markdown(f"""
    <style>
      /* Layout */
      .block-container {{
        padding-top: 1.2rem;
        padding-bottom: 2.2rem;
        max-width: 1200px;
        position: relative;
        z-index: 1;
      }}

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

      /* Background */
      {"[data-testid='stAppViewContainer']{overflow:hidden !important;}" if is_login else ""}

      /* LOGIN overlay (IMPORTANTE: overlay NO bloquea inputs) */
      .login-overlay {{
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.40);
        z-index: 9998;
        pointer-events: none;   /* <-- NO bloquea click/teclado */
      }}

      .login-wrap {{
        position: fixed;
        inset: 0;
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 18px;
        z-index: 9999;
        pointer-events: auto;   /* <-- login sí recibe click/teclado */
      }}

      .login-card {{
        width: min(560px, 94vw);
        background: rgba(255,255,255,0.97);
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

      /* ===== FIX FRANJA BLANCA (widgets flotantes Streamlit) ===== */
      [data-testid="stStatusWidget"] {{ display: none !important; }}
      [data-testid="stToastContainer"] {{ display: none !important; }}
      [data-testid="stToolbar"] {{ display: none !important; }}
      [data-testid="stDecoration"] {{ display: none !important; }}
      div[role="status"] {{ display: none !important; }}

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
            background: rgba(255,255,255,0.28);
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
# Auth (recuperación admin por Secrets)
# =========================================================
def get_user(username: str) -> Optional[Dict[str, Any]]:
    _, wss = get_db()
    for u in ws_all_records(wss["users"]):
        if str(u.get("username", "")).strip().lower() == username.strip().lower():
            return u
    return None

def ensure_admin_seed_and_optional_reset():
    _, wss = get_db()
    users = ws_all_records(wss["users"])

    # Seed si vacío
    if not users:
        ws_append(wss["users"], ["admin", hash_password("admin123"), "supervisor", "Administrador", True, _now_iso()])
        return

    # Reset opcional
    reset_pw = None
    try:
        reset_pw = st.secrets.get("ADMIN_RESET_PASSWORD")
    except Exception:
        reset_pw = None

    if reset_pw:
        ok = ws_update_row_by_key(wss["users"], "username", "admin", {"password_hash": hash_password(reset_pw), "is_active": True})
        if not ok:
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
# Signatures
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
# Minimal app state + admin seed
# =========================================================
if "user" not in st.session_state:
    st.session_state.user = None

ensure_admin_seed_and_optional_reset()


# =========================================================
# LOGIN (solo login, nada más)
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

    username = st.text_input("Usuario", placeholder="Ej: operador1", key="login_user")
    password = st.text_input("Contraseña", type="password", placeholder="********", key="login_pass")

    if st.button("Entrar", use_container_width=True, key="btn_login"):
        user = authenticate(username, password)
        if not user:
            st.error("Usuario o contraseña incorrecta.")
        else:
            st.session_state.user = user
            st.rerun()

    st.caption("Si olvidaste tu acceso, el supervisor debe crear tu usuario.")
    st.markdown("</div></div>", unsafe_allow_html=True)

    st.stop()


# =========================================================
# POST LOGIN
# =========================================================
inject_css(is_login=False)

user = st.session_state.user
role = (user.get("role") or "operador").lower()

def logout():
    st.session_state.user = None
    st.session_state.pop("selected_submission", None)

def card_open():
    st.markdown('<div class="card">', unsafe_allow_html=True)

def card_close():
    st.markdown("</div>", unsafe_allow_html=True)

def role_badge(role: str) -> str:
    role = (role or "").lower()
    return "🛡️ Supervisor" if role == "supervisor" else "👷 Operador"

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
        page = st.radio("Secciones", ["Usuarios"], index=0)


# =========================================================
# Nota: Por ahora dejo solo Usuarios para que puedas probar login
# (Luego reactivamos todo el flujo)
# =========================================================
if role != "supervisor":
    st.info("Entraste como operador. Si no ves equipos, revisa checklist_config.json.")
    st.stop()

# =========================
# Supervisor -> Usuarios
# =========================
def ws_list_users():
    _, wss = get_db()
    return pd.DataFrame(ws_all_records(wss["users"])), wss

card_open()
st.markdown("### Gestión de usuarios (Supervisor)")

users_df, wss = ws_list_users()
if not users_df.empty:
    st.dataframe(users_df[["username", "role", "full_name", "is_active", "created_at"]],
                 use_container_width=True, hide_index=True)
else:
    st.info("No hay usuarios.")

st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
st.markdown("#### Crear / actualizar usuario")

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    new_username = st.text_input("Username", placeholder="ej: operador1").strip()
with c2:
    role_new = st.selectbox("Rol", ["operador", "supervisor"], index=0)
with c3:
    full_name = st.text_input("Nombre completo", placeholder="Ej: Juan Pérez")

pw = st.text_input("Contraseña (obligatoria al crear)", type="password")
active = st.checkbox("Activo", value=True)

if st.button("Guardar usuario"):
    if not new_username or not re.match(r"^[a-zA-Z0-9_.-]{3,}$", new_username):
        st.error("Username inválido (mín 3 caracteres).")
        st.stop()

    existing = next((u for u in ws_all_records(wss["users"])
                     if str(u.get("username","")).strip().lower() == new_username.lower()), None)

    if not existing:
        if not pw:
            st.error("Para crear usuario, la contraseña es obligatoria.")
            st.stop()
        ws_append(wss["users"], [new_username, hash_password(pw), role_new, full_name, active, _now_iso()])
        st.success("Usuario creado ✅")
    else:
        updates = {"role": role_new, "full_name": full_name, "is_active": active}
        if pw:
            updates["password_hash"] = hash_password(pw)
        ok = ws_update_row_by_key(wss["users"], "username", new_username, updates)
        if ok:
            st.success("Usuario actualizado ✅")
        else:
            st.error("No pude actualizar (revisa la hoja users).")
    st.rerun()

card_close()
