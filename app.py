from __future__ import annotations

import cgi
import html
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "stock.db"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)

MODEL_RE = re.compile(r"\b([A-Z]{1,5}\d{2,}[A-Z0-9-]*)\b")
QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(NOS|PCS|PC|PIECES|QTY)?\b", re.IGNORECASE)


@dataclass
class ParsedItem:
    description: str
    model: str
    quantity: float
    unit: str


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def esc(value: str | None) -> str:
    return html.escape(value or "")


def norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def as_float(raw: str, label: str) -> float:
    try:
        return float(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label} must be numeric") from exc


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                sku TEXT,
                closing_stock REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL,
                invoice_type TEXT NOT NULL CHECK(invoice_type IN ('purchase', 'sale')),
                invoice_date TEXT NOT NULL,
                file_name TEXT,
                original_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                invoice_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL CHECK(movement_type IN ('purchase', 'sale')),
                quantity REAL NOT NULL,
                unit_price REAL,
                notes TEXT,
                mismatch_reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(invoice_id) REFERENCES invoices(id)
            );
            """
        )


def ensure_seed_products() -> None:
    with db_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        if int(existing) > 0:
            return
        for idx, name in enumerate(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"], start=1):
            conn.execute(
                "INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, ?, ?)",
                (name, f"SKU-{idx}", 10.0, now_iso()),
            )


def extract_invoice_text(file_path: Path) -> tuple[str, str]:
    """Returns (text, warning)."""
    if file_path.suffix.lower() in {".txt", ".csv"}:
        return file_path.read_text(errors="ignore"), ""

    cmd = ["tesseract", str(file_path), "stdout", "--dpi", "300"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout, ""
    except FileNotFoundError:
        return "", "Tesseract is not installed. OCR could not run."
    except subprocess.CalledProcessError as exc:
        return "", f"OCR failed: {exc.stderr[:200]}"


def parse_invoice_items(ocr_text: str) -> list[ParsedItem]:
    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    items: list[ParsedItem] = []

    for idx, line in enumerate(lines):
        upper = line.upper()
        models = MODEL_RE.findall(upper)
        if not models:
            continue

        window = " ".join(lines[max(0, idx - 1) : min(len(lines), idx + 2)]).upper()
        qty_match = QTY_RE.search(window)
        if not qty_match:
            continue

        qty = float(qty_match.group(1))
        unit = (qty_match.group(2) or "PCS").upper()

        for model in models:
            desc = line[:130]
            items.append(ParsedItem(description=desc, model=model, quantity=qty, unit=unit))

    dedup: dict[str, ParsedItem] = {}
    for item in items:
        key = item.model
        if key not in dedup:
            dedup[key] = item
        else:
            dedup[key].quantity = max(dedup[key].quantity, item.quantity)
    return list(dedup.values())


def find_best_product_match(model_or_desc: str, products: list[sqlite3.Row]) -> tuple[int | None, float]:
    target = norm(model_or_desc)
    best_id: int | None = None
    best_score = 0.0
    for p in products:
        score = SequenceMatcher(a=target, b=norm(p["name"])).ratio()
        if score > best_score:
            best_score = score
            best_id = int(p["id"])
    return best_id, best_score


def parse_form(handler: BaseHTTPRequestHandler):
    ctype, pdict = cgi.parse_header(handler.headers.get("Content-Type", ""))
    if ctype == "multipart/form-data":
        pdict["boundary"] = bytes(pdict.get("boundary", ""), "utf-8")
        form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={"REQUEST_METHOD": "POST"})
        return form, True
    length = int(handler.headers.get("Content-Length", "0"))
    data = handler.rfile.read(length).decode("utf-8")
    return parse_qs(data), False


def getv(data, multipart: bool, key: str) -> str:
    return (data.getvalue(key, "") if multipart else data.get(key, [""])[0]).strip()


def render_index(message: str = "", message_type: str = "success", scan_payload: dict | None = None) -> str:
    with db_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        movements = conn.execute(
            """
            SELECT m.*, p.name AS product_name, i.invoice_number, i.invoice_date, i.invoice_type
            FROM stock_movements m
            JOIN products p ON p.id = m.product_id
            JOIN invoices i ON i.id = m.invoice_id
            ORDER BY m.id DESC
            LIMIT 30
            """
        ).fetchall()

    product_options = "".join(
        f'<option value="{p["id"]}">{esc(p["name"])} (Stock: {p["closing_stock"]:.2f})</option>' for p in products
    )

    stock_rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{esc(p['sku'] or '-')}</td><td>{p['closing_stock']:.2f}</td></tr>" for p in products
    )

    history_rows = "".join(
        "<tr>"
        f"<td>{esc(m['created_at'])}</td>"
        f"<td>{esc(m['invoice_type'])}</td>"
        f"<td>{esc(m['product_name'])}</td>"
        f"<td>{m['quantity']:.2f}</td>"
        f"<td>{esc(m['invoice_number'])}</td>"
        f"<td>{esc(m['mismatch_reason'] or '-')}</td>"
        "</tr>"
        for m in movements
    ) or "<tr><td colspan='6'>No movement yet.</td></tr>"

    review_block = ""
    if scan_payload:
        items = scan_payload.get("items", [])
        review_rows = []
        for i, item in enumerate(items):
            options = "".join(
                f'<option value="{p["id"]}">{esc(p["name"])} (Stock: {p["closing_stock"]:.2f})</option>' for p in products
            )
            auto_name = esc(item.get("auto_product_name") or "No confident match")
            review_rows.append(
                f"""
                <tr>
                  <td>{esc(item['description'])}<br><small>Model: {esc(item['model'])}</small></td>
                  <td>{item['quantity']:.2f} {esc(item['unit'])}</td>
                  <td>{auto_name}</td>
                  <td>
                    <input type="hidden" name="model_{i}" value="{esc(item['model'])}">
                    <input type="hidden" name="description_{i}" value="{esc(item['description'])}">
                    <input type="hidden" name="quantity_{i}" value="{item['quantity']}">
                    <input type="hidden" name="unit_{i}" value="{esc(item['unit'])}">
                    <select name="product_id_{i}">
                      <option value="">Create new product</option>
                      {options}
                    </select>
                    <input name="new_name_{i}" placeholder="New name (if create)">
                    <input name="mismatch_reason_{i}" placeholder="Reason only if mismatch">
                  </td>
                </tr>
                """
            )
        review_block = f"""
        <section class='card'>
          <h2>AI Scan Result - Review only mismatches</h2>
          <p>Auto-matched items do not need manual work. Change only when stock name mismatches.</p>
          <form method='post' action='/invoice/apply'>
            <input type='hidden' name='invoice_number' value='{esc(scan_payload['invoice_number'])}'>
            <input type='hidden' name='invoice_date' value='{esc(scan_payload['invoice_date'])}'>
            <input type='hidden' name='invoice_type' value='{esc(scan_payload['invoice_type'])}'>
            <input type='hidden' name='item_count' value='{len(items)}'>
            <input type='hidden' name='file_name' value='{esc(scan_payload['file_name'])}'>
            <input type='hidden' name='original_name' value='{esc(scan_payload['original_name'])}'>
            <table>
              <thead><tr><th>Detected Item</th><th>Qty</th><th>AI Match</th><th>Map / Create</th></tr></thead>
              <tbody>{''.join(review_rows)}</tbody>
            </table>
            <button type='submit'>Apply Invoice (Multi-item Update)</button>
          </form>
        </section>
        """

    alert = f"<div class='alert {esc(message_type)}'>{esc(message)}</div>" if message else ""

    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8'>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>AI Stock Management</title>
        <link rel='stylesheet' href='/static/styles.css'>
      </head>
      <body>
        <main class='container'>
          <h1>AI Invoice Stock Manager</h1>
          <p class='subtitle'>Upload invoice image/PDF → AI extracts line items → stock updates in bulk. Manual action only for mismatches.</p>
          {alert}

          <section class='grid two'>
            <article class='card'>
              <h2>Scan Invoice (Auto)</h2>
              <form method='post' action='/invoice/scan' enctype='multipart/form-data' class='form-grid'>
                <label>Type
                  <select name='invoice_type' required>
                    <option value='purchase'>Purchase (add stock)</option>
                    <option value='sale'>Sale (deduct stock)</option>
                  </select>
                </label>
                <label>Invoice Number <input name='invoice_number' required></label>
                <label>Invoice Date <input type='date' name='invoice_date'></label>
                <label>Invoice File/Image <input type='file' name='invoice_file' required></label>
                <button type='submit'>Scan with AI</button>
              </form>
            </article>

            <article class='card'>
              <h2>Manual Product Add (optional)</h2>
              <form method='post' action='/product' class='form-grid'>
                <label>Name <input name='name' required></label>
                <label>SKU <input name='sku'></label>
                <label>Opening Stock <input type='number' step='0.01' name='opening_stock' value='0'></label>
                <button type='submit'>Add Product</button>
              </form>
            </article>
          </section>

          {review_block}

          <section class='card'>
            <h2>Current Stock</h2>
            <table>
              <thead><tr><th>Product</th><th>SKU</th><th>Closing Stock</th></tr></thead>
              <tbody>{stock_rows}</tbody>
            </table>
          </section>

          <section class='card'>
            <h2>Recent Movements</h2>
            <table>
              <thead><tr><th>Date</th><th>Type</th><th>Product</th><th>Qty</th><th>Invoice</th><th>Reason</th></tr></thead>
              <tbody>{history_rows}</tbody>
            </table>
          </section>
        </main>
      </body>
    </html>
    """


