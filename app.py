import base64, io, json, os, re, hashlib
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm

st.set_page_config(page_title="Checklist Equipos", page_icon="✅", layout="wide")

# =========================
# Helpers
# =========================
def _now_iso(): return datetime.now().isoformat(timespec="seconds")
def _today_iso(): return date.today().isoformat()
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

# =========================
# UI / CSS (APP LOOK)
# =========================
def inject_css(login: bool):
    fondo_b64 = _safe_b64("fondo.png")
    st.markdown("""
    <style>
      #MainMenu, header, footer {visibility:hidden;}
      .block-container {max-width: 1100px; padding-top: 1rem; padding-bottom: 2rem;}
      [data-testid="stSidebar"]{background: rgba(255,255,255,0.96); border-right: 1px solid rgba(0,0,0,0.06);}
      .stButton>button{border-radius: 12px; padding: .65rem 1rem; font-weight: 700;}
      .stTextInput input,.stTextArea textarea, .stSelectbox div[data-baseweb="select"]{border-radius: 12px !important;}
      .card{background: rgba(255,255,255,0.96); border: 1px solid rgba(0,0,0,0.08); border-radius: 18px; padding: 18px; box-shadow: 0 12px 34px rgba(0,0,0,0.10);}
      .topbar{display:flex; align-items:center; justify-content:space-between; padding: 10px 14px; border-radius: 16px; background: rgba(255,255,255,0.75); border: 1px solid rgba(0,0,0,0.06);}
      .topbar h1{font-size: 18px; margin:0; font-weight: 900;}
      .topbar p{margin:0; font-size: 12px; opacity: .7;}

      /* Login */
      .login-wrap{min-height: 82vh; display:flex; justify-content:center; align-items:center;}
      .login-card{width:min(520px, 94vw); background: rgba(255,255,255,0.98); border: 1px solid rgba(0,0,0,0.10); border-radius: 20px; padding: 22px; box-shadow: 0 18px 45px rgba(0,0,0,0.20);}
      .brand{display:flex; gap:12px; align-items:center;}
      .brand-title{font-size: 20px; font-weight: 950; margin:0;}
      .brand-sub{margin:0; font-size: 13px; opacity:.7;}

      /* Kill floating white pill / widgets */
      [data-testid="stStatusWidget"], [data-testid="stToastContainer"], [data-testid="stToolbar"], [data-testid="stDecoration"], div[role="status"]{
        display:none !important;
      }
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
          }}
        </style>
        """, unsafe_allow_html=True)

def topbar(title: str, subtitle: str):
    st.markdown(f"""
      <div class="topbar">
        <div>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
      </div>
    """, unsafe_allow_html=True)

# =========================
# Google Sheets
# =========================
def get_gsheet_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    else:
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not gac:
            raise RuntimeError("Falta gcp_service_account en Secrets o GOOGLE_APPLICATION_CREDENTIALS.")
        creds = Credentials.from_service_account_file(gac, scopes=scopes)
    return gspread.authorize(creds)

def get_sheet_id() -> str:
    sid = st.secrets.get("GSHEET_ID", None)
    if not sid:
        sid = os.environ.get("GSHEET_ID")
    if not sid:
        raise RuntimeError("Falta GSHEET_ID en Secrets.")
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
            ws = sh.add_worksheet(title=name, rows="3000", cols=str(max(12, len(headers) + 5)))
            ws.append_row(headers); return
        if not ws.get_all_values():
            ws.append_row(headers)

    _ensure(SHEETS["users"], ["username","password_hash","role","full_name","is_active","created_at"])
    _ensure(SHEETS["submissions"], ["submission_id","date","created_at","equipo","operador_username","operador_full_name","estado_general","nota","firma_operador_b64","status","updated_at"])
    _ensure(SHEETS["submission_items"], ["submission_id","item_id","item_text","estado","comentario"])
    _ensure(SHEETS["photos"], ["submission_id","item_id","filename","photo_b64"])
    _ensure(SHEETS["approvals"], ["submission_id","approved_at","supervisor_username","supervisor_full_name","conforme","observaciones","firma_supervisor_b64","pdf_b64"])

