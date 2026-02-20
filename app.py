import os
import io
import base64
import sqlite3
import json
import re
import tempfile
from datetime import datetime, date, timedelta
from hashlib import pbkdf2_hmac
from typing import Dict, List, Tuple

import streamlit as st
from PIL import Image
from streamlit_drawable_canvas import st_canvas

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, PageBreak
)
from reportlab.lib.enums import TA_CENTER
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


# ---------------------------
# CONFIG
# ---------------------------
APP_TITLE = "Checklist Equipos - Ferrosalt"
DB_PATH = os.path.join("data", "app.db")
ASSETS_DIR = "assets"
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")

SUPERVISOR_NOMBRE_DEFAULT = st.secrets.get("SUPERVISOR_NOMBRE", "Miguel Alarcón")
ADMIN_USER = st.secrets.get("ADMIN_USER", "administracion")
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "1234")

STATUS_OPCIONES = ["OPERATIVO", "OPERATIVO CON FALLA", "INOPERATIVO"]

# ---------------------------
# GOOGLE (ENV VARS EN RENDER)
# ---------------------------
# Debes crear estas variables en Render > Environment:
# GCP_SA_JSON            -> TODO el JSON de la service account (en una sola línea)
# SHEET_ID               -> ID del Google Sheet (no URL completa)
# DRIVE_PDFS_ID          -> folder id para PDFs
# DRIVE_PHOTOS_ID        -> folder id para fotos
# DRIVE_SIGNATURES_ID    -> folder id para firmas


@st.cache_resource
def get_google_clients():
    sa_json = os.environ.get("GCP_SA_JSON", "").strip()
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sa_json or not sheet_id:
        return None, None, None

    info = json.loads(sa_json)
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds)
    return gc, drive, sheet_id


def extract_drive_file_id(url_or_id: str) -> str:
    if not url_or_id:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", url_or_id):
        return url_or_id
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return ""


def upload_file_to_drive(local_path: str, folder_id: str) -> str:
    if not local_path or not os.path.exists(local_path) or not folder_id:
        return ""
    _, drive, _ = get_google_clients()
    if not drive:
        return ""

    metadata = {"name": os.path.basename(local_path), "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)

    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id",
    ).execute()

    fid = created.get("id", "")
    return f"https://drive.google.com/file/d/{fid}/view" if fid else ""


