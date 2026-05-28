"""
app.py — Poultry Record Digitizer
Complete Flask application with SQLite persistence.
"""

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_file, flash, jsonify, g
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os, io, json, time
from functools import wraps

# ── Local modules ──────────────────────────────────────────────
from ocr_engine     import smart_ocr
from data_extractor import extract_structured_data
from database       import (
    init_db, get_db, close_db,
    create_user, get_user_by_name, get_user_by_id,
    save_record, update_record, get_record,
    get_user_records, get_record_stats,
    log_download, get_user_downloads,
)
from export_utils   import generate_excel, generate_pdf
from quality_check  import check_image_quality

# ── App setup ──────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key       = "poultry-digitizer-2025-secret"
app.config["UPLOAD_FOLDER"]      = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
ALLOWED_EXT = {"png", "jpg", "jpeg", "bmp", "tiff", "webp"}

os.makedirs("uploads", exist_ok=True)

# Initialise DB and register teardown
init_db(app)
app.teardown_appcontext(close_db)


# ── Helpers ────────────────────────────────────────────────────
def allowed(filename: str) -> bool:
    return "." in filename and \
           filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return dec


def current_user():
    """Return user row for the logged-in user, or None."""
    uid = session.get("user_id")
    if uid:
        return get_user_by_id(get_db(), uid)
    return None


def get_session_result() -> dict | None:
    """Load latest extracted data from session."""
    raw = session.get("last_result")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_session_result(data: dict):
    """Save extracted data to session (JSON-safe fields only)."""
    safe = {k: v for k, v in data.items()
            if isinstance(v, (str, int, float, list, type(None), bool))}
    session["last_result"]   = json.dumps(safe)
    session["last_record_id"] = data.get("_record_id")


