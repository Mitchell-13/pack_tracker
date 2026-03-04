from __future__ import annotations

import sqlite3
import csv
import re
from io import StringIO
from datetime import datetime, date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from flask import Flask, Response, g, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "tickets.db"
FAVICON_UPLOAD_DIR = BASE_DIR / "static" / "uploads"

DEFAULT_APPEARANCE_SETTINGS = {
    "primary_color": "#1565c0",
    "background_color": "#ffffff",
    "text_color": "#222222",
    "font_css_url": "",
    "font_family": "Arial, sans-serif",
    "favicon_path": "",
}

ALLOWED_FAVICON_EXTENSIONS = {"ico", "png", "svg", "jpg", "jpeg", "webp"}

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DATABASE)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            ai_analysis TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            shared_with_manager INTEGER NOT NULL DEFAULT 0,
            favorite INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (category_id) REFERENCES categories (id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_tags (
            ticket_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (ticket_id, tag_id),
            FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
        )
        """
    )

    ticket_columns = {
        row[1] for row in db.execute("PRAGMA table_info(tickets)").fetchall()
    }
    if "ai_analysis" not in ticket_columns:
        db.execute("ALTER TABLE tickets ADD COLUMN ai_analysis TEXT NOT NULL DEFAULT ''")

    db.commit()
    db.close()


def _extract_font_family(font_css_url: str) -> str:
    parsed = urlparse(font_css_url)
    if parsed.netloc != "fonts.googleapis.com" or not parsed.path.startswith("/css2"):
        return ""

    family_values = parse_qs(parsed.query).get("family", [])
    if not family_values:
        return ""

    first_family = unquote(family_values[0]).split(":", maxsplit=1)[0]
    cleaned = re.sub(r"\+", " ", first_family).strip()
    return cleaned


def _is_valid_hex_color(color_value: str) -> bool:
    return bool(re.match(r"^#[0-9a-fA-F]{6}$", color_value))


def get_app_settings(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute("SELECT key, value FROM app_settings").fetchall()
    settings = dict(DEFAULT_APPEARANCE_SETTINGS)
    for row in rows:
        settings[row["key"]] = row["value"]
    return settings


def save_app_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO app_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


@app.context_processor
def inject_appearance_settings() -> dict[str, dict[str, str]]:
    db = get_db()
    settings = get_app_settings(db)
    favicon_path = settings.get("favicon_path", "")
    settings["favicon_url"] = url_for("static", filename=favicon_path) if favicon_path else ""
    return {"appearance_settings": settings}


def _parse_tags(raw_tags: str) -> list[str]:
    tag_names: list[str] = []
    seen: set[str] = set()

    for part in raw_tags.split(","):
        tag = part.strip()
        if not tag:
            continue
        lowered_tag = tag.lower()
        if lowered_tag in seen:
            continue
        seen.add(lowered_tag)
        tag_names.append(tag)

    return tag_names


def _sync_ticket_tags(db: sqlite3.Connection, ticket_id: int, tag_names: list[str]) -> None:
    db.execute("DELETE FROM ticket_tags WHERE ticket_id = ?", (ticket_id,))

    for tag_name in tag_names:
        db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
        tag_id_row = db.execute("SELECT id FROM tags WHERE LOWER(name) = LOWER(?)", (tag_name,)).fetchone()
        if tag_id_row is None:
            continue
        db.execute(
            "INSERT OR IGNORE INTO ticket_tags (ticket_id, tag_id) VALUES (?, ?)",
            (ticket_id, tag_id_row["id"]),
        )


def _validated_ticket_fields(form: Any) -> tuple[str, str, str, str, str, int, int, list[str]] | None:
    link = form.get("link", "").strip()
    category_id = form.get("category_id", "").strip()
    description = form.get("description", "").strip()
    ai_analysis = form.get("ai_analysis", "")
    date_value = form.get("date", "").strip()
    shared_with_manager = 1 if form.get("shared_with_manager") == "on" else 0
    favorite = 1 if form.get("favorite") == "on" else 0
    tags = _parse_tags(form.get("tags", ""))

    if not link or not category_id or not description or not date_value:
        return None

    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        return None

    return (
        link,
        category_id,
        description,
        ai_analysis,
        date_value,
        shared_with_manager,
        favorite,
        tags,
    )


@app.route("/", methods=["GET"])
def index() -> str:
    sort_by = request.args.get("sort_by", "date")
    order = request.args.get("order", "desc")
    description_search = request.args.get("q", "").strip()
    category_filter = request.args.get("category_id", "").strip()
    shared_only = request.args.get("shared_only", "0") == "1"
    favorite_only = request.args.get("favorite_only", "0") == "1"
    tag_filter = request.args.get("tags", "").strip()
    edit_id = request.args.get("edit_id", "").strip()

    allowed_sort = {"date": "t.date", "category": "c.name"}
    sort_column = allowed_sort.get(sort_by, "t.date")
    sort_order = "ASC" if order == "asc" else "DESC"

    where_clauses: list[str] = []
    params: list[Any] = []

    if description_search:
        where_clauses.append("LOWER(t.description) LIKE ?")
        params.append(f"%{description_search.lower()}%")

    if category_filter.isdigit():
        where_clauses.append("t.category_id = ?")
        params.append(int(category_filter))

    if shared_only:
        where_clauses.append("t.shared_with_manager = 1")

    if favorite_only:
        where_clauses.append("t.favorite = 1")

    for tag in _parse_tags(tag_filter):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ticket_tags tt_filter
                JOIN tags tg_filter ON tg_filter.id = tt_filter.tag_id
                WHERE tt_filter.ticket_id = t.id
                  AND LOWER(tg_filter.name) = LOWER(?)
            )
            """
        )
        params.append(tag)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    db = get_db()
    tickets = db.execute(
        f"""
        SELECT t.id, t.link, c.name AS category, t.description, t.date,
               t.ai_analysis, t.shared_with_manager, t.favorite,
               COALESCE(GROUP_CONCAT(tg.name, ', '), '') AS tags
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        LEFT JOIN ticket_tags tt ON tt.ticket_id = t.id
        LEFT JOIN tags tg ON tg.id = tt.tag_id
        {where_sql}
        GROUP BY t.id
        ORDER BY {sort_column} {sort_order}, t.id DESC
        """,
        params,
    ).fetchall()

    categories = db.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()

    ticket_to_edit = None
    if edit_id.isdigit():
        ticket_to_edit = db.execute(
            """
            SELECT id, link, category_id, description, ai_analysis, date, shared_with_manager, favorite
            FROM tickets
            WHERE id = ?
            """,
            (edit_id,),
        ).fetchone()
        if ticket_to_edit is not None:
            ticket_tags = db.execute(
                """
                SELECT tg.name
                FROM tags tg
                JOIN ticket_tags tt ON tt.tag_id = tg.id
                WHERE tt.ticket_id = ?
                ORDER BY tg.name ASC
                """,
                (ticket_to_edit["id"],),
            ).fetchall()
            ticket_to_edit = dict(ticket_to_edit)
            ticket_to_edit["tags"] = ", ".join(tag["name"] for tag in ticket_tags)

    return render_template(
        "index.html",
        tickets=tickets,
        categories=categories,
        sort_by=sort_by,
        order=order,
        description_search=description_search,
        category_filter=category_filter,
        shared_only=shared_only,
        favorite_only=favorite_only,
        tag_filter=tag_filter,
        ticket_to_edit=ticket_to_edit,
        today_date=date.today().isoformat(),
    )


