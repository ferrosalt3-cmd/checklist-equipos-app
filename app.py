import base64
import io
import json
import os
import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm


# =========================
# CONFIG STREAMLIT
# =========================
st.set_page_config(page_title="Checklist Equipos", page_icon="✅", layout="wide")

# =========================
# UTIL: ESTILO / FONDO / LOGO
# =========================
def _file_to_b64(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

def set_bg_and_branding():
    fondo_b64 = _file_to_b64("fondo.png")
    logo_b64 = _file_to_b64("logo.png")

    css = """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
      .stButton>button { border-radius: 10px; padding: 0.55rem 0.9rem; }
      .stTextInput>div>div>input { border-radius: 10px; }
      .stSelectbox>div>div { border-radius: 10px; }
      .stTextArea textarea { border-radius: 10px; }
      .card {
          background: rgba(255,255,255,0.88);
          border: 1px solid rgba(0,0,0,0.06);
          border-radius: 16px;
          padding: 16px;
          box-shadow: 0 6px 18px rgba(0,0,0,0.06);
      }
      .muted { color: rgba(0,0,0,0.6); font-size: 0.9rem; }
      .title { font-size: 1.35rem; font-weight: 700; }
      .subtitle { font-size: 1.05rem; font-weight: 600; margin-top: 0.4rem; }
      .pill {
          display: inline-block;
          padding: 6px 10px;
          border-radius: 999px;
          background: rgba(0,0,0,0.05);
          font-size: 0.85rem;
          margin-right: 6px;
      }
      .hr { height: 1px; background: rgba(0,0,0,0.08); margin: 14px 0; }
      .sigbox {
          background: rgba(255,255,255,0.95);
          border: 1px dashed rgba(0,0,0,0.25);
          border-radius: 14px;
          padding: 12px;
      }
    </style>
    """

    if fondo_b64:
        css += f"""
        <style>
          .stApp {{
            background-image: url("data:image/png;base64,{fondo_b64}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
          }}
        </style>
        """

    st.markdown(css, unsafe_allow_html=True)

    # header
    col1, col2 = st.columns([1, 5])
    with col1:
        if logo_b64:
            st.image("logo.png", width=120)
    with col2:
        st.markdown('<div class="title">Checklist de Equipos</div>', unsafe_allow_html=True)
        st.markdown('<div class="muted">Operador llena • Supervisor revisa y aprueba • PDF final con firmas</div>', unsafe_allow_html=True)

set_bg_and_branding()


# =========================
# SECRETS / CREDENCIALES (STREAMLIT CLOUD)
# =========================
def get_gsheet_client() -> gspread.Client:
    """
    Usa st.secrets["gcp_service_account"] si está en Streamlit Cloud.
    En local, permite GOOGLE_APPLICATION_CREDENTIALS.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    else:
        # Local fallback
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not gac:
            raise RuntimeError("Falta GOOGLE_APPLICATION_CREDENTIALS (local) o Secrets (Streamlit Cloud).")
        creds = Credentials.from_service_account_file(gac, scopes=scopes)

    return gspread.authorize(creds)

def get_sheet_id() -> str:
    sid = st.secrets.get("GSHEET_ID") if hasattr(st, "secrets") else None
    if not sid:
        # fallback local env
        sid = os.environ.get("GSHEET_ID")
    if not sid:
        raise RuntimeError("Falta GSHEET_ID en Secrets o variable de entorno.")
    return sid


# =========================
# CHECKLIST CONFIG (JSON)
# =========================
def load_checklist_config() -> Dict[str, Any]:
    # Soporta: checklist_config.json
    cfg_path = "checklist_config.json"
    if not os.path.exists(cfg_path):
        raise RuntimeError("No encuentro checklist_config.json en la raíz del repo.")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_checklist_config()

def list_equipos() -> List[str]:
    # Espera formato: {"equipos":[{"nombre":"...","items":[...]}]}
    equipos = CONFIG.get("equipos", [])
    return [e.get("nombre", "SIN_NOMBRE") for e in equipos]

def get_items_for_equipo(nombre_equipo: str) -> List[Dict[str, Any]]:
    for e in CONFIG.get("equipos", []):
        if e.get("nombre") == nombre_equipo:
            return e.get("items", [])
    return []


# =========================
# DB HELPERS (GOOGLE SHEETS)
# =========================
SHEETS = {
    "users": "users",
    "submissions": "submissions",
    "submission_items": "submission_items",
    "approvals": "approvals",
    "photos": "photos",  # opcional (en este prototipo guardamos base64 en sheet)
}

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _today_iso() -> str:
    return date.today().isoformat()

def ensure_worksheets(sh: gspread.Spreadsheet):
    """
    Crea hojas si no existen, con headers.
    """
    def _ensure(name: str, headers: List[str]):
        try:
            ws = sh.worksheet(name)
        except Exception:
            ws = sh.add_worksheet(title=name, rows="1000", cols=str(max(10, len(headers) + 5)))
            ws.append_row(headers)
            return
        # si está vacía, agrega header
        try:
            values = ws.get_all_values()
            if not values:
                ws.append_row(headers)
        except Exception:
            pass

    _ensure(SHEETS["users"], ["username", "password_hash", "role", "full_name", "is_active", "created_at"])
    _ensure(SHEETS["submissions"], [
        "submission_id", "date", "created_at", "equipo", "operador_username", "operador_full_name",
        "estado_general", "nota", "firma_operador_b64", "status", "updated_at"
    ])
    _ensure(SHEETS["submission_items"], [
        "submission_id", "item_id", "item_text", "estado", "comentario"
    ])
    _ensure(SHEETS["photos"], [
        "submission_id", "item_id", "filename", "photo_b64"
    ])
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

def ws_update_row_by_key(ws: gspread.Worksheet, key_col: str, key_val: str, updates: Dict[str, Any]):
    """
    Busca fila por key_col==key_val y actualiza columnas dadas.
    """
    data = ws.get_all_values()
    if len(data) < 2:
        return False
    headers = data[0]
    key_idx = headers.index(key_col)
    row_idx = None
    for i in range(1, len(data)):
        if str(data[i][key_idx]).strip() == str(key_val).strip():
            row_idx = i + 1  # 1-based in Sheets
            break
    if not row_idx:
        return False

    # actualizar cada columna
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
        if str(data[i][key_idx]).strip() == str(key_val).strip():
            ws.delete_rows(i + 1)
            return True
    return False


# =========================
# AUTH / USERS
# =========================
def hash_password(pw: str) -> str:
    # simple (mejorable a bcrypt si quieres)
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def get_user(username: str) -> Optional[Dict[str, Any]]:
    _, wss = get_db()
    users = ws_all_records(wss["users"])
    for u in users:
        if str(u.get("username", "")).strip().lower() == username.strip().lower():
            return u
    return None

def ensure_admin_seed():
    """
    Si no hay usuarios, crea supervisor por defecto.
    """
    _, wss = get_db()
    users = ws_all_records(wss["users"])
    if users:
        return
    ws_append(wss["users"], [
        "admin", hash_password("admin123"), "supervisor", "Administrador", True, _now_iso()
    ])

def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    u = get_user(username)
    if not u:
        return None
    if not bool(u.get("is_active", True)):
        return None
    if u.get("password_hash") != hash_password(password):
        return None
    return u


# =========================
# SUBMISSIONS
# =========================
def make_submission_id() -> str:
    # ID corto con fecha+random
    import random, string
    return f"S{datetime.now().strftime('%Y%m%d%H%M%S')}{''.join(random.choices(string.ascii_uppercase+string.digits,k=4))}"

def create_submission(equipo: str, operador: Dict[str, Any], estado_general: str, nota: str, firma_b64: str) -> str:
    _, wss = get_db()
    sid = make_submission_id()
    ws_append(wss["submissions"], [
        sid,
        _today_iso(),
        _now_iso(),
        equipo,
        operador["username"],
        operador.get("full_name", ""),
        estado_general,
        nota,
        firma_b64,
        "PENDIENTE",
        _now_iso()
    ])
    return sid

def upsert_submission_items(submission_id: str, items_rows: List[List[Any]]):
    _, wss = get_db()
    # simple: append (no update). Para evitar duplicados en re-envío, podríamos limpiar por submission_id.
    # Aquí hacemos limpieza previa:
    ws = wss["submission_items"]
    data = ws.get_all_values()
    if len(data) >= 2:
        headers = data[0]
        sid_idx = headers.index("submission_id")
        # borrar filas de abajo hacia arriba
        rows_to_delete = []
        for i in range(1, len(data)):
            if str(data[i][sid_idx]).strip() == submission_id:
                rows_to_delete.append(i + 1)
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
        rows_to_delete = []
        for i in range(1, len(data)):
            if str(data[i][sid_idx]).strip() == submission_id:
                rows_to_delete.append(i + 1)
        for r in reversed(rows_to_delete):
            ws.delete_rows(r)

    for row in photos_rows:
        ws_append(ws, row)

def list_pending_submissions() -> pd.DataFrame:
    _, wss = get_db()
    subs = ws_all_records(wss["submissions"])
    df = pd.DataFrame(subs)
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.sort_values("created_at", ascending=False)
    return df[df["status"].astype(str).str.upper().isin(["PENDIENTE"])]

def list_all_submissions() -> pd.DataFrame:
    _, wss = get_db()
    subs = ws_all_records(wss["submissions"])
    df = pd.DataFrame(subs)
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.sort_values("created_at", ascending=False)
    return df

def get_submission_detail(submission_id: str) -> Tuple[Optional[Dict[str, Any]], pd.DataFrame, pd.DataFrame, Optional[Dict[str, Any]]]:
    _, wss = get_db()
    subs = ws_all_records(wss["submissions"])
    sub = None
    for s in subs:
        if str(s.get("submission_id")) == submission_id:
            sub = s
            break

    items = ws_all_records(wss["submission_items"])
    df_items = pd.DataFrame([i for i in items if str(i.get("submission_id")) == submission_id])

    photos = ws_all_records(wss["photos"])
    df_photos = pd.DataFrame([p for p in photos if str(p.get("submission_id")) == submission_id])

    approvals = ws_all_records(wss["approvals"])
    appr = None
    for a in approvals:
        if str(a.get("submission_id")) == submission_id:
            appr = a
            break

    return sub, df_items, df_photos, appr


# =========================
# PDF GENERATION
# =========================
def b64_to_bytes(b64str: str) -> bytes:
    return base64.b64decode(b64str.encode("utf-8"))

def make_pdf_bytes(sub: Dict[str, Any], df_items: pd.DataFrame, df_photos: pd.DataFrame, appr: Dict[str, Any]) -> bytes:
    """
    Genera PDF simple y profesional con firmas embebidas.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # header
    y = height - 2.0*cm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(2.0*cm, y, "Checklist de Equipos - Reporte Aprobado")
    y -= 0.7*cm
    c.setFont("Helvetica", 10)
    c.drawString(2.0*cm, y, f"Equipo: {sub.get('equipo','')}")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Fecha: {sub.get('date','')}   |   Creado: {sub.get('created_at','')}")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Operador: {sub.get('operador_full_name','')} ({sub.get('operador_username','')})")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Estado general: {sub.get('estado_general','')}")
    y -= 0.6*cm

    if sub.get("nota"):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(2.0*cm, y, "Nota del operador:")
        y -= 0.45*cm
        c.setFont("Helvetica", 10)
        c.drawString(2.0*cm, y, str(sub.get("nota"))[:120])
        y -= 0.6*cm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2.0*cm, y, "Detalle de ítems")
    y -= 0.4*cm
    c.setFont("Helvetica", 9)

    # tabla simple
    for _, row in df_items.fillna("").iterrows():
        line = f"- {row.get('item_text','')} | Estado: {row.get('estado','')} | Comentario: {str(row.get('comentario',''))[:60]}"
        if y < 4.0*cm:
            c.showPage()
            y = height - 2.0*cm
            c.setFont("Helvetica", 9)
        c.drawString(2.0*cm, y, line[:140])
        y -= 0.35*cm

    # sección aprobación
    if y < 8.0*cm:
        c.showPage()
        y = height - 2.0*cm

    y -= 0.4*cm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(2.0*cm, y, "Aprobación Supervisor")
    y -= 0.5*cm
    c.setFont("Helvetica", 10)
    c.drawString(2.0*cm, y, f"Supervisor: {appr.get('supervisor_full_name','')} ({appr.get('supervisor_username','')})")
    y -= 0.5*cm
    c.drawString(2.0*cm, y, f"Aprobado: {appr.get('approved_at','')}   |   Conforme: {appr.get('conforme','')}")
    y -= 0.5*cm
    if appr.get("observaciones"):
        c.drawString(2.0*cm, y, f"Observaciones: {str(appr.get('observaciones'))[:120]}")
        y -= 0.6*cm

    # Firmas
    y -= 0.2*cm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(2.0*cm, y, "Firma Operador")
    c.drawString(11.0*cm, y, "Firma Supervisor")
    y -= 3.2*cm

    # Dibujar imágenes si existen
    try:
        op_b64 = sub.get("firma_operador_b64") or ""
        sup_b64 = appr.get("firma_supervisor_b64") or ""
        if op_b64:
            op_img = io.BytesIO(b64_to_bytes(op_b64))
            c.drawImage(op_img, 2.0*cm, y, width=7.5*cm, height=3.0*cm, preserveAspectRatio=True, mask='auto')
        if sup_b64:
            sup_img = io.BytesIO(b64_to_bytes(sup_b64))
            c.drawImage(sup_img, 11.0*cm, y, width=7.5*cm, height=3.0*cm, preserveAspectRatio=True, mask='auto')
    except Exception:
        # si falla imagen, no romper el PDF
        pass

    y -= 0.7*cm

    # Evidencias (solo listado)
    if not df_photos.empty:
        if y < 5.0*cm:
            c.showPage()
            y = height - 2.0*cm

        c.setFont("Helvetica-Bold", 11)
        c.drawString(2.0*cm, y, "Evidencias (fotos adjuntas)")
        y -= 0.5*cm
        c.setFont("Helvetica", 9)
        for _, prow in df_photos.fillna("").iterrows():
            line = f"- Item {prow.get('item_id','')}: {prow.get('filename','')}"
            if y < 2.5*cm:
                c.showPage()
                y = height - 2.0*cm
                c.setFont("Helvetica", 9)
            c.drawString(2.0*cm, y, line[:140])
            y -= 0.35*cm

    c.showPage()
    c.save()
    return buf.getvalue()

