from __future__ import annotations

import cgi
import html
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "stock.db"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)

MODEL_RE = re.compile(r"\b([A-Z]{1,6}\d{2,}[A-Z0-9-]*)\b")
QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(NOS|PCS|PC|PIECES|QTY|UNITS)?\b", re.IGNORECASE)


@dataclass
class ParsedItem:
    model: str
    description: str
    quantity: float
    unit: str


def esc(value: str | None) -> str:
    return html.escape(value or "")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                notes TEXT,
                mismatch_reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(invoice_id) REFERENCES invoices(id)
            );
            """
        )


def seed_products() -> None:
    with db_conn() as conn:
        if conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]:
            return
        for i, name in enumerate(list("ABCDEFGHIJ"), start=1):
            conn.execute(
                "INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, ?, ?)",
                (name, f"SKU-{i}", 10.0, now_iso()),
            )


def parse_form(handler: BaseHTTPRequestHandler):
    ctype, pdict = cgi.parse_header(handler.headers.get("Content-Type", ""))
    if ctype == "multipart/form-data":
        pdict["boundary"] = bytes(pdict.get("boundary", ""), "utf-8")
        form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={"REQUEST_METHOD": "POST"})
        return form, True
    length = int(handler.headers.get("Content-Length", "0"))
    data = handler.rfile.read(length).decode("utf-8")
    return parse_qs(data), False


def getv(data, is_multipart: bool, key: str) -> str:
    return (data.getvalue(key, "") if is_multipart else data.get(key, [""])[0]).strip()


def extract_text(file_path: Path) -> tuple[str, str | None]:
    if file_path.suffix.lower() in {".txt", ".csv"}:
        return file_path.read_text(errors="ignore"), None
    try:
        result = subprocess.run(
            ["tesseract", str(file_path), "stdout", "--dpi", "300"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout, None
    except FileNotFoundError:
        return "", "OCR engine (tesseract) not installed. Install it to scan images/PDF automatically."
    except subprocess.CalledProcessError as exc:
        return "", f"OCR failed: {exc.stderr[:160]}"


def parse_items(text: str) -> list[ParsedItem]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found: dict[str, ParsedItem] = {}

    for i, line in enumerate(lines):
        upper = line.upper()
        models = MODEL_RE.findall(upper)
        if not models:
            continue
        nearby = " ".join(lines[max(0, i - 1): min(len(lines), i + 3)]).upper()
        qty_match = QTY_RE.search(nearby)
        if not qty_match:
            continue
        qty = float(qty_match.group(1))
        unit = (qty_match.group(2) or "PCS").upper()
        for model in models:
            if model not in found:
                found[model] = ParsedItem(model=model, description=line[:150], quantity=qty, unit=unit)
            else:
                found[model].quantity = max(found[model].quantity, qty)
    return list(found.values())


def pick_product(item: ParsedItem, products: list[sqlite3.Row]) -> tuple[int | None, str | None, str | None]:
    # Returns product_id, auto_match_name, reason
    target = norm(item.model)

    for p in products:
        if norm(p["name"]) == target or norm(p["sku"] or "") == target:
            return int(p["id"]), p["name"], None

    best_id = None
    best_score = 0.0
    for p in products:
        score = max(
            SequenceMatcher(a=target, b=norm(p["name"])).ratio(),
            SequenceMatcher(a=target, b=norm(p["sku"] or "")).ratio(),
        )
        if score > best_score:
            best_score, best_id = score, int(p["id"])

    if best_id and best_score >= 0.90:
        name = next(p["name"] for p in products if int(p["id"]) == best_id)
        return best_id, name, f"Fuzzy auto-match ({best_score:.2f})"
    return None, None, None


def apply_movement(conn: sqlite3.Connection, invoice_id: int, invoice_type: str, product_id: int, qty: float, notes: str, mismatch_reason: str | None) -> None:
    row = conn.execute("SELECT name, closing_stock FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError("Product not found")
    current = float(row["closing_stock"])
    new_stock = current + qty if invoice_type == "purchase" else current - qty
    if invoice_type == "sale" and new_stock < 0:
        raise ValueError(f"Sale exceeds stock for {row['name']}")

    conn.execute(
        "INSERT INTO stock_movements (product_id, invoice_id, movement_type, quantity, notes, mismatch_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (product_id, invoice_id, invoice_type, qty, notes, mismatch_reason, now_iso()),
    )
    conn.execute("UPDATE products SET closing_stock=? WHERE id=?", (new_stock, product_id))


def render_home(message: str = "", level: str = "success", review: dict | None = None) -> str:
    with db_conn() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        movements = conn.execute(
            """
            SELECT m.*, p.name AS product_name, i.invoice_number, i.invoice_type
            FROM stock_movements m
            JOIN products p ON p.id = m.product_id
            JOIN invoices i ON i.id = m.invoice_id
            ORDER BY m.id DESC LIMIT 40
            """
        ).fetchall()

    msg = f"<div class='alert {esc(level)}'>{esc(message)}</div>" if message else ""
    stock_rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{esc(p['sku'] or '-')}</td><td>{p['closing_stock']:.2f}</td></tr>" for p in products
    )
    move_rows = "".join(
        f"<tr><td>{esc(m['created_at'])}</td><td>{esc(m['invoice_type'])}</td><td>{esc(m['product_name'])}</td><td>{m['quantity']:.2f}</td><td>{esc(m['invoice_number'])}</td><td>{esc(m['mismatch_reason'] or '-')}</td></tr>"
        for m in movements
    ) or "<tr><td colspan='6'>No movement yet.</td></tr>"

    review_html = ""
    if review:
        options = "".join(f"<option value='{p['id']}'>{esc(p['name'])} (Stock {p['closing_stock']:.2f})</option>" for p in products)
        rows = []
        for i, item in enumerate(review["mismatches"]):
            rows.append(
                f"""
                <tr>
                  <td>{esc(item['model'])}<br><small>{esc(item['description'])}</small></td>
                  <td>{item['quantity']:.2f} {esc(item['unit'])}</td>
                  <td>
                    <input type='hidden' name='model_{i}' value='{esc(item['model'])}'>
                    <input type='hidden' name='description_{i}' value='{esc(item['description'])}'>
                    <input type='hidden' name='quantity_{i}' value='{item['quantity']}'>
                    <input type='hidden' name='unit_{i}' value='{esc(item['unit'])}'>
                    <select name='product_id_{i}'><option value=''>Create new</option>{options}</select>
                    <input name='new_name_{i}' placeholder='New name (if create)'>
                    <input name='reason_{i}' placeholder='Mismatch reason'>
                  </td>
                </tr>
                """
            )
        review_html = f"""
        <section class='card'>
          <h2>Mismatch Review (only these require manual action)</h2>
          <form method='post' action='/invoice/mismatch' class='form-grid'>
            <input type='hidden' name='invoice_id' value='{review['invoice_id']}'>
            <input type='hidden' name='invoice_type' value='{esc(review['invoice_type'])}'>
            <input type='hidden' name='count' value='{len(review['mismatches'])}'>
            <table>
              <thead><tr><th>Detected item</th><th>Qty</th><th>Map/Create</th></tr></thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
            <button type='submit'>Apply mismatches</button>
          </form>
        </section>
        """

    return f"""
    <!doctype html><html><head>
      <meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
      <title>AI Stock App</title><link rel='stylesheet' href='/static/styles.css'>
    </head><body><main class='container'>
      <h1>AI Invoice Stock Manager</h1>
      <p class='subtitle'>Automatic stock update from invoice scan. Manual work only for mismatches.</p>
      {msg}

      <section class='grid two'>
        <article class='card'>
          <h2>Scan Purchase / Sale Invoice</h2>
          <form method='post' action='/invoice/scan' enctype='multipart/form-data' class='form-grid'>
            <label>Invoice type
              <select name='invoice_type' required>
                <option value='purchase'>Purchase (+)</option>
                <option value='sale'>Sale (-)</option>
              </select>
            </label>
            <label>Invoice Number <input name='invoice_number' required></label>
            <label>Invoice Date <input name='invoice_date' type='date'></label>
            <label>Invoice Image/PDF/TXT <input type='file' name='invoice_file' required></label>
            <button type='submit'>Scan and Auto Update</button>
          </form>
        </article>

        <article class='card'>
          <h2>Add Product (optional)</h2>
          <form method='post' action='/product' class='form-grid'>
            <label>Name <input name='name' required></label>
            <label>SKU/Model <input name='sku'></label>
            <label>Opening Stock <input type='number' step='0.01' name='opening_stock' value='0'></label>
            <button type='submit'>Add Product</button>
          </form>
        </article>
      </section>

      {review_html}

      <section class='card'><h2>Current Stock</h2>
        <table><thead><tr><th>Product</th><th>SKU</th><th>Closing Stock</th></tr></thead><tbody>{stock_rows}</tbody></table>
      </section>

      <section class='card'><h2>Movement History</h2>
        <table><thead><tr><th>Date</th><th>Type</th><th>Product</th><th>Qty</th><th>Invoice</th><th>Mismatch</th></tr></thead><tbody>{move_rows}</tbody></table>
      </section>
    </main></body></html>
    """


class Handler(BaseHTTPRequestHandler):
    def html(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.html(render_home())
            return
        if path.startswith("/static/"):
            file_path = STATIC_DIR / path.removeprefix("/static/")
            if file_path.exists():
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/product":
            self.add_product()
            return
        if path == "/invoice/scan":
            self.scan_invoice()
            return
        if path == "/invoice/mismatch":
            self.apply_mismatch()
            return
        self.send_error(404)

    def add_product(self) -> None:
        data, mp = parse_form(self)
        name = getv(data, mp, "name")
        sku = getv(data, mp, "sku") or None
        opening = float(getv(data, mp, "opening_stock") or "0")
        if not name:
            self.html(render_home("Name is required", "error"))
            return
        with db_conn() as conn:
            if conn.execute("SELECT 1 FROM products WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
                self.html(render_home("Product already exists", "error"))
                return
            conn.execute("INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, ?, ?)", (name, sku, opening, now_iso()))
        self.html(render_home("Product added"))

    def scan_invoice(self) -> None:
        data, mp = parse_form(self)
        if not mp:
            self.html(render_home("File upload required", "error"))
            return

        inv_type = getv(data, True, "invoice_type")
        inv_no = getv(data, True, "invoice_number")
        inv_date = getv(data, True, "invoice_date") or datetime.utcnow().strftime("%Y-%m-%d")
        if inv_type not in {"purchase", "sale"} or not inv_no:
            self.html(render_home("Invoice type and number are required", "error"))
            return
        if "invoice_file" not in data or not data["invoice_file"].filename:
            self.html(render_home("Invoice file is required", "error"))
            return

        uploaded = data["invoice_file"]
        original_name = os.path.basename(uploaded.filename)
        file_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{original_name.replace(' ', '_')}"
        file_path = UPLOAD_DIR / file_name
        with open(file_path, "wb") as f:
            f.write(uploaded.file.read())

        text, warn = extract_text(file_path)
        items = parse_items(text)
        if not items:
            self.html(render_home("No invoice items detected. Use a clearer scan or install tesseract OCR.", "error"))
            return

        with db_conn() as conn:
            products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()

            inv = conn.execute(
                "INSERT INTO invoices (invoice_number, invoice_type, invoice_date, file_name, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (inv_no, inv_type, inv_date, file_name, original_name, now_iso()),
            )
            invoice_id = int(inv.lastrowid)

            auto_count = 0
            mismatches: list[dict] = []
            for item in items:
                pid, pname, reason = pick_product(item, products)
                if pid is None:
                    mismatches.append(item.__dict__)
                    continue
                try:
                    apply_movement(conn, invoice_id, inv_type, pid, item.quantity, item.description, reason)
                    auto_count += 1
                except ValueError as exc:
                    self.html(render_home(str(exc), "error"))
                    return

        msg = f"Scan complete: {auto_count} item(s) auto-updated."
        if warn:
            msg += f" Warning: {warn}"
        if mismatches:
            review = {"invoice_id": invoice_id, "invoice_type": inv_type, "mismatches": mismatches}
            self.html(render_home(msg + f" {len(mismatches)} mismatch item(s) need review.", "success", review))
        else:
            self.html(render_home(msg, "success"))

    def apply_mismatch(self) -> None:
        data, mp = parse_form(self)
        invoice_id = int(getv(data, mp, "invoice_id"))
        inv_type = getv(data, mp, "invoice_type")
        count = int(getv(data, mp, "count") or "0")

        with db_conn() as conn:
            for i in range(count):
                model = getv(data, mp, f"model_{i}")
                desc = getv(data, mp, f"description_{i}")
                qty = float(getv(data, mp, f"quantity_{i}") or "0")
                product_id_raw = getv(data, mp, f"product_id_{i}")
                new_name = getv(data, mp, f"new_name_{i}")
                reason = getv(data, mp, f"reason_{i}") or None

                if product_id_raw:
                    product_id = int(product_id_raw)
                    if not reason:
                        row = conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
                        if row and norm(row["name"]) != norm(model):
                            reason = f"Mapped {model} to {row['name']}"
                else:
                    create_name = new_name or model
                    row = conn.execute("SELECT id FROM products WHERE LOWER(name)=LOWER(?)", (create_name,)).fetchone()
                    if row:
                        product_id = int(row["id"])
                    else:
                        cur = conn.execute(
                            "INSERT INTO products (name, sku, closing_stock, created_at) VALUES (?, ?, 0, ?)",
                            (create_name, model, now_iso()),
                        )
                        product_id = int(cur.lastrowid)

                apply_movement(conn, invoice_id, inv_type, product_id, qty, desc, reason)

        self.html(render_home("Mismatch items applied successfully."))


def run() -> None:
    init_db()
    seed_products()
    server = ThreadingHTTPServer(("0.0.0.0", 5000), Handler)
    print("Server running at http://0.0.0.0:5000")
    server.serve_forever()


if __name__ == "__main__":
    run()
