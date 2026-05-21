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
              block_price REAL,
              price_group TEXT,
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
        ]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass

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
        project_block_rule_count = connection.execute("SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ?", (project_id,)).fetchone()["count"]
        library_block_rule_count = connection.execute("SELECT COUNT(*) AS count FROM block_rules WHERE project_id = ?", (GLOBAL_BLOCK_PROJECT_ID,)).fetchone()["count"]
        timeline = [row_to_dict(row) for row in connection.execute("SELECT * FROM timeline WHERE project_id = ? ORDER BY created_at DESC LIMIT 10", (project_id,))]
        mail = connection.execute("SELECT * FROM mail_drafts WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        outbox_count = connection.execute("SELECT COUNT(*) AS count FROM outbox WHERE project_id = ?", (project_id,)).fetchone()["count"]

    total_savings = sum((article["single_price"] - article["block_price"]) * article["quantity"] for article in articles)
    total_net = sum(article["block_price"] * article["quantity"] for article in articles)
    questions = sum(1 for article in articles if "rückfrage" in article["status"].lower())
    open_savings = sum(max(0, (article["single_price"] - article["block_price"]) * article["quantity"]) for article in articles if "geprüft" not in article["status"].lower())

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
        "timeline": timeline,
        "mailDraft": row_to_dict(mail) if mail else None,
    }


def create_mail_draft(connection: sqlite3.Connection, project_id: str) -> dict:
    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    article_rows = connection.execute("SELECT quantity, single_price, block_price FROM articles WHERE project_id = ?", (project_id,)).fetchall()
    savings = sum((row["single_price"] - row["block_price"]) * row["quantity"] for row in article_rows)
    subject = f"Anfrage zur Prüfung der Einsparungen - Projekt {project_id}"
    recipient = project["supplier"] or ""
    commission = project["commission"] or project["name"]
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

        connection.execute("DELETE FROM articles WHERE project_id = ?", (project_id,))
        if positions:
            write_reconciled_articles(connection, project_id, positions, rules, ab_block_data)

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
        price_match = re.search(r"Blockpreis\s+([0-9.,\s]+)", section)
        block_prices = parse_price_list(price_match.group(1) if price_match else "")
        article_matches = re.finditer(
            r"(?m)^\s*\d+\s*x\s+([A-Z0-9/-]{3,24})\s+([A-Z0-9/-]{3,24})?\s+(.+?)(?=\s{2,}H:|\n|$)",
            section,
        )
        articles = []
        for article_match in article_matches:
            article_number = article_match.group(1)
            description = article_match.group(3).strip()
            if not looks_like_article_number(article_number):
                continue
            if article_number.startswith("APR"):
                category = "Arbeitsplatten"
            else:
                category = infer_category(description)
            articles.append((article_number, description, category))

        for article_number, description, category in articles:
            for index, price_group in enumerate(price_groups):
                block_price = block_prices[index] if index < len(block_prices) else None
                rows.append(
                    {
                        "blocknummer": block_number,
                        "artikelnummer": article_number,
                        "beschreibung": description,
                        "kategorie": category,
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
    "price_group": ["preisgruppe", "pg", "preis_gruppe"],
    "chargeable": ["verrechenbar", "berechenbar", "nicht_verrechenbar", "netto_artikel"],
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
        excerpt = " | ".join(str(value) for value in row.values() if value)[:500]

        if block_price is not None or (document_type == "Blockunterlage" and block_number):
            chargeable_text = find_value(normalized, "chargeable").lower()
            chargeable = 0 if "nicht" in chargeable_text or "netto" in chargeable_text else 1
            connection.execute(
                """
                INSERT INTO block_rules (
                  id, project_id, document_id, block_number, article_number,
                  block_price, price_group, chargeable, source_file, source_excerpt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), project_id, document_id, block_number or "BLOCK-OHNE-NR", article_number, block_price, price_group, chargeable, filename, excerpt, stamp),
            )
            rules += 1

        if document_type != "Blockunterlage":
            connection.execute(
                """
                INSERT INTO extracted_positions (
                  id, project_id, document_id, document_type, article_number,
                  description, category, quantity, gross_price, net_price,
                  block_number, price_group, source_file, source_excerpt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    Returns {bc_price, moebel_eg_brutto, zubehoer_brutto, article_wg} or None.
    article_wg maps article numbers to their WG codes (MB/EG/NE/BL).
    """
    bc_price: float | None = None
    moebel_brutto: float | None = None
    eg_brutto: float | None = None
    zubehoer_brutto: float | None = None
    article_wg: dict[str, str] = {}

    price_pat = r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*$"
    wg_price_pat = re.compile(r"\b(MB|EG|NE|BL)\s+\d{1,3}(?:\.\d{3})*,\d{2}\s*$")
    article_pat = re.compile(r"([A-ZÄÖÜ][A-Z0-9ÄÖÜ/-]{2,24})")

    for line in text.splitlines():
        s = line.strip()

        m = re.match(r"Abrechnung\s+Block\s+BC\d+\s+BL\s+" + price_pat, s)
        if m:
            bc_price = parse_money(m.group(1))
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
        "bc_price": bc_price,
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


def write_reconciled_articles(connection: sqlite3.Connection, project_id: str, positions: list[dict], rules: list[dict], ab_block_data: dict | None = None) -> None:
    by_article: dict[str, dict[str, list[dict]]] = {}
    for position in positions:
        bucket = by_article.setdefault(position["article_number"], {"Bestellung": [], "Auftragsbestätigung": [], "Sonstiges": []})
        bucket.setdefault(position["document_type"], bucket["Sonstiges"]).append(position)

    rules_by_article: dict[str, dict] = {}
    for rule in rules:
        rules_by_article[rule["article_number"]] = rule

    for article_number, groups in by_article.items():
        # Skip Häcker block-reference entries (e.g. BC1623364 "Küchenblock") –
        # these are aggregate price markers in the AB, not individual articles.
        if re.fullmatch(r"BC\d+", article_number):
            continue
        order = first(groups.get("Bestellung", []))
        confirmation = first(groups.get("Auftragsbestätigung", []))
        fallback = first(groups.get("Sonstiges", [])) or order or confirmation
        rule = rules_by_article.get(article_number)
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

        if rule:
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
            status = "fehlt in AB"
            comments.append("Artikel ist in der Bestellung vorhanden, aber nicht in der AB.")
        if confirmation and not order:
            status = "zusätzlich in AB"
            comments.append("Artikel ist in der AB vorhanden, aber nicht in der Bestellung.")

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
              confirmation_found, source_refs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )


def first(items: list[dict]) -> dict | None:
    return items[0] if items else None


def best_price(position: dict | None) -> float | None:
    if not position:
        return None
    return position.get("net_price") or position.get("gross_price")


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
    connection.execute("DELETE FROM articles WHERE project_id = ?", (project_id,))
    if positions:
        write_reconciled_articles(connection, project_id, positions, rules, ab_block_data)
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
        writer.writerow(["Art.-Nr.", "Bezeichnung", "Kategorie", "Menge", "Menge Bestellung", "Menge AB", "Einzelpreis", "Blockpreis", "Blocknummer", "Preisgruppe", "Ersparnis", "Status", "Quelle", "Kommentar"])
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
            write_reconciled_articles(connection, project_id, positions, rules)

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