@app.route("/tickets/export", methods=["GET"])
def export_tickets() -> Response:
    description_search = request.args.get("q", "").strip()
    category_filter = request.args.get("category_id", "").strip()
    shared_only = request.args.get("shared_only", "0") == "1"
    favorite_only = request.args.get("favorite_only", "0") == "1"
    tag_filter = request.args.get("tags", "").strip()

    where_clauses: list[str] = []
    params: list[Any] = []

    if description_search:
        where_clauses.append("LOWER(t.description) LIKE ?")
        params.append(f"%{description_search.lower()}%")

    if category_filter.isdigit():
        where_clauses.append("t.category_id = ?")
        params.append(int(category_filter))

    if shared_only:
        where_clauses.append("t.shared_with_manager = 1")

    if favorite_only:
        where_clauses.append("t.favorite = 1")

    for tag in _parse_tags(tag_filter):
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ticket_tags tt_filter
                JOIN tags tg_filter ON tg_filter.id = tt_filter.tag_id
                WHERE tt_filter.ticket_id = t.id
                  AND LOWER(tg_filter.name) = LOWER(?)
            )
            """
        )
        params.append(tag)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    db = get_db()
    tickets = db.execute(
        f"""
        SELECT t.id,
               t.link,
               t.category_id,
               c.name AS category,
               t.description,
               t.ai_analysis,
               t.date,
               t.shared_with_manager,
               t.favorite,
               COALESCE(GROUP_CONCAT(tg.name, ', '), '') AS tags
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        LEFT JOIN ticket_tags tt ON tt.ticket_id = t.id
        LEFT JOIN tags tg ON tg.id = tt.tag_id
        {where_sql}
        GROUP BY t.id
        ORDER BY t.date DESC, t.id DESC
        """,
        params,
    ).fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "link",
            "category_id",
            "category",
            "description",
            "ai_analysis",
            "date",
            "shared_with_manager",
            "favorite",
            "tags",
        ]
    )

    for ticket in tickets:
        writer.writerow(
            [
                ticket["id"],
                ticket["link"],
                ticket["category_id"],
                ticket["category"],
                ticket["description"],
                ticket["ai_analysis"],
                ticket["date"],
                ticket["shared_with_manager"],
                ticket["favorite"],
                ticket["tags"],
            ]
        )

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickets-export.csv"},
    )


@app.route("/tickets", methods=["POST"])
def add_ticket() -> Any:
    ticket_fields = _validated_ticket_fields(request.form)
    if ticket_fields is None:
        return redirect(url_for("index"))

    link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite, tags = ticket_fields

    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO tickets (link, category_id, description, ai_analysis, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite),
    )
    _sync_ticket_tags(db, cursor.lastrowid, tags)
    db.commit()
    return redirect(url_for("index"))


@app.route("/bookmarklet/new", methods=["GET", "POST"])
def bookmarklet_add_ticket() -> str:
    db = get_db()
    categories = db.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()

    if request.method == "GET":
        link = request.args.get("link", "").strip()
        return render_template(
            "bookmarklet_form.html",
            categories=categories,
            today_date=date.today().isoformat(),
            success=False,
            error=False,
            form_values={
                "link": link,
                "description": "",
                "ai_analysis": "",
                "tags": "",
                "date": date.today().isoformat(),
                "category_id": "",
                "shared_with_manager": False,
                "favorite": False,
            },
        )

    ticket_fields = _validated_ticket_fields(request.form)
    form_values = {
        "link": request.form.get("link", "").strip(),
        "description": request.form.get("description", "").strip(),
        "ai_analysis": request.form.get("ai_analysis", ""),
        "tags": request.form.get("tags", ""),
        "date": request.form.get("date", "").strip(),
        "category_id": request.form.get("category_id", "").strip(),
        "shared_with_manager": request.form.get("shared_with_manager") == "on",
        "favorite": request.form.get("favorite") == "on",
    }

    if ticket_fields is None:
        return render_template(
            "bookmarklet_form.html",
            categories=categories,
            today_date=date.today().isoformat(),
            success=False,
            error=True,
            form_values=form_values,
        )

    link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite, tags = ticket_fields
    cursor = db.execute(
        """
        INSERT INTO tickets (link, category_id, description, ai_analysis, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite),
    )
    _sync_ticket_tags(db, cursor.lastrowid, tags)
    db.commit()

    form_values.update(
        {
            "description": "",
            "ai_analysis": "",
            "tags": "",
            "shared_with_manager": False,
            "favorite": False,
        }
    )
    return render_template(
        "bookmarklet_form.html",
        categories=categories,
        today_date=date.today().isoformat(),
        success=True,
        error=False,
        form_values=form_values,
    )


