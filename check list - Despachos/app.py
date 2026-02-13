import os, json, base64, hashlib, uuid
from datetime import datetime, date, timedelta
import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from io import BytesIO

from streamlit_drawable_canvas import st_canvas

# ---------------------------
# Config / Utilities
# ---------------------------
APP_TITLE = "Checklist de Equipos"
CONFIG_FILE = "checklist_config.json"

STATUS_OPTIONS = ["Operativo", "Operativo con falla", "Inoperativo"]

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def get_gsheet():
    sheet_id = os.environ.get("GSHEET_ID")
    if not sheet_id:
        st.error("Falta variable de entorno GSHEET_ID (id del Google Sheet).")
        st.stop()

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not os.path.exists(creds_path):
        st.error("Falta GOOGLE_APPLICATION_CREDENTIALS apuntando al service_account.json")
        st.stop()

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)

def ws_to_df(ws):
    rows = ws.get_all_records()
    return pd.DataFrame(rows)

def ensure_tabs(db):
    needed = {
        "users": ["username","password_hash","role","active","created_at"],
        "submissions": ["submission_id","date","shift","equipment_id","operator","status_global","notes","operator_signature_png_b64","created_at"],
        "submission_items": ["submission_id","item_index","section","item","status","comment"],
        "photos": ["submission_id","item_index","filename","content_type","file_b64","created_at"],
        "approvals": ["submission_id","supervisor","ok","supervisor_notes","supervisor_signature_png_b64","approved_at","pdf_b64"],
    }
    for tab, headers in needed.items():
        try:
            ws = db.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = db.add_worksheet(title=tab, rows=2000, cols=max(10, len(headers)+2))
            ws.append_row(headers)
        # Ensure header row
        existing = ws.row_values(1)
        if existing != headers:
            # Soft approach: if empty, write headers; else leave as-is
            if len(existing) == 0 or all(h == "" for h in existing):
                ws.update("A1", [headers])

def upsert_user(db, username, password_plain, role, active=True):
    ws = db.worksheet("users")
    df = ws_to_df(ws)
    now = datetime.now().isoformat(timespec="seconds")
    password_hash = sha256(password_plain)
    if df.empty or( df["username"] == username ).sum() == 0:
        ws.append_row([username, password_hash, role, str(active), now])
        return
    # update existing row
    idx = df.index[df["username"] == username][0]
    row = idx + 2  # 1-based + header
    ws.update(f"A{row}:E{row}", [[username, password_hash, role, str(active), df.loc[idx,"created_at"] or now]])

def delete_user(db, username):
    ws = db.worksheet("users")
    df = ws_to_df(ws)
    if df.empty or (df["username"] == username).sum() == 0:
        return
    idx = df.index[df["username"] == username][0]
    ws.delete_rows(idx+2)

def authenticate(db, username, password_plain):
    ws = db.worksheet("users")
    df = ws_to_df(ws)
    if df.empty:
        return None
    match = df[(df["username"] == username) & (df["password_hash"] == sha256(password_plain)) & (df["active"].astype(str).str.lower().isin(["true","1","yes","si","sí"]))]
    if match.empty:
        return None
    row = match.iloc[0].to_dict()
    return {"username": row["username"], "role": row["role"]}

def load_equipment_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    equipment = cfg["equipment"]
    # Quick dict
    by_id = {e["equipment_id"]: e for e in equipment}
    return equipment, by_id

def monday_to_saturday_range(any_day: date):
    # Week starting Monday, ending Saturday
    monday = any_day - timedelta(days=any_day.weekday())
    saturday = monday + timedelta(days=5)
    return monday, saturday