@st.cache_resource(show_spinner=False)
def get_db():
    gc = get_gsheet_client()
    sh = gc.open_by_key(get_sheet_id())
    ensure_worksheets(sh)
    wss = {k: sh.worksheet(v) for k,v in SHEETS.items()}
    return sh, wss

def ws_all_records(ws): return ws.get_all_records()
def ws_append(ws, row): ws.append_row(row, value_input_option="USER_ENTERED")

def ws_update_row_by_key(ws, key_col: str, key_val: str, updates: Dict[str,Any]) -> bool:
    data = ws.get_all_values()
    if len(data) < 2: return False
    headers = data[0]
    if key_col not in headers: return False
    key_idx = headers.index(key_col)

    row_idx = None
    for i in range(1, len(data)):
        if str(data[i][key_idx]).strip().lower() == str(key_val).strip().lower():
            row_idx = i + 1; break
    if not row_idx: return False

    for col_name, new_val in updates.items():
        if col_name not in headers: continue
        col_idx = headers.index(col_name) + 1
        ws.update_cell(row_idx, col_idx, new_val)
    return True

# =========================
# Auth
# =========================
def get_user(username: str) -> Optional[Dict[str,Any]]:
    _, wss = get_db()
    for u in ws_all_records(wss["users"]):
        if str(u.get("username","")).strip().lower() == username.strip().lower():
            return u
    return None

def ensure_admin_seed_and_optional_reset():
    _, wss = get_db()
    users = ws_all_records(wss["users"])
    if not users:
        ws_append(wss["users"], ["admin", hash_password("admin123"), "supervisor", "Administrador", True, _now_iso()])
        return
    reset_pw = st.secrets.get("ADMIN_RESET_PASSWORD", None)
    if reset_pw:
        ws_update_row_by_key(wss["users"], "username", "admin", {"password_hash": hash_password(reset_pw), "is_active": True})

def authenticate(username: str, password: str) -> Optional[Dict[str,Any]]:
    u = get_user(username)
    if not u or not bool(u.get("is_active", True)): return None
    if u.get("password_hash") != hash_password(password): return None
    return u

