import os, re, sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "lostlink.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("LOSTLINK_SECRET_KEY", "dev-only-change-this-before-deployment")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
CATEGORIES = ["Electronics", "ID / Cards", "Bags", "Books / Notes", "Keys", "Clothing", "Accessories", "Water Bottles", "Other"]
LOCATIONS = ["Library", "Computer Lab", "Classroom", "Canteen", "Auditorium", "Sports Ground", "College Bus", "Security Office", "Other"]

def connect_db():
    # Wait briefly for another request to finish its write instead of failing immediately.
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA busy_timeout = 15000")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            report_type TEXT NOT NULL CHECK(report_type IN ('lost','found')),
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            location TEXT NOT NULL,
            event_date TEXT NOT NULL,
            image_filename TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','claimed','returned','rejected')),
            handover_method TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL REFERENCES reports(id),
            claimant_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            verification_note TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','completed')),
            created_at TEXT NOT NULL,
            UNIQUE(report_id, claimant_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL REFERENCES reports(id),
            sender_id INTEGER NOT NULL REFERENCES users(id),
            recipient_id INTEGER NOT NULL REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            text TEXT NOT NULL,
            url TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """)
        # Add handover tracking columns to existing databases without deleting user data.
        claim_columns = {row["name"] for row in db.execute("PRAGMA table_info(claims)").fetchall()}
        for column, definition in (
            ("handover_started", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_confirmed", "INTEGER NOT NULL DEFAULT 0"),
            ("claimant_confirmed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in claim_columns:
                db.execute(f"ALTER TABLE claims ADD COLUMN {column} {definition}")
        admin_email = os.environ.get("LOSTLINK_ADMIN_EMAIL", "admin@lostlink.local").lower()
        admin_password = os.environ.get("LOSTLINK_ADMIN_PASSWORD", "ChangeMe123!")
        exists = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
        if not exists:
            db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                       ("LostLink Admin", admin_email, generate_password_hash(admin_password), "admin", now()))
    print("LostLink database initialized.")

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def current_user():
    if "user_id" not in session:
        return None
    with connect_db() as db:
        return db.execute("SELECT id,name,email,role FROM users WHERE id=?", (session["user_id"],)).fetchone()

@app.context_processor

def inject_globals():
    user = current_user()
    unread = 0
    if user:
        with connect_db() as db:
            unread = db.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)).fetchone()["n"]
    return {"current_user": user, "unread_notifications": unread, "categories": CATEGORIES, "locations": LOCATIONS}

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if user["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def notify(user_id, text, url, db=None):
    """Create a notification, reusing an active transaction when one is supplied.
    Reusing the caller's connection avoids opening a second writer connection while
    a claim/message/report transaction is still active (a common SQLite lock cause).
    """
    sql = "INSERT INTO notifications(user_id,text,url,created_at) VALUES(?,?,?,?)"
    values = (user_id, text, url, now())
    if db is not None:
        db.execute(sql, values)
    else:
        with connect_db() as connection:
            connection.execute(sql, values)

def tokenize(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))

def match_score(a, b):
    """Explainable heuristic, not an AI certainty/probability."""
    score = 0
    reasons = []
    if a["category"] == b["category"]:
        score += 30; reasons.append("same category")
    ta, tb = tokenize(a["title"] + " " + a["description"]), tokenize(b["title"] + " " + b["description"])
    stop = {"the","and","with","from","this","that","item","lost","found","for","has","have","was","are","is","my"}
    ta -= stop; tb -= stop
    union = ta | tb
    overlap = len(ta & tb) / len(union) if union else 0
    score += round(overlap * 40)
    if overlap >= .15: reasons.append("similar descriptions")
    if a["location"] == b["location"]:
        score += 20; reasons.append("same location")
    try:
        days = abs((datetime.strptime(a["event_date"], "%Y-%m-%d") - datetime.strptime(b["event_date"], "%Y-%m-%d")).days)
        date_points = max(0, 10 - days * 2)
        score += date_points
        if days <= 2: reasons.append("close dates")
    except ValueError:
        pass
    return min(score, 100), reasons

@app.route("/")

def index():
    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "")
    category = request.args.get("category", "")
    sql = """SELECT r.*, u.name AS reporter FROM reports r JOIN users u ON u.id=r.user_id
             WHERE r.status='open'"""
    params = []
    if q:
        sql += " AND (r.title LIKE ? OR r.description LIKE ? OR r.location LIKE ?)"
        params += [f"%{q}%"] * 3
    if kind in ("lost", "found"):
        sql += " AND r.report_type=?"; params.append(kind)
    if category in CATEGORIES:
        sql += " AND r.category=?"; params.append(category)
    sql += " ORDER BY r.created_at DESC LIMIT 60"
    with connect_db() as db:
        reports = db.execute(sql, params).fetchall()
        totals = {
            "open": db.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
            "returned": db.execute("SELECT COUNT(*) FROM reports WHERE status='returned'").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]
        }
    return render_template("index.html", reports=reports, totals=totals, q=q, selected_type=kind, selected_category=category)

@app.route("/register", methods=["GET", "POST"])

def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(name) < 2 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a name and valid email address.", "danger")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        else:
            try:
                with connect_db() as db:
                    db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                               (name, email, generate_password_hash(password), "student", now()))
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That email is already registered.", "danger")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])

def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with connect_db() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}!", "success")
            dest = request.args.get("next", "")
            if dest.startswith("/") and not dest.startswith("//"):
                return redirect(dest)
            return redirect(url_for("index"))
        flash("Email or password is incorrect.", "danger")
    return render_template("auth.html", mode="login")

@app.route("/logout")

def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))

@app.route("/report", methods=["GET", "POST"])
@login_required

def create_report():
    if request.method == "POST":
        user = current_user()
        report_type = request.form.get("report_type")
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "")
        description = request.form.get("description", "").strip()
        location = request.form.get("location", "")
        event_date = request.form.get("event_date", "")
        image = request.files.get("image")
        if report_type not in ("lost", "found") or not title or len(title) > 100 or len(description) < 10 or category not in CATEGORIES or location not in LOCATIONS:
            flash("Please complete all fields. Description must be at least 10 characters.", "danger")
            return render_template("report.html")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            flash("Choose a valid date.", "danger")
            return render_template("report.html")
        filename = None
        if image and image.filename:
            if not allowed_file(image.filename):
                flash("Image must be PNG, JPG, JPEG or WEBP.", "danger")
                return render_template("report.html")
            filename = secure_filename(f"{user['id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{image.filename}")
            image.save(UPLOAD_DIR / filename)
        with connect_db() as db:
            cur = db.execute("""INSERT INTO reports(user_id,report_type,title,category,description,location,event_date,image_filename,created_at)
                             VALUES(?,?,?,?,?,?,?,?,?)""",
                             (user["id"], report_type, title, category, description, location, event_date, filename, now()))
            report_id = cur.lastrowid
            other_type = "found" if report_type == "lost" else "lost"
            candidates = db.execute("SELECT * FROM reports WHERE report_type=? AND status='open' AND id<>?", (other_type, report_id)).fetchall()
        new_report = {"category": category, "title": title, "description": description, "location": location, "event_date": event_date}
        for candidate in candidates:
            score, reasons = match_score(new_report, candidate)
            if score >= 45:
                notify(candidate["user_id"], f"Possible match for “{candidate['title']}” ({score}/100 match score).", url_for("report_detail", report_id=report_id), db=db)
                notify(user["id"], f"Possible match found for “{title}” ({score}/100 match score).", url_for("report_detail", report_id=candidate["id"]), db=db)
        flash("Report published. LostLink checked for possible matches.", "success")
        return redirect(url_for("report_detail", report_id=report_id))
    return render_template("report.html")

@app.route("/report/<int:report_id>")

def report_detail(report_id):
    with connect_db() as db:
        report = db.execute(
            "SELECT r.*,u.name AS reporter FROM reports r JOIN users u ON u.id=r.user_id WHERE r.id=?",
            (report_id,)
        ).fetchone()
        if not report:
            abort(404)
        opposite = "found" if report["report_type"] == "lost" else "lost"
        candidates = db.execute(
            "SELECT * FROM reports WHERE report_type=? AND status='open' AND id<>? ORDER BY created_at DESC",
            (opposite, report_id)
        ).fetchall()
        matches = []
        for candidate in candidates:
            score, reasons = match_score(report, candidate)
            if score >= 30:
                matches.append({"report": candidate, "score": score, "reasons": reasons})
        matches.sort(key=lambda item: item["score"], reverse=True)
        matches = matches[:5]
        user = current_user()
        existing_claim = None
        if user:
            existing_claim = db.execute(
                "SELECT * FROM claims WHERE report_id=? AND claimant_id=?",
                (report_id, user["id"])
            ).fetchone()
        claims = []
        if user and (user["id"] == report["user_id"] or user["role"] == "admin"):
            claims = db.execute(
                """SELECT c.*,u.name AS claimant
                   FROM claims c JOIN users u ON u.id=c.claimant_id
                   WHERE c.report_id=? ORDER BY c.created_at DESC""",
                (report_id,)
            ).fetchall()
    return render_template(
        "detail.html",
        report=report,
        matches=matches,
        existing_claim=existing_claim,
        claims=claims
    )

@app.route("/claim/<int:report_id>", methods=["POST"])
@login_required

def create_claim(report_id):
    user = current_user()
    message = request.form.get("message", "").strip()
    verification = request.form.get("verification_note", "").strip()
    with connect_db() as db:
        report = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report: abort(404)
        if report["user_id"] == user["id"]:
            flash("You cannot claim your own report.", "danger")
            return redirect(url_for("report_detail", report_id=report_id))
        if report["status"] != "open":
            flash("This report is no longer open for claims.", "warning")
            return redirect(url_for("report_detail", report_id=report_id))
        if len(message) < 5 or len(verification) < 5:
            flash("Add a message and enough detail for the owner/finder to verify the claim.", "danger")
            return redirect(url_for("report_detail", report_id=report_id))
        try:
            db.execute("INSERT INTO claims(report_id,claimant_id,message,verification_note,created_at) VALUES(?,?,?,?,?)",
                       (report_id,user["id"],message,verification,now()))
        except sqlite3.IntegrityError:
            flash("You already submitted a claim for this report.", "warning")
            return redirect(url_for("report_detail", report_id=report_id))
        notify(report["user_id"], f"{user['name']} submitted a claim for “{report['title']}”.", url_for("report_detail", report_id=report_id), db=db)
    flash("Claim submitted. The report owner or admin can review it.", "success")
    return redirect(url_for("report_detail", report_id=report_id))

@app.route("/claim/<int:claim_id>/<action>", methods=["POST"])
@login_required

def review_claim(claim_id, action):
    user = current_user()
    if action not in ("approve", "reject", "start", "confirm"):
        abort(404)
    with connect_db() as db:
        claim = db.execute(
            """SELECT c.*,r.user_id AS owner_id,r.id AS report_id,r.title,
                      r.status AS report_status,r.handover_method
               FROM claims c JOIN reports r ON r.id=c.report_id
               WHERE c.id=?""",
            (claim_id,)
        ).fetchone()
        if not claim:
            abort(404)
        is_report_owner = user["id"] == claim["owner_id"]
        is_claimant = user["id"] == claim["claimant_id"]
        if action in ("approve", "reject", "start"):
            if not is_report_owner and user["role"] != "admin":
                abort(403)
        elif not (is_report_owner or is_claimant):
            abort(403)
        if action == "approve":
            if claim["status"] != "pending":
                flash("This claim is no longer pending.", "warning")
            elif claim["report_status"] != "open":
                flash("Another claim has already been approved or this report is no longer open.", "warning")
            else:
                db.execute("UPDATE claims SET status='approved' WHERE id=?", (claim_id,))
                db.execute("UPDATE reports SET status='claimed' WHERE id=?", (claim["report_id"],))
                notify(
                    claim["claimant_id"],
                    f"Your claim for “{claim['title']}” was approved. The report owner can now start the handover.",
                    url_for("report_detail", report_id=claim["report_id"]),
                    db=db
                )
                flash("Claim approved. Start the handover when you are ready.", "success")
        elif action == "reject":
            if claim["status"] != "pending":
                flash("Only pending claims can be rejected.", "warning")
            else:
                db.execute("UPDATE claims SET status='rejected' WHERE id=?", (claim_id,))
                notify(
                    claim["claimant_id"],
                    f"Your claim for “{claim['title']}” was not approved.",
                    url_for("report_detail", report_id=claim["report_id"]),
                    db=db
                )
                flash("Claim rejected.", "success")
        elif action == "start":
            if claim["status"] != "approved":
                flash("Approve the claim before starting a handover.", "warning")
            else:
                method = request.form.get("handover_method", "").strip()
                allowed_methods = {
                    "Security office",
                    "Library desk",
                    "In-person safe campus location"
                }
                if method not in allowed_methods:
                    flash("Please choose a valid handover method.", "danger")
                else:
                    db.execute(
                        "UPDATE reports SET handover_method=? WHERE id=?",
                        (method, claim["report_id"])
                    )
                    db.execute(
                        """UPDATE claims
                           SET handover_started=1, owner_confirmed=0, claimant_confirmed=0
                           WHERE id=?""",
                        (claim_id,)
                    )
                    notify(
                        claim["claimant_id"],
                        f"Handover started for “{claim['title']}” using: {method}. Open Inbox to arrange the details.",
                        url_for("inbox", report_id=claim["report_id"]),
                        db=db
                    )
                    flash(
                        "Handover started. The item has NOT been marked returned. Use Inbox to arrange the handover.",
                        "success"
                    )
        elif action == "confirm":
            if claim["status"] == "completed":
                flash("This handover is already complete.", "info")
            elif claim["status"] != "approved" or not claim["handover_started"]:
                flash("The handover has not been started yet.", "warning")
            else:
                if is_report_owner:
                    db.execute("UPDATE claims SET owner_confirmed=1 WHERE id=?", (claim_id,))
                else:
                    db.execute("UPDATE claims SET claimant_confirmed=1 WHERE id=?", (claim_id,))
                updated = db.execute(
                    "SELECT owner_confirmed,claimant_confirmed FROM claims WHERE id=?",
                    (claim_id,)
                ).fetchone()
                if updated["owner_confirmed"] and updated["claimant_confirmed"]:
                    db.execute("UPDATE claims SET status='completed' WHERE id=?", (claim_id,))
                    db.execute(
                        "UPDATE reports SET status='returned' WHERE id=?",
                        (claim["report_id"],)
                    )
                    notify(
                        claim["owner_id"],
                        f"Both parties confirmed the handover of “{claim['title']}”. It is now marked returned.",
                        url_for("report_detail", report_id=claim["report_id"]),
                        db=db
                    )
                    notify(
                        claim["claimant_id"],
                        f"Both parties confirmed the handover of “{claim['title']}”. It is now marked returned.",
                        url_for("report_detail", report_id=claim["report_id"]),
                        db=db
                    )
                    flash("Both parties confirmed. The item is now marked returned.", "success")
                else:
                    other_id = claim["claimant_id"] if is_report_owner else claim["owner_id"]
                    notify(
                        other_id,
                        f"The other party confirmed the handover of “{claim['title']}”. Please confirm after you receive/hand over the item.",
                        url_for("report_detail", report_id=claim["report_id"]),
                        db=db
                    )
                    flash("Your confirmation was saved. The item will be marked returned only after the other person confirms.", "success")
    return redirect(url_for("report_detail", report_id=claim["report_id"]))

@app.route("/message/<int:report_id>", methods=["POST"])
@login_required

def send_message(report_id):
    user = current_user()
    body = request.form.get("body", "").strip()
    with connect_db() as db:
        report = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            abort(404)
        claim = db.execute(
            """SELECT * FROM claims
               WHERE report_id=? AND status IN ('approved','completed')
                 AND handover_started=1""",
            (report_id,)
        ).fetchone()
        if not claim:
            flash("Private chat becomes available after the report owner starts the handover.", "warning")
            return redirect(url_for("report_detail", report_id=report_id))
        if user["id"] == report["user_id"]:
            recipient = claim["claimant_id"]
        elif user["id"] == claim["claimant_id"]:
            recipient = report["user_id"]
        else:
            abort(403)
        if not body or len(body) > 1000:
            flash("Message must be 1–1000 characters.", "danger")
            return redirect(url_for("inbox", report_id=report_id))
        db.execute(
            "INSERT INTO messages(report_id,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?)",
            (report_id, user["id"], recipient, body, now())
        )
        notify(
            recipient,
            f"New private message about “{report['title']}”.",
            url_for("inbox", report_id=report_id),
            db=db
        )
    flash("Private message sent.", "success")
    return redirect(url_for("inbox", report_id=report_id))

@app.route("/inbox")
@login_required

def inbox():
    user = current_user()
    selected_report_id = request.args.get("report_id", type=int)
    with connect_db() as db:
        conversations = db.execute(
            """SELECT c.report_id,p.title AS report_title,p.handover_method,p.status AS report_status,
                      CASE WHEN p.user_id=? THEN c.claimant_id ELSE p.user_id END AS other_user_id,
                      other.name AS other_name,
                      (SELECT body FROM messages mx WHERE mx.report_id=p.id
                       ORDER BY mx.id DESC LIMIT 1) AS last_message,
                      (SELECT created_at FROM messages mt WHERE mt.report_id=p.id
                       ORDER BY mt.id DESC LIMIT 1) AS last_time
               FROM claims c
               JOIN reports p ON p.id=c.report_id
               JOIN users other ON other.id=CASE WHEN p.user_id=? THEN c.claimant_id ELSE p.user_id END
               WHERE c.handover_started=1
                 AND c.status IN ('approved','completed')
                 AND (p.user_id=? OR c.claimant_id=?)
               ORDER BY COALESCE(last_time,c.created_at) DESC""",
            (user["id"], user["id"], user["id"], user["id"])
        ).fetchall()
        active_report = None
        conversation_messages = []
        other_user = None
        if selected_report_id:
            active_report = db.execute(
                """SELECT p.*,c.id AS claim_id,c.status AS claim_status,
                          c.owner_confirmed,c.claimant_confirmed,c.handover_started,
                          CASE WHEN p.user_id=? THEN c.claimant_id ELSE p.user_id END AS other_user_id
                   FROM reports p
                   JOIN claims c ON c.report_id=p.id
                   WHERE p.id=? AND c.handover_started=1
                     AND c.status IN ('approved','completed')
                     AND (p.user_id=? OR c.claimant_id=?)""",
                (user["id"], selected_report_id, user["id"], user["id"])
            ).fetchone()
            if not active_report:
                abort(403)
            other_user = db.execute(
                "SELECT id,name FROM users WHERE id=?",
                (active_report["other_user_id"],)
            ).fetchone()
            conversation_messages = db.execute(
                """SELECT m.*,u.name AS sender_name
                   FROM messages m JOIN users u ON u.id=m.sender_id
                   WHERE m.report_id=? AND
                         (m.sender_id=? OR m.recipient_id=?)
                   ORDER BY m.id ASC""",
                (selected_report_id, user["id"], user["id"])
            ).fetchall()
    return render_template(
        "inbox.html",
        messages=conversation_messages,
        conversations=conversations,
        active_report=active_report,
        other_user=other_user,
        user=user
    )

@app.route("/notifications")
@login_required

def notifications():
    user = current_user()
    with connect_db() as db:
        items = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    return render_template("notifications.html", items=items)

@app.route("/my-reports")
@login_required

def my_reports():
    user = current_user()
    with connect_db() as db:
        reports = db.execute("SELECT * FROM reports WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        claims = db.execute("""SELECT c.*,r.title,r.id AS report_id FROM claims c JOIN reports r ON r.id=c.report_id
                               WHERE c.claimant_id=? ORDER BY c.created_at DESC""", (user["id"],)).fetchall()
    return render_template("my_reports.html", reports=reports, claims=claims)

@app.route("/admin")
@admin_required

def admin_dashboard():
    with connect_db() as db:
        stats = {
            "users": db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
            "reports": db.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
            "open": db.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
            "returned": db.execute("SELECT COUNT(*) FROM reports WHERE status='returned'").fetchone()[0],
            "pending": db.execute("SELECT COUNT(*) FROM claims WHERE status='pending'").fetchone()[0]
        }
        reports = db.execute("SELECT r.*,u.name AS reporter FROM reports r JOIN users u ON u.id=r.user_id ORDER BY r.created_at DESC LIMIT 100").fetchall()
        claims = db.execute("SELECT c.*,u.name AS claimant,r.title FROM claims c JOIN users u ON u.id=c.claimant_id JOIN reports r ON r.id=c.report_id ORDER BY c.created_at DESC LIMIT 100").fetchall()
    return render_template("admin.html", stats=stats, reports=reports, claims=claims)

@app.route("/admin/report/<int:report_id>/<action>", methods=["POST"])
@admin_required

def admin_report_action(report_id, action):
    if action not in ("reject", "reopen"): abort(404)
    with connect_db() as db:
        report = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report: abort(404)
        status = "rejected" if action == "reject" else "open"
        db.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
        notify(report["user_id"], f"Your report “{report['title']}” status is now {status}.", url_for("report_detail", report_id=report_id), db=db)
    flash("Report status updated.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/uploads/<path:filename>")

def uploaded_file(filename):
    # Only serve filenames generated by this application.
    if Path(filename).name != filename: abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.errorhandler(403)

def forbidden(_):
    return render_template("error.html", code=403, message="You do not have permission to access this page."), 403

@app.errorhandler(404)

def not_found(_):
    return render_template("error.html", code=404, message="We couldn't find that page."), 404

@app.errorhandler(413)

def too_large(_):
    flash("Image upload is too large. Maximum upload size is 5 MB.", "danger")
    return redirect(request.referrer or url_for("index"))
with app.app_context():
    init_db()

if __name__ == "__main__":
    # Debug mode is for local development only. Do not use Flask's dev server in production.
    app.run(debug=True)
