# Streamlit Checklist App (operador/supervisor) + Google Sheets + PDF

## Qué incluye este starter
- Login con roles (operador / supervisor) basado en Google Sheets.
- Operador: selecciona equipo, llena checklist (Operativo / Operativo con falla / Inoperativo), sube foto si hay falla, firma digital.
- Supervisor: ve pendientes, revisa, firma conformidad; recién ahí se genera PDF “revisado”.
- Dashboard básico y export semanal (Lunes–Sábado) a XLSX.

## Setup rápido
1. `pip install -r requirements.txt`
2. Crear un Service Account en Google Cloud y descargar `service_account.json`
3. Compartir tu Google Sheet (el “DB”) con el email del service account.
4. Exportar variables de entorno:
   - `GSHEET_ID` (id del spreadsheet)
   - `GOOGLE_APPLICATION_CREDENTIALS=/ruta/service_account.json`
5. `streamlit run app.py`

## Estructura Google Sheets (tabs)
- `users`: username, password_hash, role, active, created_at
- `submissions`: submission_id, date, shift, equipment_id, operator, status_global, notes, operator_signature_png_b64, created_at
- `submission_items`: submission_id, item_index, section, item, status, comment
- `photos`: submission_id, item_index, filename, content_type, file_b64, created_at  (simple; alternativa: subir a Drive y guardar link)
- `approvals`: submission_id, supervisor, ok, supervisor_notes, supervisor_signature_png_b64, approved_at, pdf_b64

> Nota: Guardar binarios en Sheets funciona para prototipo, pero para producción conviene Google Drive/Cloud Storage.