def make_pdf(submission_row: dict, items_df: pd.DataFrame, approval_row: dict|None):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 40

    def line(txt, dy=14, font="Helvetica", size=10):
        nonlocal y
        c.setFont(font, size)
        c.drawString(40, y, txt)
        y -= dy
        if y < 60:
            c.showPage()
            y = h - 40

    line("CHECKLIST DE EQUIPOS (REVISADO)" if approval_row else "CHECKLIST DE EQUIPOS (PENDIENTE)", font="Helvetica-Bold", size=14, dy=22)
    line(f"ID: {submission_row['submission_id']}")
    line(f"Fecha: {submission_row['date']}   Turno: {submission_row.get('shift','')}")
    line(f"Equipo: {submission_row['equipment_id']}")
    line(f"Operador: {submission_row['operator']}")
    line(f"Estado global: {submission_row.get('status_global','')}")
    if submission_row.get("notes"):
        line(f"Observaciones: {submission_row['notes']}")
    line("")

    line("Items:", font="Helvetica-Bold", size=12, dy=18)
    for _, r in items_df.iterrows():
        line(f"- [{r.get('status','')}] {r.get('section','')} :: {r.get('item','')}  ({r.get('comment','')})")

    line("")
    line("Firmas:", font="Helvetica-Bold", size=12, dy=18)

    # Signatures are stored as PNG base64
    def draw_sig(b64, label):
        nonlocal y
        if not b64:
            line(f"{label}: (sin firma)")
            return
        imgdata = base64.b64decode(b64)
        tmp = BytesIO(imgdata)
        # Reportlab needs ImageReader-like; easiest: write to temp file is not ideal; use ImageReader from reportlab
        from reportlab.lib.utils import ImageReader
        ir = ImageReader(tmp)
        c.drawString(40, y, label)
        y -= 10
        c.drawImage(ir, 40, y-60, width=220, height=60, preserveAspectRatio=True, mask='auto')
        y -= 80

    draw_sig(submission_row.get("operator_signature_png_b64"), "Firma Operador")
    if approval_row:
        draw_sig(approval_row.get("supervisor_signature_png_b64"), f"Firma Supervisor ({approval_row.get('supervisor','')})")
        line(f"Conformidad: {'OK' if str(approval_row.get('ok','')).lower() in ['true','1','ok','si','sí','yes'] else 'NO'}")
        if approval_row.get("supervisor_notes"):
            line(f"Notas supervisor: {approval_row['supervisor_notes']}")
        line(f"Aprobado: {approval_row.get('approved_at','')}")

    c.showPage()
    c.save()
    pdf_bytes = buf.getvalue()
    return pdf_bytes

def b64_png_from_canvas(canvas_result):
    if not canvas_result or canvas_result.image_data is None:
        return None
    # canvas_result.image_data is RGBA numpy array; streamlit-drawable-canvas returns it as list-like
    import numpy as np
    from PIL import Image
    arr = np.array(canvas_result.image_data).astype("uint8")
    img = Image.fromarray(arr)
    out = BytesIO()
    img.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("utf-8")

# ---------------------------
# UI
# ---------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="✅", layout="wide")

# Light styling
st.markdown("""
<style>
.block-container {padding-top: 1.2rem;}
[data-testid="stSidebar"] {padding-top: 1rem;}
</style>
""", unsafe_allow_html=True)

equipment_list, equipment_by_id = load_equipment_config()

db = get_gsheet()
ensure_tabs(db)

# Session
if "user" not in st.session_state:
    st.session_state.user = None

def logout():
    st.session_state.user = None
    st.rerun()

def login_view():
    st.title(APP_TITLE)
    st.caption("Inicia sesión para continuar")
    col1, col2 = st.columns([1,1])
    with col1:
        username = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        if st.button("Entrar", use_container_width=True):
            u = authenticate(db, username.strip(), password)
            if not u:
                st.error("Usuario/contraseña inválidos o usuario inactivo.")
            else:
                st.session_state.user = u
                st.rerun()
    with col2:
        st.info("Si es la primera vez: crea un usuario supervisor en la pestaña `users` del Google Sheet o usa la función 'Bootstrap' (solo para demo).")

def bootstrap_demo_user():
    st.warning("Bootstrap demo: crea un supervisor admin/admin (cámbialo luego).")
    if st.button("Crear supervisor demo"):
        upsert_user(db, "admin", "admin", "supervisor", True)
        st.success("Listo: usuario admin / contraseña admin")

