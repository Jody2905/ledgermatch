"""Web interface tests: the whole month done through the browser forms."""

import re

import pytest

from ledgermatch import sample_data
from ledgermatch.db import connect, init_db
from ledgermatch.service import ReconciliationService
from ledgermatch.web import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture
def app(tmp_path):
    db = tmp_path / "web.db"
    svc = ReconciliationService(init_db(db))
    svc.create_user("kbaptiste", "Kerry Baptiste", "preparer", PASSWORD)
    svc.create_user("tlewis", "Tara Lewis", "preparer", PASSWORD)
    svc.create_user("mjames", "Marcus James", "approver", PASSWORD)
    svc.create_account("Consolidated Fund - Main Account", "Eastern Caribbean Bank", "4821")
    svc.db.close()
    app = create_app(db, secret_key="test-secret")
    app.config["TESTING"] = True
    app.config["FILES"] = tmp_path / "files"
    app.config["BALANCES"] = sample_data.write(tmp_path / "files")
    return app


def token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def login(client, username):
    return client.post("/login", data={"username": username, "password": PASSWORD,
                                       "csrf_token": token(client)})


def upload(client, app, bank_name="bank_statement_oct2026.csv", bank_bytes=None, **overrides):
    files = app.config["FILES"]
    bank_bal, book_bal = app.config["BALANCES"]
    data = {"csrf_token": token(client, "/runs/new"), "account_id": "1", "period_end": "2026-10-31",
            "bank_balance": str(bank_bal), "book_balance": str(book_bal),
            "bank_file": (open(files / "bank_statement_oct2026.csv", "rb") if bank_bytes is None
                          else __import__("io").BytesIO(bank_bytes), bank_name),
            "ledger_file": (open(files / "cash_book_oct2026.csv", "rb"), "cash_book_oct2026.csv")}
    data.update(overrides)
    return client.post("/runs/new", data=data, content_type="multipart/form-data")


def post(client, path, **data):
    data["csrf_token"] = token(client, "/")
    return client.post(path, data=data, follow_redirects=True)


def open_lines(app, run_id):
    """Line numbers of the two problem items in the demo month, read from the database."""
    svc = ReconciliationService(connect(app.config["DB_PATH"]))
    s = svc.statement(run_id)
    telecom = [e for e in s.investigate_ledger if e.payee == "Eastern Telecom"]
    dup = telecom[-1]
    rest = [e for e in s.investigate_ledger if e is not dup and e.payee != "Eastern Telecom"]
    bank_line = s.investigate_bank[0]
    svc.db.close()
    return dup.line_no, bank_line.line_no, rest[0].line_no


@pytest.fixture
def preparer(app):
    c = app.test_client()
    login(c, "kbaptiste")
    return c


@pytest.fixture
def run_id(app, preparer):
    r = upload(preparer, app)
    assert r.status_code == 302
    return int(r.headers["Location"].rsplit("/", 1)[1])


def resolve(client, app, run_id):
    dup, bank_line, ledger_line = open_lines(app, run_id)
    post(client, f"/runs/{run_id}/error", item=f"ledger:{dup}",
         reason="Voucher keyed twice; reversal journal JV-2611")
    return post(client, f"/runs/{run_id}/match", bank=str(bank_line), ledger=str(ledger_line),
                reason="Cheque was 1,520.00; books keyed 1,250.00. JV-2612")


# ---- Login and security --------------------------------------------------------

def test_pages_need_login(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.get("/runs/1").status_code == 302


def test_wrong_password_is_refused(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "kbaptiste", "password": "nope-nope-nope",
                               "csrf_token": token(c)})
    assert r.status_code == 401
    assert "Invalid username or password" in r.get_data(as_text=True)


def test_login_then_logout(app, preparer):
    assert "Kerry Baptiste" in preparer.get("/").get_data(as_text=True)
    post(preparer, "/logout")
    assert preparer.get("/").status_code == 302


def test_post_without_csrf_token_is_rejected(app, preparer, run_id):
    r = preparer.post(f"/runs/{run_id}/submit")
    assert r.status_code == 400
    r = preparer.post(f"/runs/{run_id}/submit", data={"csrf_token": "wrong"})
    assert r.status_code == 400


def test_login_form_also_needs_csrf(app):
    c = app.test_client()
    c.get("/login")
    r = c.post("/login", data={"username": "kbaptiste", "password": PASSWORD})
    assert r.status_code == 400


def test_open_redirect_after_login_is_blocked(app):
    c = app.test_client()
    t = token(c)
    r = c.post("/login?next=//evil.example", data={"username": "kbaptiste", "password": PASSWORD,
                                                    "csrf_token": t})
    assert r.headers["Location"] == "/"


def test_security_headers(app, preparer):
    r = preparer.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"


def test_missing_run_is_404(preparer):
    r = preparer.get("/runs/999")
    assert r.status_code == 404
    assert "doesn&#39;t exist" in r.get_data(as_text=True)


# ---- Uploading --------------------------------------------------------------------

def test_upload_matches_and_shows_the_statement(app, preparer, run_id):
    html = preparer.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Bank reconciliation statement as at 31 October 2026" in html
    assert "Difference: <strong>0.00</strong>" in html
    assert "Needs investigation (2)" in html
    assert "Draft" in html


