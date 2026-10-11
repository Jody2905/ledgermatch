"""LedgerMatch web interface (Flask).

Every page needs a login. Every form that changes something carries a CSRF
token. All the business rules (who may approve, reasons required, locking)
live in ReconciliationService; this file only turns web requests into
service calls and shows the results.
"""

import io
import os
import secrets
from datetime import date
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request, session, url_for)

from .db import connect
from .money import fmt, parse_amount
from .service import ImportProblem, NotAllowed, NotFound, ReconciliationService

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

CONTENT_SECURITY_POLICY = ("default-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
                           "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def create_app(db_path, secret_key=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or os.environ.get("LEDGERMATCH_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        DB_PATH=str(db_path),
    )
    app.jinja_env.filters["money"] = fmt
    app.jinja_env.globals["csrf_token"] = _csrf_token

    # ---- Per-request database connection and service ----

    def service():
        if "svc" not in g:
            g.svc = ReconciliationService(connect(app.config["DB_PATH"]))
        return g.svc

    @app.teardown_appcontext
    def close_db(_exc):
        svc = g.pop("svc", None)
        if svc is not None:
            svc.db.close()

    # ---- Security on every request ----

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        if "user_id" in session:
            g.user = service().db.execute("SELECT * FROM users WHERE id = ? AND active = 1",
                                          (session["user_id"],)).fetchone()
            if g.user is None:
                session.clear()
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not sent or not expected or not secrets.compare_digest(sent, expected):
                abort(400, "Invalid or missing form token. Please go back, reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if g.get("user"):
            response.headers["Cache-Control"] = "no-store"   # financial pages are never cached
        return response

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    # ---- Login ----

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = service().authenticate(request.form.get("username"), request.form.get("password"))
            if user is None:
                flash("Invalid username or password.", "error")
                return render_template("login.html"), 401
            session.clear()                     # a fresh session at login
            session["user_id"] = user["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You have been logged out.", "ok")
        return redirect(url_for("login"))

    # ---- Reconciliations ----

    @app.get("/")
    @login_required
    def index():
        return render_template("index.html", runs=service().runs())

    @app.route("/runs/new", methods=["GET", "POST"])
    @login_required
    def new_run():
        svc = service()
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                account_id = int(form.get("account_id", ""))
                period_end = date.fromisoformat(form.get("period_end", ""))
                bank_balance = parse_amount(form.get("bank_balance"))
                book_balance = parse_amount(form.get("book_balance"))
                bank_text = _uploaded_text("bank_file")
                ledger_text = _uploaded_text("ledger_file")
                run_id = svc.create_run(account_id, period_end, bank_text, ledger_text,
                                        bank_balance, book_balance, g.user["id"])
                flash("Reconciliation created. Automatic matching is done; review the items below.", "ok")
                return redirect(url_for("show_run", run_id=run_id))
            except ImportProblem as e:
                return render_template("new_run.html", accounts=svc.accounts(), form=form,
                                       import_errors=e.errors), 400
            except (ValueError, NotAllowed, NotFound) as e:
                flash(str(e), "error")
                return render_template("new_run.html", accounts=svc.accounts(), form=form), 400
        return render_template("new_run.html", accounts=svc.accounts(), form=form)

    @app.get("/runs/<int:run_id>")
    @login_required
    def show_run(run_id):
        svc = service()
        try:
            run = svc.run(run_id)
        except NotFound:
            abort(404)
        statement = svc.statement(run_id)
        bank = {r["line_no"]: r for r in svc.db.execute(
            "SELECT * FROM bank_lines WHERE run_id = ?", (run_id,))}
        ledger = {r["line_no"]: r for r in svc.db.execute(
            "SELECT * FROM ledger_entries WHERE run_id = ?", (run_id,))}
        user = g.user
        editable = run["status"] in ("draft", "rejected") and run["prepared_by"] == user["id"]
        return render_template(
            "run.html", run=run, s=statement, matches=svc.matches(run_id), bank=bank, ledger=ledger,
            errors_bank=[r for r in bank.values() if r["error_reason"]],
            errors_ledger=[r for r in ledger.values() if r["error_reason"]],
            audit=svc.audit_trail(run_id), editable=editable,
            can_submit=editable,
            can_review=(run["status"] == "submitted" and user["role"] == "approver"
                        and run["prepared_by"] != user["id"]))

    # ---- Actions: each one calls the service and reports the outcome ----

    def act(run_id, action, success):
        try:
            action()
            flash(success, "ok")
        except (NotAllowed, NotFound, ValueError) as e:
            flash(str(e), "error")
        return redirect(url_for("show_run", run_id=run_id))

    @app.post("/runs/<int:run_id>/match")
    @login_required
    def manual_match(run_id):
        bank = [int(x) for x in request.form.getlist("bank")]
        ledger = [int(x) for x in request.form.getlist("ledger")]
        return act(run_id, lambda: service().manual_match(run_id, g.user["id"], bank, ledger,
                                                          request.form.get("reason")), "Items matched.")

    @app.post("/runs/<int:run_id>/unmatch/<int:match_id>")
    @login_required
    def unmatch(run_id, match_id):
        return act(run_id, lambda: service().unmatch(run_id, g.user["id"], match_id, request.form.get("reason")),
                   "Match removed; the items are unmatched again.")

    @app.post("/runs/<int:run_id>/error")
    @login_required
    def mark_error(run_id):
        side, line_no = _item(request.form.get("item", ""))
        return act(run_id, lambda: service().mark_error(run_id, g.user["id"], side, line_no,
                                                        request.form.get("reason")), "Marked as an error.")

    @app.post("/runs/<int:run_id>/clear-error")
    @login_required
    def clear_error(run_id):
        side, line_no = request.form.get("side"), int(request.form.get("line_no", "0"))
        return act(run_id, lambda: service().clear_error(run_id, g.user["id"], side, line_no), "Error mark removed.")

    @app.post("/runs/<int:run_id>/dismiss/<int:issue_id>")
    @login_required
    def dismiss(run_id, issue_id):
        return act(run_id, lambda: service().dismiss_issue(run_id, g.user["id"], issue_id,
                                                           request.form.get("reason")), "Issue dismissed.")

    @app.post("/runs/<int:run_id>/submit")
    @login_required
    def submit(run_id):
        return act(run_id, lambda: service().submit(run_id, g.user["id"]), "Submitted for approval.")

    @app.post("/runs/<int:run_id>/approve")
    @login_required
    def approve(run_id):
        return act(run_id, lambda: service().approve(run_id, g.user["id"]), "Reconciliation approved and locked.")

    @app.post("/runs/<int:run_id>/reject")
    @login_required
    def reject(run_id):
        return act(run_id, lambda: service().reject(run_id, g.user["id"], request.form.get("reason")),
                   "Sent back to the preparer.")

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(e):
        messages = {404: "That page doesn't exist.", 413: "That file is too large (5 MB maximum)."}
        message = messages.get(e.code, e.description)
        return render_template("error.html", code=e.code, message=message), e.code

    return app


def _item(value):
    """Form value 'ledger:12' -> ('ledger', 12)."""
    side, _, line = value.partition(":")
    if side not in ("bank", "ledger") or not line.isdigit():
        abort(400, "Choose an item first.")
    return side, int(line)


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def _uploaded_text(field):
    f = request.files.get(field)
    if f is None or not f.filename:
        raise ValueError("Please choose both files")
    if not f.filename.lower().endswith(".csv"):
        raise ValueError("Files must be .csv")
    try:
        return io.StringIO(f.read().decode("utf-8-sig"), newline="")
    except UnicodeDecodeError:
        raise ValueError(f"{f.filename} isn't a readable text (UTF-8) CSV file") from None
