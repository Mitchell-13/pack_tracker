from __future__ import annotations

import sqlite3
from datetime import datetime
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


@app.route("/", methods=["GET"])
def index() -> str:
    sort_by = request.args.get("sort_by", "date")
    order = request.args.get("order", "desc")

    allowed_sort = {"date": "t.date", "category": "c.name"}
    sort_column = allowed_sort.get(sort_by, "t.date")
    sort_order = "ASC" if order == "asc" else "DESC"

    db = get_db()
    tickets = db.execute(
        f"""
        SELECT t.id, t.link, c.name AS category, t.description, t.date,
               t.shared_with_manager, t.favorite
        FROM tickets t
        JOIN categories c ON t.category_id = c.id
        ORDER BY {sort_column} {sort_order}, t.id DESC
        """
    ).fetchall()

    categories = db.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()

    return render_template(
        "index.html",
        tickets=tickets,
        categories=categories,
        sort_by=sort_by,
        order=order,
    )


@app.route("/tickets", methods=["POST"])
def add_ticket() -> Any:
    link = request.form.get("link", "").strip()
    category_id = request.form.get("category_id", "").strip()
    description = request.form.get("description", "").strip()
    date_value = request.form.get("date", "").strip()
    shared_with_manager = 1 if request.form.get("shared_with_manager") == "on" else 0
    favorite = 1 if request.form.get("favorite") == "on" else 0

    if not link or not category_id or not description or not date_value:
        return redirect(url_for("index"))

    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("index"))

    db = get_db()
    db.execute(
        """
        INSERT INTO tickets (link, category_id, description, date, shared_with_manager, favorite)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (link, category_id, description, date_value, shared_with_manager, favorite),
    )
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