def download_drive_file(file_id_or_url: str, suffix: str = "") -> str:
    file_id = extract_drive_file_id(file_id_or_url)
    if not file_id:
        return ""
    _, drive, _ = get_google_clients()
    if not drive:
        return ""

    fd, out_path = tempfile.mkstemp(suffix=suffix or ".bin")
    os.close(fd)

    request = drive.files().get_media(fileId=file_id)
    with open(out_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return out_path


def append_row_sheet(sheet_name: str, row: list):
    gc, _, sheet_id = get_google_clients()
    if not gc:
        return
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")


def update_report_row_in_sheet(report_id: int, updates: dict):
    """
    Busca report_id en la hoja 'reports' (columna A) y actualiza columnas por header.
    updates = {"supervisor_user": "...", "pdf_path": "...", ...}
    """
    gc, _, sheet_id = get_google_clients()
    if not gc:
        return

    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet("reports")

    headers = ws.row_values(1)
    if not headers:
        return

    cell = ws.find(str(report_id))
    if not cell:
        return

    row_idx = cell.row
    for key, val in updates.items():
        if key in headers:
            col_idx = headers.index(key) + 1
            ws.update_cell(row_idx, col_idx, val)


# ---------------------------
# EQUIPOS + CHECKLISTS (NO TOCAR)
# ---------------------------
EQUIPOS = [
    {"tipo": "apilador", "codigo": "AP1", "nombre": "Apilador 1"},
    {"tipo": "apilador", "codigo": "AP3", "nombre": "Apilador 3"},
    {"tipo": "apilador", "codigo": "AP4", "nombre": "Apilador 4"},
    {"tipo": "transpaleta", "codigo": "TP3", "nombre": "Transpaleta 3"},
    {"tipo": "transpaleta", "codigo": "TP4", "nombre": "Transpaleta 4"},
    {"tipo": "electrico", "codigo": "ME7", "nombre": "Montacargas Eléctrico 7 (ME7)"},
    {"tipo": "electrico", "codigo": "ME8", "nombre": "Montacargas Eléctrico 8 (ME8)"},
    {"tipo": "electrico", "codigo": "ME11", "nombre": "Montacargas Eléctrico 11 (ME11)"},
    {"tipo": "combustion", "codigo": "MC5", "nombre": "Montacargas Combustión 5"},
]

CHECKLISTS: Dict[str, List[Tuple[str, List[str]]]] = {
    "apilador": [
        ("INSPECCIÓN VISUAL Y SENSORIAL", [
            "Pintura", "Espejos", "Extintor", "Rueda central", "Rueda de carga", "Rueda de apoyo",
            "Seguro de uñas", "Cargador y conectores de la batería", "Estado de las horquillas", "Estado de las cadenas"
        ]),
        ("NIVELES DE LÍQUIDOS", ["Agua destilada de la batería", "Aceite hidráulico"]),
        ("CONSERVACIÓN DEL EQUIPO", ["Lubricación de los puntos de engrase", "Engrase del mástil", "Pulverizado y limpieza del equipo"]),
        ("INSPECCIÓN DE FUNCIONAL", ["Panel visualizador (Display)", "Palanca de mando (joystick)", "Estado de la cámara",
                                     "Pedal de control", "Luces delanteras", "Luces posteriores, circulina", "Claxon/alarma de retroceso"]),
        ("OPERACIÓN DEL MÁSTIL", ["Elevación al máximo", "Estado del vástago de pistón de elevación central",
                                  "Estado del vástago de pistones de elevación lateral", "Inclinación al máximo",
                                  "Estado del vástago de pistones de inclinación", "Desplazador lateral",
                                  "Estado del vástago pistón de desplazamiento", "Estado de las mangueras hidráulicas",
                                  "Estado del carro porta horquilla retráctil", "Estado del porta horquillas tipo pantógrafo"])
    ],
    "transpaleta": [
        ("COMPONENTE", [
            "Panel indicador", "Rueda central", "Rueda de apoyo", "Rueda de carga",
            "Lubricación de puntos de engrase", "Manubrio de control", "Brazo de seguridad",
            "Claxon", "Luces delanteras", "Nivel de agua destilada de la batería",
            "Cable de alimentación", "Otros"
        ])
    ],
    "electrico": [
        ("INSPECCIÓN VISUAL Y SENSORIAL (equipo apagado)", [
            "Pintura", "Cabina", "Extintor", "Asiento", "Cinturón de seguridad", "Espejos",
            "Seguro de uñas", "Llanta de tracción", "Llanta de dirección", "Cargador y conectores de la batería"
        ]),
        ("NIVELES (equipo apagado)", ["Agua destilada de la batería", "Líquido de freno", "Aceite hidráulico", "Aceite de corona"]),
        ("FUNCIONAL (equipo encendido)", ["Nivel de carga de la batería", "Indicador de horómetro", "Indicador de freno de estacionamiento"]),
        ("CONDUCCIÓN", [
            "Marcha adelante", "Marcha atrás", "Dirección ruedas traseras", "Tracción ruedas delanteras",
            "Pedal de control", "Luces delanteras", "Luces posteriores (freno, intermitente, direccionales)",
            "Claxon/alarma de retroceso", "Freno de servicio (pedal)", "Freno de estacionamiento (manual)"
        ]),
        ("OPERACIÓN DEL MÁSTIL (equipo encendido)", [
            "Mecanismo de pistón de elevación", "Mecanismo de pistón de inclinación", "Mecanismo de pistón de desplazamiento",
            "Estado del vástago de pistón de elevación central", "Estado del vástago de pistones de elevación lateral",
            "Estado del vástago de pistones de inclinación", "Estado del vástago de pistón de desplazamiento",
            "Estado de las mangueras hidráulicas", "Estado de las cadenas", "Estado de los rodamientos"
        ]),
        ("LIMPIEZA Y CONSERVACIÓN", ["Lubricación de puntos de engrase", "Engrase del mástil", "Pulverizado de partes internas", "Limpieza del equipo", "Otros"])
    ],
    "combustion": [
        ("INSPECCIÓN VISUAL Y SENSORIAL (equipo apagado)", [
            "Pintura", "Cabina", "Extintor", "Asiento", "Cinturón de seguridad", "Espejos",
            "Seguro de uñas", "Llanta de tracción", "Llanta de dirección"
        ]),
        ("NIVELES (equipo apagado)", [
            "Aceite de motor", "Agua de radiador", "Aceite de transmisión", "Líquido de freno",
            "Aceite hidráulico", "Aceite de caja y corona", "Cargador y conectores de la batería"
        ]),
        ("FUNCIONAL (equipo encendido)", ["Nivel de carga de la batería", "Indicador de horómetro", "Indicador de freno de estacionamiento"]),
        ("CONDUCCIÓN", [
            "Marcha adelante", "Marcha atrás", "Dirección ruedas traseras", "Tracción ruedas delanteras",
            "Pedal de control", "Luces delanteras", "Luces posteriores (freno, intermitente, direccionales)",
            "Claxon/alarma de retroceso", "Freno de servicio (pedal)", "Freno de estacionamiento (manual)"
        ]),
        ("OPERACIÓN DEL MÁSTIL (equipo encendido)", [
            "Mecanismo de pistón de elevación", "Mecanismo de pistón de inclinación", "Mecanismo de pistón de desplazamiento",
            "Estado del vástago de pistón de elevación central", "Estado del vástago de pistones de elevación lateral",
            "Estado del vástago de pistones de inclinación", "Estado del vástago de pistón de desplazamiento",
            "Estado de las mangueras hidráulicas", "Estado de las cadenas", "Estado de los rodamientos"
        ]),
        ("LIMPIEZA Y CONSERVACIÓN", ["Lubricación de puntos de engrase", "Engrase del mástil", "Pulverizado de partes internas", "Limpieza del equipo", "Otros"])
    ]
}


# ---------------------------
# DB
# ---------------------------
def ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs(os.path.join("data", "photos"), exist_ok=True)
    os.makedirs(os.path.join("data", "signatures"), exist_ok=True)
    os.makedirs(os.path.join("data", "pdfs"), exist_ok=True)
    os.makedirs("assets", exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt: bytes) -> str:
    dk = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.b64encode(dk).decode("utf-8")


def init_db():
    ensure_dirs()
    conn = db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('operador','supervisor')),
            active INTEGER NOT NULL DEFAULT 1,
            salt TEXT NOT NULL,
            pw_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            created_date TEXT NOT NULL,
            equipment_tipo TEXT NOT NULL,
            equipment_codigo TEXT NOT NULL,
            equipment_nombre TEXT NOT NULL,
            horometro INTEGER NOT NULL,
            operador_user TEXT NOT NULL,
            operador_nombre TEXT NOT NULL,
            obs_general TEXT,
            resultado_final TEXT NOT NULL,
            estado_general TEXT NOT NULL,
            firma_operador_path TEXT,
            supervisor_user TEXT,
            supervisor_nombre TEXT,
            firma_supervisor_path TEXT,
            aprobado INTEGER NOT NULL DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS report_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            seccion TEXT NOT NULL,
            item TEXT NOT NULL,
            estado TEXT NOT NULL,
            observacion TEXT,
            foto_path TEXT,
            FOREIGN KEY(report_id) REFERENCES reports(id)
        )
    """)

    conn.commit()

    c.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_USER,))
    if not c.fetchone():
        salt = os.urandom(16)
        salt_b64 = base64.b64encode(salt).decode("utf-8")
        pw_hash = hash_password(ADMIN_PASSWORD, salt)
        c.execute("""
            INSERT INTO users (username, full_name, role, active, salt, pw_hash, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            ADMIN_USER, "Supervisor", "supervisor", 1, salt_b64, pw_hash,
            datetime.now().isoformat(timespec="seconds")
        ))
        conn.commit()


