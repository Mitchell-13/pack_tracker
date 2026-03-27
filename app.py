from __future__ import annotations

import sqlite3
import csv
import re
from io import StringIO
from datetime import datetime, date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "tickets.db"

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


def _human_readable_date(raw_date: str) -> str:
    parsed_date = datetime.strptime(raw_date, "%Y-%m-%d")
    return f"{parsed_date:%B} {parsed_date.day}, {parsed_date:%Y}"


def _validated_filter_date(raw_date: str) -> str:
    normalized_date = raw_date.strip()
    if not normalized_date:
        return ""

    try:
        datetime.strptime(normalized_date, "%Y-%m-%d")
    except ValueError:
        return ""

    return normalized_date


def _entry_link_label(link: str) -> str:
    parsed_link = urlparse(link)
    host = parsed_link.netloc.lower()
    path = parsed_link.path.strip("/")

    if "zendesk" in host:
        match = re.search(r"/tickets/(\d+)", parsed_link.path, re.IGNORECASE)
        if match:
            return f"ZD {match.group(1)}"

    if "atlassian" in host:
        ticket_key = path.rsplit("/", maxsplit=1)[-1]
        match = re.fullmatch(r"([A-Za-z]+)-(\d+)", ticket_key)
        if match:
            return f"{match.group(1).upper()} {match.group(2)}"

    return "Link"


def _get_or_create_category_id(db: sqlite3.Connection, category_id: str, new_category_name: str) -> str:
    normalized_category_id = category_id.strip()
    normalized_new_category = new_category_name.strip()

    if normalized_category_id:
        return normalized_category_id

    if not normalized_new_category:
        return ""

    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (normalized_new_category,))
    category_row = db.execute(
        "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
        (normalized_new_category,),
    ).fetchone()
    if category_row is None:
        return ""

    return str(category_row["id"])


def _category_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()


def _tag_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE ASC").fetchall()


def _create_category(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    normalized_name = name.strip()
    if not normalized_name:
        return None

    db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (normalized_name,))
    return db.execute(
        "SELECT id, name FROM categories WHERE LOWER(name) = LOWER(?)",
        (normalized_name,),
    ).fetchone()


def _delete_category_if_unused(db: sqlite3.Connection, category_id: int) -> bool:
    linked_ticket = db.execute(
        "SELECT 1 FROM tickets WHERE category_id = ? LIMIT 1",
        (category_id,),
    ).fetchone()
    if linked_ticket is not None:
        return False

    db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    return True


def _build_entry_filters(args: Any) -> tuple[list[str], list[Any], dict[str, Any]]:
    description_search = args.get("q", "").strip()
    category_filter = args.get("category_id", "").strip()
    shared_only = args.get("shared_only", "0") == "1"
    favorite_only = args.get("favorite_only", "0") == "1"
    tag_filter = args.get("tags", "").strip()
    date_from = _validated_filter_date(args.get("date_from", ""))
    date_to = _validated_filter_date(args.get("date_to", ""))

    where_clauses: list[str] = []
    params: list[Any] = []

    if description_search:
        where_clauses.append("(LOWER(t.description) LIKE ? OR LOWER(t.link) LIKE ?)")
        search_value = f"%{description_search.lower()}%"
        params.extend([search_value, search_value])

    if category_filter.isdigit():
        where_clauses.append("t.category_id = ?")
        params.append(int(category_filter))

    if shared_only:
        where_clauses.append("t.shared_with_manager = 1")

    if favorite_only:
        where_clauses.append("t.favorite = 1")

    if date_from:
        where_clauses.append("t.date >= ?")
        params.append(date_from)

    if date_to:
        where_clauses.append("t.date <= ?")
        params.append(date_to)

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

    filter_state = {
        "description_search": description_search,
        "category_filter": category_filter,
        "shared_only": shared_only,
        "favorite_only": favorite_only,
        "tag_filter": tag_filter,
        "date_from": date_from,
        "date_to": date_to,
    }
    return where_clauses, params, filter_state


def _validated_entry_fields(form: Any) -> tuple[str, str, str, str, str, int, int, list[str]] | None:
    link = form.get("link", "").strip()
    category_id = form.get("category_id", "").strip()
    description = form.get("description", "").strip()
    notes = form.get("ai_analysis", "")
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
        notes,
        date_value,
        shared_with_manager,
        favorite,
        tags,
    )


@app.route("/favicon.ico")
def favicon() -> Response:
    return redirect(url_for("static", filename="favicon.ico"))