def operator_view():
    st.header("🧰 Llenar checklist")
    st.caption(f"Operador: **{st.session_state.user['username']}**")
    eq = st.selectbox("Selecciona equipo", [e["equipment_id"] for e in equipment_list])

    shift = st.selectbox("Turno", ["Día", "Noche", "Otro"])
    d = st.date_input("Fecha", value=date.today())

    status_global = st.radio("Estado general del equipo", STATUS_OPTIONS, horizontal=True)
    notes = st.text_area("Observaciones (opcional)")

    cfg = equipment_by_id[eq]
    st.subheader("Checklist")
    items = []
    for i, it in enumerate(cfg["items"]):
        with st.container(border=True):
            st.markdown(f"**{it['section']}** — {it['item']}")
            c1, c2 = st.columns([2,3])
            with c1:
                status = st.selectbox("Estado", STATUS_OPTIONS, key=f"status_{i}")
            with c2:
                comment = st.text_input("Comentario (opcional)", key=f"comment_{i}")
            photo_b64 = None
            photo_meta = None
            if status == "Operativo con falla":
                photo = st.file_uploader("Foto de la falla/daño (obligatorio)", type=["png","jpg","jpeg"], key=f"photo_{i}")
                if photo is not None:
                    photo_bytes = photo.read()
                    photo_b64 = base64.b64encode(photo_bytes).decode("utf-8")
                    photo_meta = {"filename": photo.name, "content_type": photo.type}
            items.append({"item_index": i, "section": it["section"], "item": it["item"], "status": status, "comment": comment, "photo_b64": photo_b64, "photo_meta": photo_meta})

    st.subheader("Firma digital (operador)")
    canvas_res = st_canvas(
        fill_color="rgba(255, 255, 255, 0)",
        stroke_width=3,
        stroke_color="#000000",
        background_color="#ffffff",
        height=180,
        drawing_mode="freedraw",
        key="sig_operator",
    )
    sig_b64 = b64_png_from_canvas(canvas_res)

    # validations
    missing_photos = [it for it in items if it["status"] == "Operativo con falla" and not it["photo_b64"]]
    if st.button("Enviar a supervisor", type="primary", use_container_width=True):
        if not sig_b64:
            st.error("Falta la firma del operador.")
            st.stop()
        if missing_photos:
            st.error("Hay items con 'Operativo con falla' sin foto. Adjunta la foto.")
            st.stop()

        submission_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec="seconds")

        # Write submission header
        ws_sub = db.worksheet("submissions")
        ws_sub.append_row([submission_id, d.isoformat(), shift, eq, st.session_state.user["username"], status_global, notes, sig_b64, now])

        # Write items
        ws_items = db.worksheet("submission_items")
        for it in items:
            ws_items.append_row([submission_id, it["item_index"], it["section"], it["item"], it["status"], it["comment"]])

        # Write photos
        ws_ph = db.worksheet("photos")
        for it in items:
            if it["photo_b64"]:
                ws_ph.append_row([submission_id, it["item_index"], it["photo_meta"]["filename"], it["photo_meta"]["content_type"], it["photo_b64"], now])

        st.success("Enviado. Queda pendiente de revisión del supervisor.")
        st.balloons()