# =========================
# Config equipos
# =========================
def load_config() -> Dict[str,Any]:
    if not os.path.exists("checklist_config.json"):
        return {"equipos": []}
    with open("checklist_config.json","r",encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and isinstance(raw.get("equipos"), list):
        return raw
    return {"equipos": []}

CONFIG = load_config()

def list_equipos() -> List[str]:
    return [e.get("nombre","SIN_NOMBRE") for e in CONFIG.get("equipos", [])]

def get_items_for_equipo(nombre: str) -> List[Dict[str,str]]:
    for e in CONFIG.get("equipos", []):
        if e.get("nombre") == nombre:
            return e.get("items", [])
    return []

# =========================
# Signatures
# =========================
try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:
    CANVAS_AVAILABLE = False

def signature_input(label: str, key_prefix: str) -> Optional[str]:
    st.markdown(f"**{label}**")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    if CANVAS_AVAILABLE:
        res = st_canvas(
            fill_color="rgba(255,255,255,0)",
            stroke_width=3,
            stroke_color="#000000",
            background_color="rgba(255,255,255,1)",
            height=160, width=520,
            drawing_mode="freedraw",
            key=f"{key_prefix}_canvas"
        )
        st.caption("Firma con el dedo (celular) o mouse (PC).")
        st.markdown("</div>", unsafe_allow_html=True)
        if res.image_data is None:
            return None
        import numpy as np
        from PIL import Image
        img = Image.fromarray(res.image_data.astype(np.uint8))
        arr = np.array(img.convert("L"))
        if (arr < 250).sum() < 220:
            return None
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        return base64.b64encode(bio.getvalue()).decode("utf-8")
    else:
        up = st.file_uploader("Sube imagen de firma (PNG/JPG)", type=["png","jpg","jpeg"], key=f"{key_prefix}_up")
        st.markdown("</div>", unsafe_allow_html=True)
        if up:
            return base64.b64encode(up.read()).decode("utf-8")
        return None

# =========================
# PDF + Data
# =========================
def make_submission_id() -> str:
    import random, string
    return f"S{datetime.now().strftime('%Y%m%d%H%M%S')}{''.join(random.choices(string.ascii_uppercase+string.digits,k=4))}"

def create_submission(equipo: str, operador: Dict[str,Any], estado_general: str, nota: str, firma_b64: str) -> str:
    _, wss = get_db()
    sid = make_submission_id()
    ws_append(wss["submissions"], [sid, _today_iso(), _now_iso(), equipo, operador["username"], operador.get("full_name",""), estado_general, nota, firma_b64, "PENDIENTE", _now_iso()])
    return sid

def replace_rows_by_submission(ws, submission_id: str):
    data = ws.get_all_values()
    if len(data) < 2: return
    headers = data[0]
    sid_idx = headers.index("submission_id")
    rows = [i+1 for i in range(1, len(data)) if str(data[i][sid_idx]).strip() == submission_id]
    for r in reversed(rows):
        ws.delete_rows(r)

def save_items_and_photos(submission_id: str, items_rows: List[List[Any]], photos_rows: List[List[Any]]):
    _, wss = get_db()
    ws_items = wss["submission_items"]
    ws_photos = wss["photos"]
    replace_rows_by_submission(ws_items, submission_id)
    replace_rows_by_submission(ws_photos, submission_id)
    for r in items_rows: ws_append(ws_items, r)
    for r in photos_rows: ws_append(ws_photos, r)

def list_all_submissions() -> pd.DataFrame:
    _, wss = get_db()
    df = pd.DataFrame(ws_all_records(wss["submissions"]))
    if df.empty: return df
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df.sort_values("created_at", ascending=False)

def get_submission_detail(submission_id: str):
    _, wss = get_db()
    subs = ws_all_records(wss["submissions"])
    sub = next((s for s in subs if str(s.get("submission_id")) == submission_id), None)
    items = pd.DataFrame([i for i in ws_all_records(wss["submission_items"]) if str(i.get("submission_id")) == submission_id])
    photos = pd.DataFrame([p for p in ws_all_records(wss["photos"]) if str(p.get("submission_id")) == submission_id])
    apprs = ws_all_records(wss["approvals"])
    appr = next((a for a in apprs if str(a.get("submission_id")) == submission_id), None)
    return sub, items, photos, appr

def make_pdf_bytes(sub: Dict[str,Any], df_items: pd.DataFrame, appr: Dict[str,Any]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 2*cm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(2*cm, y, "Checklist de Equipos - Aprobado")
    y -= 0.8*cm

    c.setFont("Helvetica", 10)
    c.drawString(2*cm, y, f"Equipo: {sub.get('equipo','')}")
    y -= 0.5*cm
    c.drawString(2*cm, y, f"Fecha: {sub.get('date','')} | Creado: {sub.get('created_at','')}")
    y -= 0.5*cm
    c.drawString(2*cm, y, f"Operador: {sub.get('operador_full_name','')} ({sub.get('operador_username','')})")
    y -= 0.5*cm
    c.drawString(2*cm, y, f"Estado general: {sub.get('estado_general','')}")
    y -= 0.6*cm
    if sub.get("nota"):
        c.drawString(2*cm, y, f"Obs operador: {str(sub.get('nota'))[:100]}")
        y -= 0.6*cm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2*cm, y, "Detalle de ítems")
    y -= 0.45*cm
    c.setFont("Helvetica", 9)

    for _, r in df_items.fillna("").iterrows():
        line = f"- {r.get('item_text','')} | {r.get('estado','')} | {str(r.get('comentario',''))[:55]}"
        if y < 3.5*cm:
            c.showPage()
            y = h - 2*cm
            c.setFont("Helvetica", 9)
        c.drawString(2*cm, y, line[:140])
        y -= 0.34*cm

    if y < 8*cm:
        c.showPage()
        y = h - 2*cm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2*cm, y, "Aprobación Supervisor")
    y -= 0.5*cm
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, y, f"Supervisor: {appr.get('supervisor_full_name','')} ({appr.get('supervisor_username','')})")
    y -= 0.5*cm
    c.drawString(2*cm, y, f"Conforme: {appr.get('conforme','')} | Fecha: {appr.get('approved_at','')}")
    y -= 0.6*cm
    if appr.get("observaciones"):
        c.drawString(2*cm, y, f"Obs supervisor: {str(appr.get('observaciones'))[:100]}")
        y -= 0.6*cm

    c.showPage()
    c.save()
    return buf.getvalue()