def approve_submission(submission_id: str, supervisor: Dict[str, Any], conforme: str, observaciones: str, firma_supervisor_b64: str):
    _, wss = get_db()
    # cambiar status en submissions
    ok = ws_update_row_by_key(
        wss["submissions"],
        "submission_id",
        submission_id,
        {"status": "APROBADO", "updated_at": _now_iso()}
    )
    if not ok:
        raise RuntimeError("No pude actualizar submissions.")

    # generar PDF
    sub, df_items, df_photos, _appr = get_submission_detail(submission_id)
    if not sub:
        raise RuntimeError("No encontré submission.")
    appr_row = {
        "submission_id": submission_id,
        "approved_at": _now_iso(),
        "supervisor_username": supervisor["username"],
        "supervisor_full_name": supervisor.get("full_name", ""),
        "conforme": conforme,
        "observaciones": observaciones,
        "firma_supervisor_b64": firma_supervisor_b64,
    }
    pdf_bytes = make_pdf_bytes(sub, df_items, df_photos, appr_row)
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    # upsert approvals: si existe, reemplazar (delete + append)
    ws = wss["approvals"]
    data = ws.get_all_values()
    if len(data) >= 2:
        headers = data[0]
        sid_idx = headers.index("submission_id")
        rows_to_delete = []
        for i in range(1, len(data)):
            if str(data[i][sid_idx]).strip() == submission_id:
                rows_to_delete.append(i + 1)
        for r in reversed(rows_to_delete):
            ws.delete_rows(r)

    ws_append(ws, [
        submission_id, appr_row["approved_at"],
        appr_row["supervisor_username"], appr_row["supervisor_full_name"],
        conforme, observaciones, firma_supervisor_b64, pdf_b64
    ])


