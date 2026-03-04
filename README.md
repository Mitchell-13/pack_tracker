# Ticket Tracker (Flask)

Simple Flask app for tracking tickets with:
- ticket link
- category (with custom category creation)
- description
- date
- checkbox for "shared with manager"
- checkbox for favorites
- description search
- filters for shared/favorite tickets
- in-place editing of existing tickets

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000
