# Pack Tracker

Simple Flask app for tracking entries with:
- entry link
- category (with custom category creation)
- description
- date
- description search
- filters for favorite entries

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000

## Bookmarklet helper

This project includes `static/bookmarklet.js`, which opens a quick-add form and auto-fills the current page URL as the link.

1. Make sure this app is running.
2. Create a browser bookmark with this URL (single line):

```text
javascript: (function () {  const configuredBaseUrl = window.PACKTracker_BASE_URL || "http://127.0.0.1:5000";  let targetUrl;  try {    targetUrl = new URL("/bookmarklet/new", configuredBaseUrl);  } catch (error) {    alert("PackTracker bookmarklet is misconfigured. Set a valid PACKTracker_BASE_URL.");    return;  }  targetUrl.searchParams.set("link", window.location.href);  const popup = window.open(targetUrl.toString(), "packTracker-bookmarklet", "width=900,height=820,resizable=yes,scrollbars=yes");  if (!popup) {    window.location.href = targetUrl.toString();  }})();
```

3. While viewing a webpage, click the bookmark.
4. Fill in description/category/etc. in the popup form and submit. It saves directly into the database.