# =========================
# EXPORT SEMANAL (LUN-SAB)
# =========================
def get_week_range_lun_sab(ref: date) -> Tuple[date, date]:
    # Lunes=0 ... Domingo=6
    dow = ref.weekday()
    lunes = ref - timedelta(days=dow)
    sabado = lunes + timedelta(days=5)
    return lunes, sabado

def export_weekly_xlsx(start_date: date, end_date: date) -> bytes:
    df = list_all_submissions()
    if df.empty:
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            pd.DataFrame().to_excel(writer, index=False, sheet_name="submissions")
        return out.getvalue()

    # filtrar por date (col "date" string iso)
    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    mask = (df["date_dt"] >= start_date) & (df["date_dt"] <= end_date)
    dfw = df.loc[mask].drop(columns=["date_dt"])

    # Items en rango
    _, wss = get_db()
    items = pd.DataFrame(ws_all_records(wss["submission_items"]))
    if not items.empty and not dfw.empty:
        items = items[items["submission_id"].isin(dfw["submission_id"].tolist())]
    else:
        items = pd.DataFrame()

    approvals = pd.DataFrame(ws_all_records(wss["approvals"]))
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


# =========================
# SIMPLE SIGNATURE CANVAS (base64 PNG via st_canvas)
# =========================
# Usamos streamlit-drawable-canvas si está instalado; si no, fallback a upload
try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:
    CANVAS_AVAILABLE = False


