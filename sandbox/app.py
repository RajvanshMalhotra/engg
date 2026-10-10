"""Kestrel Labs sandbox: a small but real company environment for the operator to work in.

Systems:
  /mail      — AP team mailbox (UI only)
  /erp       — accounting ERP: vendors, bills, payments (UI for writes, token-auth read API at /erp/api)
  /__admin   — test harness only (reset, fault injection, state dump); the agent is blocked from it

Fault injection (comma list via POST /__admin/faults):
  flaky_submit    first create-bill / create-vendor POST fails with a 500
  layout_b        ERP relabels nav + form fields (UI drift)
  toasts          decoy notification toasts on every page (unrelated UI change)
  session_expiry  ERP session expires once, mid-task
  iframe_payments payment form lives inside an embedded frame (common for payment widgets)
  shadow_forms    new-bill form lives inside a web component's shadow DOM
  confirm_dialogs browser "are you sure?" pop-up before scheduling a payment
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sandbox import db, seed

ERP_USER = os.getenv("ERP_USER", "ap.operator")
ERP_PASSWORD = os.getenv("ERP_PASSWORD", "kestrel-sandbox-pass")
ERP_API_TOKEN = os.getenv("ERP_API_TOKEN", "sandbox-readonly-token")
SESSION_TOKEN = hashlib.sha256(f"{ERP_USER}:{ERP_PASSWORD}".encode()).hexdigest()[:24]

app = FastAPI(title="Kestrel Labs sandbox")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

TOASTS = ["3 new notifications", "Sync complete", "Reminder: quarterly close on Oct 31",
          "New comment on BILL-1003", "Your export is ready"]


@app.on_event("startup")
def _ensure_seeded() -> None:
    if not db.DB_PATH.exists():
        seed.reset()


def faults() -> set[str]:
    return {f for f in (db.kv_get("faults") or "").split(",") if f}


def audit(actor: str, action: str, detail: str = "") -> None:
    db.execute("INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
               datetime.now().isoformat(timespec="seconds"), actor, action, detail)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    active = faults()
    toast = None
    if "toasts" in active:
        n = int(db.kv_get("toast_n", "0")) + 1
        db.kv_set("toast_n", str(n))
        toast = TOASTS[n % len(TOASTS)]
    ctx.update(request=request, layout_b="layout_b" in active, toast=toast,
               iframe_payments="iframe_payments" in active, confirm_dialogs="confirm_dialogs" in active,
               shadow_forms="shadow_forms" in active,
               msg=request.query_params.get("msg"), err=ctx.get("err"))
    return templates.TemplateResponse(request, name, ctx, status_code=ctx.pop("status", 200))


# ----------------------------------------------------------------------------- mail

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html")


@app.get("/mail", response_class=HTMLResponse)
def mail_list(request: Request, folder: str = "inbox", q: str = ""):
    sql = "SELECT e.*, (SELECT COUNT(*) FROM attachments a WHERE a.email_id = e.id) AS n_att FROM emails e WHERE folder = ?"
    params: list = [folder]
    if q:
        sql += " AND (subject LIKE ? OR body LIKE ? OR sender LIKE ? OR sender_name LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY received_at DESC"
    return render(request, "mail_list.html", emails=db.rows(sql, *params), folder=folder, q=q)


@app.get("/mail/m/{email_id}", response_class=HTMLResponse)
def mail_view(request: Request, email_id: int):
    email = db.row("SELECT * FROM emails WHERE id = ?", email_id)
    if not email:
        raise HTTPException(404, "No such message")
    db.execute("UPDATE emails SET unread = 0 WHERE id = ?", email_id)
    atts = db.rows("SELECT * FROM attachments WHERE email_id = ?", email_id)
    return render(request, "mail_view.html", email=email, attachments=atts)


@app.get("/mail/attachments/{att_id}/{filename}")
def mail_attachment(att_id: int, filename: str):
    att = db.row("SELECT * FROM attachments WHERE id = ? AND filename = ?", att_id, filename)
    if not att:
        raise HTTPException(404, "No such attachment")
    path = db.ATTACH_DIR / str(att_id) / filename
    return FileResponse(path, media_type=att["content_type"], filename=filename,
                        content_disposition_type="attachment")


@app.get("/mail/compose", response_class=HTMLResponse)
def mail_compose(request: Request, reply_to: int | None = None):
    to = subject = ""
    if reply_to:
        orig = db.row("SELECT * FROM emails WHERE id = ?", reply_to)
        if orig:
            to = orig["sender"]
            subject = orig["subject"] if orig["subject"].startswith("Re:") else f"Re: {orig['subject']}"
    return render(request, "mail_compose.html", to=to, subject=subject)


@app.post("/mail/send")
def mail_send(to: str = Form(...), subject: str = Form(...), body: str = Form(...)):
    if not to.strip() or "@" not in to:
        return RedirectResponse("/mail/compose?msg=Invalid+recipient", status_code=303)
    db.execute("INSERT INTO emails (folder, sender, sender_name, recipients, subject, body, received_at, unread)"
               " VALUES ('sent', ?, 'Kestrel AP', ?, ?, ?, ?, 0)",
               seed.AP_INBOX, to.strip(), subject, body, datetime.now().isoformat(timespec="seconds"))
    audit("mail", "send", f"to={to} subject={subject}")
    return RedirectResponse("/mail?folder=sent&msg=Message+sent", status_code=303)


# ----------------------------------------------------------------------------- erp auth

def erp_authed(request: Request) -> bool:
    if request.cookies.get("erp_session") != SESSION_TOKEN:
        return False
    if "session_expiry" in faults():
        n = int(db.kv_get("erp_hits", "0")) + 1
        db.kv_set("erp_hits", str(n))
        if n == 4 and db.kv_get("expired_once") != "1":
            db.kv_set("expired_once", "1")
            return False
    return True


def login_redirect(request: Request) -> RedirectResponse:
    resp = RedirectResponse(f"/erp/login?next={request.url.path}&msg=Session+expired+or+not+signed+in", 303)
    resp.delete_cookie("erp_session")
    return resp


@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_page(request: Request, next: str = "/erp"):
    return render(request, "erp_login.html", next=next)


@app.post("/erp/login")
def erp_login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/erp")):
    if username != ERP_USER or password != ERP_PASSWORD:
        return render(request, "erp_login.html", next=next, err="Invalid username or password", status=401)
    resp = RedirectResponse(next if next.startswith("/erp") else "/erp", status_code=303)
    resp.set_cookie("erp_session", SESSION_TOKEN, httponly=True)
    audit(username, "login")
    return resp


# ----------------------------------------------------------------------------- erp pages

@app.get("/erp", response_class=HTMLResponse)
def erp_home(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    stats = db.row("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status != 'Paid' THEN amount END), 0) AS open_amt FROM bills")
    return render(request, "erp_home.html", stats=stats,
                  pending=db.rows("SELECT * FROM vendors WHERE status != 'Approved'"))


@app.get("/erp/bills", response_class=HTMLResponse)
def erp_bills(request: Request, q: str = "", vendor_id: str = ""):
    if not erp_authed(request):
        return login_redirect(request)
    sql = "SELECT b.*, v.name AS vendor_name FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE 1=1"
    params: list = []
    if q:
        sql += " AND (b.invoice_number LIKE ? OR b.id LIKE ? OR b.description LIKE ?)"
        params += [f"%{q}%"] * 3
    if vendor_id:
        sql += " AND b.vendor_id = ?"
        params.append(vendor_id)
    bills = db.rows(sql + " ORDER BY b.id DESC", *params)
    return render(request, "erp_bills.html", bills=bills, q=q, vendor_id=vendor_id,
                  vendors=db.rows("SELECT * FROM vendors ORDER BY name"))


@app.get("/erp/bills/new", response_class=HTMLResponse)
def erp_bill_new_page(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    return render(request, "erp_bill_new.html", vendors=db.rows("SELECT * FROM vendors ORDER BY name"), form={})


def _first_attempt(key: str) -> bool:
    """For flaky_submit: True exactly once per key."""
    if "flaky_submit" not in faults() or db.kv_get(f"flaked:{key}") == "1":
        return False
    db.kv_set(f"flaked:{key}", "1")
    return True


@app.post("/erp/bills/new")
async def erp_bill_create(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    form = dict(await request.form())
    # layout_b uses different field names — both are accepted by the server.
    vendor_id = form.get("vendor_id") or form.get("supplier")
    number = (form.get("invoice_number") or form.get("supplier_ref") or "").strip()
    inv_date = form.get("invoice_date") or form.get("doc_date")
    due = form.get("due_date") or form.get("pay_by")
    amount_raw = (form.get("amount") or form.get("total_payable") or "").replace(",", "").replace("₹", "").strip()
    desc = form.get("description") or form.get("memo") or ""
    vendors = db.rows("SELECT * FROM vendors ORDER BY name")
    errors = []
    vendor = db.row("SELECT * FROM vendors WHERE id = ?", vendor_id or "")
    if not vendor:
        errors.append("Select a vendor")
    elif vendor["status"] != "Approved":
        errors.append(f"Vendor {vendor['name']} is not approved; bills cannot be recorded yet")
    if not number:
        errors.append("Invoice number is required")
    try:
        amount = round(float(amount_raw), 2)
        if amount <= 0:
            raise ValueError
    except ValueError:
        amount = None
        errors.append("Amount must be a positive number")
    for label, val in (("Invoice date", inv_date), ("Due date", due)):
        try:
            datetime.strptime(val or "", "%Y-%m-%d")
        except ValueError:
            errors.append(f"{label} must be a valid date (YYYY-MM-DD)")
    if errors:
        return render(request, "erp_bill_new.html", vendors=vendors, form=form, err="; ".join(errors), status=422)
    if _first_attempt("bill"):
        return render(request, "erp_error.html", status=500,
                      err="502 Upstream ledger service timed out. Your bill was NOT saved. Please try again.")
    seq = int(db.kv_get("bill_seq", "1003")) + 1
    db.kv_set("bill_seq", str(seq))
    bill_id = f"BILL-{seq}"
    db.execute("INSERT INTO bills (id, vendor_id, invoice_number, invoice_date, due_date, amount, description, status,"
               " created_by, created_at) VALUES (?,?,?,?,?,?,?, 'Recorded', ?, ?)",
               bill_id, vendor_id, number, inv_date, due, amount, desc, ERP_USER,
               datetime.now().isoformat(timespec="seconds"))
    audit(ERP_USER, "bill.create", f"{bill_id} {vendor_id} {number} {amount}")
    return RedirectResponse(f"/erp/bills/{bill_id}?msg=Bill+{bill_id}+recorded", status_code=303)


@app.get("/erp/bills/{bill_id}", response_class=HTMLResponse)
def erp_bill_view(request: Request, bill_id: str):
    if not erp_authed(request):
        return login_redirect(request)
    bill = db.row("SELECT b.*, v.name AS vendor_name, v.bank_account, v.ifsc FROM bills b"
                  " JOIN vendors v ON v.id = b.vendor_id WHERE b.id = ?", bill_id)
    if not bill:
        raise HTTPException(404, "No such bill")
    return render(request, "erp_bill_view.html", bill=bill)


@app.get("/erp/bills/{bill_id}/pay", response_class=HTMLResponse)
def erp_pay_frame(request: Request, bill_id: str):
    """Embedded payment widget (iframe_payments fault)."""
    if not erp_authed(request):
        return login_redirect(request)
    bill = db.row("SELECT * FROM bills WHERE id = ?", bill_id)
    if not bill:
        raise HTTPException(404, "No such bill")
    return render(request, "erp_pay_frame.html", bill=bill)


@app.post("/erp/bills/{bill_id}/schedule-payment")
def erp_schedule_payment(request: Request, bill_id: str, payment_date: str = Form(...)):
    if not erp_authed(request):
        return login_redirect(request)
    bill = db.row("SELECT * FROM bills WHERE id = ?", bill_id)
    if not bill:
        raise HTTPException(404, "No such bill")
    if bill["status"] != "Recorded":
        return RedirectResponse(f"/erp/bills/{bill_id}?msg=Bill+is+already+{bill['status'].replace(' ', '+')}", 303)
    try:
        datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return RedirectResponse(f"/erp/bills/{bill_id}?msg=Invalid+payment+date", 303)
    db.execute("UPDATE bills SET status = 'Payment scheduled', payment_date = ? WHERE id = ?", payment_date, bill_id)
    audit(ERP_USER, "bill.schedule_payment", f"{bill_id} {payment_date}")
    return RedirectResponse(f"/erp/bills/{bill_id}?msg=Payment+scheduled+for+{payment_date}", status_code=303)


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    return render(request, "erp_vendors.html", vendors=db.rows("SELECT * FROM vendors ORDER BY id"))


@app.get("/erp/vendors/new", response_class=HTMLResponse)
def erp_vendor_new_page(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    return render(request, "erp_vendor_new.html", form={})


@app.post("/erp/vendors/new")
async def erp_vendor_create(request: Request):
    if not erp_authed(request):
        return login_redirect(request)
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    required = ["name", "contact_email", "email_domain", "gstin", "bank_account", "ifsc"]
    missing = [k for k in required if not form.get(k)]
    if missing:
        return render(request, "erp_vendor_new.html", form=form, status=422,
                      err="Missing required fields: " + ", ".join(missing))
    if db.row("SELECT id FROM vendors WHERE lower(name) = lower(?) OR gstin = ?", form["name"], form["gstin"]):
        return render(request, "erp_vendor_new.html", form=form, status=409,
                      err="A vendor with this name or GSTIN already exists")
    if _first_attempt("vendor"):
        return render(request, "erp_error.html", status=500,
                      err="503 Vendor master is temporarily unavailable. The vendor was NOT created. Please retry.")
    seq = int(db.kv_get("vendor_seq", "3")) + 1
    db.kv_set("vendor_seq", str(seq))
    vid = f"V{seq:03d}"
    db.execute("INSERT INTO vendors VALUES (?,?,?,?,?,?,?, 'Pending verification')",
               vid, form["name"], form["email_domain"], form["contact_email"], form["gstin"],
               form["bank_account"], form["ifsc"])
    audit(ERP_USER, "vendor.create", f"{vid} {form['name']}")
    return RedirectResponse(f"/erp/vendors/{vid}?msg=Vendor+{vid}+created+(pending+verification)", status_code=303)


@app.get("/erp/vendors/{vendor_id}", response_class=HTMLResponse)
def erp_vendor_view(request: Request, vendor_id: str):
    if not erp_authed(request):
        return login_redirect(request)
    vendor = db.row("SELECT * FROM vendors WHERE id = ?", vendor_id)
    if not vendor:
        raise HTTPException(404, "No such vendor")
    bills = db.rows("SELECT * FROM bills WHERE vendor_id = ? ORDER BY id DESC", vendor_id)
    return render(request, "erp_vendor_view.html", vendor=vendor, bills=bills)


@app.post("/erp/vendors/{vendor_id}/bank")
def erp_vendor_bank(request: Request, vendor_id: str, bank_account: str = Form(...), ifsc: str = Form(...)):
    if not erp_authed(request):
        return login_redirect(request)
    db.execute("UPDATE vendors SET bank_account = ?, ifsc = ? WHERE id = ?", bank_account, ifsc, vendor_id)
    audit(ERP_USER, "vendor.bank_change", f"{vendor_id} {bank_account} {ifsc}")
    return RedirectResponse(f"/erp/vendors/{vendor_id}?msg=Bank+details+updated", status_code=303)


@app.post("/erp/vendors/{vendor_id}/approve")
def erp_vendor_approve(request: Request, vendor_id: str):
    if not erp_authed(request):
        return login_redirect(request)
    db.execute("UPDATE vendors SET status = 'Approved' WHERE id = ?", vendor_id)
    audit(ERP_USER, "vendor.approve", vendor_id)
    return RedirectResponse(f"/erp/vendors/{vendor_id}?msg=Vendor+approved", status_code=303)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/erp/api", "/__admin")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(request, "erp_error.html", err=f"{exc.status_code}: {exc.detail}", status=exc.status_code)


# ----------------------------------------------------------------------------- erp read API

def _api_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {ERP_API_TOKEN}":
        raise HTTPException(401, "Missing or invalid API token")


@app.get("/erp/api/vendors")
def api_vendors(authorization: str | None = Header(None), q: str = ""):
    _api_auth(authorization)
    return db.rows("SELECT * FROM vendors"
                   " WHERE name LIKE ? ORDER BY id", f"%{q}%")


@app.get("/erp/api/vendors/{vendor_id}")
def api_vendor(vendor_id: str, authorization: str | None = Header(None)):
    _api_auth(authorization)
    vendor = db.row("SELECT * FROM vendors WHERE id = ?", vendor_id)
    if not vendor:
        raise HTTPException(404, "No such vendor")
    return vendor


@app.get("/erp/api/bills")
def api_bills(authorization: str | None = Header(None), vendor_id: str = "", invoice_number: str = "", status: str = ""):
    _api_auth(authorization)
    sql = "SELECT * FROM bills WHERE 1=1"
    params = []
    for col, val in (("vendor_id", vendor_id), ("invoice_number", invoice_number), ("status", status)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    return db.rows(sql + " ORDER BY id", *params)


@app.get("/erp/api/bills/{bill_id}")
def api_bill(bill_id: str, authorization: str | None = Header(None)):
    _api_auth(authorization)
    bill = db.row("SELECT * FROM bills WHERE id = ?", bill_id)
    if not bill:
        raise HTTPException(404, "No such bill")
    return bill


# ----------------------------------------------------------------------------- admin (eval harness only)

@app.post("/__admin/reset")
def admin_reset():
    seed.reset()
    return {"ok": True}


@app.post("/__admin/faults")
async def admin_faults(request: Request):
    body = await request.json()
    db.kv_set("faults", ",".join(body.get("faults", [])))
    return {"faults": sorted(faults())}


@app.get("/__admin/state")
def admin_state():
    return {
        "vendors": db.rows("SELECT * FROM vendors ORDER BY id"),
        "bills": db.rows("SELECT * FROM bills ORDER BY id"),
        "sent": db.rows("SELECT * FROM emails WHERE folder = 'sent' ORDER BY id"),
        "audit": db.rows("SELECT * FROM audit ORDER BY id"),
        "faults": sorted(faults()),
    }
