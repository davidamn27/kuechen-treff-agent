from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sqlite3
import sys
import zipfile
import zlib
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "kuechen_agent.sqlite3"
DEFAULT_BLOCK_LIBRARY_PATH = DATA_DIR / "alliance_haecker_2026_concept130_blockdatenbank.csv"
DEFAULT_BLOCK_LIBRARY_VERSION = "20260523-egeraete-netto-blockpreis"
CURRENT_PROJECT_ID = "PRJ-START"
GLOBAL_BLOCK_PROJECT_ID = "__BLOCK_LIBRARY__"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    ensure_dirs()
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              commission TEXT NOT NULL,
              customer TEXT NOT NULL,
              supplier TEXT NOT NULL,
              manufacturer TEXT NOT NULL,
              catalog_year TEXT NOT NULL,
              owner TEXT NOT NULL,
              status TEXT NOT NULL,
              order_number TEXT,
              confirmation_number TEXT,
              delivery_week TEXT,
              notes TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              filename TEXT NOT NULL,
              stored_path TEXT NOT NULL,
              document_type TEXT NOT NULL,
              content_type TEXT,
              size INTEGER NOT NULL,
              uploaded_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS articles (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              article_number TEXT NOT NULL,
              description TEXT NOT NULL,
              category TEXT NOT NULL,
              quantity INTEGER NOT NULL,
              single_price REAL NOT NULL,
              block_price REAL NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              comment TEXT,
              planned_dimensions TEXT,
              manufacturer_dimensions TEXT,
              dimension_status TEXT,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS timeline (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              user TEXT NOT NULL,
              action TEXT NOT NULL,
              description TEXT NOT NULL,
              linked_file TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS mail_drafts (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              recipient TEXT NOT NULL,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS extracted_positions (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              document_id TEXT NOT NULL,
              document_type TEXT NOT NULL,
              article_number TEXT NOT NULL,
              description TEXT,
              category TEXT,
              quantity INTEGER NOT NULL,
              gross_price REAL,
              net_price REAL,
              block_number TEXT,
              price_group TEXT,
              dimensions TEXT,
              source_file TEXT NOT NULL,
              source_excerpt TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id),
              FOREIGN KEY(document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS block_rules (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              document_id TEXT NOT NULL,
              block_number TEXT NOT NULL,
              article_number TEXT NOT NULL,
              gross_price REAL,
              appliance_value REAL,
              block_price REAL,
              price_group TEXT,
              dimensions TEXT,
              chargeable INTEGER NOT NULL,
              source_file TEXT NOT NULL,
              source_excerpt TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id),
              FOREIGN KEY(document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              mail_draft_id TEXT NOT NULL,
              recipient TEXT NOT NULL,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            """
        )

        for statement in [
            "ALTER TABLE documents ADD COLUMN extracted_text TEXT",
            "ALTER TABLE documents ADD COLUMN analysis_status TEXT DEFAULT 'offen'",
            "ALTER TABLE documents ADD COLUMN analysis_notes TEXT",
            "ALTER TABLE articles ADD COLUMN order_quantity INTEGER",
            "ALTER TABLE articles ADD COLUMN confirmation_quantity INTEGER",
            "ALTER TABLE articles ADD COLUMN block_number TEXT",
            "ALTER TABLE articles ADD COLUMN price_group TEXT",
            "ALTER TABLE articles ADD COLUMN order_found INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN confirmation_found INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN source_refs TEXT",
            "ALTER TABLE articles ADD COLUMN planned_dimensions TEXT",
            "ALTER TABLE articles ADD COLUMN manufacturer_dimensions TEXT",
            "ALTER TABLE articles ADD COLUMN dimension_status TEXT",
            "ALTER TABLE extracted_positions ADD COLUMN dimensions TEXT",
            "ALTER TABLE block_rules ADD COLUMN dimensions TEXT",
            "ALTER TABLE block_rules ADD COLUMN gross_price REAL",
            "ALTER TABLE block_rules ADD COLUMN appliance_value REAL",
        ]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass

        ensure_default_block_library(connection)

        project = connection.execute("SELECT id FROM projects WHERE id = ?", (CURRENT_PROJECT_ID,)).fetchone()
        if project:
            return

        stamp = now_iso()
        connection.execute(
            """
            INSERT INTO projects (
              id, name, commission, customer, supplier, manufacturer,
              catalog_year, owner, status, order_number, confirmation_number,
              delivery_week, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CURRENT_PROJECT_ID,
                "Neues Prüfprojekt",
                "",
                "",
                "",
                "",
                "2026",
                "Max Mustermann",
                "Analyse vorbereitet",
                "",
                "",
                "",
                "Bitte Dokumente hochladen und Analyse starten.",
                stamp,
                stamp,
            ),
        )

        add_timeline(connection, "Projekt erstellt", "Projektakte wurde angelegt.", None, stamp)
        create_mail_draft(connection, CURRENT_PROJECT_ID)


def add_timeline(
    connection: sqlite3.Connection,
    action: str,
    description: str,
    linked_file: str | None = None,
    created_at: str | None = None,
    project_id: str = CURRENT_PROJECT_ID,
) -> None:
    connection.execute(
        """
        INSERT INTO timeline (id, project_id, user, action, description, linked_file, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (str(uuid4()), project_id, "Max Mustermann", action, description, linked_file, created_at or now_iso()),
    )


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def document_payload(document: dict) -> dict:
    document.pop("extracted_text", None)
    return document


def project_payload(project_id: str = CURRENT_PROJECT_ID) -> dict:
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise KeyError(project_id)

        articles = [row_to_dict(row) for row in connection.execute("SELECT * FROM articles WHERE project_id = ? ORDER BY rowid", (project_id,))]
        documents = [document_payload(row_to_dict(row)) for row in connection.execute("SELECT * FROM documents WHERE project_id = ? ORDER BY uploaded_at DESC", (project_id,))]
        positions = [row_to_dict(row) for row in connection.execute("SELECT * FROM extracted_positions WHERE project_id = ? ORDER BY created_at DESC LIMIT 100", (project_id,))]
        project_block_rules = [row_to_dict(row) for row in connection.execute("SELECT * FROM block_rules WHERE project_id = ? ORDER BY created_at DESC LIMIT 100", (project_id,))]
        library_block_rules = [row_to_dict(row) for row in connection.execute("SELECT * FROM block_rules WHERE project_id = ? ORDER BY created_at DESC LIMIT 100", (GLOBAL_BLOCK_PROJECT_ID,))]
        block_rules = project_block_rules + library_block_rules
        insight_block_rules = [
            row_to_dict(row)
            for row in connection.execute(
                "SELECT * FROM block_rules WHERE project_id IN (?, ?)",
                (project_id, GLOBAL_BLOCK_PROJECT_ID),
            )
        ]
        project_block_rule_count = connection.execute("SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ?", (project_id,)).fetchone()["count"]
        library_block_rule_count = connection.execute("SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ?", (GLOBAL_BLOCK_PROJECT_ID,)).fetchone()["count"]
        timeline = [row_to_dict(row) for row in connection.execute("SELECT * FROM timeline WHERE project_id = ? ORDER BY created_at DESC LIMIT 10", (project_id,))]
        mail = connection.execute("SELECT * FROM mail_drafts WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        outbox_count = connection.execute("SELECT COUNT(*) AS count FROM outbox WHERE project_id = ?", (project_id,)).fetchone()["count"]
        ab_block_data = get_ab_block_data(connection, project_id)

    total_savings = sum((article["single_price"] - article["block_price"]) * article["quantity"] for article in articles)
    total_net = sum(article["block_price"] * article["quantity"] for article in articles)
    questions = sum(1 for article in articles if "rückfrage" in article["status"].lower())
    open_savings = sum(max(0, (article["single_price"] - article["block_price"]) * article["quantity"]) for article in articles if "geprüft" not in article["status"].lower())
    insights = build_agent_insights(articles, insight_block_rules, ab_block_data)

    return {
        "project": row_to_dict(project),
        "summary": {
            "estimated_savings": round(total_savings, 2),
            "position_count": len(articles),
            "total_net": round(total_net, 2),
            "questions": questions,
            "matched_positions": max(0, len(articles) - questions),
            "open_savings": round(open_savings, 2),
            "block_rule_count": project_block_rule_count + library_block_rule_count,
            "project_block_rule_count": project_block_rule_count,
            "library_block_rule_count": library_block_rule_count,
            "extracted_position_count": len(positions),
            "sent_mail_count": outbox_count,
        },
        "articles": articles,
        "documents": documents,
        "positions": positions,
        "blockRules": block_rules,
        "insights": insights,
        "timeline": timeline,
        "mailDraft": row_to_dict(mail) if mail else None,
    }


def create_mail_draft(connection: sqlite3.Connection, project_id: str) -> dict:
    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    article_rows = connection.execute("SELECT quantity, single_price, block_price FROM articles WHERE project_id = ?", (project_id,)).fetchall()
    savings = sum((row["single_price"] - row["block_price"]) * row["quantity"] for row in article_rows)
    ab_text = latest_document_text(connection, project_id, "Auftragsbestätigung")
    ab_meta = extract_ab_mail_meta(ab_text or "")
    ab_block_data = extract_ab_block_data(ab_text or "") if ab_text else None
    rules = [
        row_to_dict(row)
        for row in connection.execute(
            "SELECT * FROM block_rules WHERE project_id IN (?, ?)",
            (project_id, GLOBAL_BLOCK_PROJECT_ID),
        )
    ]
    recommended_block = recommend_block_change(ab_block_data, rules)
    recipient = project["supplier"] or "Rueckfrage@haecker-kuechen.de"
    commission = ab_meta.get("commission") or project["commission"] or project["name"]

    if recommended_block:
        subject = mail_subject_for_block_change(ab_meta, commission)
        body = f"Bitte Block ändern auf {recommended_block['block_number']}."
    else:
        subject = f"Anfrage zur Prüfung der Einsparungen - Projekt {project_id}"
        body = (
            "Sehr geehrte Damen und Herren,\n\n"
            f"bitte prüfen Sie die beigefügten Positionen zur Blockverrechnung für die Kommission {commission}.\n"
            f"Nach unserer aktuellen Auswertung ergibt sich eine geschätzte offene Einsparung von {format_money(savings)}.\n\n"
            "Bitte bestätigen Sie die Korrektur oder senden Sie uns Ihre Rückmeldung zu abweichenden Positionen.\n\n"
            "Mit freundlichen Grüßen\n"
            "Max Mustermann"
        )
    draft_id = str(uuid4())
    stamp = now_iso()
    connection.execute(
        """
        INSERT INTO mail_drafts (id, project_id, recipient, subject, body, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (draft_id, project_id, recipient, subject, body, "Entwurf", stamp, stamp),
    )
    return {"id": draft_id, "recipient": recipient, "subject": subject, "body": body, "status": "Entwurf"}


def latest_document_text(connection: sqlite3.Connection, project_id: str, document_type: str) -> str:
    row = connection.execute(
        "SELECT extracted_text FROM documents WHERE project_id = ? AND document_type = ? ORDER BY uploaded_at DESC LIMIT 1",
        (project_id, document_type),
    ).fetchone()
    return row["extracted_text"] if row and row["extracted_text"] else ""


def extract_ab_mail_meta(text: str) -> dict:
    patterns = {
        "ab_number": r"\bAB-Nr\.\s*([A-Z0-9-]+)",
        "customer_number": r"\bKunden-Nr\.\s*([A-Z0-9-]+)",
        "commission": r"\bKommission\s+([A-Z0-9ÄÖÜäöüß /_.-]+)",
        "order_number": r"\bBestell-Nr\.\s*([A-Z0-9-]+)",
    }
    meta = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).strip()
            meta[key] = re.split(r"\s{2,}|\n", value)[0].strip()
    return meta


def mail_subject_for_block_change(meta: dict, commission: str) -> str:
    parts = []
    if meta.get("ab_number"):
        parts.append(f"AB-Nr. {meta['ab_number']}")
    if meta.get("customer_number"):
        parts.append(f"KD-Nr. {meta['customer_number']}")
    if commission:
        parts.append(f"Kom. {commission}")
    if meta.get("order_number"):
        parts.append(f"Best.-Nr. {meta['order_number']}")
    return "Mail zu " + " / ".join(parts) if parts else "Rückfrage zur Blockänderung"


def recommend_block_change(ab_block_data: dict | None, block_rules: list[dict]) -> dict | None:
    if not ab_block_data:
        return None
    current_block = str(ab_block_data.get("block_number") or "").strip()
    current_price = ab_block_data.get("bc_price") or 0.0
    furniture_value = ab_block_data.get("moebel_brutto") or 0.0
    appliance_value = ab_block_data.get("eg_brutto") or 0.0
    price_group = str(ab_block_data.get("price_group") or "2").strip() or "2"
    if current_price <= 0 or furniture_value <= 0:
        return None

    grouped: dict[tuple[str, str], dict] = {}
    for rule in block_rules:
        block_number = str(rule.get("block_number") or "").strip()
        rule_pg = str(rule.get("price_group") or "").strip()
        if not block_number or rule_pg != price_group or block_number == current_block:
            continue
        gross_price = rule.get("gross_price") or 0.0
        block_price = rule.get("block_price") or 0.0
        eg_value = rule.get("appliance_value") or 0.0
        if gross_price <= 0 or block_price <= 0 or eg_value <= 0 or block_price >= current_price:
            continue
        key = (block_number, rule_pg)
        if key not in grouped:
            grouped[key] = {
                "block_number": block_number,
                "price_group": rule_pg,
                "furniture_block_value": gross_price,
                "appliance_block_value": eg_value,
                "block_price": block_price,
                "fill_gross": round(furniture_value - gross_price, 2),
                "fill_net": round(appliance_value - eg_value, 2),
                "saving": round(current_price - block_price, 2),
            }

    candidates = [candidate for candidate in grouped.values() if candidate["fill_gross"] >= 0]
    if not candidates:
        candidates = list(grouped.values())
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate["furniture_block_value"],
            abs(candidate["fill_net"]),
            candidate["block_price"],
        ),
    )[0]


def regenerate_mail_draft(connection: sqlite3.Connection, project_id: str) -> None:
    connection.execute("DELETE FROM mail_drafts WHERE project_id = ?", (project_id,))
    create_mail_draft(connection, project_id)
    add_timeline(connection, "Mailentwurf erstellt", "Der Lieferantenentwurf wurde aus den aktuellen Prüfdaten erzeugt.", project_id=project_id)


def analyze_project(project_id: str) -> dict:
    with db() as connection:
        reprocess_documents(connection, project_id)
        positions = [row_to_dict(row) for row in connection.execute("SELECT * FROM extracted_positions WHERE project_id = ?", (project_id,))]
        rules = block_rules_for_project(connection, project_id)
        ab_block_data = get_ab_block_data(connection, project_id)
        has_confirmation_document = has_document_type(connection, project_id, "Auftragsbestätigung")

        connection.execute("DELETE FROM articles WHERE project_id = ?", (project_id,))
        if positions:
            write_reconciled_articles(connection, project_id, positions, rules, ab_block_data, has_confirmation_document)

        add_timeline(connection, "Analyse ausgeführt", "Artikelabgleich, Blockprüfung und Einsparungsberechnung wurden regelbasiert aktualisiert.", project_id=project_id)
        connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))
        regenerate_mail_draft(connection, project_id)

    return project_payload(project_id)


