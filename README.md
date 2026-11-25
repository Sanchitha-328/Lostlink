# LostLink — Smart Campus Lost & Found

A Flask + SQLite full-stack starter project for reporting lost/found items, suggesting possible matches, submitting ownership claims, private messaging, notifications, safe handover tracking, and admin moderation.

## Requirements
- Python 3.10+
- pip
- Windows, macOS, or Linux

## Run on Windows (PowerShell)
1. Extract the ZIP file.
2. Open the extracted `LostLink` folder in VS Code.
3. Open Terminal → New Terminal.
4. Create a virtual environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation, use:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe app.py
   ```
5. Install dependencies:
   ```powershell
   python -m pip install -r requirements.txt
   ```
6. Start:
   ```powershell
   python app.py
   ```
7. Open http://127.0.0.1:5000

## First admin login (local demo only)
- Email: `admin@lostlink.local`
- Password: `ChangeMe123!`

Change this immediately for any shared or deployed instance. The initial admin is created only if that email doesn't already exist in the database. For a different initial admin, set environment variables **before the first run**:
- `LOSTLINK_ADMIN_EMAIL`
- `LOSTLINK_ADMIN_PASSWORD`
- `LOSTLINK_SECRET_KEY`

PowerShell example:
```powershell
$env:LOSTLINK_ADMIN_EMAIL="your-admin@example.com"
$env:LOSTLINK_ADMIN_PASSWORD="use-a-long-unique-password"
$env:LOSTLINK_SECRET_KEY="replace-with-a-long-random-secret"
python app.py
```
Do not share or commit real secrets.

## Main routes
- `/` Browse open reports
- `/register`, `/login`, `/logout`
- `/report` Create a lost/found report
- `/report/<id>` Report detail, matching suggestions, claims
- `/my-reports` Your reports and claims
- `/notifications` Notifications
- `/inbox` Private messages associated with claims
- `/admin` Admin dashboard

## Included
- Password hashing and session login
- SQLite relational tables
- Image extension validation and 5 MB upload limit
- Explainable heuristic match score based on category, words, location, and date
- Claim review and status transitions
- Admin moderation and statistics
- Private messaging available to users involved in a claim
- Mobile-friendly templates

## Important limitations before real deployment
This is a working educational MVP, not yet a security-audited production service. Before public deployment:
- Replace the development secret with a long random secret stored in environment variables.
- Use a production WSGI server (e.g. Waitress on Windows or Gunicorn on Linux); do not use Flask debug server.
- Add CSRF protection (e.g. Flask-WTF), login rate limiting, email verification/password reset, and audit logging.
- Add image content sniffing/re-encoding and malware scanning; extension checks alone are not sufficient for hostile public uploads.
- Configure HTTPS, secure cookie settings, backups, monitoring, and retention/privacy policy.
- Use a managed database and object storage if traffic grows.
- Have college administration approve the process before using real student information.
- The match score is a heuristic score, **not a verified probability** or proof of ownership. Always confirm ownership safely and use the campus security desk for handover.

## Reset local demo data
Stop the app, then delete `instance/lostlink.db` and files inside `uploads/`. Restart `python app.py`. This deletes all local demo accounts/reports/messages.

## Git hygiene
Do not commit `.venv/`, `instance/lostlink.db`, uploaded personal images, or secrets. See `.gitignore`.