def signature_input(label: str, key_prefix: str) -> Optional[str]:
    """
    Devuelve PNG en base64 (sin header) o None.
    """
    st.markdown(f"**{label}**")
    st.markdown('<div class="sigbox">', unsafe_allow_html=True)

    if CANVAS_AVAILABLE:
        canvas_res = st_canvas(
            fill_color="rgba(255, 255, 255, 0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="rgba(255, 255, 255, 1)",
            height=180,
            width=520,
            drawing_mode="freedraw",
            key=f"{key_prefix}_canvas",
        )
        st.caption("Firma con el mouse o dedo (en celular).")
        st.markdown("</div>", unsafe_allow_html=True)

        if canvas_res.image_data is not None:
            # Convertir numpy array a PNG bytes
            import numpy as np
            from PIL import Image

            img = Image.fromarray(canvas_res.image_data.astype(np.uint8))
            # validar que no sea “vacía” (muy blanca)
            # calculamos cantidad de pixeles no blancos
            gray = img.convert("L")
            arr = np.array(gray)
            nonwhite = (arr < 250).sum()
            if nonwhite < 200:  # umbral
                return None

            bio = io.BytesIO()
            img.save(bio, format="PNG")
            return base64.b64encode(bio.getvalue()).decode("utf-8")
        return None

    else:
        # fallback: subir imagen de firma
        up = st.file_uploader("Sube una imagen PNG/JPG con tu firma", type=["png", "jpg", "jpeg"], key=f"{key_prefix}_upload")
        st.markdown("</div>", unsafe_allow_html=True)
        if up:
            return base64.b64encode(up.read()).decode("utf-8")
        return None