def approve_submission(submission_id: str, supervisor: Dict[str,Any], conforme: str, observ: str, firma_sup_b64: str):
    _, wss = get_db()
    ok = ws_update_row_by_key(wss["submissions"], "submission_id", submission_id, {"status":"APROBADO","updated_at":_now_iso()})
    if not ok:
        raise RuntimeError("No pude actualizar estado.")

    sub, df_items, _df_photos, _ = get_submission_detail(submission_id)
    appr_row = {
        "submission_id": submission_id,
        "approved_at": _now_iso(),
        "supervisor_username": supervisor["username"],
        "supervisor_full_name": supervisor.get("full_name",""),
        "conforme": conforme,
        "observaciones": observ,
        "firma_supervisor_b64": firma_sup_b64,
    }
    pdf_bytes = make_pdf_bytes(sub, df_items, appr_row)
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    ws = wss["approvals"]
    replace_rows_by_submission(ws, submission_id)
    ws_append(ws, [submission_id, appr_row["approved_at"], appr_row["supervisor_username"], appr_row["supervisor_full_name"], conforme, observ, firma_sup_b64, pdf_b64])

# =========================
# Session
# =========================
if "user" not in st.session_state:
    st.session_state.user = None
if "selected_submission" not in st.session_state:
    st.session_state.selected_submission = ""

ensure_admin_seed_and_optional_reset()

def logout():
    st.session_state.user = None
    st.session_state.selected_submission = ""

# =========================
# LOGIN
# =========================
inject_css(login=True)

