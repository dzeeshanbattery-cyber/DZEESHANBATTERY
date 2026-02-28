# AI Invoice Stock Management App

Python web app for stock management where invoice scanning is automatic and manual work is needed only for stock-name mismatch mapping.

## What it does
- Seeds initial stock products `A` to `J` with quantity `10` each (first run only).
- Supports **purchase invoice scan** to auto-add stock in bulk.
- Supports **sale invoice scan** to auto-deduct stock in bulk.
- Uses OCR (`tesseract`) + rule-based AI parsing to detect model and quantity lines.
- Shows review step for mismatch cases:
  - map invoice item to existing stock
  - or create new stock item
- Saves invoice files and full movement audit trail.

## Run
```bash
python app.py
```
Open: `http://localhost:5000`

## OCR dependency
Install `tesseract` in your system for best scan quality.
If OCR is not available, the app still opens and provides a fallback parser behavior.
