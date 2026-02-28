# AI Invoice Stock Management App

Working stock app with AI-style invoice extraction and automatic stock updates.

## Flow
1. Seed stock starts with products `A..J` and qty `10` each (first run).
2. Upload purchase/sale invoice image/PDF/TXT.
3. App extracts model + quantity from invoice text.
4. Confident matches auto-update stock in bulk.
5. Only mismatch items are shown for manual map/create.

## Features
- Purchase invoice adds stock.
- Sale invoice deducts stock with negative stock protection.
- Bulk multi-item invoice support.
- Invoice + movement audit trail stored in SQLite.
- Manual action only for mismatches.

## Run
```bash
python app.py
```
Open `http://localhost:5000`

## OCR note
For image/PDF scanning install `tesseract` on your machine.
TXT invoices work without OCR.