if not st.session_state.user:
    st.markdown('<div class="login-wrap"><div class="login-card">', unsafe_allow_html=True)

    st.markdown('<div class="brand">', unsafe_allow_html=True)
    # Logo SOLO aquí si quieres en login; si no, comenta estas 2 líneas
    if os.path.exists("logo.png"):
        st.image("logo.png", width=70)
    st.markdown("""
      <div>
        <p class="brand-title">Checklist de Equipos</p>
        <p class="brand-sub">Acceso para Operadores y Supervisores</p>
      </div>
    """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<hr style="opacity:.15; margin:12px 0;">', unsafe_allow_html=True)

    u = st.text_input("Usuario", placeholder="Ej: operador1")
    p = st.text_input("Contraseña", type="password", placeholder="********")

    if st.button("Entrar", use_container_width=True):
        user = authenticate(u, p)
        if not user:
            st.error("Usuario o contraseña incorrecta.")
        else:
            st.session_state.user = user
            st.rerun()

    st.caption("Si olvidaste acceso, el supervisor debe crearte usuario.")
    st.markdown("</div></div>", unsafe_allow_html=True)
    st.stop()

# =========================
# APP
# =========================
inject_css(login=False)

user = st.session_state.user
role = (user.get("role") or "operador").lower()

with st.sidebar:
    # ✅ Logo SOLO en menú arriba
    if os.path.exists("logo.png"):
        st.image("logo.png", width=130)

    st.markdown("### Menú")
    st.markdown(f"**Usuario:** {user.get('full_name','') or user.get('username')}")
    st.markdown(f"**Rol:** {'🛡️ Supervisor' if role=='supervisor' else '👷 Operador'}")
    st.markdown("---")
    if role == "operador":
        page = st.radio("Secciones", ["Llenar checklist", "Mis envíos"], index=0)
    else:
        page = st.radio("Secciones", ["Pendientes", "Usuarios"], index=0)

    if st.button("Cerrar sesión", use_container_width=True):
        logout()
        st.rerun()

topbar("Checklist de Equipos", "Modo app: Operador llena • Supervisor aprueba • PDF al final")

equipos = list_equipos()

# =========================
# OPERADOR
# =========================
if role == "operador":
    if not equipos or all(e == "SIN_NOMBRE" for e in equipos):
        st.error("No se están leyendo los equipos. Reemplaza checklist_config.json por el JSON correcto.")
        st.stop()

    if page == "Llenar checklist":
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("## Llenar checklist")

        equipo = st.selectbox("Selecciona equipo", equipos)
        items = get_items_for_equipo(equipo)

        estado_general = st.selectbox("Estado general del equipo", ["Operativo", "Operativo con falla", "Inoperativo"])
        st.markdown("### Checklist por ítem")

        estados = ["Operativo", "Operativo con falla", "Inoperativo"]
        items_rows = []
        photos_rows = []
        falta_foto = False

        for idx, it in enumerate(items):
            item_id = it.get("id", f"I{idx+1:02d}")
            texto = it.get("texto", f"Item {idx+1}")

            st.markdown(f"**{idx+1}. {texto}**")
            c1, c2, c3 = st.columns([1.1, 1.8, 2.2])
            with c1:
                est = st.selectbox("Estado", estados, key=f"est_{equipo}_{item_id}", label_visibility="collapsed")
            with c2:
                com = st.text_input("Comentario (opcional)", key=f"com_{equipo}_{item_id}", label_visibility="collapsed")
            with c3:
                up = None
                if est == "Operativo con falla":
                    up = st.file_uploader("Foto evidencia (obligatoria)", type=["png","jpg","jpeg"], key=f"ph_{equipo}_{item_id}")
                    if not up:
                        falta_foto = True
                if up:
                    photos_rows.append([None, item_id, up.name, base64.b64encode(up.read()).decode("utf-8")])

            items_rows.append([None, item_id, texto, est, com])
            st.markdown('<hr style="opacity:.12;">', unsafe_allow_html=True)

        st.markdown("### Observación adicional (opcional)")
        nota = st.text_area("Escribe observaciones generales aquí", height=90)

        st.markdown("### Firma del operador")
        firma_op = signature_input("Firma digital", "firma_operador")

        if st.button("Enviar a supervisor", use_container_width=True):
            if falta_foto:
                st.error("Falta foto en ítems con 'Operativo con falla'.")
                st.stop()
            if not firma_op:
                st.error("La firma del operador es obligatoria.")
                st.stop()

            sid = create_submission(equipo, user, estado_general, nota, firma_op)
            items_rows2 = [[sid, r[1], r[2], r[3], r[4]] for r in items_rows]
            photos_rows2 = [[sid, r[1], r[2], r[3]] for r in photos_rows]
            save_items_and_photos(sid, items_rows2, photos_rows2)
            st.success(f"Enviado ✅ ID: {sid}")

        st.markdown("</div>", unsafe_allow_html=True)

    else:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("## Mis envíos")
        df = list_all_submissions()
        dfo = df[df["operador_username"].astype(str).str.lower() == user["username"].lower()] if not df.empty else pd.DataFrame()
        if dfo.empty:
            st.info("Aún no tienes envíos.")
        else:
            st.dataframe(dfo[["submission_id","date","equipo","estado_general","status","updated_at"]], use_container_width=True, hide_index=True)
        st.markdown("</div>", unsafe_allow_html=True)

# =========================
# SUPERVISOR
# =========================
else:
    _, wss = get_db()

    if page == "Pendientes":
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("## Pendientes de aprobación")

        df = list_all_submissions()
        dfp = df[df["status"].astype(str).str.upper() == "PENDIENTE"] if not df.empty else pd.DataFrame()

        if dfp.empty:
            st.success("No hay pendientes 🎉")
            st.markdown("</div>", unsafe_allow_html=True)
            st.stop()

        st.dataframe(dfp[["submission_id","created_at","date","equipo","operador_full_name","estado_general","status"]],
                     use_container_width=True, hide_index=True)

        st.markdown("### Abrir pendiente")
        sel = st.selectbox("Selecciona un ID", dfp["submission_id"].tolist())
        if st.button("Abrir detalle", use_container_width=True):
            st.session_state.selected_submission = sel
            st.rerun()

        sid = st.session_state.selected_submission.strip()
        if sid:
            st.markdown("---")
            sub, df_items, df_photos, appr = get_submission_detail(sid)
            st.markdown(f"**Equipo:** {sub.get('equipo')} • **Operador:** {sub.get('operador_full_name')} • **Estado general:** {sub.get('estado_general')}")
            if sub.get("nota"):
                st.info(f"Obs operador: {sub.get('nota')}")

            st.markdown("### Ítems")
            if df_items.empty:
                st.warning("Sin ítems.")
            else:
                st.dataframe(df_items[["item_id","item_text","estado","comentario"]], use_container_width=True, hide_index=True)

            st.markdown("### Evidencias (fotos)")
            if df_photos.empty:
                st.info("No hay fotos.")
            else:
                for _, r in df_photos.iterrows():
                    st.markdown(f"- **{r.get('item_id')}** • {r.get('filename')}")
                    try:
                        st.image(b64_to_bytes(r.get("photo_b64","")), width=420)
                    except Exception:
                        st.warning("No pude mostrar una foto.")

            st.markdown("### Observación adicional (supervisor)")
            conforme = st.selectbox("Conformidad", ["Conforme", "No conforme"])
            obs = st.text_area("Observaciones del supervisor (opcional)", height=90)

            st.markdown("### Firma del supervisor")
            firma_sup = signature_input("Firma digital", "firma_supervisor")

            if st.button("Aprobar y generar PDF", use_container_width=True):
                if not firma_sup:
                    st.error("Firma del supervisor obligatoria.")
                    st.stop()
                approve_submission(sid, user, conforme, obs, firma_sup)
                st.success("Aprobado ✅ PDF generado en approvals (pdf_b64).")
                st.session_state.selected_submission = ""
                st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)

    else:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("## Usuarios (Supervisor)")

        users_df = pd.DataFrame(ws_all_records(wss["users"]))
        if not users_df.empty:
            st.dataframe(users_df[["username","role","full_name","is_active","created_at"]], use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown("### Crear / actualizar")
        c1,c2,c3 = st.columns([1,1,2])
        with c1: uname = st.text_input("Username", placeholder="operador1").strip()
        with c2: rnew = st.selectbox("Rol", ["operador","supervisor"])
        with c3: fname = st.text_input("Nombre completo", placeholder="Juan Pérez")

        pw = st.text_input("Contraseña (obligatoria al crear)", type="password")
        active = st.checkbox("Activo", value=True)

        if st.button("Guardar usuario", use_container_width=True):
            if not uname or not re.match(r"^[a-zA-Z0-9_.-]{3,}$", uname):
                st.error("Username inválido (mín 3)."); st.stop()

            existing = next((u for u in ws_all_records(wss["users"]) if str(u.get("username","")).lower()==uname.lower()), None)
            if not existing:
                if not pw:
                    st.error("Contraseña obligatoria al crear."); st.stop()
                ws_append(wss["users"], [uname, hash_password(pw), rnew, fname, active, _now_iso()])
                st.success("Usuario creado ✅")
            else:
                upd = {"role": rnew, "full_name": fname, "is_active": active}
                if pw: upd["password_hash"] = hash_password(pw)
                ok = ws_update_row_by_key(wss["users"], "username", uname, upd)
                st.success("Usuario actualizado ✅" if ok else "No pude actualizar.")
            st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)