# ══════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session
                    else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u  = request.form.get("username", "").strip()
        p  = request.form.get("password", "")
        db = get_db()
        row = get_user_by_name(db, u)
        if row and check_password_hash(row["password"], p):
            session.permanent = True
            session["user_id"]   = row["id"]
            session["username"]  = row["username"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u  = request.form.get("username", "").strip()
        p  = request.form.get("password", "")
        p2 = request.form.get("confirm", "")
        db = get_db()
        if not u or not p:
            flash("All fields are required.", "error")
        elif get_user_by_name(db, u):
            flash("Username already taken.", "error")
        elif p != p2:
            flash("Passwords do not match.", "error")
        elif len(p) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            create_user(db, u, generate_password_hash(p))
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ══════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════

@app.route("/dashboard")
@login_required
def dashboard():
    result  = get_session_result()
    qwarn   = session.get("quality_warn", "")
    # Recent records for the mini history strip
    records = get_user_records(get_db(), session["user_id"], limit=5)
    return render_template("dashboard.html",
                           result=result,
                           quality_warn=qwarn,
                           recent=records,
                           user=session["username"])


# ══════════════════════════════════════════════════════════════
# PROCESS — AJAX OCR endpoint
# ══════════════════════════════════════════════════════════════

@app.route("/process", methods=["POST"])
@login_required
def process():
    if "image" not in request.files:
        return jsonify({"error": "No file received."}), 400

    file = request.files["image"]
    if not file or file.filename == "":
        return jsonify({"error": "No file selected."}), 400
    if not allowed(file.filename):
        return jsonify({"error": "Unsupported file type."}), 400

    filename = secure_filename(file.filename)
    path     = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(path)

    # Resize large images for speed (max 1400px)
    try:
        import cv2
        img = cv2.imread(path)
        if img is not None:
            h, w = img.shape[:2]
            if max(h, w) > 1400:
                scale = 1400 / max(h, w)
                img   = cv2.resize(img, (int(w*scale), int(h*scale)),
                                   interpolation=cv2.INTER_AREA)
                cv2.imwrite(path, img)
    except Exception:
        pass

    # Quality check
    try:
        q = check_image_quality(path)
        session["quality_warn"] = q["message"] if not q["ok"] else ""
    except Exception:
        session["quality_warn"] = ""

    # OCR → extraction pipeline
    try:
        t0      = time.time()
        ocr_out = smart_ocr(path)              # raw text + engine name
        data    = extract_structured_data(     # structured fields
                      ocr_out["raw_text"]
                  )
        # Merge OCR meta into extraction result
        data["ocr_engine"] = ocr_out["ocr_engine"]
        data["elapsed"]    = round(time.time() - t0, 1)
        data["raw_text"]   = ocr_out["raw_text"]

        # Persist to DB
        record_id = save_record(
            get_db(), session["user_id"], filename, data
        )
        data["_record_id"] = record_id
        set_session_result(data)

        return jsonify({"ok": True, "elapsed": data["elapsed"],
                        "record_id": record_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════

@app.route("/report")
@login_required
def report():
    result = get_session_result()
    if not result:
        flash("No data — please upload an image first.", "info")
        return redirect(url_for("dashboard"))
    return render_template("report.html", result=result,
                           user=session["username"])


@app.route("/save", methods=["POST"])
@login_required
def save_edited():
    """AJAX — save user-corrected values to DB and session."""
    result = get_session_result()
    if not result:
        return jsonify({"error": "No session data"}), 400

    edits     = request.get_json(force=True) or {}
    record_id = session.get("last_record_id")

    # Update in-memory result
    for field, val in edits.items():
        if field in result:
            result[field] = val
    if "abw_values" in edits and edits["abw_values"]:
        result["latest_abw"] = edits["abw_values"][-1]
    if "fcr_values" in edits and edits["fcr_values"]:
        result["latest_fcr"] = edits["fcr_values"][-1]

    # Persist to DB
    if record_id:
        update_record(get_db(), record_id, edits)

    set_session_result(result)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════

@app.route("/alerts")
@login_required
def alerts():
    result = get_session_result()
    if not result:
        flash("No data — please upload an image first.", "info")
        return redirect(url_for("dashboard"))

    alert_list = _build_alerts(result, session.get("quality_warn", ""))
    return render_template("alerts.html",
                           alerts=alert_list, user=session["username"])


def _build_alerts(result: dict, qwarn: str) -> list:
    alert_list = []
    abw  = result.get("abw_values") or []
    fcr  = result.get("fcr_values") or []
    mort = result.get("total_mortality") or result.get("mortality")

    # Sudden weight drop > 15%
    for i in range(1, len(abw)):
        try:
            if abw[i-1] > 0:
                pct = ((abw[i] - abw[i-1]) / abw[i-1]) * 100
                if pct < -15:
                    alert_list.append({
                        "level": "danger", "icon": "🚨",
                        "title": "Sudden Weight Drop",
                        "detail": f"ABW fell {abs(pct):.1f}% from week {i} to {i+1} "
                                  f"({abw[i-1]} → {abw[i]} gms). Inspect flock."
                    })
        except (TypeError, ZeroDivisionError):
            continue

    # High FCR
    poor = [v for v in fcr if isinstance(v, (int, float)) and v > 1.3]
    if poor:
        alert_list.append({
            "level": "warning", "icon": "⚠️",
            "title": "High FCR Detected",
            "detail": f"{len(poor)} reading(s) above 1.3 "
                      f"({', '.join(str(v) for v in poor)}). "
                      "High feed use relative to weight gain."
        })

    # Stagnant growth
    try:
        flat = sum(1 for i in range(1, len(abw))
                   if isinstance(abw[i], (int,float))
                   and isinstance(abw[i-1], (int,float))
                   and abw[i] <= abw[i-1])
        if flat >= 2:
            alert_list.append({
                "level": "warning", "icon": "📉",
                "title": "Stagnant Growth",
                "detail": f"ABW did not increase in {flat} week(s). "
                          "Check feed, water, and health."
            })
    except Exception:
        pass

    # Mortality
    try:
        if mort and int(mort) > 0:
            alert_list.append({
                "level": "info", "icon": "ℹ️",
                "title": "Mortality Recorded",
                "detail": f"Total {mort} bird(s) mortality found. Monitor closely."
            })
    except (TypeError, ValueError):
        pass

    # Image quality
    if qwarn:
        alert_list.append({
            "level": "warning", "icon": "🖼️",
            "title": "Image Quality Warning",
            "detail": qwarn
        })

    return alert_list


# ══════════════════════════════════════════════════════════════
# TRENDS
# ══════════════════════════════════════════════════════════════

@app.route("/trends")
@login_required
def trends():
    result = get_session_result()
    if not result:
        flash("No data — please upload an image first.", "info")
        return redirect(url_for("dashboard"))
    return render_template("trends.html", result=result,
                           user=session["username"])


# ══════════════════════════════════════════════════════════════
# HISTORY (all records for this user)
# ══════════════════════════════════════════════════════════════

@app.route("/history")
@login_required
def history():
    records = get_user_records(get_db(), session["user_id"], limit=100)
    return render_template("history.html", records=records,
                           user=session["username"])


@app.route("/history/<int:record_id>")
@login_required
def history_detail(record_id):
    """Load a past record into session and show its report."""
    rec = get_record(get_db(), record_id)
    if not rec or rec["user_id"] != session["user_id"]:
        flash("Record not found.", "error")
        return redirect(url_for("history"))
    rec["_record_id"] = record_id
    set_session_result(rec)
    return redirect(url_for("report"))


# ══════════════════════════════════════════════════════════════
# EXPORTS — FIXED: GET only, filename from query string
# ══════════════════════════════════════════════════════════════

@app.route("/export/excel", methods=["GET"])
@login_required
def export_excel():
    result = get_session_result()
    if not result:
        flash("No data to export.", "error")
        return redirect(url_for("report"))

    raw       = request.args.get("filename", "poultry_record")
    fname     = (raw or "poultry_record").strip()
    fname     = fname.replace(".xlsx","").replace(".xls","") or "poultry_record"
    safe      = secure_filename(fname) or "poultry_record"
    dl_name   = safe + ".xlsx"

    try:
        data = generate_excel(result)
        buf  = io.BytesIO(data); buf.seek(0)
        log_download(get_db(), session["user_id"],
                     session.get("last_record_id"),
                     dl_name, "Excel", round(len(data)/1024, 1))
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=dl_name,
        )
    except Exception as exc:
        flash(f"Excel export failed: {exc}", "error")
        return redirect(url_for("report"))


@app.route("/export/pdf", methods=["GET"])
@login_required
def export_pdf():
    result = get_session_result()
    if not result:
        flash("No data to export.", "error")
        return redirect(url_for("report"))

    raw     = request.args.get("filename", "poultry_record")
    fname   = (raw or "poultry_record").strip()
    fname   = fname.replace(".pdf","") or "poultry_record"
    safe    = secure_filename(fname) or "poultry_record"
    dl_name = safe + ".pdf"

    try:
        data = generate_pdf(result)
        buf  = io.BytesIO(data); buf.seek(0)
        log_download(get_db(), session["user_id"],
                     session.get("last_record_id"),
                     dl_name, "PDF", round(len(data)/1024, 1))
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=dl_name,
        )
    except Exception as exc:
        flash(f"PDF export failed: {exc}", "error")
        return redirect(url_for("report"))


# ══════════════════════════════════════════════════════════════
# SIDEBAR PAGES
# ══════════════════════════════════════════════════════════════

@app.route("/downloads")
@login_required
def downloads():
    hist = get_user_downloads(get_db(), session["user_id"])
    return render_template("downloads.html", history=hist,
                           user=session["username"])


@app.route("/account")
@login_required
def account():
    db   = get_db()
    user = get_user_by_id(db, session["user_id"])
    stats = get_record_stats(db, session["user_id"])
    dl_count = len(get_user_downloads(db, session["user_id"], limit=1000))
    info = {
        "joined":    user["joined"] if user else "—",
        "reports":   stats.get("total_records", 0),
        "downloads": dl_count,
    }
    return render_template("account.html", info=info,
                           user=session["username"])


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", user=session["username"])


@app.route("/help")
@login_required
def help_page():
    return render_template("help.html", user=session["username"])


# ── Run ────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True)