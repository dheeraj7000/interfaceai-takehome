"""
Mock Legacy Bank Application — "Heritage Federal Credit Union"

An intentionally hostile web surface that simulates the kind of legacy
back-office banking application this system targets:

- Table-based layouts (no CSS grid/flexbox)
- No data-testid attributes
- Non-semantic markup (divs, spans, nested tables)
- Auto-generated/meaningless class names
- Iframes for navigation
- Server-rendered pages (no SPA)
- Minimal accessibility attributes (some aria, inconsistent)

Flows:
1. Member Lookup — search by ID → view account details
2. Account Detail — view balances, recent transactions
3. Fund Transfer — multi-field form → confirmation → result

Error injection via query params:
  ?inject_error=not_found        → member not found
  ?inject_error=timeout          → simulated session timeout dialog
  ?inject_error=permission       → permission denied
  ?inject_error=validation       → form validation error
  ?inject_error=server_error     → 500 error page
  ?inject_error=slow             → 5-second delay before response
"""

import time
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = "legacy-bank-dev-key-not-for-production"

# ---------------------------------------------------------------------------
# Fake data store
# ---------------------------------------------------------------------------
MEMBERS = {
    "M1001": {
        "id": "M1001",
        "name": "Jane A. Doe",
        "ssn_last4": "4532",
        "status": "Active",
        "since": "03/15/2011",
        "branch": "Downtown Main",
        "accounts": [
            {"number": "1001-SAV-001", "type": "Savings", "balance": "12,450.83"},
            {"number": "1001-CHK-001", "type": "Checking", "balance": "3,221.47"},
            {"number": "1001-CD-001", "type": "Certificate", "balance": "25,000.00"},
        ],
        "transactions": [
            {"date": "09/01/2026", "desc": "Payroll Deposit", "amount": "+2,150.00", "bal": "12,450.83"},
            {"date": "08/28/2026", "desc": "ATM Withdrawal", "amount": "-200.00", "bal": "10,300.83"},
            {"date": "08/25/2026", "desc": "Transfer from CHK", "amount": "+500.00", "bal": "10,500.83"},
            {"date": "08/20/2026", "desc": "Interest Credit", "amount": "+3.21", "bal": "10,000.83"},
        ],
    },
    "M1002": {
        "id": "M1002",
        "name": "Robert B. Smith",
        "ssn_last4": "7891",
        "status": "Active",
        "since": "07/22/2018",
        "branch": "Westside Plaza",
        "accounts": [
            {"number": "1002-SAV-001", "type": "Savings", "balance": "8,102.55"},
            {"number": "1002-CHK-001", "type": "Checking", "balance": "1,445.30"},
        ],
        "transactions": [
            {"date": "09/02/2026", "desc": "Mobile Deposit", "amount": "+850.00", "bal": "8,102.55"},
            {"date": "08/30/2026", "desc": "Bill Payment - Electric", "amount": "-142.50", "bal": "7,252.55"},
        ],
    },
    "M1003": {
        "id": "M1003",
        "name": "Maria C. Garcia",
        "ssn_last4": "2468",
        "status": "Frozen",
        "since": "01/10/2015",
        "branch": "Downtown Main",
        "accounts": [
            {"number": "1003-SAV-001", "type": "Savings", "balance": "45,220.00"},
        ],
        "transactions": [],
    },
}