# =========================
# UI COMPONENTS
# =========================
def card_open():
    st.markdown('<div class="card">', unsafe_allow_html=True)

def card_close():
    st.markdown("</div>", unsafe_allow_html=True)

def require_login():
    if "user" not in st.session_state:
        st.session_state.user = None

def logout():
    st.session_state.user = None
    st.session_state.pop("selected_submission", None)

def role_badge(role: str) -> str:
    role = (role or "").lower()
    if role == "supervisor":
        return "🛡️ Supervisor"
    return "👷 Operador"


# =========================
# MAIN APP
# =========================
ensure_admin_seed()
require_login()

# Top right logout
topc1, topc2 = st.columns([6, 1])
with topc2:
    if st.session_state.user:
        if st.button("Cerrar sesión"):
            logout()
            st.rerun()

# -------------------------
# LOGIN SCREEN
# -------------------------
if not st.session_state.user:
    card_open()
    st.markdown("### Iniciar sesión")
    st.caption("Si es la primera vez: usuario `admin` / contraseña `admin123` (cámbialo luego).")
    u = st.text_input("Usuario", placeholder="Ej: operador1")
    p = st.text_input("Contraseña", type="password", placeholder="********")
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Entrar"):
            user = authenticate(u, p)
            if not user:
                st.error("Usuario o contraseña incorrecta (o usuario inactivo).")
            else:
                st.session_state.user = user
                st.success(f"Bienvenido: {user.get('full_name','') or user.get('username')} • {role_badge(user.get('role',''))}")
                st.rerun()
    card_close()
    st.stop()