def block_rules_for_project(connection: sqlite3.Connection, project_id: str) -> list[dict]:
    return [
        row_to_dict(row)
        for row in connection.execute(
            "SELECT * FROM block_rules WHERE project_id IN (?, ?)",
            (project_id, GLOBAL_BLOCK_PROJECT_ID),
        )
    ]


def has_document_type(connection: sqlite3.Connection, project_id: str, document_type: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM documents WHERE project_id = ? AND document_type = ? LIMIT 1",
        (project_id, document_type),
    ).fetchone()
    return row is not None


def reprocess_documents(connection: sqlite3.Connection, project_id: str) -> None:
    documents = [
        row_to_dict(row)
        for row in connection.execute("SELECT * FROM documents WHERE project_id = ? ORDER BY uploaded_at", (project_id,))
    ]
    connection.execute("DELETE FROM extracted_positions WHERE project_id = ?", (project_id,))
    connection.execute("DELETE FROM block_rules WHERE project_id = ?", (project_id,))
    for document in documents:
        path = ROOT / document["stored_path"]
        if not path.exists():
            continue
        try:
            rows, text = parse_document(path, document["filename"])
            position_count, rule_count = persist_extraction(
                connection,
                project_id,
                document["id"],
                document["document_type"],
                document["filename"],
                rows,
                text,
            )
            connection.execute(
                "UPDATE documents SET analysis_notes = ? WHERE id = ?",
                (f"{position_count} Positionen und {rule_count} Blockregeln erkannt.", document["id"]),
            )
        except Exception as exc:
            connection.execute(
                "UPDATE documents SET analysis_status = ?, analysis_notes = ? WHERE id = ?",
                ("Fehler", str(exc), document["id"]),
            )


def parse_multipart(headers: dict[str, str], body: bytes) -> list[dict]:
    content_type = headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return []

    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    parts = []
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        if disposition != "form-data":
            continue
        parts.append(
            {
                "name": part.get_param("name", header="content-disposition"),
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "data": part.get_payload(decode=True) or b"",
            }
        )
    return parts


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip()
    return cleaned or "upload.bin"


def parse_document(path: Path, filename: str) -> tuple[list[dict], str]:
    suffix = filename.lower().rsplit(".", 1)[-1]
    if suffix == "csv":
        rows = parse_csv_rows(path)
        return rows, rows_to_text(rows)
    if suffix in {"xlsx", "xlsm"}:
        rows = parse_xlsx_rows(path)
        return rows, rows_to_text(rows)
    if suffix == "pdf":
        text = extract_pdf_text(path)
        block_rows = parse_block_library_rows(text)
        if block_rows:
            return block_rows, text
        return parse_text_rows(text), text
    if suffix == "xml":
        text = path.read_text(encoding="utf-8", errors="replace")
        return parse_xml_rows(text), text
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_text_rows(text), text