# ---------------------------------------------------------------------------
# Error injection helper
# ---------------------------------------------------------------------------
def check_error_injection():
    """Check if an error should be injected via query param."""
    err = request.args.get("inject_error", "")
    if err == "slow":
        time.sleep(5)
    return err


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Main frame page — uses frameset to be extra legacy."""
    return render_template("index.html")


@app.route("/nav")
def nav():
    """Navigation frame content."""
    return render_template("nav.html")


@app.route("/main")
def main_content():
    """Main content frame — default landing."""
    return render_template("main.html")


@app.route("/member/search", methods=["GET", "POST"])
def member_search():
    """Member lookup — the primary flow entry point."""
    err = check_error_injection()

    if err == "server_error":
        return render_template("error_500.html"), 500

    if err == "timeout":
        return render_template("session_timeout.html")

    if err == "permission":
        return render_template("permission_denied.html")

    error_msg = None
    if request.method == "POST":
        member_id = request.form.get("memberid", "").strip().upper()

        if err == "validation":
            error_msg = "VALIDATION ERROR: Member ID format invalid. Expected: M followed by 4+ digits."
        elif not member_id:
            error_msg = "VALIDATION ERROR: Member ID is required."
        elif member_id not in MEMBERS:
            error_msg = f"NO RESULTS: Member '{member_id}' not found in system."
        else:
            session["last_member"] = member_id
            return redirect(url_for("member_detail", member_id=member_id))

    return render_template("member_search.html", error_msg=error_msg)


@app.route("/member/<member_id>")
def member_detail(member_id):
    """Member detail page — shows accounts and balances."""
    err = check_error_injection()

    if err == "timeout":
        return render_template("session_timeout.html")

    member = MEMBERS.get(member_id.upper())
    if not member:
        return render_template("member_search.html",
                               error_msg=f"NO RESULTS: Member '{member_id}' not found in system.")

    return render_template("member_detail.html", member=member)


@app.route("/transfer", methods=["GET", "POST"])
def transfer():
    """Fund transfer form — multi-field, requires confirmation."""
    err = check_error_injection()

    if err == "timeout":
        return render_template("session_timeout.html")

    if err == "permission":
        return render_template("permission_denied.html")

    member_id = request.args.get("member_id", session.get("last_member", ""))
    member = MEMBERS.get(member_id) if member_id else None

    if request.method == "POST":
        from_acct = request.form.get("from_account", "")
        to_acct = request.form.get("to_account", "")
        amount = request.form.get("amount", "")

        if err == "validation":
            return render_template("transfer.html", member=member,
                                   error_msg="VALIDATION ERROR: Invalid transfer amount.",
                                   from_acct=from_acct, to_acct=to_acct, amount=amount)

        # Basic validation
        if not all([from_acct, to_acct, amount]):
            return render_template("transfer.html", member=member,
                                   error_msg="VALIDATION ERROR: All fields are required.",
                                   from_acct=from_acct, to_acct=to_acct, amount=amount)

        if from_acct == to_acct:
            return render_template("transfer.html", member=member,
                                   error_msg="VALIDATION ERROR: Source and destination accounts must differ.",
                                   from_acct=from_acct, to_acct=to_acct, amount=amount)

        try:
            amt = float(amount.replace(",", "").replace("$", ""))
            if amt <= 0:
                raise ValueError()
        except ValueError:
            return render_template("transfer.html", member=member,
                                   error_msg="VALIDATION ERROR: Amount must be a positive number.",
                                   from_acct=from_acct, to_acct=to_acct, amount=amount)

        # Show confirmation page
        return render_template("transfer_confirm.html",
                               member=member, from_acct=from_acct,
                               to_acct=to_acct, amount=f"${amt:,.2f}")

    return render_template("transfer.html", member=member,
                           error_msg=None, from_acct="", to_acct="", amount="")


@app.route("/transfer/execute", methods=["POST"])
def transfer_execute():
    """Execute the transfer (simulated) — shows result page."""
    err = check_error_injection()

    if err == "timeout":
        return render_template("session_timeout.html")

    from_acct = request.form.get("from_account", "")
    to_acct = request.form.get("to_account", "")
    amount = request.form.get("amount", "")
    member_id = request.form.get("member_id", "")

    return render_template("transfer_result.html",
                           from_acct=from_acct, to_acct=to_acct,
                           amount=amount, member_id=member_id,
                           ref_number="TXN-2026-0908-00142")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