# ---------------------------
# AUTH
# ---------------------------
def auth_user(username: str, password: str):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),))
    row = c.fetchone()
    if not row:
        return None
    salt = base64.b64decode(row["salt"])
    pw_hash = hash_password(password, salt)
    if pw_hash != row["pw_hash"]:
        return None
    return dict(row)


def create_user(username: str, full_name: str, password: str, role: str, active: bool):
    conn = db()
    c = conn.cursor()
    salt = os.urandom(16)
    salt_b64 = base64.b64encode(salt).decode("utf-8")
    pw_hash = hash_password(password, salt)
    c.execute("""
        INSERT INTO users (username, full_name, role, active, salt, pw_hash, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (
        username.strip(), full_name.strip(), role, 1 if active else 0,
        salt_b64, pw_hash, datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()


def fetch_users():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, username, full_name, role, active, created_at FROM users ORDER BY created_at DESC")
    return [dict(r) for r in c.fetchall()]


# ---------------------------
# LOGIC
# ---------------------------
def compute_result(items_estado: List[str]) -> Tuple[str, str]:
    if any(s == "INOPERATIVO" for s in items_estado):
        return ("INOPERATIVO", "NO APTO")
    if any(s == "OPERATIVO CON FALLA" for s in items_estado):
        return ("FALLA", "RESTRICCIONES")
    return ("OPERATIVO", "APTO")


def save_uploaded_image(uploaded_file, folder: str, prefix: str) -> str:
    """
    Guarda local (por compatibilidad) y sube a Drive.
    Retorna URL de Drive (preferido). Si no hay Google config, retorna path local.
    """
    if not uploaded_file:
        return ""
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".webp"]:
        ext = ".png"
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    local_path = os.path.join("data", folder, filename)
    with open(local_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    folder_id = os.environ.get("DRIVE_PHOTOS_ID", "").strip()
    drive_url = upload_file_to_drive(local_path, folder_id)
    return drive_url or local_path


def save_signature_from_canvas(canvas_result, folder: str, prefix: str) -> str:
    """
    Guarda local (por compatibilidad) y sube a Drive.
    Retorna URL de Drive (preferido). Si no hay Google config, retorna path local.
    """
    if canvas_result is None or canvas_result.image_data is None:
        return ""
    img = Image.fromarray(canvas_result.image_data.astype("uint8")).convert("RGBA")
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    local_path = os.path.join("data", folder, filename)
    img.save(local_path)

    folder_id = os.environ.get("DRIVE_SIGNATURES_ID", "").strip()
    drive_url = upload_file_to_drive(local_path, folder_id)
    return drive_url or local_path


def insert_report(payload: dict) -> int:
    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO reports (
            created_at, created_date, equipment_tipo, equipment_codigo, equipment_nombre, horometro,
            operador_user, operador_nombre, obs_general,
            resultado_final, estado_general, firma_operador_path,
            supervisor_user, supervisor_nombre, firma_supervisor_path, aprobado
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        payload["created_at"], payload["created_date"], payload["equipment_tipo"], payload["equipment_codigo"], payload["equipment_nombre"],
        payload["horometro"], payload["operador_user"], payload["operador_nombre"], payload.get("obs_general", ""),
        payload["resultado_final"], payload["estado_general"], payload.get("firma_operador_path", ""),
        None, None, None, 0
    ))
    report_id = c.lastrowid

    for it in payload["items"]:
        c.execute("""
            INSERT INTO report_items (report_id, seccion, item, estado, observacion, foto_path)
            VALUES (?,?,?,?,?,?)
        """, (
            report_id, it["seccion"], it["item"], it["estado"],
            it.get("observacion", ""), it.get("foto_path", "")
        ))

    conn.commit()
    return report_id


def fetch_pending_reports():
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, created_at, equipment_codigo, equipment_nombre, operador_nombre, resultado_final, estado_general
        FROM reports
        WHERE aprobado=0
        ORDER BY created_at DESC
    """)
    return [dict(r) for r in c.fetchall()]


