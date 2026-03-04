from __future__ import annotations

import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any

from flask import Flask, g, redirect, render_template, request, url_for

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
            date TEXT NOT NULL,
            shared_with_manager INTEGER NOT NULL DEFAULT 0,
            favorite INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (category_id) REFERENCES categories (id)
        )
        """
    )
    db.commit()
    db.close()


def _validated_ticket_fields(form: Any) -> tuple[str, str, str, str, int, int] | None:
    link = form.get("link", "").strip()
    category_id = form.get("category_id", "").strip()
    description = form.get("description", "").strip()
    date_value = form.get("date", "").strip()
    shared_with_manager = 1 if form.get("shared_with_manager") == "on" else 0
    favorite = 1 if form.get("favorite") == "on" else 0

    if not link or not category_id or not description or not date_value:
        return None

    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        return None

    return link, category_id, description, date_value, shared_with_manager, favorite


@app.route("/", methods=["GET"])
def index() -> str:
    sort_by = request.args.get("sort_by", "date")
    order = request.args.get("order", "desc")
    description_search = request.args.get("q", "").strip()
    category_filter = request.args.get("category_id", "").strip()
    shared_only = request.args.get("shared_only", "0") == "1"
    favorite_only = request.args.get("favorite_only", "0") == "1"
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

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    db = get_db()
    tickets = db.execute(
        f"""
        SELECT t.id, t.link, c.name AS category, t.description, t.date,
               t.shared_with_manager, t.favorite
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        {where_sql}
        ORDER BY {sort_column} {sort_order}, t.id DESC
        """,
        params,
    ).fetchall()

    categories = db.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()

    ticket_to_edit = None
    if edit_id.isdigit():
        ticket_to_edit = db.execute(
            """
            SELECT id, link, category_id, description, date, shared_with_manager, favorite
            FROM tickets
            WHERE id = ?
            """,
            (edit_id,),
        ).fetchone()

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
        ticket_to_edit=ticket_to_edit,
        today_date=date.today().isoformat(),
    )


@app.route("/tickets", methods=["POST"])
def add_ticket() -> Any:
    ticket_fields = _validated_ticket_fields(request.form)
    if ticket_fields is None:
        return redirect(url_for("index"))

    db = get_db()
    db.execute(
        """
        INSERT INTO tickets (link, category_id, description, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ticket_fields,
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/tickets/<int:ticket_id>/edit", methods=["POST"])
def edit_ticket(ticket_id: int) -> Any:
    ticket_fields = _validated_ticket_fields(request.form)
    if ticket_fields is None:
        return redirect(url_for("index", edit_id=ticket_id))

    db = get_db()
    db.execute(
        """
        UPDATE tickets
        SET link = ?,
            category_id = ?,
            description = ?,
            date = ?,
            shared_with_manager = ?,
            favorite = ?
        WHERE id = ?
        """,
        (*ticket_fields, ticket_id),
    )
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
