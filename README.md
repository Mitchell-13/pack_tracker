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

## Bookmarklet helper

This project includes `static/bookmarklet.js`, which opens a quick-add form and auto-fills the current page URL as the ticket link.

1. Make sure this app is running.
2. Create a browser bookmark with this URL (single line):

```text
javascript:(function(){var s=document.createElement('script');s.src='http://127.0.0.1:5000/static/bookmarklet.js';document.body.appendChild(s);}());
```

3. While viewing a ticket page, click the bookmark.
4. Fill in description/category/etc. in the popup form and submit. It saves directly into the database.