def fetch_report_detail(report_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM reports WHERE id=?", (report_id,))
    rep = c.fetchone()
    if not rep:
        return None, []
    c.execute("SELECT * FROM report_items WHERE report_id=? ORDER BY id ASC", (report_id,))
    items = [dict(r) for r in c.fetchall()]
    return dict(rep), items


def approve_report(report_id: int, supervisor_user: str, supervisor_nombre: str, firma_path: str):
    conn = db()
    c = conn.cursor()
    c.execute("""
        UPDATE reports
        SET aprobado=1, supervisor_user=?, supervisor_nombre=?, firma_supervisor_path=?
        WHERE id=?
    """, (supervisor_user, supervisor_nombre, firma_path, report_id))
    conn.commit()


# ---------------------------
# PDF STYLES
# ---------------------------
NAVY = colors.HexColor("#0B2A5A")
styles = getSampleStyleSheet()

STYLE_TITLE = ParagraphStyle("t", parent=styles["Title"], alignment=TA_CENTER,
                             fontName="Helvetica-Bold", fontSize=14, textColor=NAVY)

STYLE_H2 = ParagraphStyle("h2", parent=styles["Heading2"],
                          fontName="Helvetica-Bold", textColor=NAVY, spaceBefore=6, spaceAfter=6)

STYLE_SMALL = ParagraphStyle("sm", parent=styles["Normal"], fontSize=9, leading=11)
STYLE_SMALL_B = ParagraphStyle("smb", parent=STYLE_SMALL, fontName="Helvetica-Bold")

STYLE_CENTER = ParagraphStyle("c", parent=STYLE_SMALL, alignment=TA_CENTER)

STYLE_CENTER_W = ParagraphStyle("cw", parent=STYLE_CENTER, textColor=colors.white, fontName="Helvetica-Bold")
STYLE_SMALL_B_W = ParagraphStyle("smbw", parent=STYLE_SMALL_B, textColor=colors.white, fontName="Helvetica-Bold")


def _rl_img(path: str, w_mm: float, h_mm: float):
    """
    Soporta:
    - path local
    - link drive https://drive.google.com/file/d/.../view
    """
    if not path:
        return None

    # si viene como link de Drive -> lo descargamos temporalmente
    if isinstance(path, str) and path.startswith("http"):
        tmp = download_drive_file(path, suffix=".png")
        if tmp and os.path.exists(tmp):
            path = tmp

    if not os.path.exists(path):
        return None
    img = RLImage(path, width=w_mm * mm, height=h_mm * mm)
    img.hAlign = "LEFT"
    return img


# ---------------------------
# PDF: CHECKLIST (Supervisor)
# ---------------------------
def generate_checklist_pdf(report_id: int) -> str:
    rep, items = fetch_report_detail(report_id)
    if not rep:
        return ""

    pdf_name = f"CHECKLIST_{rep['equipment_codigo']}_{rep['created_date']}.pdf"
    pdf_path = os.path.join("data", "pdfs", pdf_name)

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm
    )

    story = []

    logo = _rl_img(LOGO_PATH, 35, 10)
    header_tbl = Table([[logo if logo else "", Paragraph("CHECKLIST DE EQUIPO", STYLE_TITLE)]],
                       colWidths=[45 * mm, 135 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(header_tbl)

    info = [
        [Paragraph(f"<b>Equipo:</b> {rep['equipment_nombre']}", STYLE_SMALL),
         Paragraph(f"<b>Código:</b> {rep['equipment_codigo']}", STYLE_SMALL),
         Paragraph(f"<b>Tipo:</b> {rep['equipment_tipo']}", STYLE_SMALL)],
        [Paragraph(f"<b>Operador:</b> {rep['operador_nombre']}", STYLE_SMALL),
         Paragraph(f"<b>Horómetro:</b> {rep['horometro']}", STYLE_SMALL),
         Paragraph(f"<b>Fecha:</b> {rep['created_at']}", STYLE_SMALL)],
        [Paragraph(f"<b>Resultado:</b> {rep['resultado_final']}", STYLE_SMALL_B),
         Paragraph(f"<b>Estado:</b> {rep['estado_general']}", STYLE_SMALL_B),
         Paragraph("", STYLE_SMALL)],
    ]
    info_tbl = Table(info, colWidths=[70 * mm, 55 * mm, 55 * mm])
    info_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6 * mm))

    data = [[
        Paragraph("Sección", STYLE_CENTER_W),
        Paragraph("Ítem", STYLE_CENTER_W),
        Paragraph("Estado", STYLE_CENTER_W),
        Paragraph("Observación", STYLE_CENTER_W),
    ]]

    for it in items:
        data.append([
            Paragraph(it["seccion"], STYLE_SMALL),
            Paragraph(it["item"], STYLE_SMALL),
            Paragraph(it["estado"], STYLE_SMALL),
            Paragraph((it.get("observacion") or "-"), STYLE_SMALL),
        ])

    tbl = Table(data, colWidths=[55 * mm, 65 * mm, 30 * mm, 35 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("<b>Observaciones generales:</b> " + (rep.get("obs_general") or "NINGUNA"), STYLE_SMALL))

    fotos = [(it["item"], it["seccion"], it.get("foto_path") or "") for it in items if it.get("foto_path")]
    if fotos:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Fotos adjuntas (solo ítems con evidencia)", STYLE_H2))
        grid = []
        row = []
        for (item_name, sec, pth) in fotos:
            cell_story = []
            cell_story.append(Paragraph(f"<b>{item_name}</b><br/>{sec}", STYLE_SMALL))
            img = _rl_img(pth, 80, 45)
            if img:
                cell_story.append(Spacer(1, 2 * mm))
                cell_story.append(img)
            row.append(cell_story)
            if len(row) == 2:
                grid.append(row)
                row = []
        if row:
            row.append("")
            grid.append(row)

        photo_tbl = Table(grid, colWidths=[90 * mm, 90 * mm])
        photo_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(photo_tbl)

    story.append(PageBreak())
    story.append(Paragraph("Firmas", STYLE_H2))

    op_sig = _rl_img(rep.get("firma_operador_path") or "", 80, 28)
    sup_sig = _rl_img(rep.get("firma_supervisor_path") or "", 80, 28)

    sig_table = Table([
        [Paragraph("Firma Operador", STYLE_SMALL_B_W), Paragraph("Firma Supervisor", STYLE_SMALL_B_W)],
        [op_sig if op_sig else Paragraph("—", STYLE_SMALL), sup_sig if sup_sig else Paragraph("—", STYLE_SMALL)],
        [Paragraph(rep["operador_nombre"], STYLE_SMALL), Paragraph(rep.get("supervisor_nombre") or SUPERVISOR_NOMBRE_DEFAULT, STYLE_SMALL)]
    ], colWidths=[90 * mm, 90 * mm])

    sig_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(sig_table)

    doc.build(story)
    return pdf_path


# ---------------------------
# PDF: GERENCIA
# ---------------------------
def _chart_pie_resultados(counts: Dict[str, int], w=180 * mm, h=75 * mm):
    labels = list(counts.keys())
    values = [counts[k] for k in labels]

    d = Drawing(w, h)
    pie = Pie()
    pie.x = 20
    pie.y = 8
    pie.width = 85 * mm
    pie.height = 65 * mm
    pie.data = values
    pie.labels = [f"{lab} ({val})" for lab, val in zip(labels, values)]
    pie.sideLabels = True
    pie.simpleLabels = False
    pie.slices.strokeWidth = 0.5
    d.add(pie)

    leg = Legend()
    leg.x = 115 * mm
    leg.y = 12
    leg.fontName = 'Helvetica'
    leg.fontSize = 8
    leg.colorNamePairs = [(pie.slices[i].fillColor, pie.labels[i]) for i in range(len(labels))]
    d.add(leg)

    d.add(String(0, h - 10, "Resultados", fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))
    return d


def _chart_bar(title: str, pairs: List[Tuple[str, int]], w=180 * mm, h=75 * mm, rotate_labels=True):
    if not pairs:
        pairs = [("Sin datos", 0)]
    labels = [p[0] for p in pairs]
    data = [[p[1] for p in pairs]]

    d = Drawing(w, h)
    d.add(String(0, h - 10, title, fontName="Helvetica-Bold", fontSize=10, fillColor=NAVY))

    bc = VerticalBarChart()
    bc.x = 10
    bc.y = 10
    bc.height = 55 * mm
    bc.width = 165 * mm
    bc.data = data
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(1, max(data[0]))
    bc.valueAxis.valueStep = max(1, int(bc.valueAxis.valueMax / 4))

    bc.categoryAxis.categoryNames = labels
    if rotate_labels:
        bc.categoryAxis.labels.boxAnchor = 'ne'
        bc.categoryAxis.labels.angle = 30
        bc.categoryAxis.labels.dy = -2
        bc.categoryAxis.labels.dx = -2

    d.add(bc)

    vmax = max(1, bc.valueAxis.valueMax)
    slots = len(data[0])
    slot_w = (165 * mm) / max(1, slots)
    for i, v in enumerate(data[0]):
        x = 10 + (i * slot_w) + slot_w * 0.38
        y = 10 + (55 * mm) * (v / vmax) + 2
        d.add(String(x, y, str(v), fontName="Helvetica-Bold", fontSize=8, fillColor=colors.black))

    return d


def generate_gerencia_pdf(start: date, end: date, supervisor_name: str) -> str:
    pdf_path = os.path.join("data", "pdfs", f"INFORME_GERENCIA_{start}_{end}.pdf")

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, created_date, operador_nombre, equipment_nombre, equipment_codigo, estado_general, resultado_final
        FROM reports
        WHERE created_date >= ? AND created_date <= ?
        ORDER BY created_date DESC, id DESC
    """, (start.isoformat(), end.isoformat()))
    rows = [dict(r) for r in c.fetchall()]

    total_informes = len(rows)
    operadores = len(set(r["operador_nombre"] for r in rows)) if rows else 0
    equipos_con_envio = len(set(r["equipment_codigo"] for r in rows)) if rows else 0
    total_equipos = len(EQUIPOS)
    equipos_sin_envio = total_equipos - equipos_con_envio

    res_counts = {"APTO": 0, "RESTRICCIONES": 0, "NO APTO": 0}
    falla_count = 0
    for r in rows:
        res_counts[r["resultado_final"]] = res_counts.get(r["resultado_final"], 0) + 1
        if r["estado_general"] in ("FALLA", "INOPERATIVO"):
            falla_count += 1

    eq_counts = {}
    for r in rows:
        eq_counts[r["equipment_codigo"]] = eq_counts.get(r["equipment_codigo"], 0) + 1
    top_eq = sorted(eq_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    op_counts = {}
    for r in rows:
        op_counts[r["operador_nombre"]] = op_counts.get(r["operador_nombre"], 0) + 1
    top_op = sorted(op_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    day_counts = {}
    for r in rows:
        dte = r["created_date"]
        day_counts[dte] = day_counts.get(dte, 0) + 1
    top_days = sorted(day_counts.items(), key=lambda x: x[0])[-14:]

    c.execute("""
        SELECT r.created_date, r.equipment_codigo, r.equipment_nombre, ri.seccion, ri.item, ri.foto_path
        FROM report_items ri
        JOIN reports r ON r.id = ri.report_id
        WHERE r.created_date >= ? AND r.created_date <= ?
          AND ri.foto_path IS NOT NULL AND ri.foto_path <> ''
        ORDER BY r.created_date DESC, r.id DESC, ri.id ASC
    """, (start.isoformat(), end.isoformat()))
    photo_rows = [dict(r) for r in c.fetchall()]

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm
    )
    story = []

    logo = _rl_img(LOGO_PATH, 35, 10)
    header_tbl = Table([[logo if logo else "", Paragraph("INFORME GERENCIA - CHECKLIST EQUIPOS", STYLE_TITLE)]],
                       colWidths=[45 * mm, 135 * mm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(header_tbl)
    story.append(Paragraph(f"Rango: <b>{start}</b> a <b>{end}</b>  |  Supervisor: <b>{supervisor_name}</b>", STYLE_SMALL))
    story.append(Spacer(1, 4 * mm))

    kpi_data = [
        ["Informes", "Operadores", "Equipos (Total)", "Equipos con envío", "Equipos sin envío", "Fallas"],
        [str(total_informes), str(operadores), str(total_equipos), str(equipos_con_envio), str(equipos_sin_envio), str(falla_count)]
    ]
    kpi_tbl = Table(kpi_data, colWidths=[30 * mm, 30 * mm, 32 * mm, 32 * mm, 32 * mm, 28 * mm])
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, 1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    kpi_tbl._cellvalues[0] = [
        Paragraph("Informes", STYLE_CENTER_W),
        Paragraph("Operadores", STYLE_CENTER_W),
        Paragraph("Equipos (Total)", STYLE_CENTER_W),
        Paragraph("Equipos con envío", STYLE_CENTER_W),
        Paragraph("Equipos sin envío", STYLE_CENTER_W),
        Paragraph("Fallas", STYLE_CENTER_W),
    ]
    story.append(kpi_tbl)
    story.append(Spacer(1, 6 * mm))

    story.append(_chart_pie_resultados(res_counts))
    story.append(Spacer(1, 4 * mm))
    story.append(_chart_bar("Top equipos (envíos)", [(k, v) for k, v in top_eq]))
    story.append(PageBreak())

    story.append(header_tbl)
    story.append(Paragraph("Dashboard adicional", STYLE_H2))
    story.append(_chart_bar("Top operadores (envíos)", [(k, v) for k, v in top_op]))
    story.append(Spacer(1, 4 * mm))
    story.append(_chart_bar("Envíos por día (últimos 14)", [(k, v) for k, v in top_days]))
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Detalle de registros (rango)", STYLE_H2))
    reg_data = [[
        Paragraph("Fecha", STYLE_CENTER_W),
        Paragraph("Operador", STYLE_CENTER_W),
        Paragraph("Equipo", STYLE_CENTER_W),
        Paragraph("Código", STYLE_CENTER_W),
        Paragraph("Estado", STYLE_CENTER_W),
        Paragraph("Resultado", STYLE_CENTER_W),
    ]]
    for r in rows:
        reg_data.append([
            Paragraph(r["created_date"], STYLE_SMALL),
            Paragraph(r["operador_nombre"], STYLE_SMALL),
            Paragraph(r["equipment_nombre"], STYLE_SMALL),
            Paragraph(r["equipment_codigo"], STYLE_SMALL),
            Paragraph(r["estado_general"], STYLE_SMALL),
            Paragraph(r["resultado_final"], STYLE_SMALL),
        ])

    reg_tbl = Table(reg_data, colWidths=[20 * mm, 35 * mm, 60 * mm, 18 * mm, 25 * mm, 25 * mm], repeatRows=1)
    reg_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(reg_tbl)

    if photo_rows:
        story.append(PageBreak())
        story.append(header_tbl)
        story.append(Paragraph("Fotos de fallas (rango seleccionado)", STYLE_H2))
        story.append(Paragraph("Evidencia adjunta (útil para compras/repuestos).", STYLE_SMALL))
        story.append(Spacer(1, 4 * mm))

        grid = []
        row = []
        for pr in photo_rows:
            cell_story = []
            cell_story.append(Paragraph(
                f"<b>{pr['equipment_nombre']} ({pr['equipment_codigo']})</b><br/>"
                f"{pr['created_date']}<br/>"
                f"{pr['seccion']}<br/><b>{pr['item']}</b>",
                STYLE_SMALL
            ))
            img = _rl_img(pr["foto_path"], 80, 45)
            if img:
                cell_story.append(Spacer(1, 2 * mm))
                cell_story.append(img)
            else:
                cell_story.append(Paragraph("Foto no disponible", STYLE_SMALL))
            row.append(cell_story)

            if len(row) == 2:
                grid.append(row)
                row = []

            if len(grid) == 6:
                photo_tbl = Table(grid, colWidths=[90 * mm, 90 * mm])
                photo_tbl.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]))
                story.append(photo_tbl)
                story.append(PageBreak())
                story.append(header_tbl)
                story.append(Paragraph("Fotos de fallas (continuación)", STYLE_H2))
                story.append(Spacer(1, 4 * mm))
                grid = []

        if row:
            row.append("")
            grid.append(row)
        if grid:
            photo_tbl = Table(grid, colWidths=[90 * mm, 90 * mm])
            photo_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(photo_tbl)

    doc.build(story)
    return pdf_path


# ---------------------------
# UI
# ---------------------------
def sidebar_user():
    name = st.session_state.get("full_name")
    role = st.session_state.get("role")
    if not name or not role:
        return

    st.sidebar.markdown(f"### 👤 {name}")
    st.sidebar.markdown(f"🔑 **Rol:** {role}")
    if st.sidebar.button("Cerrar sesión"):
        st.session_state.clear()
        st.rerun()


def login_ui():
    st.title("🔐 Ingreso")
    st.caption("Operadores y supervisores ingresan con usuario y clave.")

    with st.form("login_form", clear_on_submit=False):
        u = st.text_input("Usuario")
        p = st.text_input("Clave", type="password")
        ok = st.form_submit_button("Ingresar")

    if ok:
        user = auth_user(u, p)
        if not user:
            st.error("Usuario o clave incorrectos.")
            return
        st.session_state["user"] = user["username"]
        st.session_state["role"] = user["role"]
        st.session_state["full_name"] = user["full_name"]
        st.rerun()


def _bar_list(title: str, pairs: List[Tuple[str, int]], max_items=10):
    pairs = pairs[:max_items]
    if not pairs:
        st.info("Sin datos.")
        return

    maxv = max(v for _, v in pairs) if pairs else 1

    st.markdown(f"### {title}")
    for label, value in pairs:
        pct = 0 if maxv == 0 else int((value / maxv) * 100)
        html = f"""
        <div style="border:1px solid #e5e7eb;border-radius:10px;padding:10px;margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:#111;">
            <div style="max-width:75%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{label}</div>
            <div style="font-weight:700;">{value}</div>
          </div>
          <div style="background:#f3f4f6;border-radius:10px;height:10px;margin-top:8px;overflow:hidden;">
            <div style="width:{pct}%;height:10px;background:#1f77b4;"></div>
          </div>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)