def test_bad_row_rejects_the_whole_upload(app, preparer):
    bad = b"Date,Description,Reference,Amount\n01/10/2026,Deposit,,100.00\n02/10/2026,Fee,,12.345\n"
    r = upload(preparer, app, bank_bytes=bad)
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "nothing was saved" in html and "row 3" in html
    assert "No reconciliations yet" in preparer.get("/").get_data(as_text=True)


def test_non_csv_and_non_utf8_files_are_refused(app, preparer):
    r = upload(preparer, app, bank_name="statement.xlsx")
    assert r.status_code == 400 and ".csv" in r.get_data(as_text=True)
    r = upload(preparer, app, bank_name="s.csv", bank_bytes=b"\xff\xfe\x00bad")
    assert r.status_code == 400 and "UTF-8" in r.get_data(as_text=True)


def test_bad_balance_keeps_what_was_typed(app, preparer):
    r = upload(preparer, app, bank_balance="12,34x", period_end="2026-10-31")
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert 'value="2026-10-31"' in html


def test_too_large_upload_is_refused(app, preparer):
    app.config["MAX_CONTENT_LENGTH"] = 1000
    r = upload(preparer, app)
    assert r.status_code == 413
    assert "too large" in r.get_data(as_text=True)


# ---- Working the reconciliation ------------------------------------------------------

def test_cannot_submit_with_open_issues(app, preparer, run_id):
    html = post(preparer, f"/runs/{run_id}/submit").get_data(as_text=True)
    assert "flash-error" in html
    assert "Draft" in html


def test_resolve_submit_and_approve(app, preparer, run_id):
    html = resolve(preparer, app, run_id).get_data(as_text=True)
    assert "Items matched." in html
    assert "Ready to submit" in html
    assert "Needs investigation" not in html

    html = post(preparer, f"/runs/{run_id}/submit").get_data(as_text=True)
    assert "Submitted for approval." in html

    checker = app.test_client()
    login(checker, "mjames")
    html = checker.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Waiting for your review" in html
    html = post(checker, f"/runs/{run_id}/approve").get_data(as_text=True)
    assert "approved and locked" in html
    assert "approved by Marcus James" in html
    assert "Match selected" not in html          # locked: no editing forms
    assert "Mark as error" not in html


def test_preparer_cannot_approve_own_work(app, preparer, run_id):
    resolve(preparer, app, run_id)
    post(preparer, f"/runs/{run_id}/submit")
    html = preparer.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Waiting for your review" not in html
    html = post(preparer, f"/runs/{run_id}/approve").get_data(as_text=True)
    assert "flash-error" in html
    assert "Submitted" in html


def test_reject_sends_it_back_with_the_reason(app, preparer, run_id):
    resolve(preparer, app, run_id)
    post(preparer, f"/runs/{run_id}/submit")
    checker = app.test_client()
    login(checker, "mjames")
    html = post(checker, f"/runs/{run_id}/reject", reason="Attach the bank's copy of the cheque").get_data(as_text=True)
    assert "Sent back to the preparer." in html
    html = preparer.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Attach the bank&#39;s copy of the cheque" in html
    assert "Submit for approval" in html


def test_another_preparer_cannot_change_the_run(app, preparer, run_id):
    other = app.test_client()
    login(other, "tlewis")
    dup, _, _ = open_lines(app, run_id)
    html = post(other, f"/runs/{run_id}/error", item=f"ledger:{dup}",
                reason="Trying to change someone else's work").get_data(as_text=True)
    assert "flash-error" in html
    assert "Needs investigation (2)" in html


def test_reason_is_required(app, preparer, run_id):
    dup, _, _ = open_lines(app, run_id)
    html = post(preparer, f"/runs/{run_id}/error", item=f"ledger:{dup}", reason="").get_data(as_text=True)
    assert "flash-error" in html


def test_mark_error_needs_a_chosen_item(app, preparer, run_id):
    r = post(preparer, f"/runs/{run_id}/error", item="", reason="No item chosen here")
    assert r.status_code == 400


def test_undo_error_and_unmatch(app, preparer, run_id):
    resolve(preparer, app, run_id)
    dup, _, _ = open_lines_after_error(app, run_id)
    html = post(preparer, f"/runs/{run_id}/clear-error", side="ledger", line_no=str(dup)).get_data(as_text=True)
    assert "Error mark removed." in html
    match_id = re.search(rf'/runs/{run_id}/unmatch/(\d+)', html).group(1)
    html = post(preparer, f"/runs/{run_id}/unmatch/{match_id}", reason="Matched the wrong items").get_data(as_text=True)
    assert "Match removed" in html


def open_lines_after_error(app, run_id):
    conn = connect(app.config["DB_PATH"])
    line = conn.execute("SELECT line_no FROM ledger_entries WHERE run_id = ? AND error_reason IS NOT NULL",
                        (run_id,)).fetchone()["line_no"]
    conn.close()
    return line, None, None


def test_audit_trail_records_who_did_what(app, preparer, run_id):
    resolve(preparer, app, run_id)
    html = preparer.get(f"/runs/{run_id}").get_data(as_text=True)
    audit = html.split("Audit trail", 1)[1]
    assert "Kerry Baptiste" in audit
    assert "JV-2611" in audit and "JV-2612" in audit


def test_reasons_are_escaped_not_run_as_html(app, preparer, run_id):
    dup, _, _ = open_lines(app, run_id)
    post(preparer, f"/runs/{run_id}/error", item=f"ledger:{dup}", reason="<script>alert(1)</script>")
    html = preparer.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