def supervisor_view():
    st.header("🧑‍💼 Panel Supervisor")
    st.caption(f"Supervisor: **{st.session_state.user['username']}**")

    tab1, tab2, tab3 = st.tabs(["Pendientes", "Usuarios", "Reportes / Export"])

    ws_sub = db.worksheet("submissions")
    ws_items = db.worksheet("submission_items")
    ws_appr = db.worksheet("approvals")

    df_sub = ws_to_df(ws_sub)
    df_items = ws_to_df(ws_items)
    df_appr = ws_to_df(ws_appr)

    if not df_sub.empty:
        # pending = submissions not in approvals
        approved_ids = set(df_appr["submission_id"].astype(str).tolist()) if not df_appr.empty else set()
        df_sub["is_approved"] = df_sub["submission_id"].astype(str).isin(approved_ids)
    else:
        df_sub = pd.DataFrame(columns=["submission_id","date","equipment_id","operator","status_global","notes","created_at","is_approved"])

    with tab1:
        st.subheader("Pendientes de revisión")
        pending = df_sub[~df_sub["is_approved"]].sort_values("created_at", ascending=False)
        if pending.empty:
            st.success("No hay pendientes 🎉")
        else:
            sid = st.selectbox("Selecciona un envío", pending["submission_id"].tolist())
            row = pending[pending["submission_id"] == sid].iloc[0].to_dict()
            st.markdown(f"**Equipo:** {row['equipment_id']}  \n**Fecha:** {row['date']}  \n**Operador:** {row['operator']}  \n**Estado global:** {row.get('status_global','')}")
            if row.get("notes"):
                st.info(f"Observaciones operador: {row['notes']}")

            items = df_items[df_items["submission_id"] == sid].copy()
            if items.empty:
                st.warning("No se encontraron items.")
            else:
                st.dataframe(items[["section","item","status","comment"]], use_container_width=True, hide_index=True)

            ok = st.checkbox("Conformidad (OK)", value=True)
            supervisor_notes = st.text_area("Notas supervisor (opcional)")

            st.subheader("Firma supervisor")
            canvas_res = st_canvas(
                fill_color="rgba(255, 255, 255, 0)",
                stroke_width=3,
                stroke_color="#000000",
                background_color="#ffffff",
                height=180,
                drawing_mode="freedraw",
                key="sig_supervisor",
            )
            sig_b64 = b64_png_from_canvas(canvas_res)

            if st.button("Aprobar y generar PDF", type="primary"):
                if not sig_b64:
                    st.error("Falta la firma del supervisor.")
                    st.stop()
                # Generate PDF bytes
                pdf_bytes = make_pdf(row, items, {
                    "submission_id": sid,
                    "supervisor": st.session_state.user["username"],
                    "ok": str(ok),
                    "supervisor_notes": supervisor_notes,
                    "supervisor_signature_png_b64": sig_b64,
                    "approved_at": datetime.now().isoformat(timespec="seconds"),
                })
                pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

                ws_appr.append_row([sid, st.session_state.user["username"], str(ok), supervisor_notes, sig_b64, datetime.now().isoformat(timespec="seconds"), pdf_b64])
                st.success("Aprobado. PDF generado.")
                st.download_button("Descargar PDF", data=pdf_bytes, file_name=f"checklist_{sid}.pdf", mime="application/pdf")

    with tab2:
        st.subheader("Gestión de usuarios")
        df_users = ws_to_df(db.worksheet("users"))
        if df_users.empty:
            st.info("No hay usuarios.")
        else:
            st.dataframe(df_users, use_container_width=True, hide_index=True)

        with st.expander("Crear / actualizar usuario"):
            u = st.text_input("Username", key="new_user")
            p = st.text_input("Contraseña", type="password", key="new_pass")
            role = st.selectbox("Rol", ["operador","supervisor"], key="new_role")
            active = st.checkbox("Activo", value=True, key="new_active")
            if st.button("Guardar usuario"):
                if not u or not p:
                    st.error("Completa usuario y contraseña.")
                else:
                    upsert_user(db, u.strip(), p, role, active)
                    st.success("Guardado.")
                    st.rerun()

        with st.expander("Eliminar usuario"):
            udel = st.text_input("Username a eliminar", key="del_user")
            if st.button("Eliminar"):
                if udel:
                    delete_user(db, udel.strip())
                    st.success("Eliminado.")
                    st.rerun()

    with tab3:
        st.subheader("Dashboard")
        if df_sub.empty:
            st.info("Aún no hay registros.")
        else:
            # Basic KPIs
            c1,c2,c3 = st.columns(3)
            c1.metric("Total checklists", len(df_sub))
            c2.metric("Aprobados", int(df_sub["is_approved"].sum()))
            c3.metric("Pendientes", int((~df_sub["is_approved"]).sum()))

            # Fallas por equipo (si status_global contiene 'falla' o 'inoperativo')
            tmp = df_sub.copy()
            tmp["has_issue"] = tmp["status_global"].astype(str).isin(["Operativo con falla","Inoperativo"])
            by_eq = tmp.groupby("equipment_id").agg(total=("submission_id","count"), fallas=("has_issue","sum")).reset_index()
            st.dataframe(by_eq.sort_values(["fallas","total"], ascending=False), use_container_width=True, hide_index=True)

            st.subheader("Export semanal (Lunes–Sábado)")
            any_day = st.date_input("Semana de referencia", value=date.today(), key="week_ref")
            start, end = monday_to_saturday_range(any_day)
            st.caption(f"Rango: {start.isoformat()} a {end.isoformat()}")

            df_sub["date"] = pd.to_datetime(df_sub["date"], errors="coerce").dt.date
            mask = (df_sub["date"] >= start) & (df_sub["date"] <= end)
            week_sub = df_sub[mask].copy()

            if week_sub.empty:
                st.warning("No hay registros en ese rango.")
            else:
                # Join items for export
                week_items = df_items[df_items["submission_id"].isin(week_sub["submission_id"].astype(str))].copy()
                # XLSX in memory
                out = BytesIO()
                with pd.ExcelWriter(out, engine="openpyxl") as writer:
                    week_sub.to_excel(writer, index=False, sheet_name="submissions")
                    week_items.to_excel(writer, index=False, sheet_name="items")
                out_bytes = out.getvalue()
                st.download_button("Descargar XLSX", data=out_bytes, file_name=f"reporte_{start}_{end}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def main():
    if st.session_state.user is None:
        login_view()
        bootstrap_demo_user()
        return

    role = st.session_state.user["role"].strip().lower()
    with st.sidebar:
        st.markdown(f"**Usuario:** {st.session_state.user['username']}")
        st.markdown(f"**Rol:** {role}")
        st.button("Salir", on_click=logout, use_container_width=True)

    if role == "supervisor":
        supervisor_view()
    else:
        # operator: hide sidebar navigation by keeping single view
        operator_view()

if __name__ == "__main__":
    main()