@app.route("/", methods=["GET"])
def index() -> str:
    sort_by = request.args.get("sort_by", "date")
    order = request.args.get("order", "desc")
    edit_id = request.args.get("edit_id", "").strip()
    page_value = request.args.get("page", "1").strip()

    try:
        page = max(1, int(page_value))
    except ValueError:
        page = 1

    per_page = 25

    allowed_sort = {"date": "t.date", "category": "c.name"}
    sort_column = allowed_sort.get(sort_by, "t.date")
    sort_order = "ASC" if order == "asc" else "DESC"

    where_clauses, params, filter_state = _build_entry_filters(request.args)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    db = get_db()
    total_count = db.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        {where_sql}
        """,
        params,
    ).fetchone()["total"]

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    ticket_rows = db.execute(
        f"""
        SELECT t.id, t.link, c.name AS category, t.description, t.date,
               t.ai_analysis AS notes, t.shared_with_manager, t.favorite,
               COALESCE(GROUP_CONCAT(tg.name, ', '), '') AS tags
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        LEFT JOIN ticket_tags tt ON tt.ticket_id = t.id
        LEFT JOIN tags tg ON tg.id = tt.tag_id
        {where_sql}
        GROUP BY t.id
        ORDER BY {sort_column} {sort_order}, t.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    ).fetchall()

    tickets = []
    for ticket in ticket_rows:
        ticket_dict = dict(ticket)
        ticket_dict["display_date"] = _human_readable_date(ticket_dict["date"])
        ticket_dict["link_label"] = _entry_link_label(ticket_dict["link"])
        tickets.append(ticket_dict)

    categories = _category_rows(db)
    available_tags = [row["name"] for row in _tag_rows(db)]

    ticket_to_edit = None
    if edit_id.isdigit():
        ticket_to_edit = db.execute(
            """
            SELECT id, link, category_id, description, ai_analysis AS notes, date, shared_with_manager, favorite
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
        total_count=total_count,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        sort_by=sort_by,
        order=order,
        **filter_state,
        ticket_to_edit=ticket_to_edit,
        available_tags=available_tags,
        today_date=date.today().isoformat(),
    )


@app.route("/tickets/export", methods=["GET"])
def export_tickets() -> Response:
    where_clauses, params, _ = _build_entry_filters(request.args)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    db = get_db()
    tickets = db.execute(
        f"""
        SELECT t.id,
               t.link,
               t.category_id,
               c.name AS category,
               t.description,
               t.ai_analysis AS notes,
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
            "notes",
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
                ticket["notes"],
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
    entry_fields = _validated_entry_fields(request.form)
    if entry_fields is None:
        return redirect(url_for("index"))

    link, category_id, description, notes, date_value, shared_with_manager, favorite, tags = entry_fields

    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO tickets (link, category_id, description, ai_analysis, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (link, category_id, description, notes, date_value, shared_with_manager, favorite),
    )
    _sync_ticket_tags(db, cursor.lastrowid, tags)
    db.commit()
    return redirect(url_for("index"))


@app.route("/bookmarklet/new", methods=["GET", "POST"])
def bookmarklet_add_ticket() -> str:
    db = get_db()
    categories = _category_rows(db)

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
                "notes": "",
                "tags": "",
                "date": date.today().isoformat(),
                "category_id": "",
                "new_category_name": "",
                "shared_with_manager": False,
                "favorite": False,
            },
        )

    selected_category_id = _get_or_create_category_id(
        db,
        request.form.get("category_id", ""),
        request.form.get("new_category_name", ""),
    )
    submitted_form = request.form.copy()
    submitted_form["category_id"] = selected_category_id

    entry_fields = _validated_entry_fields(submitted_form)
    form_values = {
        "link": request.form.get("link", "").strip(),
        "description": request.form.get("description", "").strip(),
        "notes": request.form.get("ai_analysis", ""),
        "tags": request.form.get("tags", ""),
        "date": request.form.get("date", "").strip(),
        "category_id": selected_category_id,
        "new_category_name": request.form.get("new_category_name", "").strip(),
        "shared_with_manager": request.form.get("shared_with_manager") == "on",
        "favorite": request.form.get("favorite") == "on",
    }

    if entry_fields is None:
        categories = _category_rows(db)
        return render_template(
            "bookmarklet_form.html",
            categories=categories,
            today_date=date.today().isoformat(),
            success=False,
            error=True,
            form_values=form_values,
        )

    link, category_id, description, notes, date_value, shared_with_manager, favorite, tags = entry_fields
    cursor = db.execute(
        """
        INSERT INTO tickets (link, category_id, description, ai_analysis, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (link, category_id, description, notes, date_value, shared_with_manager, favorite),
    )
    _sync_ticket_tags(db, cursor.lastrowid, tags)
    db.commit()

    form_values.update(
        {
            "description": "",
            "notes": "",
            "tags": "",
            "new_category_name": "",
            "shared_with_manager": False,
            "favorite": False,
        }
    )
    categories = _category_rows(db)
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
    entry_fields = _validated_entry_fields(request.form)
    if entry_fields is None:
        return redirect(url_for("index", edit_id=ticket_id))

    link, category_id, description, notes, date_value, shared_with_manager, favorite, tags = entry_fields

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
        (link, category_id, description, notes, date_value, shared_with_manager, favorite, ticket_id),
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
    name = request.form.get("name", "")
    db = get_db()
    category_row = _create_category(db, name)
    if category_row is None:
        return redirect(url_for("index"))

    db.commit()
    return redirect(url_for("index"))


@app.route("/categories/json", methods=["POST"])
def add_category_json() -> Response:
    db = get_db()
    category_row = _create_category(db, request.form.get("name", ""))
    if category_row is None:
        return jsonify({"success": False, "error": "Category name is required."}), 400

    db.commit()
    categories = [{"id": row["id"], "name": row["name"]} for row in _category_rows(db)]
    return jsonify(
        {
            "success": True,
            "category": {"id": category_row["id"], "name": category_row["name"]},
            "categories": categories,
        }
    )


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id: int) -> Any:
    db = get_db()
    deleted = _delete_category_if_unused(db, category_id)
    if not deleted:
        return redirect(url_for("index"))

    db.commit()
    return redirect(url_for("index"))


@app.route("/categories/<int:category_id>/delete/json", methods=["POST"])
def delete_category_json(category_id: int) -> Response:
    db = get_db()
    deleted = _delete_category_if_unused(db, category_id)
    if not deleted:
        return jsonify({"success": False, "error": "Category is in use and cannot be deleted."}), 400

    db.commit()
    categories = [{"id": row["id"], "name": row["name"]} for row in _category_rows(db)]
    return jsonify({"success": True, "categories": categories})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