user = st.session_state.user
role = (user.get("role") or "operador").lower()

# -------------------------
# SIDEBAR MENU
# -------------------------
with st.sidebar:
    st.markdown("### Menú")
    st.markdown(f"**Usuario:** {user.get('full_name','') or user.get('username')}")
    st.markdown(f"**Rol:** {role_badge(role)}")
    st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

    if role == "operador":
        page = st.radio("Secciones", ["Llenar checklist", "Mis envíos"], index=0)
    else:
        page = st.radio("Secciones", ["Pendientes", "Reportes", "Usuarios", "Export semanal"], index=0)

    st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
    st.caption("Tip: En celular, abre el menú con ☰ arriba a la izquierda.")

# -------------------------
# OPERADOR
# -------------------------
if role == "operador":
    if page == "Llenar checklist":
        card_open()
        st.markdown("### Llenar checklist")
        equipo = st.selectbox("Selecciona equipo", options=list_equipos())
        items = get_items_for_equipo(equipo)
        st.markdown(f'<span class="pill">Equipo: {equipo}</span> <span class="pill">Ítems: {len(items)}</span>', unsafe_allow_html=True)
        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

        estado_general = st.selectbox(
            "Estado general del equipo",
            ["Operativo", "Operativo con falla", "Inoperativo"],
            index=0
        )
        nota = st.text_area("Observaciones del operador (opcional)", placeholder="Describe hallazgos, detalles, etc.")

        st.markdown("#### Checklist por ítem")
        estados_opciones = ["Operativo", "Operativo con falla", "Inoperativo"]

        items_rows = []
        photos_rows = []

        # Para exigir foto cuando hay falla, por ítem:
        any_missing_required_photo = False

        for idx, it in enumerate(items):
            item_id = it.get("id") or f"I{idx+1}"
            text = it.get("texto") or it.get("item") or it.get("name") or f"Item {idx+1}"

            st.markdown(f"**{idx+1}. {text}**")
            cA, cB, cC = st.columns([1.2, 2.2, 2.2])

            with cA:
                estado = st.selectbox(
                    "Estado",
                    estados_opciones,
                    key=f"estado_{item_id}",
                    label_visibility="collapsed"
                )
            with cB:
                comentario = st.text_input(
                    "Comentario",
                    key=f"coment_{item_id}",
                    placeholder="(Opcional)"
                )
            with cC:
                up = None
                if estado == "Operativo con falla":
                    up = st.file_uploader(
                        "Foto de evidencia (obligatoria si hay falla)",
                        type=["png", "jpg", "jpeg"],
                        key=f"foto_{item_id}",
                        accept_multiple_files=False
                    )
                    if not up:
                        any_missing_required_photo = True

                if up:
                    filename = up.name
                    photo_b64 = base64.b64encode(up.read()).decode("utf-8")
                    photos_rows.append([None, item_id, filename, photo_b64])  # submission_id se llena luego

            items_rows.append([None, item_id, text, estado, comentario])  # submission_id se llena luego
            st.markdown('<div class="hr"></div>', unsafe_allow_html=True)

        st.markdown("#### Firma del operador")
        firma_b64 = signature_input("Firma digital", "firma_operador")

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("Enviar a supervisor"):
                # validaciones
                if any_missing_required_photo:
                    st.error("Hay ítems con 'Operativo con falla' sin foto. Adjunta la evidencia.")
                    st.stop()
                if not firma_b64:
                    st.error("La firma del operador es obligatoria.")
                    st.stop()

                sid = create_submission(equipo, user, estado_general, nota, firma_b64)

                # completar submission_id en filas
                items_rows2 = [[sid, r[1], r[2], r[3], r[4]] for r in items_rows]
                photos_rows2 = [[sid, r[1], r[2], r[3]] for r in photos_rows]

                upsert_submission_items(sid, items_rows2)
                upsert_photos(sid, photos_rows2)

                st.success(f"Enviado ✅ ID: {sid} (queda PENDIENTE para revisión del supervisor)")
        card_close()

    elif page == "Mis envíos":
        card_open()
        st.markdown("### Mis envíos")
        df = list_all_submissions()
        if df.empty:
            st.info("Aún no tienes envíos.")
            card_close()
            st.stop()

        dfo = df[df["operador_username"].astype(str).str.lower() == user["username"].lower()]
        if dfo.empty:
            st.info("Aún no tienes envíos.")
            card_close()
            st.stop()

        # Mostrar tabla
        show_cols = ["submission_id", "date", "equipo", "estado_general", "status", "updated_at"]
        st.dataframe(dfo[show_cols], use_container_width=True, hide_index=True)

        # Descargar PDF aprobado si existe
        st.markdown("#### Descargar PDF (solo aprobados)")
        sid = st.text_input("Ingresa ID de envío aprobado", placeholder="Ej: S2026...")
        if st.button("Buscar PDF"):
            sub, df_items, df_photos, appr = get_submission_detail(sid)
            if not sub:
                st.error("No existe ese ID.")
            elif str(sub.get("status", "")).upper() != "APROBADO":
                st.warning("Ese envío aún no está APROBADO.")
            elif not appr or not appr.get("pdf_b64"):
                st.error("No encuentro PDF en approvals.")
            else:
                pdf_bytes = b64_to_bytes(appr["pdf_b64"])
                st.download_button(
                    "Descargar PDF aprobado",
                    data=pdf_bytes,
                    file_name=f"checklist_{sid}.pdf",
                    mime="application/pdf"
                )
        card_close()