def parse_csv_rows(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    sample = raw[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(raw.splitlines(), delimiter=delimiter)
    if reader.fieldnames:
        return [{normalize_header(key): (value or "").strip() for key, value in row.items()} for row in reader]
    return []


def parse_xlsx_rows(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        shared = read_shared_strings(archive)
        sheet_names = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        if not sheet_names:
            return []
        table = read_sheet(archive, sheet_names[0], shared)
    if not table:
        return []
    headers = [normalize_header(cell) for cell in table[0]]
    rows = []
    for line in table[1:]:
        if not any(line):
            continue
        rows.append({headers[index]: line[index].strip() if index < len(line) else "" for index in range(len(headers))})
    return rows


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    values = []
    for item in root.findall(f"{namespace}si"):
        text = "".join(node.text or "" for node in item.iter(f"{namespace}t"))
        values.append(text)
    return values


def read_sheet(archive: zipfile.ZipFile, name: str, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(archive.read(name))
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows = []
    for row in root.iter(f"{namespace}row"):
        cells = []
        for cell in row.findall(f"{namespace}c"):
            value = cell.find(f"{namespace}v")
            text = value.text if value is not None else ""
            if cell.attrib.get("t") == "s" and text.isdigit():
                text = shared[int(text)] if int(text) < len(shared) else ""
            cells.append(html.unescape(text or ""))
        rows.append(cells)
    return rows


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_texts = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(page_texts).strip()
        if len(text) > 100:
            return text
    except Exception:
        pass

    data = path.read_bytes()
    chunks = []
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        for candidate in (stream, stream.strip()):
            try:
                chunks.append(zlib.decompress(candidate))
                break
            except zlib.error:
                continue
    raw = b"\n".join(chunks) if chunks else data
    text_parts = []
    for match in re.findall(rb"\((.*?)\)", raw, re.S):
        text_parts.append(match.decode("latin-1", errors="ignore"))
    if not text_parts:
        text_parts.append(raw.decode("latin-1", errors="ignore"))
    text = "\n".join(text_parts)
    return re.sub(r"\\([()\\])", r"\1", text)


def parse_block_library_rows(text: str) -> list[dict]:
    if "Block-Nr:" not in text or "Blockpreis" not in text:
        return []

    rows = []
    sections = re.split(r"(?=Block-Nr:\s*[A-Z0-9-]+)", text)
    for section in sections:
        block_match = re.search(r"Block-Nr:\s*([A-Z0-9-]+)\s+PG\s+([0-7](?:\s+[0-7])*)", section)
        if not block_match:
            continue
        block_number = block_match.group(1)
        price_groups = block_match.group(2).split()
        bek_match = re.search(r"BEK\s+.+?\s+zur\s+Verr\s+([0-9.,\s]+)", section)
        bek_prices = parse_price_list(bek_match.group(1) if bek_match else "")
        price_match = re.search(r"Blockpreis[^\n]*?((?:\d{1,3}(?:\.\d{3})*,\d{2}\s*)+)", section)
        block_prices = parse_price_list(price_match.group(1) if price_match else "")
        appliance_match = re.search(r"E-Geräte\s+Netto\s+zur\s+Verr\.?\s+([0-9.,\s]+)", section)
        appliance_prices = parse_price_list(appliance_match.group(1) if appliance_match else "")
        article_matches = re.finditer(
            r"(?m)^\s*\d+\s*x\s+([A-Z0-9/-]{3,24})\s+([A-Z0-9/-]{3,24})?\s+(.+?)(?=\s{2,}H:|\n|$)",
            section,
        )
        articles = []
        for article_match in article_matches:
            article_number = article_match.group(1)
            article_alias = article_match.group(2) or ""
            description = article_match.group(3).strip()
            line_end = section.find("\n", article_match.start())
            source_line = section[article_match.start() : line_end if line_end != -1 else len(section)]
            dimensions = parse_dimensions(source_line)
            source_codes = sorted(extract_article_codes(source_line))
            if not looks_like_article_number(article_number):
                continue
            if article_number.startswith("APR"):
                category = "Arbeitsplatten"
            else:
                category = infer_category(description)
            articles.append((article_number, article_alias, source_codes, description, category, dimensions))

        for article_number, article_alias, source_codes, description, category, dimensions in articles:
            for index, price_group in enumerate(price_groups):
                bek_price = bek_prices[index] if index < len(bek_prices) else None
                block_price = block_prices[index] if index < len(block_prices) else None
                appliance_value = appliance_prices[index] if index < len(appliance_prices) else None
                rows.append(
                    {
                        "blocknummer": block_number,
                        "artikelnummer": article_number,
                        "artikelalias": article_alias if looks_like_article_number(article_alias) else "",
                        "quellcodes": " ".join(source_codes),
                        "beschreibung": description,
                        "kategorie": category,
                        "masse": dimensions or "",
                        "bruttopreis": bek_price if bek_price is not None else "",
                        "egeraete_netto": appliance_value if appliance_value is not None else "",
                        "blockpreis": block_price if block_price is not None else "",
                        "preisgruppe": price_group,
                        "verrechenbar": "ja",
                    }
                )
    return rows


def parse_price_list(value: str) -> list[float]:
    prices = []
    for match in re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", value):
        parsed = parse_money(match)
        if parsed is not None:
            prices.append(parsed)
    return prices


def parse_xml_rows(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return parse_text_rows(text)
    rows = []
    for element in root.iter():
        values = {normalize_header(child.tag.split("}")[-1]): (child.text or "").strip() for child in list(element)}
        if find_value(values, "article_number"):
            rows.append(values)
    return rows


def parse_text_rows(text: str) -> list[dict]:
    haecker_rows = parse_haecker_order_rows(text)
    if haecker_rows:
        return haecker_rows

    rows = []
    pattern = re.compile(
        r"(?P<article>[A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24})\s+"
        r"(?:[LR]\s+)?"
        r"(?P<quantity>\d{1,3})\s*(?:x|Stk\.?|Stück)?\s+"
        r"(?P<description>.{3,60}?)\s+"
        r"(?P<price>\d{1,3}(?:\.\d{3})*,\d{2})\s*€?",
    )
    for line in text.splitlines():
        if is_pdf_noise(line):
            continue
        match = pattern.search(line)
        if match:
            article = match.group("article").strip()
            description = match.group("description").strip()
            if not looks_like_article_number(article) or not looks_like_description(description):
                continue
            rows.append(
                {
                    "artikelnummer": article,
                    "menge": match.group("quantity"),
                    "beschreibung": description,
                    "bruttopreis": match.group("price"),
                    "quelle": line.strip(),
                }
            )
    return rows


def parse_haecker_order_rows(text: str) -> list[dict]:
    if " Pos " not in text or " Menge " not in text or " Modell " not in text:
        return []

    lines = [line.strip() for line in text.splitlines()]

    # Inline format: "{pos} {1,00} {MODEL} [L|R] {description...}" – one line per position.
    inline_pattern = re.compile(
        r"^(\d+(?:\.\d+)?)\s+(\d{1,3},\d{2})\s+([A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24})\s+(?:[LR]\s+)?(.+)"
    )
    inline_rows = []
    for line in lines:
        m = inline_pattern.match(line)
        if not m:
            continue
        model = m.group(3)
        if not looks_like_article_number(model):
            continue
        qty_str = m.group(2).replace(",", ".")
        try:
            qty = max(1, round(float(qty_str)))
        except ValueError:
            qty = 1
        description = re.split(r"\s+(?:B:|H:|T:)\s*\d", m.group(4))[0].strip()
        if not description or not looks_like_description(description):
            description = model
        inline_rows.append(
            {
                "artikelnummer": model,
                "menge": str(qty),
                "beschreibung": description,
                "quelle": line.strip(),
            }
        )
    if inline_rows:
        return inline_rows

    # Multi-line format: position number, quantity, model on separate lines.
    rows = []
    index = 0
    while index < len(lines) - 4:
        if not re.fullmatch(r"\d+(?:\.\d+)?", lines[index] or ""):
            index += 1
            continue
        if not re.fullmatch(r"\d{1,3},\d{2}", lines[index + 1] or ""):
            index += 1
            continue
        model = lines[index + 2]
        if not looks_like_article_number(model):
            index += 1
            continue

        cursor = index + 3
        if cursor < len(lines) and (not lines[cursor] or re.fullmatch(r"[LR]", lines[cursor])):
            cursor += 1
        while cursor < len(lines) and not lines[cursor]:
            cursor += 1
        if cursor >= len(lines):
            break

        description_parts = []
        while cursor < len(lines):
            value = lines[cursor]
            if not value:
                cursor += 1
                continue
            if value.startswith(("B:", "H:", "T:")) or re.fullmatch(r"\d+(?:\.\d+)?", value):
                break
            if value in {"Kontakt:", "Pos", "Menge", "Modell", "L/R", "Beschreibung", "Kopf"}:
                break
            description_parts.append(value)
            cursor += 1
            if len(description_parts) >= 2:
                break

        description = " ".join(description_parts).strip()
        if description:
            rows.append(
                {
                    "artikelnummer": model,
                    "menge": lines[index + 1],
                    "beschreibung": description,
                    "quelle": " | ".join(lines[index : min(cursor + 1, len(lines))]),
                }
            )
        index = max(cursor, index + 4)

    return rows


def is_pdf_noise(line: str) -> bool:
    value = line.strip()
    if not value:
        return True
    noise_tokens = ["<<", ">>", "/Font", "/Type", "/Length", " obj", "endobj", "stream", "xref", "BT", "ET"]
    return any(token in value for token in noise_tokens)


def looks_like_article_number(value: str) -> bool:
    if len(value) < 3 or len(value) > 24:
        return False
    if not any(char.isdigit() for char in value):
        return False
    if value.lower() in {"cm", "x65cm"}:
        return False
    return bool(re.fullmatch(r"[A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24}", value))


def looks_like_description(value: str) -> bool:
    lowered = value.lower()
    if any(token in lowered for token in ["font", "obj", "xref", "stream"]):
        return False
    return any(char.isalpha() for char in value)


def rows_to_text(rows: list[dict]) -> str:
    return "\n".join(" | ".join(f"{key}: {value}" for key, value in row.items() if value) for row in rows[:200])


def normalize_header(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    normalized = normalized.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


FIELD_ALIASES = {
    "article_number": ["artikelnummer", "artikel_nr", "art_nr", "art", "code", "code_nummer", "modellnummer", "modell", "pos_artikel"],
    "description": ["beschreibung", "bezeichnung", "text", "artikeltext", "name"],
    "category": ["kategorie", "warengruppe", "gruppe", "bereich"],
    "quantity": ["menge", "anzahl", "qty", "quantity", "stueck", "stk"],
    "gross_price": ["bruttopreis", "brutto", "listenpreis", "einzelpreis", "preis"],
    "net_price": ["nettopreis", "netto", "finaler_preis", "ab_preis", "neuer_preis"],
    "block_number": ["blocknummer", "block", "block_nr"],
    "block_price": ["blockpreis", "block_preis", "verrechnungspreis"],
    "appliance_value": ["egeraete_netto", "e_geraete_netto", "blockwert_e_geraete_netto", "geraete_netto", "spuelen_netto", "eg_wert"],
    "price_group": ["preisgruppe", "pg", "preis_gruppe"],
    "chargeable": ["verrechenbar", "berechenbar", "nicht_verrechenbar", "netto_artikel"],
    "dimensions": ["masse", "mass", "abmessung", "abmessungen", "dimension", "dimensionen", "b_h_t", "breite_hoehe_tiefe"],
    "width": ["breite", "b", "width"],
    "height": ["hoehe", "h", "height"],
    "depth": ["tiefe", "t", "depth"],
}


def find_value(row: dict, field: str) -> str:
    for alias in FIELD_ALIASES[field]:
        if alias in row and str(row[alias]).strip():
            return str(row[alias]).strip()
    return ""


def parse_money(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9,.-]", "", value)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_int(value: str | None, default: int = 1) -> int:
    if not value:
        return default
    match = re.search(r"\d+", value)
    return int(match.group()) if match else default


def parse_dimensions(*values: object) -> str | None:
    text = " ".join(str(value) for value in values if value is not None and str(value).strip())
    if not text:
        return None

    labelled = {}
    for label, number in re.findall(r"\b([BHTbht])\s*[:=]?\s*(\d{2,4}(?:[,.]\d+)?)", text):
        labelled[label.upper()] = normalize_dimension_number(number)
    if all(key in labelled for key in ("B", "H", "T")):
        return format_dimensions([labelled["B"], labelled["H"], labelled["T"]])

    match = re.search(
        r"(\d{2,4}(?:[,.]\d+)?)\s*(?:mm|cm)?\s*[x×/]\s*"
        r"(\d{2,4}(?:[,.]\d+)?)\s*(?:mm|cm)?\s*[x×/]\s*"
        r"(\d{2,4}(?:[,.]\d+)?)\s*(?:mm|cm)?",
        text,
        re.I,
    )
    if match:
        return format_dimensions([normalize_dimension_number(value) for value in match.groups()])
    return None


def dimensions_from_row(normalized: dict, description: str, excerpt: str) -> str | None:
    explicit = parse_dimensions(find_value(normalized, "dimensions"))
    if explicit:
        return explicit

    width = find_value(normalized, "width")
    height = find_value(normalized, "height")
    depth = find_value(normalized, "depth")
    if width and height and depth:
        return format_dimensions([normalize_dimension_number(width), normalize_dimension_number(height), normalize_dimension_number(depth)])

    return parse_dimensions(description, excerpt)


def normalize_dimension_number(value: str) -> str:
    cleaned = value.strip().replace(",", ".")
    try:
        number = float(cleaned)
    except ValueError:
        return cleaned
    return str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip(".")


def format_dimensions(values: list[str]) -> str:
    return " x ".join(values)


def same_dimensions(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    return normalize_dimension_text(left) == normalize_dimension_text(right)


def normalize_dimension_text(value: str) -> str:
    return re.sub(r"\s+", "", value.lower().replace("×", "x"))


def infer_category(description: str) -> str:
    text = description.lower()
    if "hoch" in text:
        return "Hochschränke"
    if "unter" in text or "spüle" in text:
        return "Unterschränke"
    if "ober" in text:
        return "Oberschränke"
    if "front" in text or "blende" in text:
        return "Fronten / Blenden"
    if "arbeitsplatte" in text:
        return "Arbeitsplatten"
    return "Zubehör"


def persist_extraction(connection: sqlite3.Connection, project_id: str, document_id: str, document_type: str, filename: str, rows: list[dict], text: str) -> tuple[int, int]:
    positions = 0
    rules = 0
    stamp = now_iso()
    connection.execute("UPDATE documents SET extracted_text = ?, analysis_status = ? WHERE id = ?", (text[:100000], "analysiert", document_id))
    for row in rows:
        normalized = {normalize_header(key): value for key, value in row.items()}
        article_number = find_value(normalized, "article_number")
        if not article_number:
            continue
        description = find_value(normalized, "description") or article_number
        quantity = parse_int(find_value(normalized, "quantity"))
        gross_price = parse_money(find_value(normalized, "gross_price"))
        net_price = parse_money(find_value(normalized, "net_price")) or gross_price
        block_number = find_value(normalized, "block_number")
        price_group = find_value(normalized, "price_group")
        block_price = parse_money(find_value(normalized, "block_price"))
        appliance_value = parse_money(find_value(normalized, "appliance_value"))
        excerpt = " | ".join(str(value) for value in row.values() if value)[:500]
        dimensions = dimensions_from_row(normalized, description, excerpt)

        if block_price is not None or (document_type == "Blockunterlage" and block_number):
            chargeable_text = find_value(normalized, "chargeable").lower()
            chargeable = 0 if "nicht" in chargeable_text or "netto" in chargeable_text else 1
            connection.execute(
                """
                INSERT INTO block_rules (
                  id, project_id, document_id, block_number, article_number,
                  gross_price, appliance_value, block_price, price_group, dimensions, chargeable,
                  source_file, source_excerpt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    project_id,
                    document_id,
                    block_number or "BLOCK-OHNE-NR",
                    article_number,
                    gross_price,
                    appliance_value,
                    block_price,
                    price_group,
                    dimensions,
                    chargeable,
                    filename,
                    excerpt,
                    stamp,
                ),
            )
            rules += 1

        if document_type != "Blockunterlage":
            connection.execute(
                """
                INSERT INTO extracted_positions (
                  id, project_id, document_id, document_type, article_number,
                  description, category, quantity, gross_price, net_price,
                  block_number, price_group, dimensions, source_file, source_excerpt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    project_id,
                    document_id,
                    document_type,
                    article_number,
                    description,
                    find_value(normalized, "category") or infer_category(description),
                    quantity,
                    gross_price,
                    net_price,
                    block_number,
                    price_group,
                    dimensions,
                    filename,
                    excerpt,
                    stamp,
                ),
            )
            positions += 1
    return positions, rules


def extract_ab_block_data(text: str) -> dict | None:
    """
    Parse Häcker AB text for exact block pricing data.

    Returns block sums split like the Häcker blockfinder: furniture/APL,
    E-Geräte/Spülen and Zubehör.
    article_wg maps article numbers to their WG codes (MB/EG/NE/BL).
    """
    bc_price: float | None = None
    moebel_brutto: float | None = None
    eg_brutto: float | None = None
    zubehoer_brutto: float | None = None
    block_number: str | None = None
    price_group: str | None = None
    article_wg: dict[str, str] = {}

    price_pat = r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*$"
    wg_price_pat = re.compile(r"\b(MB|EG|NE|BL)\s+\d{1,3}(?:\.\d{3})*,\d{2}\s*$")
    article_pat = re.compile(r"([A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24})")

    for line in text.splitlines():
        s = line.strip()

        if price_group is None:
            pg_match = re.search(r"Preisgruppe\s*[:\-]?\s*([0-7])(?:\s*[-A-Z])?", s, re.I)
            if pg_match:
                price_group = pg_match.group(1)

        m = re.match(r"Abrechnung\s+Block\s+(BC\d+)\s+BL\s+" + price_pat, s)
        if m:
            block_number = m.group(1)
            bc_price = parse_money(m.group(2))
            continue

        if bc_price is None:
            m = re.search(r"\b(?:Blockwert|Blockpreis|Block)\b.*?(BC\d+)?.*?(\d{1,3}(?:\.\d{3})*,\d{2})", s, re.I)
            if m:
                block_number = block_number or m.group(1)
                bc_price = parse_money(m.group(2))
                continue

        m = re.search(r"Summe Auftragswert M.belteile brutto.*?" + price_pat, s)
        if m:
            moebel_brutto = parse_money(m.group(1))
            continue

        m = re.search(r"Summe Auftragswert E-Ger.te.*?" + price_pat, s)
        if m:
            eg_brutto = parse_money(m.group(1))
            continue

        m = re.search(r"Summe Auftragswert Zubeh.r.*?" + price_pat, s)
        if m:
            zubehoer_brutto = parse_money(m.group(1))
            continue

        wg_m = wg_price_pat.search(s)
        if wg_m:
            art_m = article_pat.search(s)
            if art_m and looks_like_article_number(art_m.group(1)):
                article_wg[art_m.group(1)] = wg_m.group(1)

    if bc_price is None or moebel_brutto is None:
        return None

    return {
        "block_number": block_number or "",
        "price_group": price_group or "",
        "bc_price": bc_price,
        "moebel_brutto": moebel_brutto,
        "eg_brutto": eg_brutto or 0.0,
        "moebel_eg_brutto": moebel_brutto + (eg_brutto or 0.0),
        "zubehoer_brutto": zubehoer_brutto or 0.0,
        "article_wg": article_wg,
    }


def get_ab_block_data(connection: sqlite3.Connection, project_id: str) -> dict | None:
    row = connection.execute(
        "SELECT extracted_text FROM documents WHERE project_id = ? AND document_type = 'Auftragsbestätigung' ORDER BY uploaded_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row and row[0]:
        return extract_ab_block_data(row[0])
    return None


def build_agent_insights(articles: list[dict], block_rules: list[dict], ab_block_data: dict | None) -> dict:
    order_count = sum(1 for article in articles if article.get("order_found"))
    confirmation_count = sum(1 for article in articles if article.get("confirmation_found"))
    missing_in_ab = [article for article in articles if article.get("order_found") and not article.get("confirmation_found")]
    additional_in_ab = [article for article in articles if article.get("confirmation_found") and not article.get("order_found")]
    quantity_mismatches = [
        article
        for article in articles
        if article.get("order_quantity") is not None
        and article.get("confirmation_quantity") is not None
        and article.get("order_quantity") != article.get("confirmation_quantity")
    ]
    dimension_mismatches = [article for article in articles if article.get("dimension_status") == "abweichung"]

    return {
        "abComparison": {
            "order_count": order_count,
            "confirmation_count": confirmation_count,
            "missing_in_ab_count": len(missing_in_ab),
            "additional_in_ab_count": len(additional_in_ab),
            "quantity_mismatch_count": len(quantity_mismatches),
            "dimension_mismatch_count": len(dimension_mismatches),
            "dimension_mismatches": comparison_preview(dimension_mismatches, "Maßabweichung"),
            "missing_in_ab": comparison_preview(missing_in_ab, "Fehlt in AB"),
            "additional_in_ab": comparison_preview(additional_in_ab, "Zusätzlich in AB"),
        },
        "fillValue": build_fill_value_insight(ab_block_data, block_rules, articles),
        "blockFinder": build_blockfinder_insight(ab_block_data, block_rules, articles),
    }


def comparison_preview(articles: list[dict], label: str, limit: int = 4) -> list[dict]:
    preview = []
    for article in articles[:limit]:
        preview.append(
            {
                "article_number": article.get("article_number", ""),
                "description": article.get("description", ""),
                "label": label,
                "planned_dimensions": article.get("planned_dimensions") or "",
                "manufacturer_dimensions": article.get("manufacturer_dimensions") or "",
                "comment": article.get("comment") or "",
            }
        )
    return preview


def build_fill_value_insight(ab_block_data: dict | None, block_rules: list[dict], articles: list[dict] | None = None) -> dict:
    if not ab_block_data:
        return {
            "available": False,
            "message": "AB hochladen, um Blockwert und Füllwert zu prüfen.",
            "block_price": 0.0,
            "actual_value": 0.0,
            "fill_value": 0.0,
            "suggestions": [],
        }

    blockfinder = build_blockfinder_insight(ab_block_data, block_rules, articles)
    selected = blockfinder.get("selected") or {}
    block_price = selected.get("block_price") or ab_block_data.get("bc_price") or 0.0
    actual_value = (ab_block_data.get("moebel_brutto") or 0.0) + (ab_block_data.get("eg_brutto") or 0.0)
    raw_fill_value = selected.get("fill_total") if selected else block_price - actual_value
    fill_value = round(max(0.0, raw_fill_value or 0.0), 2)
    action = f"Noch ein Elektrogerät bis ca. {format_money(fill_value)} ergänzen." if fill_value > 0 else "Kein zusätzlicher Füllwert offen."
    return {
        "available": True,
        "message": "Füllwert offen." if fill_value > 0 else "Blockwert ist ausgeschöpft oder überschritten.",
        "block_price": round(block_price, 2),
        "actual_value": round(actual_value, 2),
        "fill_value": fill_value,
        "action": action,
        "suggestions": suggest_fill_items(block_rules, fill_value, selected),
    }


def build_blockfinder_insight(ab_block_data: dict | None, block_rules: list[dict], articles: list[dict] | None = None, limit: int = 8) -> dict:
    if not ab_block_data:
        return {"available": False, "message": "AB hochladen, um den Häcker-Blockfinder-Vergleich zu berechnen.", "candidates": []}

    furniture_value = ab_block_data.get("moebel_brutto") or 0.0
    appliance_value = ab_block_data.get("eg_brutto") or 0.0
    if furniture_value <= 0 and appliance_value <= 0:
        return {"available": False, "message": "In der AB wurden noch keine getrennten Auftragswerte gefunden.", "candidates": []}

    requested_pg = str(ab_block_data.get("price_group") or "").strip()
    requested_block = str(ab_block_data.get("block_number") or "").strip()
    order_aliases: set[str] = set()
    for article in articles or []:
        if article.get("order_found") or article.get("confirmation_found"):
            order_aliases.update(position_aliases(article))
    grouped: dict[tuple[str, str], dict] = {}
    for rule in block_rules:
        block_number = str(rule.get("block_number") or "").strip()
        price_group = str(rule.get("price_group") or "").strip()
        if not block_number or not price_group:
            continue
        gross_price = rule.get("gross_price") or 0.0
        block_price = rule.get("block_price") or 0.0
        eg_value = rule.get("appliance_value") or 0.0
        if gross_price <= 0 or block_price <= 0:
            continue
        key = (block_number, price_group)
        matched = len(rule_aliases(rule) & order_aliases) if order_aliases else 0
        if key not in grouped or (not grouped[key].get("appliance_block_value") and eg_value):
            grouped[key] = {
                "block_number": block_number,
                "price_group": price_group,
                "furniture_block_value": gross_price,
                "appliance_block_value": eg_value,
                "block_price": block_price,
                "matched_articles": 0,
            }
        grouped[key]["matched_articles"] += matched

    preferred_pg = requested_pg or "2"
    candidates = []
    for candidate in grouped.values():
        if preferred_pg and candidate["price_group"] != preferred_pg:
            continue
        if order_aliases and not candidate.get("matched_articles") and candidate["block_number"] != requested_block:
            continue
        furniture_fill = round(furniture_value - candidate["furniture_block_value"], 2)
        appliance_fill = round(appliance_value - candidate["appliance_block_value"], 2)
        fill_total = round(furniture_fill + appliance_fill, 2)
        score = (
            0 if requested_block and candidate["block_number"] == requested_block else 1,
            0 if appliance_value <= 0 or candidate["appliance_block_value"] > 0 else 1,
            abs(min(0.0, furniture_fill)) + abs(min(0.0, appliance_fill)),
            abs(fill_total),
            -int(candidate.get("matched_articles") or 0),
            candidate["block_price"],
        )
        candidates.append(
            {
                **candidate,
                "actual_furniture_value": round(furniture_value, 2),
                "actual_appliance_value": round(appliance_value, 2),
                "fill_gross": furniture_fill,
                "fill_net": appliance_fill,
                "fill_total": fill_total,
                "_score": score,
            }
        )

    candidates.sort(key=lambda item: item["_score"])
    for candidate in candidates:
        candidate.pop("_score", None)

    current = next((candidate for candidate in candidates if candidate["block_number"] == requested_block), None)
    recommendation = recommend_block_change(ab_block_data, block_rules)
    recommended_candidate = None
    if recommendation:
        recommended_number = recommendation["block_number"]
        recommended_candidate = next((candidate for candidate in candidates if candidate["block_number"] == recommended_number), None)
        if not recommended_candidate:
            recommended_candidate = {
                **recommendation,
                "matched_articles": 0,
                "actual_furniture_value": round(furniture_value, 2),
                "actual_appliance_value": round(appliance_value, 2),
                "fill_total": round((recommendation.get("fill_gross") or 0.0) + (recommendation.get("fill_net") or 0.0), 2),
            }
        if recommended_candidate:
            candidates = [recommended_candidate] + [
                candidate
                for candidate in candidates
                if candidate["block_number"] != recommended_number and candidate["block_number"] != requested_block
            ]
            if current:
                candidates.append(current)

    selected = recommended_candidate or (candidates[0] if candidates else current)
    return {
        "available": bool(candidates),
        "message": "Häcker-Blockfinder-Werte aus AB und Alliance/Häcker-Datenbank berechnet." if candidates else "Keine passende Preisgruppe in der Blockdatenbank gefunden.",
        "price_group": preferred_pg,
        "actual_furniture_value": round(furniture_value, 2),
        "actual_appliance_value": round(appliance_value, 2),
        "selected": selected,
        "current": current,
        "recommendation": recommended_candidate,
        "candidates": candidates[:limit],
    }


def suggest_fill_items(block_rules: list[dict], fill_value: float, selected_block: dict | None = None, limit: int = 2) -> list[dict]:
    if fill_value <= 0:
        return []
    suggestions = []
    if selected_block and selected_block.get("appliance_block_value"):
        appliance_value = selected_block.get("appliance_block_value") or fill_value
        suggestions.append(
            {
                "article_number": "E-GERAET",
                "description": "Elektrogerät / Spüle ergänzen",
                "estimated_value": round(min(fill_value, appliance_value), 2),
                "block_number": selected_block.get("block_number") or "",
                "price_group": selected_block.get("price_group") or "",
                "label": "Empfehlung: Elektrogerät ergänzen",
                "action": f"Noch ein Elektrogerät bis ca. {format_money(fill_value)} ergänzen.",
            }
        )
    seen = set()
    candidates = []
    for rule in block_rules:
        article_number = rule.get("article_number") or ""
        description = rule.get("source_excerpt") or article_number
        key = (article_number, description)
        if not article_number or key in seen:
            continue
        seen.add(key)
        estimated_value = suggestion_value(rule)
        if estimated_value <= 0:
            continue
        distance = abs(fill_value - estimated_value)
        if estimated_value <= fill_value * 1.15:
            candidates.append((distance, -estimated_value, article_number, rule.get("block_number") or "", rule, estimated_value))

    ordered_candidates = sorted(candidates, key=lambda item: (item[0], item[1], item[2], item[3]))
    for _distance, _negative_value, _article_number, _block_number, rule, estimated_value in ordered_candidates[:limit]:
        if len(suggestions) >= limit:
            break
        suggestions.append(
            {
                "article_number": rule.get("article_number") or "",
                "description": rule_description(rule),
                "estimated_value": round(estimated_value, 2),
                "block_number": rule.get("block_number") or "",
                "price_group": rule.get("price_group") or "",
                "label": "Alternative Füllwert-Option",
            }
        )
    return suggestions


def suggestion_value(rule: dict) -> float:
    gross_price = rule.get("gross_price") or 0.0
    block_price = rule.get("block_price") or 0.0
    if gross_price and block_price and gross_price > block_price * 1.5:
        return min(gross_price, block_price)
    return gross_price or block_price


def rule_description(rule: dict) -> str:
    excerpt = rule.get("source_excerpt") or ""
    parts = [part.strip() for part in excerpt.split("|")]
    if len(parts) >= 3:
        return parts[2]
    return rule.get("article_number") or "Ergänzungsposition"


def write_reconciled_articles(
    connection: sqlite3.Connection,
    project_id: str,
    positions: list[dict],
    rules: list[dict],
    ab_block_data: dict | None = None,
    has_confirmation_document: bool = False,
) -> None:
    by_article: dict[str, dict[str, list[dict]]] = {}
    for position in positions:
        key = position_group_key(position, by_article.keys())
        bucket = by_article.setdefault(key, {"Bestellung": [], "Auftragsbestätigung": [], "Sonstiges": []})
        bucket.setdefault(position["document_type"], bucket["Sonstiges"]).append(position)

    rules_by_article: dict[str, dict] = {}
    for rule in rules:
        rules_by_article[rule["article_number"]] = rule

    block_matches = match_block_candidates(by_article, rules)

    for article_number, groups in by_article.items():
        # Skip Häcker block-reference entries (e.g. BC1623364 "Küchenblock") –
        # these are aggregate price markers in the AB, not individual articles.
        if re.fullmatch(r"BC\d+", article_number):
            continue
        order = first(groups.get("Bestellung", []))
        confirmation = first(groups.get("Auftragsbestätigung", []))
        fallback = first(groups.get("Sonstiges", [])) or order or confirmation
        rule = rules_by_article.get(article_number)
        planned_dimensions = (order or fallback or confirmation).get("dimensions")
        manufacturer_dimensions = (
            (confirmation.get("dimensions") if confirmation else None)
            or (rule.get("dimensions") if rule else None)
            or ((fallback or {}).get("dimensions") if fallback and fallback is not order else None)
        )
        dimension_status = "offen"
        if planned_dimensions and manufacturer_dimensions:
            dimension_status = "ok" if same_dimensions(planned_dimensions, manufacturer_dimensions) else "abweichung"
        elif planned_dimensions:
            dimension_status = "Herstellermaß offen"
        elif manufacturer_dimensions:
            dimension_status = "Planmaß fehlt"
        quantity = int((confirmation or order or fallback)["quantity"])
        single_price = (
            (order.get("gross_price") if order else None)
            or (confirmation.get("gross_price") if confirmation else None)
            or (fallback.get("gross_price") if fallback else None)
            or best_price(order)
            or best_price(confirmation)
            or best_price(fallback)
            or 0.0
        )
        block_price = single_price
        comments = []
        status = "geprüft"
        block_match = block_matches.get(article_number)

        if block_match:
            if single_price <= 0:
                single_price = block_match["allocated_gross"]
            block_price = block_match["allocated_price"]
            status = "Einsparung (Block)"
            rule = block_match["rule"]
            comments.append(
                f"Bestellung passt zu Block {block_match['block_number']} PG {block_match['price_group']} "
                f"({block_match['matched_rule_count']} von {block_match['full_rule_count']} Positionen erkannt, "
                f"anteiliger Blockpreis {money_number(block_match['block_price'])} EUR, "
                f"Gesamtblock {money_number(block_match['full_block_price'])} EUR)."
            )
        elif rule:
            if rule["chargeable"]:
                block_price = rule["block_price"] if rule["block_price"] is not None else single_price
            else:
                comments.append("Laut Blockunterlage nicht verrechenbar oder Netto-Artikel.")
                status = "nicht verrechenbar"
        elif ab_block_data and single_price > 0:
            wg = ab_block_data["article_wg"].get(article_number, "")
            if wg == "NE":
                # Zubehör – nicht im Block, kein Rabatt
                status = "geprüft"
                comments.append("Zubehör (NE) – nicht im Block.")
            elif confirmation and ab_block_data["moebel_eg_brutto"] > 0:
                ratio = ab_block_data["bc_price"] / ab_block_data["moebel_eg_brutto"]
                block_price = round(single_price * ratio, 2)
                status = "Einsparung (geschätzt)"
                discount_pct = round((1 - ratio) * 100, 1)
                comments.append(f"Blockpreis aus BC-Gesamtpreis berechnet ({discount_pct} % Rabatt auf Möbelteile + E-Geräte).")
        else:
            status = "Rückfrage"
            comments.append("Keine passende Blockregel gefunden.")

        order_qty = int(order["quantity"]) if order else None
        confirmation_qty = int(confirmation["quantity"]) if confirmation else None
        if order and confirmation and order_qty != confirmation_qty:
            status = "Rückfrage"
            comments.append(f"Mengenabweichung Bestellung {order_qty}, AB {confirmation_qty}.")
        if order and not confirmation:
            if has_confirmation_document:
                if not block_match:
                    status = "fehlt in AB"
                comments.append("Artikel ist in der Bestellung vorhanden, aber nicht in der AB.")
            else:
                if not block_match:
                    status = "AB offen"
                comments.append("AB vom Hersteller noch offen.")
        if confirmation and not order:
            status = "zusätzlich in AB"
            comments.append("Artikel ist in der AB vorhanden, aber nicht in der Bestellung.")
        if dimension_status == "abweichung":
            status = "Rückfrage"
            comments.append(f"Maßabweichung: geplant {planned_dimensions}, Hersteller {manufacturer_dimensions}.")

        source_refs = {
            "bestellung": order["source_file"] if order else None,
            "auftragsbestaetigung": confirmation["source_file"] if confirmation else None,
            "blockunterlage": rule["source_file"] if rule else None,
        }

        source = ", ".join(value for value in source_refs.values() if value) or fallback["source_file"]
        connection.execute(
            """
            INSERT INTO articles (
              id, project_id, article_number, description, category, quantity,
              single_price, block_price, source, status, comment, order_quantity,
              confirmation_quantity, block_number, price_group, order_found,
              confirmation_found, source_refs, planned_dimensions, manufacturer_dimensions,
              dimension_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                project_id,
                article_number,
                (confirmation or order or fallback)["description"],
                (confirmation or order or fallback)["category"],
                quantity,
                single_price,
                block_price,
                source,
                status,
                " ".join(comments),
                order_qty,
                confirmation_qty,
                rule["block_number"] if rule else (confirmation or order or fallback)["block_number"],
                rule["price_group"] if rule else (confirmation or order or fallback)["price_group"],
                1 if order else 0,
                1 if confirmation else 0,
                json.dumps(source_refs, ensure_ascii=False),
                planned_dimensions,
                manufacturer_dimensions,
                dimension_status,
            ),
        )


def match_block_candidates(by_article: dict[str, dict[str, list[dict]]], rules: list[dict]) -> dict[str, dict]:
    order_positions = {
        article_number: first(groups.get("Bestellung", []))
        for article_number, groups in by_article.items()
        if first(groups.get("Bestellung", []))
    }
    if not order_positions:
        return {}

    groups: dict[tuple[str, str], list[dict]] = {}
    for rule in rules:
        if not rule.get("chargeable") or rule.get("block_price") is None:
            continue
        key = (rule.get("block_number") or "", rule.get("price_group") or "")
        if not key[0]:
            continue
        groups.setdefault(key, []).append(rule)

    candidates = []
    order_article_numbers = set(order_positions)
    for (block_number, price_group), block_rules in groups.items():
        matched_rules = []
        used_articles = set()
        matched_article_numbers = set()
        for rule in block_rules:
            matched_article = first_matching_article(rule, order_article_numbers - used_articles)
            if matched_article:
                matched_rules.append((matched_article, rule))
                used_articles.add(matched_article)
        matched_article_numbers = {article_number for article_number, _rule in matched_rules}

        if len(matched_rules) < 2:
            continue

        order_gross_total = 0.0
        full_rule_gross_total = block_group_gross_total(block_rules)
        rule_gross_total = matched_group_gross_total(block_rules, matched_rules)
        for article_number in matched_article_numbers:
            position = order_positions[article_number]
            order_gross_total += (position.get("gross_price") or position.get("net_price") or 0.0) * int(position.get("quantity") or 1)
        gross_total = order_gross_total if order_gross_total > 0 else rule_gross_total
        block_price = block_rules[0].get("block_price") or 0.0
        allocated_block_price = block_price
        if full_rule_gross_total > 0 and rule_gross_total > 0:
            allocated_block_price = round(block_price * (rule_gross_total / full_rule_gross_total), 2)
        saving = gross_total - allocated_block_price
        if saving <= 0:
            continue

        candidates.append(
            {
                "block_number": block_number,
                "price_group": price_group,
                "rules": matched_rules,
                "articles": matched_article_numbers,
                "gross_total": gross_total,
                "full_gross_total": full_rule_gross_total,
                "block_price": allocated_block_price,
                "full_block_price": block_price,
                "full_rule_count": len(block_rules),
                "matched_rule_count": len(matched_rules),
                "saving": saving,
            }
        )

    if not candidates:
        return {}

    best = max(candidates, key=lambda candidate: (candidate["saving"], len(candidate["articles"])))
    result = {}
    rules_by_article = {article_number: rule for article_number, rule in best["rules"]}
    for article_number in best["articles"]:
        position = order_positions[article_number]
        quantity = int(position.get("quantity") or 1)
        gross_value = (position.get("gross_price") or position.get("net_price") or 0.0) * quantity
        if gross_value <= 0 and rules_by_article.get(article_number):
            gross_value = best["full_gross_total"] / best["full_rule_count"] if best["full_rule_count"] else 0.0
        share = gross_value / best["gross_total"] if best["gross_total"] else 0
        allocated_total = round(best["block_price"] * share, 2)
        result[article_number] = {
            "allocated_price": round(allocated_total / quantity, 2) if quantity else allocated_total,
            "allocated_gross": round(gross_value / quantity, 2) if quantity else gross_value,
            "block_number": best["block_number"],
            "price_group": best["price_group"],
            "block_price": best["block_price"],
            "full_block_price": best["full_block_price"],
            "matched_rule_count": best["matched_rule_count"],
            "full_rule_count": best["full_rule_count"],
            "rule": rules_by_article.get(article_number),
        }
    return result


def first_matching_article(rule: dict, article_numbers: set[str]) -> str | None:
    aliases = rule_aliases(rule)
    for article_number in article_numbers:
        article_aliases = haecker_article_aliases(article_number)
        if article_aliases & aliases:
            return article_number
    return None


def position_group_key(position: dict, existing_keys) -> str:
    aliases = position_aliases(position)
    for key in existing_keys:
        if haecker_article_aliases(str(key)) & aliases:
            return str(key)
    return position.get("article_number") or ""


def position_aliases(position: dict) -> set[str]:
    aliases = {position.get("article_number") or ""}
    text = " ".join(
        str(position.get(field) or "")
        for field in ("description", "source_excerpt", "source_file")
    )
    aliases.update(extract_article_codes(text))
    expanded = set()
    for alias in aliases:
        expanded.update(haecker_article_aliases(alias))
    return {alias for alias in expanded if alias}


def block_group_gross_total(block_rules: list[dict]) -> float:
    gross_values = [rule.get("gross_price") or 0.0 for rule in block_rules if rule.get("gross_price")]
    if not gross_values:
        return 0.0
    if len({round(value, 2) for value in gross_values}) == 1:
        return gross_values[0]
    return sum(gross_values)


def matched_group_gross_total(block_rules: list[dict], matched_rules: list[tuple[str, dict]]) -> float:
    full_total = block_group_gross_total(block_rules)
    if full_total <= 0:
        return 0.0
    gross_values = [rule.get("gross_price") or 0.0 for rule in block_rules if rule.get("gross_price")]
    if gross_values and len({round(value, 2) for value in gross_values}) == 1:
        return full_total * (len(matched_rules) / len(block_rules))
    return sum((rule.get("gross_price") or 0.0) for _article_number, rule in matched_rules)


def rule_aliases(rule: dict) -> set[str]:
    aliases = {rule.get("article_number") or ""}
    excerpt = rule.get("source_excerpt") or ""
    aliases.update(extract_article_codes(excerpt))
    expanded = set()
    for alias in aliases:
        expanded.update(haecker_article_aliases(alias))
    return {alias for alias in expanded if alias}


def extract_article_codes(text: str) -> set[str]:
    codes = set()
    for token in re.findall(r"\b[A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24}\b", text or ""):
        if looks_like_article_number(token):
            codes.add(token)
    for group in re.findall(r"\(([^)]+)\)", text or ""):
        for token in re.findall(r"\b[A-ZÄÖÜ]?[A-Z0-9ÄÖÜ/-]{2,24}\b", group):
            if looks_like_article_number(token):
                codes.add(token)
    return codes


def haecker_article_aliases(article_number: str) -> set[str]:
    value = (article_number or "").upper()
    aliases = {value}
    changed = True
    while changed:
        changed = False
        for alias in list(aliases):
            variants = {
                alias.replace("78", "71"),
                alias.replace("71", "78"),
                re.sub(r"D\d+$", "D", alias),
                alias.replace("MAH", ""),
                re.sub(r"^V", "", alias),
            }
            for variant in variants:
                if variant and variant not in aliases:
                    aliases.add(variant)
                    changed = True
    return {alias for alias in aliases if alias}


def first(items: list[dict]) -> dict | None:
    return items[0] if items else None


def best_price(position: dict | None) -> float | None:
    if not position:
        return None
    return position.get("net_price") or position.get("gross_price")


def guess_server_document_type(filename: str) -> str:
    lower = filename.lower()
    if "block" in lower or "vereinbarung" in lower or "concept" in lower:
        return "Blockunterlage"
    if "auftrag" in lower or "ab" in lower:
        return "Auftragsbestätigung"
    if "bestell" in lower:
        return "Bestellung"
    return "Sonstiges"


def save_upload(project_id: str, parts: list[dict]) -> list[dict]:
    saved = []
    document_type = "Sonstiges"
    for part in parts:
        if part["name"] == "document_type":
            document_type = part["data"].decode("utf-8", errors="replace") or "Sonstiges"

    project_dir = UPLOAD_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    with db() as connection:
        for part in parts:
            if not part["filename"]:
                continue
            original_name = safe_filename(part["filename"])
            if document_type == "Blockunterlage" or guess_server_document_type(original_name) == "Blockunterlage":
                raise ValueError("Vereinbarungen sind bereits als zentrale Blockdatenbank hinterlegt. Bitte nur Bestellung oder AB hochladen.")
            stored_name = f"{uuid4()}_{original_name}"
            path = project_dir / stored_name
            path.write_bytes(part["data"])

            document_id = str(uuid4())
            stamp = now_iso()
            connection.execute(
                """
                INSERT INTO documents (
                  id, project_id, filename, stored_path, document_type,
                  content_type, size, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, project_id, original_name, str(path.relative_to(ROOT)), document_type, part["content_type"], len(part["data"]), stamp),
            )
            add_timeline(connection, "Datei hochgeladen", f"{original_name} wurde als {document_type} gespeichert.", original_name, stamp, project_id)
            saved.append(
                {
                    "id": document_id,
                    "filename": original_name,
                    "document_type": document_type,
                    "content_type": part["content_type"],
                    "size": len(part["data"]),
                    "uploaded_at": stamp,
                }
            )
            try:
                rows, text = parse_document(path, original_name)
                position_count, rule_count = persist_extraction(connection, project_id, document_id, document_type, original_name, rows, text)
                add_timeline(
                    connection,
                    "Dokument analysiert",
                    f"{original_name}: {position_count} Positionen und {rule_count} Blockregeln erkannt.",
                    original_name,
                    stamp,
                    project_id,
                )
            except Exception as exc:
                connection.execute(
                    "UPDATE documents SET analysis_status = ?, analysis_notes = ? WHERE id = ?",
                    ("Fehler", str(exc), document_id),
                )
                add_timeline(connection, "Analyse fehlgeschlagen", f"{original_name}: {exc}", original_name, stamp, project_id)

        write_reconciled_if_possible(connection, project_id)

    return saved


def write_reconciled_if_possible(connection: sqlite3.Connection, project_id: str) -> None:
    positions = [row_to_dict(row) for row in connection.execute("SELECT * FROM extracted_positions WHERE project_id = ?", (project_id,))]
    rules = block_rules_for_project(connection, project_id)
    ab_block_data = get_ab_block_data(connection, project_id)
    has_confirmation_document = has_document_type(connection, project_id, "Auftragsbestätigung")
    connection.execute("DELETE FROM articles WHERE project_id = ?", (project_id,))
    if positions:
        write_reconciled_articles(connection, project_id, positions, rules, ab_block_data, has_confirmation_document)
    connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))


def export_csv(project_id: str) -> Path:
    payload = project_payload(project_id)
    path = EXPORT_DIR / f"{project_id}_auswertung.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["Projekt", payload["project"]["name"]])
        writer.writerow(["Kommission", payload["project"]["commission"]])
        writer.writerow(["Status", payload["project"]["status"]])
        writer.writerow([])
        writer.writerow(["Art.-Nr.", "Bezeichnung", "Kategorie", "Menge", "Menge Bestellung", "Menge AB", "Planmaß", "Herstellermaß", "Maßstatus", "Einzelpreis", "Blockpreis", "Blocknummer", "Preisgruppe", "Ersparnis", "Status", "Quelle", "Kommentar"])
        for article in payload["articles"]:
            saving = (article["single_price"] - article["block_price"]) * article["quantity"]
            writer.writerow(
                [
                    article["article_number"],
                    article["description"],
                    article["category"],
                    article["quantity"],
                    article.get("order_quantity") or "",
                    article.get("confirmation_quantity") or "",
                    article.get("planned_dimensions") or "",
                    article.get("manufacturer_dimensions") or "",
                    article.get("dimension_status") or "",
                    money_number(article["single_price"]),
                    money_number(article["block_price"]),
                    article.get("block_number") or "",
                    article.get("price_group") or "",
                    money_number(saving),
                    article["status"],
                    article["source"],
                    article["comment"],
                ]
            )
    return path


def create_project(payload: dict) -> dict:
    project_id = f"PRJ-{datetime.now().year}-{uuid4().hex[:6].upper()}"
    stamp = now_iso()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO projects (
              id, name, commission, customer, supplier, manufacturer,
              catalog_year, owner, status, order_number, confirmation_number,
              delivery_week, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.get("name") or "Neues Projekt",
                payload.get("commission") or "",
                payload.get("customer") or "",
                payload.get("supplier") or "",
                payload.get("manufacturer") or "",
                payload.get("catalog_year") or str(datetime.now().year),
                payload.get("owner") or "Max Mustermann",
                "Analyse vorbereitet",
                payload.get("order_number") or "",
                payload.get("confirmation_number") or "",
                payload.get("delivery_week") or "",
                payload.get("notes") or "",
                stamp,
                stamp,
            ),
        )
        add_timeline(connection, "Projekt erstellt", "Projektakte wurde angelegt.", project_id=project_id)
        create_mail_draft(connection, project_id)
    return project_payload(project_id)


def send_mail(project_id: str) -> dict:
    with db() as connection:
        draft = connection.execute("SELECT * FROM mail_drafts WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        if not draft:
            raise KeyError("mail_draft")
        if not draft["recipient"]:
            raise ValueError("Bitte zuerst eine Empfänger-E-Mail im Mailentwurf eintragen.")
        stamp = now_iso()
        connection.execute(
            """
            INSERT INTO outbox (id, project_id, mail_draft_id, recipient, subject, body, status, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), project_id, draft["id"], draft["recipient"], draft["subject"], draft["body"], "freigegeben", stamp),
        )
        connection.execute("UPDATE mail_drafts SET status = ?, updated_at = ? WHERE id = ?", ("Gesendet", stamp, draft["id"]))
        connection.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("Lieferant angefragt", stamp, project_id))
        add_timeline(connection, "Mail freigegeben", f"Mail an {draft['recipient']} wurde in der Outbox abgelegt.", None, stamp, project_id)
    return project_payload(project_id)


def archive_project(project_id: str) -> dict:
    with db() as connection:
        connection.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("Archiviert", now_iso(), project_id))
        add_timeline(connection, "Projekt archiviert", "Projekt wurde archiviert.", project_id=project_id)
    return project_payload(project_id)


def clear_order_confirmation(project_id: str) -> dict:
    with db() as connection:
        removable_docs = [
            row_to_dict(row)
            for row in connection.execute(
                "SELECT id, filename, stored_path FROM documents WHERE project_id = ? AND document_type IN (?, ?)",
                (project_id, "Bestellung", "Auftragsbestätigung"),
            )
        ]
        document_ids = [document["id"] for document in removable_docs]
        for document in removable_docs:
            path = ROOT / document["stored_path"]
            if path.exists():
                path.unlink()

        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            connection.execute(f"DELETE FROM extracted_positions WHERE document_id IN ({placeholders})", document_ids)
            connection.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", document_ids)

        positions = [row_to_dict(row) for row in connection.execute("SELECT * FROM extracted_positions WHERE project_id = ?", (project_id,))]
        rules = block_rules_for_project(connection, project_id)
        connection.execute("DELETE FROM articles WHERE project_id = ?", (project_id,))
        if positions:
            write_reconciled_articles(connection, project_id, positions, rules, None, False)

        add_timeline(
            connection,
            "Bestellung und AB entfernt",
            "Bestell- und Auftragsbestätigungsdaten wurden entfernt. Blockunterlagen bleiben erhalten.",
            project_id=project_id,
        )
        regenerate_mail_draft(connection, project_id)
    return project_payload(project_id)


def delete_document(project_id: str, document_id: str) -> dict:
    with db() as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE id = ? AND project_id = ?",
            (document_id, project_id),
        ).fetchone()
        if not document:
            raise ValueError("Dokument wurde nicht gefunden.")

        document = row_to_dict(document)
        path = ROOT / document["stored_path"]
        if path.exists():
            path.unlink()

        connection.execute("DELETE FROM extracted_positions WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM block_rules WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        write_reconciled_if_possible(connection, project_id)
        add_timeline(
            connection,
            "Datei entfernt",
            f"{document['filename']} wurde aus dem Projekt entfernt.",
            document["filename"],
            project_id=project_id,
        )
        regenerate_mail_draft(connection, project_id)
    return project_payload(project_id)


def import_block_library_from_path(path: Path) -> tuple[int, int]:
    if not path.exists():
        raise ValueError(f"Datei nicht gefunden: {path}")
    document_id = str(uuid4())
    stamp = now_iso()
    filename = safe_filename(path.name)
    rows, text = parse_document(path, filename)
    with db() as connection:
        old_documents = [
            row_to_dict(row)
            for row in connection.execute(
                "SELECT id FROM documents WHERE project_id = ? AND filename = ?",
                (GLOBAL_BLOCK_PROJECT_ID, filename),
            )
        ]
        for document in old_documents:
            connection.execute("DELETE FROM block_rules WHERE document_id = ?", (document["id"],))
            connection.execute("DELETE FROM extracted_positions WHERE document_id = ?", (document["id"],))
            connection.execute("DELETE FROM documents WHERE id = ?", (document["id"],))
        connection.execute(
            """
            INSERT INTO documents (
              id, project_id, filename, stored_path, document_type,
              content_type, size, uploaded_at, extracted_text, analysis_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                GLOBAL_BLOCK_PROJECT_ID,
                filename,
                str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "Blockunterlage",
                "application/octet-stream",
                path.stat().st_size,
                stamp,
                text[:100000],
                "analysiert",
            ),
        )
        position_count, rule_count = persist_extraction(
            connection,
            GLOBAL_BLOCK_PROJECT_ID,
            document_id,
            "Blockunterlage",
            filename,
            rows,
            text,
        )
    return position_count, rule_count


def ensure_default_block_library(connection: sqlite3.Connection) -> None:
    if not DEFAULT_BLOCK_LIBRARY_PATH.exists():
        return
    existing = connection.execute(
        "SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ?",
        (GLOBAL_BLOCK_PROJECT_ID,),
    ).fetchone()["count"]
    current_version = connection.execute(
        "SELECT analysis_notes FROM documents WHERE project_id = ? AND filename = ? ORDER BY uploaded_at DESC LIMIT 1",
        (GLOBAL_BLOCK_PROJECT_ID, DEFAULT_BLOCK_LIBRARY_PATH.name),
    ).fetchone()
    has_appliance_values = connection.execute(
        "SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ? AND appliance_value IS NOT NULL",
        (GLOBAL_BLOCK_PROJECT_ID,),
    ).fetchone()["count"]
    if existing and current_version and current_version["analysis_notes"] == DEFAULT_BLOCK_LIBRARY_VERSION and has_appliance_values:
        return

    old_documents = [
        row_to_dict(row)
        for row in connection.execute(
            "SELECT id FROM documents WHERE project_id = ? AND filename = ?",
            (GLOBAL_BLOCK_PROJECT_ID, DEFAULT_BLOCK_LIBRARY_PATH.name),
        )
    ]
    for document in old_documents:
        connection.execute("DELETE FROM block_rules WHERE document_id = ?", (document["id"],))
        connection.execute("DELETE FROM extracted_positions WHERE document_id = ?", (document["id"],))
        connection.execute("DELETE FROM documents WHERE id = ?", (document["id"],))

    document_id = str(uuid4())
    stamp = now_iso()
    filename = DEFAULT_BLOCK_LIBRARY_PATH.name
    rows, text = parse_document(DEFAULT_BLOCK_LIBRARY_PATH, filename)
    connection.execute(
        """
        INSERT INTO documents (
          id, project_id, filename, stored_path, document_type,
          content_type, size, uploaded_at, extracted_text, analysis_status, analysis_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            GLOBAL_BLOCK_PROJECT_ID,
            filename,
            str(DEFAULT_BLOCK_LIBRARY_PATH.relative_to(ROOT)),
            "Blockunterlage",
            "text/csv",
            DEFAULT_BLOCK_LIBRARY_PATH.stat().st_size,
            stamp,
            text[:100000],
            "analysiert",
            DEFAULT_BLOCK_LIBRARY_VERSION,
        ),
    )
    persist_extraction(
        connection,
        GLOBAL_BLOCK_PROJECT_ID,
        document_id,
        "Blockunterlage",
        filename,
        rows,
        text,
    )


def money_number(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def format_money(value: float) -> str:
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} €"


class AgentHandler(SimpleHTTPRequestHandler):
    server_version = "KuechenAgent/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/projects/current":
            self.send_json(project_payload())
            return

        match = re.fullmatch(r"/api/projects/([^/]+)/export", path)
        if match:
            export_path = export_csv(match.group(1))
            self.send_file(export_path, "text/csv; charset=utf-8")
            return

        if path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)

            if path == "/api/projects":
                payload = json.loads(body.decode("utf-8") or "{}")
                self.send_json(create_project(payload), HTTPStatus.CREATED)
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/documents", path)
            if match:
                headers = {key.lower(): value for key, value in self.headers.items()}
                saved = save_upload(match.group(1), parse_multipart(headers, body))
                self.send_json({"documents": saved, "project": project_payload(match.group(1))}, HTTPStatus.CREATED)
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/analyze", path)
            if match:
                self.send_json(analyze_project(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/mail-draft", path)
            if match:
                payload = json.loads(body.decode("utf-8") or "{}")
                with db() as connection:
                    draft = connection.execute(
                        "SELECT id FROM mail_drafts WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
                        (match.group(1),),
                    ).fetchone()
                    if draft:
                        connection.execute(
                            """
                            UPDATE mail_drafts
                            SET recipient = ?, subject = ?, body = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                payload.get("recipient", ""),
                                payload.get("subject", ""),
                                payload.get("body", ""),
                                now_iso(),
                                draft["id"],
                            ),
                        )
                    add_timeline(connection, "Mailentwurf aktualisiert", "Der Lieferantenentwurf wurde gespeichert.", project_id=match.group(1))
                self.send_json(project_payload(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/mail-send", path)
            if match:
                self.send_json(send_mail(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/mail-regenerate", path)
            if match:
                with db() as connection:
                    regenerate_mail_draft(connection, match.group(1))
                self.send_json(project_payload(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/archive", path)
            if match:
                self.send_json(archive_project(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/clear-order-confirmation", path)
            if match:
                self.send_json(clear_order_confirmation(match.group(1)))
                return

            match = re.fullmatch(r"/api/projects/([^/]+)/documents/([^/]+)/delete", path)
            if match:
                self.send_json(delete_document(match.group(1), match.group(2)))
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as file:
            shutil.copyfileobj(file, self.wfile)


def run() -> None:
    init_db()
    for argument in sys.argv[1:]:
        path = Path(argument).expanduser().resolve()
        position_count, rule_count = import_block_library_from_path(path)
        print(f"Blockbibliothek importiert: {path.name} ({position_count} Positionen, {rule_count} Regeln)")
    if len(sys.argv) > 1:
        return
    import os
    port = int(os.environ.get("PORT", 5173))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), AgentHandler)
    print(f"Küchen Agent läuft auf http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    run()