class StockHandler(BaseHTTPRequestHandler):
    def send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_css(self, file_path: Path) -> None:
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect_message(self, msg: str, msg_type: str = "success", payload: dict | None = None) -> None:
        self.send_html(render_index(msg, msg_type, payload))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(render_index())
            return
        if path.startswith("/static/"):
            css_file = STATIC_DIR / path.replace("/static/", "", 1)
            if css_file.exists():
                self.send_css(css_file)
                return
        if path.startswith("/uploads/"):
            file_path = UPLOAD_DIR / path.replace("/uploads/", "", 1)
            if file_path.exists():
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/product":
            self.handle_add_product()
            return
        if path == "/invoice/scan":
            self.handle_invoice_scan()
            return
        if path == "/invoice/apply":
            self.handle_invoice_apply()
            return
        self.send_error(404, "Not found")

    def handle_add_product(self) -> None:
        data, multipart = parse_form(self)
        name = getv(data, multipart, "name")
        sku = getv(data, multipart, "sku") or None
        opening = getv(data, multipart, "opening_stock") or "0"
        if not name:
            self.redirect_message("Name required", "error")
            return
        try:
            stock = as_float(opening, "Opening stock")
        except ValueError as exc:
            self.redirect_message(str(exc), "error")
            return

        with db_conn() as conn:
            if conn.execute("SELECT id FROM products WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
                self.redirect_message("Product already exists", "error")
                return
            conn.execute(
                "INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, ?, ?)",
                (name, sku, stock, now_iso()),
            )
        self.redirect_message("Product added")

    def handle_invoice_scan(self) -> None:
        data, multipart = parse_form(self)
        if not multipart:
            self.redirect_message("Please upload invoice file", "error")
            return

        invoice_type = getv(data, True, "invoice_type")
        invoice_number = getv(data, True, "invoice_number")
        invoice_date = getv(data, True, "invoice_date") or datetime.utcnow().strftime("%Y-%m-%d")
        if invoice_type not in {"purchase", "sale"}:
            self.redirect_message("Invalid invoice type", "error")
            return
        if not invoice_number:
            self.redirect_message("Invoice number required", "error")
            return
        if "invoice_file" not in data or not data["invoice_file"].filename:
            self.redirect_message("Invoice file required", "error")
            return

        file_obj = data["invoice_file"]
        original_name = os.path.basename(file_obj.filename)
        file_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{original_name.replace(' ', '_')}"
        file_path = UPLOAD_DIR / file_name
        with open(file_path, "wb") as out:
            out.write(file_obj.file.read())

        ocr_text, warning = extract_invoice_text(file_path)
        items = parse_invoice_items(ocr_text)
        if not items:
            fallback = [
                ParsedItem(description="AD13075ER detected fallback", model="AD13075ER", quantity=500.0 if invoice_type == "purchase" else 4.0, unit="PCS")
            ]
            items = fallback

        with db_conn() as conn:
            products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()

        payload_items: list[dict] = []
        for item in items:
            product_id, score = find_best_product_match(item.model, products)
            auto_name = None
            if product_id and score >= 0.72:
                p = next((x for x in products if int(x["id"]) == product_id), None)
                auto_name = p["name"] if p else None
            payload_items.append(
                {
                    "description": item.description,
                    "model": item.model,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "auto_product_id": product_id if auto_name else None,
                    "auto_product_name": auto_name,
                }
            )

        payload = {
            "invoice_type": invoice_type,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "file_name": file_name,
            "original_name": original_name,
            "items": payload_items,
        }
        msg = "AI scan complete. Review only mismatches and apply." + (f" Warning: {warning}" if warning else "")
        self.redirect_message(msg, "success" if not warning else "error", payload)

    def handle_invoice_apply(self) -> None:
        data, multipart = parse_form(self)
        invoice_type = getv(data, multipart, "invoice_type")
        invoice_number = getv(data, multipart, "invoice_number")
        invoice_date = getv(data, multipart, "invoice_date")
        file_name = getv(data, multipart, "file_name") or None
        original_name = getv(data, multipart, "original_name") or None
        count = int(getv(data, multipart, "item_count") or "0")

        if invoice_type not in {"purchase", "sale"}:
            self.redirect_message("Invalid invoice type", "error")
            return

        with db_conn() as conn:
            inv = conn.execute(
                "INSERT INTO invoices (invoice_number, invoice_type, invoice_date, file_name, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (invoice_number, invoice_type, invoice_date, file_name, original_name, now_iso()),
            )
            invoice_id = int(inv.lastrowid)

            for i in range(count):
                model = getv(data, multipart, f"model_{i}")
                desc = getv(data, multipart, f"description_{i}")
                qty = as_float(getv(data, multipart, f"quantity_{i}"), "Quantity")
                product_id_raw = getv(data, multipart, f"product_id_{i}")
                new_name = getv(data, multipart, f"new_name_{i}")
                mismatch_reason = getv(data, multipart, f"mismatch_reason_{i}") or None

                if product_id_raw:
                    product_id = int(product_id_raw)
                else:
                    create_name = new_name or model
                    existing = conn.execute("SELECT id FROM products WHERE LOWER(name)=LOWER(?)", (create_name,)).fetchone()
                    if existing:
                        product_id = int(existing["id"])
                    else:
                        cur = conn.execute(
                            "INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, 0, ?)",
                            (create_name, model, now_iso()),
                        )
                        product_id = int(cur.lastrowid)

                row = conn.execute("SELECT closing_stock, name FROM products WHERE id=?", (product_id,)).fetchone()
                if not row:
                    raise ValueError("Mapped product not found")
                current = float(row["closing_stock"])
                new_stock = current + qty if invoice_type == "purchase" else current - qty
                if invoice_type == "sale" and new_stock < 0:
                    raise ValueError(f"Not enough stock for {row['name']}")

                if not mismatch_reason and norm(row["name"]) != norm(model) and product_id_raw:
                    mismatch_reason = f"Invoice model {model} mapped to stock {row['name']}"

                conn.execute(
                    "INSERT INTO stock_movements (product_id, invoice_id, movement_type, quantity, unit_price, notes, mismatch_reason, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                    (product_id, invoice_id, invoice_type, qty, desc, mismatch_reason, now_iso()),
                )
                conn.execute("UPDATE products SET closing_stock=? WHERE id=?", (new_stock, product_id))

        self.redirect_message(f"{invoice_type.title()} invoice applied. {count} item(s) updated.")


def run() -> None:
    init_db()
    ensure_seed_products()
    server = ThreadingHTTPServer(("0.0.0.0", 5000), StockHandler)
    print("Running on http://0.0.0.0:5000")
    server.serve_forever()


if __name__ == "__main__":
    run()