# -------------------------
# SUPERVISOR
# -------------------------
else:
    if page == "Pendientes":
        card_open()
        st.markdown("### Pendientes de aprobación")

        dfp = list_pending_submissions()
        if dfp.empty:
            st.success("No hay pendientes 🎉")
            card_close()
            st.stop()

        show_cols = ["submission_id", "created_at", "date", "equipo", "operador_full_name", "estado_general", "status"]
        st.dataframe(dfp[show_cols], use_container_width=True, hide_index=True)

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.markdown("#### Revisar y aprobar")
        sid = st.text_input("ID de submission", value=st.session_state.get("selected_submission", ""), placeholder="Ej: S2026...")
        c1, c2 = st.columns([1, 2])
        with c1:
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
            if df_items.empty:
                st.warning("No hay ítems guardados.")
            else:
                st.dataframe(df_items[["item_id", "item_text", "estado", "comentario"]], use_container_width=True, hide_index=True)

            st.markdown("##### Evidencias (fotos)")
            if df_photos.empty:
                st.info("No hay fotos.")
            else:
                for _, prow in df_photos.iterrows():
                    st.markdown(f"- **Item {prow.get('item_id')}** • {prow.get('filename')}")
                    try:
                        st.image(b64_to_bytes(prow.get("photo_b64","")), width=420)
                    except Exception:
                        st.warning("No pude renderizar una foto.")

            st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
            st.markdown("##### Aprobación supervisor")

            conforme = st.selectbox("Conformidad", ["Conforme", "No conforme"], index=0)
            observ = st.text_area("Observaciones (opcional)", placeholder="Comentarios del supervisor...")

            firma_sup_b64 = signature_input("Firma del supervisor", "firma_supervisor")

            cA, cB = st.columns([1, 2])
            with cA:
                if st.button("Aprobar y generar PDF"):
                    if str(sub.get("status","")).upper() != "PENDIENTE":
                        st.warning("Este submission ya no está PENDIENTE.")
                        st.stop()
                    if not firma_sup_b64:
                        st.error("La firma del supervisor es obligatoria.")
                        st.stop()

                    approve_submission(
                        submission_id=sid,
                        supervisor=user,
                        conforme=conforme,
                        observaciones=observ,
                        firma_supervisor_b64=firma_sup_b64,
                    )
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

        # filtros
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            equipo = st.selectbox("Equipo", ["(Todos)"] + sorted(df["equipo"].dropna().unique().tolist()))
        with c2:
            status = st.selectbox("Estado del flujo", ["(Todos)", "PENDIENTE", "APROBADO"])
        with c3:
            st.caption("Tip: Usa Export semanal para descargar XLSX Lun–Sáb.")

        dff = df.copy()
        if equipo != "(Todos)":
            dff = dff[dff["equipo"] == equipo]
        if status != "(Todos)":
            dff = dff[dff["status"].astype(str).str.upper() == status]

        # KPIs
        total = len(dff)
        aprob = (dff["status"].astype(str).str.upper() == "APROBADO").sum()
        pend = (dff["status"].astype(str).str.upper() == "PENDIENTE").sum()
        fallas = (dff["estado_general"].astype(str).str.lower().str.contains("falla")).sum()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total", total)
        k2.metric("Aprobados", int(aprob))
        k3.metric("Pendientes", int(pend))
        k4.metric("Con falla (general)", int(fallas))

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.markdown("#### Últimos registros")
        show_cols = ["submission_id", "date", "equipo", "operador_full_name", "estado_general", "status", "updated_at"]
        st.dataframe(dff[show_cols].head(200), use_container_width=True, hide_index=True)

        card_close()

    elif page == "Usuarios":
        card_open()
        st.markdown("### Gestión de usuarios (Supervisor)")

        _, wss = get_db()
        users = pd.DataFrame(ws_all_records(wss["users"]))
        if users.empty:
            st.info("No hay usuarios.")
        else:
            st.dataframe(users[["username", "role", "full_name", "is_active", "created_at"]], use_container_width=True, hide_index=True)

        st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
        st.markdown("#### Crear / actualizar usuario")

        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            username = st.text_input("Username", placeholder="ej: operador1").strip()
        with c2:
            role_new = st.selectbox("Rol", ["operador", "supervisor"], index=0)
        with c3:
            full_name = st.text_input("Nombre completo", placeholder="Ej: Juan Pérez")

        pw = st.text_input("Contraseña (obligatoria al crear / opcional al actualizar)", type="password")

        cA, cB, cC = st.columns([1, 1, 2])
        with cA:
            active = st.checkbox("Activo", value=True)

        def upsert_user():
            if not username or not re.match(r"^[a-zA-Z0-9_.-]{3,}$", username):
                st.error("Username inválido. Usa mínimo 3 caracteres (letras/números/._-).")
                return
            existing = get_user(username)
            if not existing:
                if not pw:
                    st.error("Para crear usuario, la contraseña es obligatoria.")
                    return
                ws_append(wss["users"], [
                    username, hash_password(pw), role_new, full_name, active, _now_iso()
                ])
                st.success("Usuario creado ✅")
            else:
                updates = {
                    "role": role_new,
                    "full_name": full_name,
                    "is_active": active
                }
                if pw:
                    updates["password_hash"] = hash_password(pw)
                ws_update_row_by_key(wss["users"], "username", username, updates)
                st.success("Usuario actualizado ✅")

        with cB:
            if st.button("Guardar usuario"):
                upsert_user()
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

    elif page == "Export semanal":
        card_open()
        st.markdown("### Export semanal (Lunes a Sábado)")

        hoy = date.today()
        lunes, sabado = get_week_range_lun_sab(hoy)

        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            start = st.date_input("Desde (lunes)", value=lunes)
        with c2:
            end = st.date_input("Hasta (sábado)", value=sabado)
        with c3:
            st.caption("Descarga XLSX con submissions + items + approvals del rango.")

        if st.button("Generar XLSX"):
            if end < start:
                st.error("La fecha final no puede ser menor que la inicial.")
                st.stop()
            data = export_weekly_xlsx(start, end)
            st.download_button(
                "Descargar Excel",
                data=data,
                file_name=f"reporte_{start.isoformat()}_a_{end.isoformat()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        card_close()