def supervisor_panel():
    st.subheader(f"🧑‍💼 Supervisor: {st.session_state.get('full_name','')}")
    tabs = st.tabs(["Usuarios", "Pendientes", "Panel de control", "Informe Gerencia (PDF)"])

    with tabs[0]:
        st.markdown("## Crear usuario")
        with st.form("create_user"):
            username = st.text_input("Usuario (sin espacios)")
            full_name = st.text_input("Nombre completo")
            password = st.text_input("Clave", type="password")
            role = st.selectbox("Rol", ["operador", "supervisor"])
            active = st.checkbox("Activo", value=True)
            save = st.form_submit_button("Guardar usuario")

        if save:
            try:
                if not username or not full_name or not password:
                    st.error("Completa usuario, nombre y clave.")
                else:
                    create_user(username, full_name, password, role, active)
                    st.success("✅ Usuario creado correctamente")
            except sqlite3.IntegrityError:
                st.error("Ese usuario ya existe.")

        st.markdown("## Lista de usuarios")
        st.dataframe(fetch_users(), use_container_width=True)

    with tabs[1]:
        st.markdown("## Reportes pendientes (requiere firma supervisor)")
        pending = fetch_pending_reports()
        if not pending:
            st.info("No hay reportes pendientes.")
        else:
            options = {f"{p['equipment_nombre']} ({p['equipment_codigo']}) | {p['created_at']} | {p['resultado_final']}": p["id"] for p in pending}
            choice = st.selectbox("Selecciona un informe", list(options.keys()))
            rep_id = options[choice]

            rep, items = fetch_report_detail(rep_id)
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Equipo:** {rep['equipment_nombre']}")
                st.write(f"**Código:** {rep['equipment_codigo']}")
                st.write(f"**Tipo:** {rep['equipment_tipo']}")
                st.write(f"**Horómetro:** {rep['horometro']}")
                st.write(f"**Operador:** {rep['operador_nombre']}")
            with col2:
                st.write(f"**Fecha:** {rep['created_at']}")
                st.write(f"**Resultado:** {rep['resultado_final']}")
                st.write(f"**Estado:** {rep['estado_general']}")
                st.write(f"**Obs:** {rep.get('obs_general') or 'NINGUNA'}")

            st.dataframe(items, use_container_width=True, hide_index=True)

            st.markdown("### Supervisor (firma obligatoria)")
            supervisor_nombre = st.text_input("Nombre Supervisor", value=SUPERVISOR_NOMBRE_DEFAULT, key=f"sup_name_{rep_id}")

            sig = st_canvas(
                fill_color="rgba(255,255,255,0)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#FFFFFF",
                height=120,
                width=520,
                drawing_mode="freedraw",
                key=f"sig_sup_{rep_id}"
            )

            if st.button("✅ Aprobar y generar PDF final", key=f"ap_{rep_id}"):
                firma_path = save_signature_from_canvas(sig, "signatures", f"SUP_{rep_id}")
                if not firma_path:
                    st.error("Firma supervisor obligatoria.")
                else:
                    approve_report(rep_id, st.session_state["user"], supervisor_nombre, firma_path)

                    pdf_path = generate_checklist_pdf(rep_id)

                    # Subir PDF a Drive
                    pdf_folder_id = os.environ.get("DRIVE_PDFS_ID", "").strip()
                    pdf_drive_url = upload_file_to_drive(pdf_path, pdf_folder_id)

                    # Actualizar Sheets (si está configurado)
                    update_report_row_in_sheet(rep_id, {
                        "supervisor_user": st.session_state["user"],
                        "supervisor_nombre": supervisor_nombre,
                        "supervisor_firma_path": firma_path,
                        "approved_at": datetime.now().isoformat(timespec="seconds"),
                        "estado": "APROBADO",
                        "pdf_path": pdf_drive_url,
                    })

                    st.success("Aprobado. PDF generado.")
                    if pdf_drive_url:
                        st.markdown(f"📄 **PDF en Drive:** {pdf_drive_url}")

                    with open(pdf_path, "rb") as f:
                        st.download_button("⬇️ Descargar PDF", data=f.read(),
                                           file_name=os.path.basename(pdf_path),
                                           mime="application/pdf")

    with tabs[2]:
        st.markdown("## Panel de control (Profesional)")

        rango = st.selectbox("Rango", ["Diario", "Semanal", "Mensual"], index=0, key="dash_rango")
        today = date.today()
        if rango == "Diario":
            start = today
        elif rango == "Semanal":
            start = today - timedelta(days=7)
        else:
            start = today - timedelta(days=30)

        conn = db()
        c = conn.cursor()
        c.execute("""
            SELECT created_date, operador_nombre, equipment_codigo, equipment_nombre, estado_general, resultado_final
            FROM reports
            WHERE created_date >= ?
        """, (start.isoformat(),))
        rows = [dict(r) for r in c.fetchall()]

        total = len(rows)
        operadores = len(set(r["operador_nombre"] for r in rows)) if rows else 0
        equipos_con_envio = len(set(r["equipment_codigo"] for r in rows)) if rows else 0
        total_equipos = len(EQUIPOS)
        equipos_sin_envio = total_equipos - equipos_con_envio

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Informes", total)
        k2.metric("Operadores con envíos", operadores)
        k3.metric("Equipos con envíos", equipos_con_envio)
        k4.metric("Equipos sin envío", equipos_sin_envio)

        op_counts = {}
        for r in rows:
            op_counts[r["operador_nombre"]] = op_counts.get(r["operador_nombre"], 0) + 1
        top_op = sorted(op_counts.items(), key=lambda x: x[1], reverse=True)

        eq_counts = {}
        for r in rows:
            k = f"{r['equipment_nombre']} ({r['equipment_codigo']})"
            eq_counts[k] = eq_counts.get(k, 0) + 1
        top_eq = sorted(eq_counts.items(), key=lambda x: x[1], reverse=True)

        falla_counts = {}
        for r in rows:
            if r["estado_general"] in ("FALLA", "INOPERATIVO"):
                k = f"{r['equipment_nombre']} ({r['equipment_codigo']})"
                falla_counts[k] = falla_counts.get(k, 0) + 1
        top_f = sorted(falla_counts.items(), key=lambda x: x[1], reverse=True)

        colA, colB = st.columns(2)
        with colA:
            _bar_list("Envíos por Operador (Top 10)", top_op, 10)
        with colB:
            _bar_list("Envíos por Equipo (Top 10)", top_eq, 10)

        _bar_list("Fallas por Equipo (Top 10)", top_f, 10)

        res_counts = {"APTO": 0, "RESTRICCIONES": 0, "NO APTO": 0}
        for r in rows:
            res_counts[r["resultado_final"]] = res_counts.get(r["resultado_final"], 0) + 1
        st.markdown("### Resumen Resultados")
        st.write(f"✅ APTO: **{res_counts['APTO']}**  |  ⚠️ RESTRICCIONES: **{res_counts['RESTRICCIONES']}**  |  ⛔ NO APTO: **{res_counts['NO APTO']}**")

    with tabs[3]:
        st.markdown("## Informe para Gerencia (PDF)")
        rango = st.selectbox("Tipo reporte", ["Diario", "Semanal", "Mensual"], key="ger_rango")

        today = date.today()
        if rango == "Diario":
            start = today
        elif rango == "Semanal":
            start = today - timedelta(days=7)
        else:
            start = today - timedelta(days=30)

        supervisor_nombre = st.text_input("Supervisor (para el PDF)", value=SUPERVISOR_NOMBRE_DEFAULT, key="ger_sup")

        if st.button("📄 Generar Informe Gerencia (PDF)", key="gen_ger"):
            pdf_path = generate_gerencia_pdf(start, today, supervisor_nombre)

            # opcional: subir informe gerencia a Drive también (mismo folder de PDFs)
            pdf_folder_id = os.environ.get("DRIVE_PDFS_ID", "").strip()
            pdf_drive_url = upload_file_to_drive(pdf_path, pdf_folder_id)

            st.success("Informe gerencia generado.")
            if pdf_drive_url:
                st.markdown(f"📄 **Informe en Drive:** {pdf_drive_url}")

            with open(pdf_path, "rb") as f:
                st.download_button("⬇️ Descargar Informe Gerencia (PDF)", data=f.read(),
                                   file_name=os.path.basename(pdf_path),
                                   mime="application/pdf")