@app.route("/tickets/<int:ticket_id>/edit", methods=["POST"])
def edit_ticket(ticket_id: int) -> Any:
    ticket_fields = _validated_ticket_fields(request.form)
    if ticket_fields is None:
        return redirect(url_for("index", edit_id=ticket_id))

    link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite, tags = ticket_fields

    db = get_db()
    db.execute(
        """
        UPDATE tickets
        SET link = ?,
            category_id = ?,
            description = ?,
            ai_analysis = ?,
            date = ?,
            shared_with_manager = ?,
            favorite = ?
        WHERE id = ?
        """,
        (link, category_id, description, ai_analysis, date_value, shared_with_manager, favorite, ticket_id),
    )
    _sync_ticket_tags(db, ticket_id, tags)
    db.commit()
    return redirect(url_for("index"))



@app.route("/tickets/<int:ticket_id>/delete", methods=["POST"])
def delete_ticket(ticket_id: int) -> Any:
    db = get_db()
    db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    db.commit()
    return redirect(url_for("index"))

@app.route("/categories", methods=["POST"])
def add_category() -> Any:
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("index"))

    db = get_db()
    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def appearance_settings() -> str:
    db = get_db()
    settings = get_app_settings(db)
    message = ""
    error = ""

    if request.method == "POST":
        primary_color = request.form.get("primary_color", settings["primary_color"]).strip()
        background_color = request.form.get("background_color", settings["background_color"]).strip()
        text_color = request.form.get("text_color", settings["text_color"]).strip()
        font_css_url = request.form.get("font_css_url", "").strip()

        if not all(
            _is_valid_hex_color(value)
            for value in (primary_color, background_color, text_color)
        ):
            error = "Colors must be valid 6-digit hex values."
        elif font_css_url and not font_css_url.startswith("https://fonts.googleapis.com/css2"):
            error = "Google Fonts URL must start with https://fonts.googleapis.com/css2"
        else:
            save_app_setting(db, "primary_color", primary_color)
            save_app_setting(db, "background_color", background_color)
            save_app_setting(db, "text_color", text_color)
            save_app_setting(db, "font_css_url", font_css_url)
            save_app_setting(
                db,
                "font_family",
                f"'{_extract_font_family(font_css_url)}', Arial, sans-serif" if font_css_url else DEFAULT_APPEARANCE_SETTINGS["font_family"],
            )

            favicon_file = request.files.get("favicon")
            if favicon_file and favicon_file.filename:
                extension = favicon_file.filename.rsplit(".", maxsplit=1)[-1].lower() if "." in favicon_file.filename else ""
                if extension not in ALLOWED_FAVICON_EXTENSIONS:
                    error = "Unsupported favicon type. Use .ico, .png, .svg, .jpg, .jpeg, or .webp"
                else:
                    FAVICON_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    safe_name = secure_filename(favicon_file.filename)
                    new_name = f"{uuid4().hex}-{safe_name}"
                    upload_path = FAVICON_UPLOAD_DIR / new_name
                    favicon_file.save(upload_path)
                    save_app_setting(db, "favicon_path", f"uploads/{new_name}")

            if not error:
                db.commit()
                return redirect(url_for("appearance_settings", saved="1"))

    if request.args.get("saved") == "1":
        message = "Appearance settings updated."

    return render_template("settings.html", settings=settings, message=message, error=error)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