# ✅✅ FIX: función que limpia SOLO el estado del checklist del operador
def _reset_operator_checklist_state():
    keys = list(st.session_state.keys())
    for k in keys:
        if (
            "::" in k
            or k.startswith("hor_")
            or k.startswith("sig_op_")
            or k.startswith("obsgen_")
            or k.startswith("send_")
        ):
            try:
                del st.session_state[k]
            except Exception:
                pass


def operator_panel():
    st.subheader(f"👷 Operador: {st.session_state.get('full_name','')}")
    st.info("Selecciona equipo → completa checklist → firma → enviar (queda PENDIENTE hasta firma del supervisor).")

    eq_label_map = {f"{e['nombre']}": e for e in EQUIPOS}

    def _on_equipo_change():
        _reset_operator_checklist_state()
        st.session_state["op_prev_sel"] = st.session_state.get("op_eq_select")
        st.rerun()

    sel = st.selectbox(
        "Equipo",
        list(eq_label_map.keys()),
        key="op_eq_select",
        on_change=_on_equipo_change
    )
    eq = eq_label_map[sel]

    horometro = st.number_input("Horómetro inicial", min_value=0, step=1, value=0, key=f"hor_{eq['codigo']}")

    st.markdown("## Lista de verificación")

    items_payload = []
    estados_all = []

    checklist = CHECKLISTS[eq["tipo"]]

    for seccion, items in checklist:
        st.markdown(f"### {seccion}")
        for item in items:
            c1, c2, c3 = st.columns([2.2, 1.2, 2.2])
            with c1:
                st.write(item)
            with c2:
                estado = st.selectbox(
                    "Estado",
                    STATUS_OPCIONES,
                    key=f"{eq['codigo']}::{seccion}::{item}::estado"
                )
            with c3:
                obs = st.text_input(
                    "Observación (si aplica)",
                    key=f"{eq['codigo']}::{seccion}::{item}::obs"
                )

            foto_path = ""
            if estado in ("OPERATIVO CON FALLA", "INOPERATIVO"):
                up = st.file_uploader(
                    f"Foto obligatoria: {item}",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"{eq['codigo']}::{seccion}::{item}::foto"
                )
                if up:
                    foto_path = save_uploaded_image(up, "photos", f"{eq['codigo']}_{item}".replace(" ", "_")[:40])

            estados_all.append(estado)
            items_payload.append({
                "seccion": seccion,
                "item": item,
                "estado": estado,
                "observacion": obs.strip(),
                "foto_path": foto_path
            })

    estado_general, resultado_final = compute_result(estados_all)

    st.markdown("## Firma operador")
    st.write(f"Resultado automático: **{resultado_final}**")

    sig = st_canvas(
        fill_color="rgba(255,255,255,0)",
        stroke_width=2,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=120,
        width=520,
        drawing_mode="freedraw",
        key=f"sig_op_{eq['codigo']}"
    )

    obs_general = st.text_area("Observaciones generales (opcional)", key=f"obsgen_{eq['codigo']}")

    if st.button("📨 Enviar reporte (queda PENDIENTE)", key=f"send_{eq['codigo']}"):
        firma_path = save_signature_from_canvas(sig, "signatures", f"OP_{eq['codigo']}")
        if not firma_path:
            st.error("La firma del operador es obligatoria.")
            return

        for it in items_payload:
            if it["estado"] in ("OPERATIVO CON FALLA", "INOPERATIVO") and not it.get("foto_path"):
                st.error(f"Falta foto obligatoria en: {it['item']}")
                return

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_date": date.today().isoformat(),
            "equipment_tipo": eq["tipo"],
            "equipment_codigo": eq["codigo"],
            "equipment_nombre": eq["nombre"],
            "horometro": int(horometro),
            "operador_user": st.session_state["user"],
            "operador_nombre": st.session_state.get("full_name", ""),
            "obs_general": obs_general.strip(),
            "estado_general": estado_general,
            "resultado_final": resultado_final,
            "firma_operador_path": firma_path,
            "items": items_payload
        }

        report_id = insert_report(payload)

        # ===== Guardar en Sheets (si está configurado) =====
        # Orden de columnas según tu hoja:
        # report_id, equipment_tipo, equipment_codigo, equipment_nombre, horometro_inicial,
        # operador_user, operador_nombre, created_at, resultado_final, observaciones_generales,
        # operador_firma_path, supervisor_user, supervisor_nombre, supervisor_firma_path,
        # approved_at, estado, pdf_path
        append_row_sheet("reports", [
            report_id,
            payload["equipment_tipo"],
            payload["equipment_codigo"],
            payload["equipment_nombre"],
            payload["horometro"],
            payload["operador_user"],
            payload["operador_nombre"],
            payload["created_at"],
            payload["resultado_final"],
            payload.get("obs_general", ""),
            payload.get("firma_operador_path", ""),
            "",  # supervisor_user
            "",  # supervisor_nombre
            "",  # supervisor_firma_path
            "",  # approved_at
            "PENDIENTE",  # estado
            "",  # pdf_path
        ])

        # report_items: report_id, seccion, item, estado, observacion, foto_path
        for it in payload["items"]:
            append_row_sheet("report_items", [
                report_id,
                it["seccion"],
                it["item"],
                it["estado"],
                it.get("observacion", ""),
                it.get("foto_path", ""),
            ])

        st.success(f"✅ Reporte enviado. ID: {report_id} (pendiente firma supervisor).")


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_db()

    if not st.session_state.get("user") or not st.session_state.get("role") or not st.session_state.get("full_name"):
        st.session_state.pop("user", None)
        st.session_state.pop("role", None)
        st.session_state.pop("full_name", None)
        login_ui()
        return

    sidebar_user()

    if st.session_state.get("role") == "supervisor":
        supervisor_panel()
    else:
        operator_panel()


if __name__ == "__main__":
    main()
