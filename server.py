"""
server.py v3 -- adds user management and auth to the eval dashboard.

New features:
  - Login page (/login) with session management
  - Per-user labels stored in SQLite (labels table)
  - Admin panel (/admin) to create/manage users
  - Labels attributed to specific users, shown with username
  - All existing JSON labels migrated to 'admin' user on first run

Default admin credentials: admin / changeme
Change via: python server.py --create-user <username> <password> [admin|labeler]

Run:
    python server.py
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import secrets
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from functools import wraps

from flask import Flask, Response, jsonify, redirect, render_template_string, \
    request, session, stream_with_context, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from tracing import (Tracer, create_user, delete_label, get_labels_for_run,
                     get_user_by_id, get_user_by_username, init_db,
                     list_users, load_all_labels, migrate_json_labels,
                     upsert_label)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_PATH = "traces.db"
QUESTIONS_FILE = "test_questions.json"
FAILURE_CATEGORIES = [
    "query_reformulation_thrashing",
    "stale_latest_claim",
    "ungrounded_synthesis",
    "ambiguous_metric_inconsistency",
    "other",
]
DEFAULT_QUESTIONS = [
    "What's the capital of the country with the largest population in Africa?",
    "Who is the current head of state of the country with the second-largest economy in South America?",
    "What is the population of the capital city of the country where the Eiffel Tower is located?",
    "Who directed the highest-grossing film starring the actor who won Best Actor at the most recent Oscars?",
    "What is the GDP per capita of the country that won the most recent FIFA World Cup?",
    "Which programming language is ranked most popular in the latest Stack Overflow developer survey, and who originally created it?",
    "Who is the CEO of the parent company of the airline with the most domestic routes in the United States?",
    "What is the tallest building in the city that hosted the most recent Summer Olympics?",
]

_batch_queue: queue.Queue = queue.Queue()
_batch_running = False
_tools_queue: queue.Queue = queue.Queue()

# One shared DB connection per thread
import threading as _threading
_local = _threading.local()

def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = init_db(DB_PATH)
    return _local.conn


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        user = get_user_by_id(get_conn(), session["user_id"])
        if not user or user["role"] != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated


def current_user():
    if "user_id" not in session:
        return None
    return get_user_by_id(get_conn(), session["user_id"])


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_questions():
    if os.path.exists(QUESTIONS_FILE):
        with open(QUESTIONS_FILE) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return DEFAULT_QUESTIONS[:]


def save_questions(questions):
    with open(QUESTIONS_FILE, "w") as f:
        json.dump(questions, f, indent=2)


def load_consistency_results():
    results = []
    for path in glob.glob("consistency_*.json"):
        with open(path) as f:
            results.append(json.load(f))
    return results


def load_regression_results():
    results = []
    for path in glob.glob("regression_*.json"):
        with open(path) as f:
            results.append(json.load(f))
    return results


def build_payload():
    conn = get_conn()
    tracer = Tracer(DB_PATH)
    runs = tracer.get_all_runs_full()
    all_labels = load_all_labels(conn)
    user = current_user()
    user_id = user["user_id"] if user else None

    # Get per-user labels for current user
    my_label_run_ids = set()
    if user_id:
        rows = conn.execute("SELECT run_id FROM labels WHERE user_id = ?", (user_id,)).fetchall()
        my_label_run_ids = {r[0] for r in rows}

    for run in runs:
        label = all_labels.get(run["run_id"], {})
        run["verdict"] = label.get("label", "unlabeled")
        run["failure_category"] = label.get("failure_category")
        run["notes"] = label.get("notes", "")
        run["labeled_by_me"] = run["run_id"] in my_label_run_ids

        # Per-run: all labelers' verdicts
        run_labels = get_labels_for_run(conn, run["run_id"])
        run["all_labels"] = run_labels

    total = len(runs)
    pass_count = sum(1 for r in runs if r["verdict"] == "pass")
    fail_count = sum(1 for r in runs if r["verdict"] == "fail")
    unlabeled = total - pass_count - fail_count
    category_counts = Counter(r["failure_category"] for r in runs if r["failure_category"])
    group_counts = Counter(r["group_id"] for r in runs if r["group_id"])

    return {
        "runs": runs,
        "consistency_results": load_consistency_results(),
        "regression_results": load_regression_results(),
        "failure_categories": FAILURE_CATEGORIES,
        "questions": load_questions(),
        "current_user": user,
        "stats": {
            "total": total,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "unlabeled": unlabeled,
        },
        "category_counts": dict(category_counts.most_common()),
        "group_counts": dict(group_counts.most_common()),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Calibra — Login</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'IBM Plex Sans',sans-serif;background:#080B0F;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.login-wrap{display:flex;flex-direction:column;align-items:center;width:100%;max-width:400px;padding:24px;}
.login-logo{display:flex;flex-direction:column;align-items:center;margin-bottom:32px;gap:12px;}
.login-wordmark{font-size:32px;font-weight:600;background:linear-gradient(135deg,#42B8FF,#D64FFF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-0.5px;}
.login-tagline{font-size:13px;color:#4A5568;text-align:center;}
.login-card{background:#0D1117;border:1px solid #1A2233;border-radius:12px;padding:32px;width:100%;}
.login-card h2{font-size:16px;font-weight:600;color:#E2E8F0;margin-bottom:6px;}
.login-card p{font-size:13px;color:#8896A7;margin-bottom:24px;}
.field{margin-bottom:16px;}
.field label{display:block;font-size:11px;font-weight:600;color:#8896A7;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;}
.field input{width:100%;background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:10px 14px;border-radius:8px;font-size:14px;font-family:inherit;outline:none;transition:border-color 0.15s;}
.field input:focus{border-color:#4F7CFF;}
.btn{width:100%;background:linear-gradient(135deg,#4F7CFF,#9B59F7);color:#fff;border:none;padding:12px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;margin-top:8px;}
.btn:hover{opacity:0.9;}
.error{background:#2E0D0D;border:1px solid #4A1A1A;color:#f87171;padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:16px;}
.login-footer{margin-top:20px;font-size:12px;color:#4A5568;text-align:center;}
</style>
</head>
<body>
<div class="login-wrap">
  <div class="login-logo">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600" width="56" height="56" role="img" aria-label="Calibra logo"><defs><linearGradient id="lg2" x1="12%" y1="8%" x2="88%" y2="92%"><stop offset="0%" stop-color="#42B8FF"/><stop offset="48%" stop-color="#6E76FF"/><stop offset="100%" stop-color="#D64FFF"/></linearGradient></defs><path d="M 455 130 A 220 220 0 1 0 455 470" fill="none" stroke="url(#lg2)" stroke-width="74" stroke-linecap="round"/><circle cx="182" cy="300" r="18" fill="url(#lg2)"/><rect x="222" y="260" width="40" height="80" rx="20" fill="url(#lg2)"/><rect x="287" y="215" width="40" height="170" rx="20" fill="url(#lg2)"/><rect x="352" y="155" width="40" height="290" rx="20" fill="url(#lg2)"/><rect x="417" y="225" width="40" height="150" rx="20" fill="url(#lg2)"/><circle cx="495" cy="300" r="25" fill="url(#lg2)"/></svg>
    <div class="login-wordmark">Calibra</div>
    <div class="login-tagline">Agent evaluation & observability platform</div>
  </div>
  <div class="login-card">
    <h2>Sign in</h2>
    <p>Enter your credentials to access the dashboard.</p>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="POST">
      <div class="field">
        <label>Username</label>
        <input type="text" name="username" autocomplete="username" autofocus placeholder="your username">
      </div>
      <div class="field">
        <label>Password</label>
        <input type="password" name="password" autocomplete="current-password" placeholder="••••••••">
      </div>
      <button class="btn" type="submit">Sign in</button>
    </form>
  </div>
  <div class="login-footer">Calibra &mdash; Agent Evaluation Framework</div>
</div>
</body>
</html>"""

ADMIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Calibra — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:"IBM Plex Sans",sans-serif;background:#080B0F;color:#E2E8F0;min-height:100vh;}
.topbar{background:#0D1117;border-bottom:1px solid #1A2233;padding:14px 32px;display:flex;align-items:center;justify-content:space-between;}
.topbar-left{display:flex;align-items:center;gap:12px;}
.logo-text{font-size:16px;font-weight:600;background:linear-gradient(135deg,#42B8FF,#D64FFF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.topbar-title{font-size:14px;color:#8896A7;}
.topbar-right{display:flex;gap:12px;align-items:center;}
.btn-back{font-size:13px;color:#4F7CFF;text-decoration:none;}
.btn-back:hover{color:#7BA3FF;}
.content{padding:32px;}
h1{font-size:20px;font-weight:600;color:#E2E8F0;margin-bottom:4px;}
.subtitle{font-size:13px;color:#8896A7;margin-bottom:28px;}
.msg{background:#0D2E1A;border:1px solid #1A4A28;color:#4ade80;padding:10px 16px;border-radius:6px;margin-bottom:20px;font-size:13px;}
.msg.error{background:#2E0D0D;border-color:#4A1A1A;color:#f87171;}
.section{margin-bottom:32px;}
.section-title{font-size:13px;font-weight:600;color:#8896A7;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #1A2233;}
.card{background:#0D1117;border:1px solid #1A2233;border-radius:10px;padding:20px 24px;margin-bottom:12px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:#4A5568;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;padding:8px 12px;border-bottom:1px solid #1A2233;}
td{padding:10px 12px;border-bottom:1px solid #0D1117;vertical-align:middle;}
tr:hover td{background:#0A0F18;}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;}
.badge.admin{background:#1B1B4B;color:#818CF8;border:1px solid #2D2B6E;}
.badge.labeler{background:#131C2E;color:#8896A7;border:1px solid #1A2233;}
.badge.pass{background:#0D2E1A;color:#4ade80;border:1px solid #1A4A28;}
.badge.fail{background:#2E0D0D;color:#f87171;border:1px solid #4A1A1A;}
.badge.agree{background:#0D2E1A;color:#4ade80;}
.badge.disagree{background:#2E0D0D;color:#f87171;}
input[type=text],input[type=password],select{background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit;}
.btn{border:none;padding:7px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;}
.btn-primary{background:#4F7CFF;color:#fff;}
.btn-primary:hover{background:#3D6AEE;}
.btn-danger{background:#2E0D0D;color:#f87171;border:1px solid #4A1A1A;}
.btn-danger:hover{background:#3D1616;}
.btn-secondary{background:#131C2E;color:#8896A7;border:1px solid #1A2233;}
.btn-secondary:hover{background:#1A2640;color:#E2E8F0;}
.create-form{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;}
.field label{display:block;font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;}
.inter-run{background:#080B0F;border:1px solid #1A2233;border-radius:8px;padding:14px 16px;margin-bottom:10px;}
.inter-task{font-size:13px;color:#C8D6E5;margin-bottom:6px;line-height:1.4;}
.inter-answer{font-size:11px;color:#4A5568;margin-bottom:10px;font-family:monospace;}
.inter-labels{display:flex;gap:10px;flex-wrap:wrap;}
.inter-label-card{background:#0D1117;border:1px solid #1A2233;border-radius:6px;padding:8px 12px;min-width:180px;}
.inter-label-user{font-size:11px;font-weight:600;color:#8896A7;margin-bottom:4px;}
.inter-label-verdict{font-size:13px;font-weight:600;margin-bottom:2px;}
.inter-label-cat{font-size:11px;color:#4A5568;}
.inter-label-notes{font-size:11px;color:#8896A7;font-style:italic;margin-top:4px;}
.stat-row{display:flex;gap:20px;margin-bottom:16px;flex-wrap:wrap;}
.stat-mini{background:#080B0F;border:1px solid #1A2233;border-radius:8px;padding:12px 16px;min-width:120px;}
.stat-mini-val{font-size:22px;font-weight:600;color:#E2E8F0;}
.stat-mini-label{font-size:11px;color:#4A5568;margin-top:2px;}
.empty{color:#4A5568;font-size:13px;padding:20px 0;text-align:center;}
.actions{display:flex;gap:6px;align-items:center;}
.reset-form{display:inline-flex;gap:6px;align-items:center;}
#inter-rater-content{margin-top:12px;}
.tabs{display:flex;gap:2px;margin-bottom:16px;}
.tab-btn{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;border:none;background:#131C2E;color:#8896A7;font-family:inherit;}
.tab-btn.active{background:#1A2640;color:#E2E8F0;}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600" width="28" height="28"><defs><linearGradient id="ag" x1="12%" y1="8%" x2="88%" y2="92%"><stop offset="0%" stop-color="#42B8FF"/><stop offset="48%" stop-color="#6E76FF"/><stop offset="100%" stop-color="#D64FFF"/></linearGradient></defs><path d="M 455 130 A 220 220 0 1 0 455 470" fill="none" stroke="url(#ag)" stroke-width="74" stroke-linecap="round"/><circle cx="182" cy="300" r="18" fill="url(#ag)"/><rect x="222" y="260" width="40" height="80" rx="20" fill="url(#ag)"/><rect x="287" y="215" width="40" height="170" rx="20" fill="url(#ag)"/><rect x="352" y="155" width="40" height="290" rx="20" fill="url(#ag)"/><rect x="417" y="225" width="40" height="150" rx="20" fill="url(#ag)"/><circle cx="495" cy="300" r="25" fill="url(#ag)"/></svg>
    <span class="logo-text">Calibra</span>
    <span class="topbar-title">/ Admin Panel</span>
  </div>
  <div class="topbar-right">
    <a class="btn-back" href="/">&larr; Back to dashboard</a>
  </div>
</div>

<div class="content">
  <h1>Admin Panel</h1>
  <div class="subtitle">Manage users, review labeling contributions, and track inter-rater agreement.</div>

  {% if message %}
  <div class="msg {% if "Error" in message %}error{% endif %}">{{ message }}</div>
  {% endif %}

  <!-- USER MANAGEMENT -->
  <div class="section">
    <div class="section-title">User management</div>
    <div class="card">
      <div class="section-title" style="border:none;margin-bottom:10px;">Create new user</div>
      <form method="POST" action="/admin/create-user">
        <div class="create-form">
          <div class="field"><label>Username</label><input type="text" name="username" placeholder="username"></div>
          <div class="field"><label>Password</label><input type="password" name="password" placeholder="password"></div>
          <div class="field"><label>Role</label><select name="role"><option value="labeler">Labeler</option><option value="admin">Admin</option></select></div>
          <button class="btn btn-primary" type="submit">Create user</button>
        </div>
      </form>
    </div>
    <div class="card" style="padding:0;">
      <table>
        <thead><tr>
          <th>Username</th><th>Role</th><th>Labels</th><th>Created</th><th>Actions</th>
        </tr></thead>
        <tbody>
        {% for u in users %}
        <tr>
          <td><strong>{{ u.username }}</strong></td>
          <td><span class="badge {{ u.role }}">{{ u.role }}</span></td>
          <td>{{ u.label_count }}</td>
          <td style="color:#4A5568;font-size:12px">{{ u.created_at[:10] if u.created_at else "-" }}</td>
          <td>
            <div class="actions">
              <form method="POST" action="/admin/update-user" style="display:inline;">
                <input type="hidden" name="user_id" value="{{ u.user_id }}">
                {% if u.role == "labeler" %}
                <button class="btn btn-secondary" name="action" value="make_admin" type="submit">Make admin</button>
                {% else %}
                <button class="btn btn-secondary" name="action" value="make_labeler" type="submit">Make labeler</button>
                {% endif %}
              </form>
              <form method="POST" action="/admin/update-user" class="reset-form">
                <input type="hidden" name="user_id" value="{{ u.user_id }}">
                <input type="hidden" name="action" value="reset_password">
                <input type="password" name="new_password" placeholder="new password" style="width:130px;">
                <button class="btn btn-secondary" type="submit">Reset</button>
              </form>
              <form method="POST" action="/admin/update-user" style="display:inline;" onsubmit="return confirm('Delete {{ u.username }}? This removes all their labels.')">
                <input type="hidden" name="user_id" value="{{ u.user_id }}">
                <button class="btn btn-danger" name="action" value="delete" type="submit">Delete</button>
              </form>
            </div>
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- LABELING OVERVIEW -->
  <div class="section">
    <div class="section-title">Labeling contributions</div>
    <div class="card" id="labeling-overview">
      <div style="color:#4A5568;font-size:13px">Loading...</div>
    </div>
  </div>

  <!-- INTER-RATER AGREEMENT -->
  <div class="section">
    <div class="section-title">Inter-rater agreement</div>
    <div class="card">
      <div style="font-size:13px;color:#8896A7;margin-bottom:12px;">
        Runs labeled by multiple users. Disagreements are where users assigned different verdicts to the same run — the most valuable signal for understanding where your labeling standard is ambiguous.
      </div>
      <div id="inter-rater-stats" style="color:#4A5568;font-size:13px">Loading...</div>
      <div id="inter-rater-content"></div>
    </div>
  </div>

</div>

<script>
// Load labeling overview
fetch("/api/admin/labeling-overview").then(function(r){return r.json();}).then(function(users) {
  var el = document.getElementById("labeling-overview");
  if (!users.length) { el.innerHTML = "<div class='empty'>No users yet.</div>"; return; }
  el.innerHTML = '<table><thead><tr><th>User</th><th>Role</th><th>Total labels</th><th>Pass</th><th>Fail</th><th>Last active</th></tr></thead><tbody>' +
    users.map(function(u) {
      var pct = u.total_labels ? Math.round(u.passes/u.total_labels*100) : 0;
      return '<tr>' +
        '<td><strong>' + u.username + '</strong></td>' +
        '<td><span class="badge ' + u.role + '">' + u.role + '</span></td>' +
        '<td>' + u.total_labels + '</td>' +
        '<td><span class="badge pass">' + u.passes + '</span></td>' +
        '<td><span class="badge fail">' + u.fails + '</span></td>' +
        '<td style="color:#4A5568;font-size:12px">' + (u.last_active ? u.last_active.slice(0,10) : "—") + '</td>' +
      '</tr>';
    }).join("") + '</tbody></table>';
});

// Load inter-rater
fetch("/api/admin/inter-rater").then(function(r){return r.json();}).then(function(data) {
  var statsEl = document.getElementById("inter-rater-stats");
  var contentEl = document.getElementById("inter-rater-content");

  if (data.total_multi_labeled === 0) {
    statsEl.innerHTML = "<div class='empty'>No runs have been labeled by multiple users yet. Invite another labeler and have them review some runs to see inter-rater agreement here.</div>";
    return;
  }

  statsEl.innerHTML =
    '<div class="stat-row">' +
    '<div class="stat-mini"><div class="stat-mini-val">' + data.total_multi_labeled + '</div><div class="stat-mini-label">Multi-labeled runs</div></div>' +
    '<div class="stat-mini"><div class="stat-mini-val" style="color:#4ade80">' + data.agreement_rate + '%</div><div class="stat-mini-label">Agreement rate</div></div>' +
    '<div class="stat-mini"><div class="stat-mini-val" style="color:#f87171">' + data.disagreements.length + '</div><div class="stat-mini-label">Disagreements</div></div>' +
    '<div class="stat-mini"><div class="stat-mini-val" style="color:#4ade80">' + data.agreements.length + '</div><div class="stat-mini-label">Agreements</div></div>' +
    '</div>';

  // Tabs
  var activeTab = "disagreements";
  function renderTab() {
    var items = activeTab === "disagreements" ? data.disagreements : data.agreements;
    if (!items.length) {
      contentEl.innerHTML = '<div class="empty">No ' + activeTab + ' found.</div>';
      return;
    }
    contentEl.innerHTML = '<div class="tabs">' +
      '<button class="tab-btn' + (activeTab==="disagreements"?" active":"") + '" onclick="switchTab('\'disagreements\'')">' +
        'Disagreements (' + data.disagreements.length + ')</button>' +
      '<button class="tab-btn' + (activeTab==="agreements"?" active":"") + '" onclick="switchTab('\'agreements\'')">' +
        'Agreements (' + data.agreements.length + ')</button>' +
    '</div>' +
    items.map(function(run) {
      var labels = run.labels.map(function(l) {
        var verdictColor = l.label === "pass" ? "#4ade80" : "#f87171";
        return '<div class="inter-label-card">' +
          '<div class="inter-label-user">' + l.username + '</div>' +
          '<div class="inter-label-verdict" style="color:' + verdictColor + '">' + l.label.toUpperCase() + '</div>' +
          (l.failure_category ? '<div class="inter-label-cat">' + l.failure_category + '</div>' : "") +
          (l.notes ? '<div class="inter-label-notes">' + l.notes + '</div>' : "") +
        '</div>';
      }).join("");
      return '<div class="inter-run">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">' +
          '<span class="badge ' + (run.agreement ? "agree" : "disagree") + '">' + (run.agreement ? "AGREE" : "DISAGREE") + '</span>' +
          '<span style="font-family:monospace;font-size:11px;color:#4A5568">' + run.run_id + '</span>' +
        '</div>' +
        '<div class="inter-task">' + run.task + '</div>' +
        (run.answer ? '<div class="inter-answer">Answer: ' + run.answer + '</div>' : "") +
        '<div class="inter-labels">' + labels + '</div>' +
      '</div>';
    }).join("");
  }

  window.switchTab = function(tab) { activeTab = tab; renderTab(); };
  renderTab();
});
</script>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_username(get_conn(), username)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/admin")
@admin_required
def admin_page():
    conn = get_conn()
    users = list_users(conn)
    for u in users:
        count = conn.execute("SELECT COUNT(*) FROM labels WHERE user_id = ?",
                             (u["user_id"],)).fetchone()[0]
        u["label_count"] = count
    return render_template_string(ADMIN_PAGE, users=users, message=request.args.get("msg"))


@app.route("/admin/create-user", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "labeler")
    if not username or not password:
        return redirect(url_for("admin_page", msg="Username and password required"))
    try:
        conn = get_conn()
        pw_hash = generate_password_hash(password)
        create_user(conn, username, pw_hash, role)
        return redirect(url_for("admin_page", msg=f"Created user '{username}'"))
    except Exception as e:
        return redirect(url_for("admin_page", msg=f"Error: {e}"))


@app.route("/admin/update-user", methods=["POST"])
@admin_required
def admin_update_user():
    user_id = request.form.get("user_id")
    action = request.form.get("action")
    conn = get_conn()
    try:
        if action == "delete":
            conn.execute("DELETE FROM labels WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
            return redirect(url_for("admin_page", msg="User deleted"))
        elif action == "make_admin":
            conn.execute("UPDATE users SET role = 'admin' WHERE user_id = ?", (user_id,))
            conn.commit()
            return redirect(url_for("admin_page", msg="Role updated to admin"))
        elif action == "make_labeler":
            conn.execute("UPDATE users SET role = 'labeler' WHERE user_id = ?", (user_id,))
            conn.commit()
            return redirect(url_for("admin_page", msg="Role updated to labeler"))
        elif action == "reset_password":
            new_pw = request.form.get("new_password", "")
            if not new_pw:
                return redirect(url_for("admin_page", msg="Password cannot be empty"))
            conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                        (generate_password_hash(new_pw), user_id))
            conn.commit()
            return redirect(url_for("admin_page", msg="Password reset"))
    except Exception as e:
        return redirect(url_for("admin_page", msg=f"Error: {e}"))
    return redirect(url_for("admin_page"))


@app.route("/api/admin/labeling-overview")
@admin_required
def api_labeling_overview():
    conn = get_conn()
    users = list_users(conn)
    result = []
    for u in users:
        uid = u["user_id"]
        total = conn.execute("SELECT COUNT(*) FROM labels WHERE user_id = ?", (uid,)).fetchone()[0]
        passes = conn.execute("SELECT COUNT(*) FROM labels WHERE user_id = ? AND label = 'pass'", (uid,)).fetchone()[0]
        fails = conn.execute("SELECT COUNT(*) FROM labels WHERE user_id = ? AND label = 'fail'", (uid,)).fetchone()[0]
        last = conn.execute("SELECT MAX(created_at) FROM labels WHERE user_id = ?", (uid,)).fetchone()[0]
        result.append({
            "user_id": uid,
            "username": u["username"],
            "role": u["role"],
            "total_labels": total,
            "passes": passes,
            "fails": fails,
            "last_active": last,
        })
    return jsonify(result)


@app.route("/api/admin/inter-rater")
@admin_required
def api_inter_rater():
    conn = get_conn()
    # Find runs labeled by 2+ users with different verdicts
    rows = conn.execute("""
        SELECT l.run_id, r.task, r.final_answer,
               l.user_id, u.username, l.label, l.failure_category, l.notes
        FROM labels l
        JOIN runs r ON l.run_id = r.run_id
        JOIN users u ON l.user_id = u.user_id
        ORDER BY l.run_id, l.user_id
    """).fetchall()

    # Group by run_id
    from collections import defaultdict
    by_run = defaultdict(list)
    task_by_run = {}
    answer_by_run = {}
    for row in rows:
        run_id, task, answer, uid, username, label, cat, notes = row
        by_run[run_id].append({
            "user_id": uid,
            "username": username,
            "label": label,
            "failure_category": cat,
            "notes": notes or ""
        })
        task_by_run[run_id] = task
        answer_by_run[run_id] = (answer or "")[:120]

    # Find disagreements: runs with 2+ labels where verdicts differ
    disagreements = []
    agreements = []
    for run_id, labels in by_run.items():
        if len(labels) < 2:
            continue
        verdicts = set(l["label"] for l in labels)
        entry = {
            "run_id": run_id,
            "task": task_by_run[run_id],
            "answer": answer_by_run[run_id],
            "labels": labels,
            "agreement": len(verdicts) == 1
        }
        if len(verdicts) > 1:
            disagreements.append(entry)
        else:
            agreements.append(entry)

    return jsonify({
        "disagreements": disagreements,
        "agreements": agreements,
        "total_multi_labeled": len(disagreements) + len(agreements),
        "agreement_rate": round(len(agreements) / max(1, len(disagreements) + len(agreements)) * 100, 1)
    })


# ---------------------------------------------------------------------------
# Main app routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return HTML_PAGE


@app.route("/api/data")
@login_required
def api_data():
    return jsonify(build_payload())


@app.route("/api/label", methods=["POST"])
@login_required
def api_label():
    data = request.get_json()
    run_id = data.get("run_id")
    label = data.get("label")
    failure_category = data.get("failure_category") or None
    notes = data.get("notes", "")
    task = data.get("task", "")

    if not run_id or label not in ("pass", "fail"):
        return jsonify({"error": "run_id and label (pass/fail) required"}), 400
    if label == "pass":
        failure_category = None

    user = current_user()
    upsert_label(get_conn(), run_id, user["user_id"], label, failure_category, notes, task)
    return jsonify({"ok": True, "run_id": run_id, "label": label, "username": user["username"]})


@app.route("/api/label/<run_id>", methods=["DELETE"])
@login_required
def api_delete_label(run_id):
    user = current_user()
    deleted = delete_label(get_conn(), run_id, user["user_id"])
    if deleted:
        return jsonify({"ok": True, "deleted": run_id})
    return jsonify({"error": "not found"}), 404


@app.route("/api/questions", methods=["GET"])
@login_required
def api_get_questions():
    return jsonify(load_questions())


@app.route("/api/questions", methods=["POST"])
@login_required
def api_save_questions():
    questions = request.get_json()
    if not isinstance(questions, list):
        return jsonify({"error": "expected list"}), 400
    questions = [q.strip() for q in questions if q.strip()]
    save_questions(questions)
    return jsonify({"ok": True, "count": len(questions)})


@app.route("/api/run_batch", methods=["POST"])
@login_required
def api_run_batch():
    global _batch_running
    if _batch_running:
        return jsonify({"error": "batch already running"}), 409

    data = request.get_json() or {}
    questions = data.get("questions", load_questions())

    def run():
        global _batch_running
        _batch_running = True
        try:
            # Re-import and reinitialize tracer inside the thread to avoid
            # SQLite "created in thread X, used in thread Y" errors
            import agent as _agent
            from tracing import Tracer as _Tracer
            _agent.tracer = _Tracer()
            from agent import run_agent
            group_id = f"batch_{int(time.time())}"
            _batch_queue.put({"type": "start", "total": len(questions), "group_id": group_id})
            for i, question in enumerate(questions, 1):
                _batch_queue.put({"type": "progress", "i": i, "question": question})
                try:
                    run_id, answer = run_agent(question, group_id=group_id)
                    _batch_queue.put({"type": "result", "i": i, "run_id": run_id,
                                      "question": question, "answer": answer})
                except Exception as e:
                    _batch_queue.put({"type": "error", "i": i, "question": question, "error": str(e)})
            _batch_queue.put({"type": "done", "group_id": group_id})
        finally:
            _batch_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/batch_stream")
@login_required
def api_batch_stream():
    def generate():
        while True:
            try:
                msg = _batch_queue.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error") and _batch_queue.empty():
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/batch_status")
@login_required
def api_batch_status():
    return jsonify({"running": _batch_running})


@app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    try:
        cfg = json.load(open("config.json"))
    except Exception:
        cfg = {"judge_model": "claude-haiku-4-5-20251001", "agent_model": "claude-sonnet-4-6"}
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@admin_required
def api_set_config():
    data = request.get_json() or {}
    try:
        try:
            cfg = json.load(open("config.json"))
        except Exception:
            cfg = {}
        cfg.update(data)
        json.dump(cfg, open("config.json", "w"), indent=2)
        return jsonify({"ok": True, "config": cfg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tools_stream")
@login_required
def api_tools_stream():
    def generate():
        while True:
            try:
                msg = _tools_queue.get(timeout=60)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/run_calibrate", methods=["POST"])
@login_required
def api_run_calibrate():
    def run():
        try:
            import subprocess, sys, re
            from tracing import Tracer, load_all_labels
            from judge import judge_run
            _tools_queue.put({"type": "calibrate_start"})
            tracer = Tracer()
            labels = load_all_labels(tracer.conn)
            if not labels:
                _tools_queue.put({"type": "error", "msg": "No labels found. Add some labels first."})
                return
            results = []
            for run_id, label_data in labels.items():
                try:
                    verdict = judge_run(run_id, tracer)
                    human = label_data.get("label")
                    results.append({
                        "run_id": run_id,
                        "human": human,
                        "judge": verdict.verdict,
                        "match": human == verdict.verdict,
                    })
                except Exception:
                    pass
            n = len(results)
            if n == 0:
                _tools_queue.put({"type": "error", "msg": "No runs could be scored."})
                return
            matches = sum(1 for r in results if r["match"])
            raw = matches / n
            p_pass_h = sum(1 for r in results if r["human"] == "pass") / n
            p_pass_j = sum(1 for r in results if r["judge"] == "pass") / n
            p_e = p_pass_h * p_pass_j + (1 - p_pass_h) * (1 - p_pass_j)
            kappa = round((raw - p_e) / (1 - p_e), 3) if (1 - p_e) > 0 else 0
            mismatches = [r for r in results if not r["match"]]
            _tools_queue.put({
                "type": "done",
                "tool": "calibrate",
                "n": n,
                "raw": round(raw * 100, 1),
                "kappa": kappa,
                "mismatches": len(mismatches),
            })
        except Exception as e:
            import traceback
            _tools_queue.put({"type": "error", "msg": str(e) + "\n" + traceback.format_exc()[-300:]})
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/run_consistency", methods=["POST"])
@login_required
def api_run_consistency():
    data = request.get_json() or {}
    question = data.get("question", "").strip()
    n_runs = int(data.get("n_runs", 5))
    if not question:
        return jsonify({"error": "question required"}), 400
    def run():
        try:
            import agent as _agent
            from tracing import Tracer as _Tracer
            _agent.tracer = _Tracer()
            from consistency_check import check_consistency
            _tools_queue.put({"type": "consistency_start", "question": question, "n_runs": n_runs})
            result = check_consistency(question, n_runs)
            # Save result
            import time as _time
            path = f"consistency_{int(_time.time())}.json"
            with open(path, "w") as f:
                json.dump(result, f, indent=2)
            _tools_queue.put({"type": "done", "tool": "consistency", "result": result, "saved_to": path})
        except Exception as e:
            _tools_queue.put({"type": "error", "msg": str(e)})
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/run_regression", methods=["POST"])
@login_required
def api_run_regression():
    data = request.get_json() or {}
    before_group = data.get("before_group", "")
    after_group = data.get("after_group", "")
    if not before_group or not after_group:
        return jsonify({"error": "before_group and after_group required"}), 400
    def run():
        try:
            from regression_check import regression_report
            from tracing import Tracer
            tracer = Tracer()
            conn = tracer.conn
            before_ids = [r[0] for r in conn.execute(
                "SELECT run_id FROM runs WHERE group_id = ?", (before_group,)).fetchall()]
            after_ids = [r[0] for r in conn.execute(
                "SELECT run_id FROM runs WHERE group_id = ?", (after_group,)).fetchall()]
            if not before_ids or not after_ids:
                _tools_queue.put({"type": "error", "msg": f"No runs found for one or both groups."})
                return
            _tools_queue.put({"type": "regression_start",
                              "before": before_group, "after": after_group,
                              "n_before": len(before_ids), "n_after": len(after_ids)})
            result = regression_report(before_ids, after_ids)
            import time as _time
            path = f"regression_{int(_time.time())}.json"
            with open(path, "w") as f:
                json.dump(result, f, indent=2)
            _tools_queue.put({"type": "done", "tool": "regression", "result": result, "saved_to": path})
        except Exception as e:
            _tools_queue.put({"type": "error", "msg": str(e)})
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/users")
@admin_required
def api_users():
    users = list_users(get_conn())
    return jsonify(users)


# ---------------------------------------------------------------------------
# HTML (same as v2 + auth nav additions)
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Calibra</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'IBM Plex Sans',sans-serif;background:#080B0F;color:#E2E8F0;height:100vh;display:flex;overflow:hidden;}

/* SIDEBAR */
.nav{width:220px;flex-shrink:0;background:#0D1117;border-right:1px solid #1A2233;display:flex;flex-direction:column;padding:0;height:100vh;overflow:hidden;}
.nav-title{font-size:15px;font-weight:600;color:#E2E8F0;padding:20px 20px 8px;letter-spacing:-0.3px;display:flex;align-items:center;gap:8px;}
.nav-title::before{content:"";width:8px;height:8px;border-radius:50%;background:#4F7CFF;flex-shrink:0;}
.nav-divider{height:1px;background:#1A2233;margin:8px 16px;}
.nav-section-label{font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.8px;padding:12px 20px 6px;}
.nav-tab{display:flex;align-items:center;gap:10px;padding:9px 20px;cursor:pointer;color:#8896A7;font-size:13px;font-weight:400;border:none;background:none;text-align:left;width:100%;border-radius:0;transition:none;}
.nav-tab:hover{background:#131C2E;color:#C8D6E5;}
.nav-tab.active{background:#131C2E;color:#E2E8F0;font-weight:500;border-left:2px solid #4F7CFF;}
.nav-tab.active .nav-icon{color:#4F7CFF;}
.nav-icon{font-size:15px;width:18px;text-align:center;flex-shrink:0;}
.nav-bottom{margin-top:auto;border-top:1px solid #1A2233;padding:16px 20px;}
.nav-user-name{font-size:13px;font-weight:500;color:#E2E8F0;margin-bottom:2px;}
.nav-user-role{font-size:11px;color:#4A5568;margin-bottom:10px;}
.nav-link{font-size:12px;color:#4F7CFF;text-decoration:none;display:block;margin-bottom:4px;}
.nav-link:hover{color:#7BA3FF;}
.nav-stats-mini{display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap;}
.nav-stat-mini{font-size:11px;color:#8896A7;}
.nav-stat-mini span{font-weight:600;color:#E2E8F0;}

/* MAIN CONTENT */
.main-content{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#080B0F;}
.page{display:none;flex:1;overflow:hidden;}
.page.active{display:flex;}

/* SUMMARY PAGE */
.summary-page{flex-direction:column;overflow-y:auto;padding:28px 32px;gap:20px;}
.page-header{margin-bottom:4px;}
.page-title{font-size:20px;font-weight:600;color:#E2E8F0;margin-bottom:4px;}
.page-subtitle{font-size:13px;color:#8896A7;}
.stat-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;}
.stat-card{background:#0D1117;border:1px solid #1A2233;border-radius:10px;padding:18px 20px;}
.stat-card-label{font-size:11px;font-weight:500;color:#8896A7;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;display:flex;align-items:center;gap:6px;}
.stat-card-icon{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:12px;}
.stat-card-value{font-size:28px;font-weight:600;color:#E2E8F0;letter-spacing:-0.5px;}
.stat-card-sub{font-size:11px;color:#4A5568;margin-top:4px;}
.stat-card.pass .stat-card-value{color:#4ade80;}
.stat-card.fail .stat-card-value{color:#f87171;}
.stat-card.kappa .stat-card-value{color:#818CF8;}
.summary-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.sum-card{background:#0D1117;border:1px solid #1A2233;border-radius:10px;padding:18px 20px;}
.sum-card-title{font-size:11px;font-weight:600;color:#8896A7;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:14px;}
.sum-card.full{grid-column:1/-1;}

/* BAR CHART */
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;cursor:pointer;}
.bar-row:hover .bar-label{color:#E2E8F0;}
.bar-label{width:210px;font-size:12px;color:#C8D6E5;flex-shrink:0;}
.bar-track{flex:1;background:#131C2E;border-radius:3px;height:10px;overflow:hidden;}
.bar-fill{background:#4F7CFF;height:100%;border-radius:3px;}
.bar-count{width:20px;text-align:right;font-size:12px;color:#8896A7;}

/* BADGES */
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:0.2px;}
.badge.pass{background:#0D2E1A;color:#4ade80;border:1px solid #1A4A28;}
.badge.fail{background:#2E0D0D;color:#f87171;border:1px solid #4A1A1A;}
.badge.unlabeled{background:#131C2E;color:#8896A7;border:1px solid #1A2233;}

/* CHECK CARDS */
.check-card{background:#080B0F;border:1px solid #1A2233;border-radius:6px;padding:12px 14px;margin-bottom:8px;}
.check-question{font-size:12px;color:#C8D6E5;margin-bottom:8px;line-height:1.4;}
.check-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.check-claims{margin-top:8px;padding-left:16px;font-size:11px;color:#8896A7;line-height:1.6;}

/* GROUP TABLE */
.group-table{width:100%;font-size:12px;border-collapse:collapse;}
.group-table td{padding:6px 8px;border-bottom:1px solid #1A2233;}

/* LABEL PAGE */
.label-layout{display:flex;width:100%;overflow:hidden;}
.run-list{width:320px;flex-shrink:0;border-right:1px solid #1A2233;overflow-y:auto;display:flex;flex-direction:column;background:#0D1117;}
.run-list-header{padding:12px 16px;border-bottom:1px solid #1A2233;display:flex;gap:8px;flex-shrink:0;background:#0D1117;}
.run-list-header input,.run-list-header select{background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:7px 10px;border-radius:6px;font-size:12px;font-family:inherit;}
.run-list-header input{flex:1;}
.run-item{padding:13px 16px;border-bottom:1px solid #1A2233;cursor:pointer;}
.run-item:hover{background:#0A0F18;}
.run-item.selected{background:#0A0F18;border-left:2px solid #4F7CFF;}
.run-item-task{font-size:12px;color:#C8D6E5;line-height:1.4;margin-bottom:5px;}
.run-item-meta{display:flex;align-items:center;gap:8px;font-size:11px;color:#4A5568;}
.mine-dot{width:5px;height:5px;background:#4F7CFF;border-radius:50%;display:inline-block;}

/* TRACE PANEL */
.trace-panel{flex:1;overflow-y:auto;display:flex;flex-direction:column;background:#080B0F;}
.trace-empty{flex:1;display:flex;align-items:center;justify-content:center;color:#4A5568;font-size:13px;}
.trace-header{padding:18px 24px;border-bottom:1px solid #1A2233;flex-shrink:0;background:#0D1117;}
.trace-task{font-size:15px;font-weight:600;color:#E2E8F0;margin-bottom:6px;line-height:1.4;}
.trace-meta{font-size:11px;color:#8896A7;display:flex;gap:16px;flex-wrap:wrap;}
.trace-body{flex:1;overflow-y:auto;padding:18px 24px;display:flex;gap:18px;}
.trace-steps{flex:1;min-width:0;}
.trace-label-panel{width:260px;flex-shrink:0;}

/* STEPS */
.step-card{background:#0D1117;border:1px solid #1A2233;border-radius:8px;margin-bottom:8px;overflow:hidden;}
.step-header{padding:10px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;}
.step-header:hover{background:#131C2E;}
.step-num{background:#131C2E;color:#8896A7;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;font-family:'IBM Plex Mono',monospace;}
.step-name{font-size:12px;font-weight:500;color:#C8D6E5;flex:1;}
.step-time{font-size:11px;color:#4A5568;font-family:'IBM Plex Mono',monospace;}
.step-error-badge{background:#2E0D0D;color:#f87171;font-size:10px;padding:2px 6px;border-radius:4px;}
.step-body{padding:12px 14px;display:none;border-top:1px solid #1A2233;}
.step-body.open{display:block;}
.step-io{margin-bottom:8px;}
.step-io-label{font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;}
.step-io-content{font-size:11px;font-family:'IBM Plex Mono',monospace;color:#8896A7;background:#080B0F;padding:8px 10px;border-radius:4px;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow-y:auto;line-height:1.5;}
.step-error{font-size:11px;color:#f87171;font-family:'IBM Plex Mono',monospace;margin-top:6px;}

/* FINAL ANSWER */
.final-answer{background:#0D1117;border:1px solid #1A2233;border-radius:8px;padding:14px;margin-bottom:10px;}
.final-answer-label{font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;}
.final-answer-text{font-size:12px;color:#E2E8F0;line-height:1.6;white-space:pre-wrap;max-height:250px;overflow-y:auto;}

/* LABEL PANEL */
.label-panel{background:#0D1117;border:1px solid #1A2233;border-radius:8px;padding:16px;position:sticky;top:0;}
.label-panel-title{font-size:12px;font-weight:600;color:#C8D6E5;margin-bottom:14px;}
.label-verdict-row{display:flex;gap:8px;margin-bottom:14px;}
.btn-pass{flex:1;background:#0D2E1A;color:#4ade80;border:1px solid #1A4A28;padding:10px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;}
.btn-pass:hover,.btn-pass.selected{background:#163D22;border-color:#4ade80;}
.btn-fail{flex:1;background:#2E0D0D;color:#f87171;border:1px solid #4A1A1A;padding:10px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;}
.btn-fail:hover,.btn-fail.selected{background:#3D1616;border-color:#f87171;}
.label-field{margin-bottom:12px;}
.label-field label{display:block;font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px;}
.label-field select,.label-field textarea{width:100%;background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:8px 10px;border-radius:6px;font-size:12px;font-family:inherit;}
.label-field textarea{resize:vertical;min-height:70px;line-height:1.4;}
.btn-save{width:100%;background:#4F7CFF;color:#fff;border:none;padding:10px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:8px;font-family:inherit;}
.btn-save:hover{background:#3D6AEE;}
.btn-remove{width:100%;background:transparent;color:#4A5568;border:1px solid #1A2233;padding:7px;border-radius:6px;font-size:11px;cursor:pointer;font-family:inherit;}
.btn-remove:hover{color:#f87171;border-color:#f87171;}
.current-label{background:#080B0F;border:1px solid #1A2233;border-radius:6px;padding:10px;margin-bottom:14px;}
.current-label-row{display:flex;align-items:center;gap:8px;margin-bottom:4px;}
.current-label-notes{font-size:11px;color:#8896A7;font-style:italic;}

/* RUNS PAGE */
.runs-page{flex-direction:column;overflow:hidden;}
.runs-controls{padding:14px 24px;border-bottom:1px solid #1A2233;display:flex;gap:10px;flex-shrink:0;background:#0D1117;}
.runs-controls input,.runs-controls select{background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:8px 12px;border-radius:6px;font-size:13px;font-family:inherit;}
.runs-controls input{flex:1;}
.runs-table-wrap{flex:1;overflow-y:auto;}
.runs-table{width:100%;border-collapse:collapse;font-size:12px;}
.runs-table th{text-align:left;color:#4A5568;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;padding:10px 16px;border-bottom:1px solid #1A2233;position:sticky;top:0;background:#0D1117;font-weight:600;}
.runs-table td{padding:10px 16px;border-bottom:1px solid #0D1117;vertical-align:middle;}
.runs-table tr.run-row{cursor:pointer;}
.runs-table tr.run-row:hover td{background:#0A0F18;}
.td-task{max-width:260px;color:#C8D6E5;}
.td-answer{max-width:220px;color:#8896A7;}
.td-id{font-family:'IBM Plex Mono',monospace;color:#4A5568;font-size:11px;}
.td-group{font-family:'IBM Plex Mono',monospace;color:#4A5568;font-size:10px;}
.td-runtime{font-family:'IBM Plex Mono',monospace;color:#8896A7;white-space:nowrap;}

/* TEST PAGE */
.test-page{flex-direction:row;overflow:hidden;}
.questions-panel{width:360px;flex-shrink:0;border-right:1px solid #1A2233;display:flex;flex-direction:column;overflow:hidden;background:#0D1117;}
.questions-header{padding:18px 20px;border-bottom:1px solid #1A2233;flex-shrink:0;}
.questions-header h2{font-size:14px;font-weight:600;color:#E2E8F0;margin-bottom:14px;}
.btn-run{width:100%;background:#4F7CFF;color:#fff;border:none;padding:10px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:8px;font-family:inherit;}
.btn-run:hover{background:#3D6AEE;}
.btn-run:disabled{background:#131C2E;color:#4A5568;cursor:not-allowed;}
.btn-add-q{width:100%;background:transparent;color:#8896A7;border:1px dashed #1A2233;padding:8px;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;}
.btn-add-q:hover{border-color:#4F7CFF;color:#4F7CFF;}
.questions-list{flex:1;overflow-y:auto;padding:12px;}
.question-item{background:#080B0F;border:1px solid #1A2233;border-radius:6px;padding:10px 12px;margin-bottom:8px;display:flex;gap:8px;align-items:flex-start;}
.question-item textarea{flex:1;background:transparent;border:none;color:#C8D6E5;font-size:12px;font-family:inherit;resize:none;line-height:1.4;outline:none;min-height:36px;}
.btn-del-q{background:transparent;border:none;color:#4A5568;cursor:pointer;font-size:14px;padding:2px 4px;flex-shrink:0;}
.btn-del-q:hover{color:#f87171;}
.results-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.results-header{padding:18px 24px;border-bottom:1px solid #1A2233;flex-shrink:0;background:#0D1117;}
.results-header h2{font-size:14px;font-weight:600;color:#E2E8F0;}
.results-body{flex:1;overflow-y:auto;padding:18px 24px;}
.result-card{background:#0D1117;border:1px solid #1A2233;border-radius:8px;margin-bottom:12px;overflow:hidden;}
.result-card-header{padding:10px 14px;display:flex;align-items:center;gap:10px;background:#0A0F18;}
.result-num{background:#131C2E;color:#8896A7;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;}
.result-question{font-size:12px;color:#8896A7;flex:1;}
.result-run-id{font-size:11px;font-family:'IBM Plex Mono',monospace;color:#4A5568;}
.result-answer{padding:12px 14px;font-size:12px;color:#C8D6E5;border-top:1px solid #1A2233;line-height:1.5;}
.result-pending{padding:10px 14px;font-size:12px;color:#4A5568;border-top:1px solid #1A2233;display:flex;align-items:center;gap:8px;}
.run-status-bar{padding:10px 24px;background:#0D1117;border-top:1px solid #1A2233;font-size:12px;color:#8896A7;display:none;flex-shrink:0;}
.run-status-bar.visible{display:block;}

/* TOOLS PAGE */
.tools-card{background:#0D1117;border:1px solid #1A2233;border-radius:10px;padding:20px 24px;}
.tools-card-title{font-size:14px;font-weight:600;color:#E2E8F0;margin-bottom:4px;}
.tools-card-desc{font-size:12px;color:#8896A7;margin-bottom:16px;}
.tools-field label{font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;display:block;margin-bottom:5px;}
.tools-field select,.tools-field input{background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:8px 10px;border-radius:6px;font-size:12px;font-family:inherit;}
.btn-tool{background:#4F7CFF;color:#fff;border:none;padding:10px 20px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;white-space:nowrap;}
.btn-tool:hover{background:#3D6AEE;}
.btn-tool:disabled{background:#131C2E;color:#4A5568;cursor:not-allowed;}
.tool-result{margin-top:14px;display:none;background:#080B0F;border:1px solid #1A2233;border-radius:6px;padding:12px 16px;font-size:13px;color:#C8D6E5;}

/* SPINNER */
.stat-delta{font-size:11px;margin-top:5px;}
.stat-delta.up{color:#4ade80;}
.stat-delta.down{color:#f87171;}
.stat-delta.neutral{color:#4A5568;}
.spinner{width:12px;height:12px;border:2px solid #1A2233;border-top-color:#4F7CFF;border-radius:50%;animation:spin 0.8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}

/* TOAST */
.toast{position:fixed;bottom:24px;right:24px;background:#0D2E1A;color:#4ade80;padding:10px 16px;border-radius:6px;font-size:13px;display:none;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,0.5);}
.toast.error{background:#2E0D0D;color:#f87171;}

/* SCROLLBAR */
::-webkit-scrollbar{width:5px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:#1A2233;border-radius:3px;}
::-webkit-scrollbar-thumb:hover{background:#263352;}
</style>

</head>
<body>

<div class="nav">
  <div class="nav-title"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600" width="32" height="32" role="img" aria-label="Calibra logo"><defs><linearGradient id="calibraGradient" x1="12%" y1="8%" x2="88%" y2="92%"><stop offset="0%" stop-color="#42B8FF"/><stop offset="48%" stop-color="#6E76FF"/><stop offset="100%" stop-color="#D64FFF"/></linearGradient></defs><path d="M 455 130 A 220 220 0 1 0 455 470" fill="none" stroke="url(#calibraGradient)" stroke-width="74" stroke-linecap="round"/><circle cx="182" cy="300" r="18" fill="url(#calibraGradient)"/><rect x="222" y="260" width="40" height="80" rx="20" fill="url(#calibraGradient)"/><rect x="287" y="215" width="40" height="170" rx="20" fill="url(#calibraGradient)"/><rect x="352" y="155" width="40" height="290" rx="20" fill="url(#calibraGradient)"/><rect x="417" y="225" width="40" height="150" rx="20" fill="url(#calibraGradient)"/><circle cx="495" cy="300" r="25" fill="url(#calibraGradient)"/></svg><span style="background:linear-gradient(135deg,#42B8FF,#D64FFF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;">Calibra</span></div>
  <div class="nav-divider"></div>
  <div class="nav-section-label">Navigation</div>
  <button class="nav-tab active" data-tab="summary"><span class="nav-icon">◈</span>Summary</button>
  <button class="nav-tab" data-tab="label"><span class="nav-icon">◎</span>Label</button>
  <button class="nav-tab" data-tab="runs"><span class="nav-icon">≡</span>All Runs</button>
  <button class="nav-tab" data-tab="test"><span class="nav-icon">▷</span>Test</button>
  <button class="nav-tab" data-tab="tools"><span class="nav-icon">⚙</span>Tools</button>
  <div class="nav-bottom">
    <div class="nav-stats-mini">
      <span class="nav-stat-mini">Total <span id="nav-total">-</span></span>
      <span class="nav-stat-mini" style="color:#4ade80">Pass <span id="nav-pass">-</span></span>
      <span class="nav-stat-mini" style="color:#f87171">Fail <span id="nav-fail">-</span></span>
    </div>
    <div id="nav-kappa-stat" style="display:none;margin-bottom:10px;font-size:11px;color:#8896A7;">Kappa <span id="nav-kappa-val" style="font-weight:600;color:#818CF8">-</span></div>
    <div class="nav-user-name" id="nav-username">...</div>
    <div class="nav-user-role" id="nav-role"></div>
    <span id="admin-link" style="display:none"><a class="nav-link" href="/admin">Admin panel</a></span>
    <a class="nav-link" href="/logout">Sign out</a>
  </div>
</div>

<div class="main-content">

<!-- SUMMARY PAGE -->
<div class="page summary-page active" id="page-summary">
  <div class="page-header">
    <div class="page-title">Summary</div>
    <div class="page-subtitle">Overview of your evaluation runs and performance.</div>
  </div>
  <div class="stat-cards">
    <div class="stat-card">
      <div class="stat-card-label">
        <span class="stat-card-icon" style="background:#1A2233">&#128202;</span>
        Total Runs
      </div>
      <div class="stat-card-value" id="nav-unlabeled-dummy" style="display:none"></div>
      <div class="stat-card-value" id="stat-total-val">-</div>
      <div class="stat-card-sub" id="stat-total-delta">across all batches</div>
    </div>
    <div class="stat-card pass">
      <div class="stat-card-label">
        <span class="stat-card-icon" style="background:#0D2E1A">&#10003;</span>
        Pass Rate
      </div>
      <div class="stat-card-value" id="stat-pass-val">-</div>
      <div class="stat-card-sub" id="stat-pass-pct"></div>
      <div class="stat-delta" id="stat-pass-delta"></div>
    </div>
    <div class="stat-card fail">
      <div class="stat-card-label">
        <span class="stat-card-icon" style="background:#2E0D0D">&#10007;</span>
        Fail Rate
      </div>
      <div class="stat-card-value" id="stat-fail-val">-</div>
      <div class="stat-card-sub" id="stat-fail-pct"></div>
      <div class="stat-delta" id="stat-fail-delta"></div>
    </div>
    <div class="stat-card kappa" id="kappa-card" style="display:none">
      <div class="stat-card-label">
        <span class="stat-card-icon" style="background:#1E1B4B">&#954;</span>
        Cohen's Kappa
      </div>
      <div class="stat-card-value" id="stat-kappa-val">-</div>
      <div class="stat-card-sub" id="stat-kappa-desc"></div>
    </div>
    <div class="stat-card" id="unlabeled-card">
      <div class="stat-card-label">
        <span class="stat-card-icon" style="background:#1A2233">&#9711;</span>
        Unlabeled
      </div>
      <div class="stat-card-value" id="nav-unlabeled">-</div>
      <div class="stat-card-sub" id="stat-unlabeled-delta">awaiting review</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
    <div class="sum-card" style="padding:16px 20px;">
      <div class="sum-card-title">Pass rate over time</div>
      <div style="position:relative;width:100%;height:110px;">
        <canvas id="pass-rate-chart"></canvas>
      </div>
      <div id="pass-rate-empty" style="color:#4A5568;font-size:12px;text-align:center;padding:8px 0;display:none">Run at least 2 labeled batches to see the trend.</div>
    </div>
    <div class="sum-card">
      <div class="sum-card-title">Failure categories</div>
      <div id="sum-categories"><span style="color:#4A5568;font-size:12px">No labeled failures yet.</span></div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
    <div class="sum-card">
      <div class="sum-card-title collapsible-header" data-target="grp-body" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;">
        Run groups <span class="collapse-arrow" id="grp-arrow">▾</span>
      </div>
      <div id="grp-body">
        <div id="sum-groups"><span style="color:#4A5568;font-size:12px">No groups yet.</span></div>
      </div>
    </div>
    <div class="sum-card">
      <div class="sum-card-title collapsible-header" data-target="con-body" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;">
        Consistency history <span class="collapse-arrow" id="con-arrow">▾</span>
      </div>
      <div id="con-body" style="display:none;">
        <div id="sum-consistency"><span style="color:#4A5568;font-size:12px">No consistency checks yet.</span></div>
      </div>
    </div>
  </div>
  <div class="sum-card">
    <div class="sum-card-title collapsible-header" data-target="reg-body" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;">
      Regression history <span class="collapse-arrow" id="reg-arrow">▾</span>
    </div>
    <div id="reg-body" style="display:none;">
      <div id="sum-regression"><span style="color:#4A5568;font-size:12px">No regression checks yet.</span></div>
    </div>
  </div>
</div>
<!-- LABEL PAGE -->
<div class="page" id="page-label">
  <div class="label-layout">
    <div class="run-list">
      <div class="run-list-header">
        <input type="text" id="label-search" placeholder="Search runs...">
        <select id="label-filter">
          <option value="">All</option>
          <option value="unlabeled">Unlabeled</option>
          <option value="pass">Pass</option>
          <option value="fail">Fail</option>
        </select>
      </div>
      <div id="run-items"></div>
    </div>
    <div class="trace-panel" id="trace-panel">
      <div class="trace-empty">Select a run from the list to view its trace</div>
    </div>
  </div>
</div>

<!-- RUNS PAGE -->
<div class="page runs-page" id="page-runs">
  <div class="runs-controls">
    <input type="text" id="runs-search" placeholder="Search by run ID, task, or date...">
    <select id="runs-verdict">
      <option value="">All verdicts</option>
      <option value="pass">Pass</option>
      <option value="fail">Fail</option>
      <option value="unlabeled">Unlabeled</option>
    </select>
    <select id="runs-category"><option value="">All categories</option></select>
  </div>
  <div class="runs-table-wrap">
    <table class="runs-table">
      <thead><tr>
        <th>Task</th><th>Final answer</th><th>Verdict</th>
        <th>Category</th><th>Labelers</th><th>Runtime</th><th>Group</th><th>Run ID</th>
      </tr></thead>
      <tbody id="runs-tbody"></tbody>
    </table>
  </div>
</div>

<!-- TEST PAGE -->
<div class="page test-page" id="page-test">
  <div class="questions-panel">
    <div class="questions-header">
      <h2>Test Questions</h2>
      <div style="margin-bottom:8px;">
        <label style="font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;display:block;margin-bottom:4px;">Batch name (optional)</label>
        <input type="text" id="batch-name-input" placeholder="e.g. after date fix..." style="width:100%;background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:7px 10px;border-radius:6px;font-size:12px;margin-bottom:8px;font-family:inherit;">
      </div>
      <button class="btn-run" id="btn-run-batch">Run Batch</button>
      <button class="btn-add-q" id="btn-add-q">+ Add question</button>
    </div>
    <div class="questions-list" id="questions-list"></div>
  </div>
  <div class="results-panel">
    <div class="results-header"><h2>Results</h2></div>
    <div class="results-body" id="results-body">
      <div style="color:#4A5568;font-size:13px;padding-top:40px;text-align:center;">Click Run Batch to start</div>
    </div>
    <div class="run-status-bar" id="run-status-bar"></div>
  </div>
</div>

<!-- TOOLS PAGE -->
<div class="page" id="page-tools" style="flex-direction:column;overflow-y:auto;padding:28px 32px;gap:20px;">



  <div class="tools-card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;">
      <div style="display:flex;align-items:flex-start;gap:14px;">
        <div style="width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#1B1B4B,#2D2B6E);border:1px solid #4F7CFF44;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:22px;">&#9733;</div>
        <div>
          <div class="tools-card-title">Calibrate judge</div>
          <div class="tools-card-desc">Re-score all labeled runs. Updates Cohen's kappa.</div>
        </div>
      </div>
      <div style="text-align:right;flex-shrink:0;">
        <button id="btn-calibrate" class="btn-tool">Run Calibrate</button>
        <div style="font-size:11px;color:#4A5568;margin-top:4px;">Takes a few moments</div>
      </div>
    </div>
    <div id="calibrate-result" class="tool-result"></div>
  </div>

  <div class="tools-card">
    <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:16px;">
      <div style="width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#0D2E1A,#163D22);border:1px solid #4ade8033;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:22px;">&#8801;</div>
      <div>
        <div class="tools-card-title">Consistency check</div>
        <div class="tools-card-desc">Run the same question N times and check if the agent gives a stable answer.</div>
      </div>
    </div>
    <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">
      <div class="tools-field" style="flex:1;min-width:300px;">
        <label>Question</label>
        <select id="con-question" style="width:100%;margin-bottom:6px;"></select>
        <input type="text" id="con-custom" placeholder="Or type a custom question..." style="width:100%;">
      </div>
      <div class="tools-field" style="width:80px;">
        <label>Runs</label>
        <input type="number" id="con-n" value="5" min="2" max="10" style="width:100%;">
      </div>
      <button id="btn-consistency" class="btn-tool">Run Check</button>
    </div>
    <div id="consistency-result" class="tool-result"></div>
  </div>

  <div class="tools-card">
    <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:16px;">
      <div style="width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#1A0D2E,#2D1650);border:1px solid #9B59F733;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:22px;">&#9641;</div>
      <div>
        <div class="tools-card-title">Regression check</div>
        <div class="tools-card-desc">Compare pass rates between two run groups using Fisher's exact test.</div>
      </div>
    </div>
    <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">
      <div class="tools-field" style="flex:1;min-width:200px;">
        <label>Before group</label>
        <select id="reg-before" style="width:100%;"></select>
      </div>
      <div class="tools-field" style="flex:1;min-width:200px;">
        <label>After group</label>
        <select id="reg-after" style="width:100%;"></select>
      </div>
      <button id="btn-regression" class="btn-tool">Run Check</button>
    </div>
    <div id="regression-result" class="tool-result"></div>
  </div>


  <div class="tools-card" id="config-card">
    <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:16px;">
      <div style="width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#0D1A2E,#1A2E4A);border:1px solid #4F7CFF33;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:22px;">&#9881;</div>
      <div style="flex:1;">
        <div class="tools-card-title">Model configuration</div>
        <div class="tools-card-desc">Configure which models are used for agent runs and judge evaluation.</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
      <div>
        <div style="font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Agent model</div>
        <select id="cfg-agent" style="width:100%;background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:8px 10px;border-radius:6px;font-size:12px;font-family:inherit;">
          <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
          <option value="claude-opus-4-6">claude-opus-4-6</option>
          <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
        </select>
        <div style="font-size:11px;color:#4A5568;margin-top:4px;">Used for generating agent answers</div>
      </div>
      <div>
        <div style="font-size:10px;font-weight:600;color:#4A5568;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Judge model</div>
        <select id="cfg-judge" style="width:100%;background:#080B0F;border:1px solid #1A2233;color:#E2E8F0;padding:8px 10px;border-radius:6px;font-size:12px;font-family:inherit;">
          <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
          <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
          <option value="claude-opus-4-6">claude-opus-4-6</option>
        </select>
        <div style="font-size:11px;color:#4A5568;margin-top:4px;">Used for scoring runs — best when different from agent</div>
      </div>
    </div>
    <div style="margin-top:12px;display:flex;align-items:center;gap:10px;">
      <button id="btn-save-config" class="btn-tool" style="padding:8px 18px;">Save configuration</button>
      <span id="config-status" style="font-size:12px;color:#4A5568;"></span>
    </div>
    <div id="config-kappa-note" style="margin-top:10px;display:none;background:#080B0F;border:1px solid #1A2233;border-radius:6px;padding:10px 14px;font-size:12px;color:#8896A7;">
      &#9432; After changing the judge model, run Calibrate to measure the new kappa against your human labels.
    </div>
  </div>
</div>

</div><!-- end main-content -->

<div class="toast" id="toast"></div>


<script>


var DATA = null;
var selectedRunId = null;
var currentLabelVerdict = null;
var batchEventSource = null;

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function toast(msg, isError) {
  var t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.style.display = "block";
  clearTimeout(t._timer);
  t._timer = setTimeout(function() { t.style.display = "none"; }, 2500);
}

function refreshData(cb) {
  fetch("/api/data").then(function(r) { return r.json(); }).then(function(d) {
    DATA = d;
    try { if (cb) cb(); else renderAll(); } catch(e) { console.error("render error:", e); }
  }).catch(function(e) { console.error("fetch error:", e); });
}

function renderAll() {
  renderSummary();
  renderTools();
  document.getElementById("nav-total").textContent = DATA.stats.total;
  document.getElementById("nav-pass").textContent = DATA.stats.pass_count;
  document.getElementById("nav-fail").textContent = DATA.stats.fail_count;
  document.getElementById("nav-unlabeled").textContent = DATA.stats.unlabeled;
  if (document.getElementById("stat-total-val")) document.getElementById("stat-total-val").textContent = DATA.stats.total;
  if (document.getElementById("stat-pass-val")) { document.getElementById("stat-pass-val").textContent = DATA.stats.pass_count; if(document.getElementById("stat-pass-pct")) document.getElementById("stat-pass-pct").textContent = DATA.stats.total ? Math.round(DATA.stats.pass_count/DATA.stats.total*100) + "% of runs" : ""; }
  if (document.getElementById("stat-fail-val")) { document.getElementById("stat-fail-val").textContent = DATA.stats.fail_count; if(document.getElementById("stat-fail-pct")) document.getElementById("stat-fail-pct").textContent = DATA.stats.total ? Math.round(DATA.stats.fail_count/DATA.stats.total*100) + "% of runs" : ""; }
  if (DATA.current_user) {
    document.getElementById("nav-username").textContent = DATA.current_user.username;
    if (DATA.current_user.role === "admin") {
      document.getElementById("admin-link").style.display = "inline";
    }
  }
  renderRunList();
  renderRunsTable();
  renderQuestions();
  populateCategoryFilter();
}

// ===== LABEL TAB =====

function renderRunList() {
  var search = document.getElementById("label-search").value.toLowerCase();
  var filter = document.getElementById("label-filter").value;
  var container = document.getElementById("run-items");
  var filtered = DATA.runs.filter(function(r) {
    if (filter && r.verdict !== filter) return false;
    if (search) {
      var h = (r.run_id + " " + r.task + " " + (r.started_at||"")).toLowerCase();
      if (h.indexOf(search) === -1) return false;
    }
    return true;
  });
  container.innerHTML = filtered.map(function(r) {
    var bc = (r.verdict==="pass"||r.verdict==="fail") ? r.verdict : "unlabeled";
    var mineDot = r.labeled_by_me ? '<span class="mine-dot" title="your label"></span>' : "";
    var date = r.started_at ? r.started_at.substring(0,10) : "";
    var labelCount = r.all_labels ? r.all_labels.length : 0;
    var labelInfo = labelCount > 0 ? '<span>' + labelCount + ' label' + (labelCount>1?'s':'') + '</span>' : '';
    return '<div class="run-item' + (r.run_id===selectedRunId?" selected":"") + '" data-id="' + r.run_id + '">' +
      '<div class="run-item-task">' + esc(r.task) + '</div>' +
      '<div class="run-item-meta">' +
        '<span class="badge ' + bc + '">' + bc + '</span>' + mineDot +
        '<span>' + r.run_id + '</span><span>' + date + '</span>' + labelInfo +
      '</div></div>';
  }).join("");
  container.querySelectorAll(".run-item").forEach(function(el) {
    el.addEventListener("click", function() { selectRun(el.dataset.id); });
  });
}

function selectRun(runId) {
  selectedRunId = runId;
  currentLabelVerdict = null;
  renderRunList();
  var run = DATA.runs.find(function(r) { return r.run_id === runId; });
  if (!run) return;
  renderTracePanel(run);
}

function renderTracePanel(run) {
  var panel = document.getElementById("trace-panel");
  var date = run.started_at ? run.started_at.replace("T"," ").substring(0,19) : "";
  var bc = (run.verdict==="pass"||run.verdict==="fail") ? run.verdict : "unlabeled";

  var stepsHtml = run.substeps.map(function(s, idx) {
    return '<div class="step-card">' +
      '<div class="step-header" data-step="' + idx + '">' +
        '<span class="step-num">Step ' + s.step_index + '</span>' +
        '<span class="step-name">' + esc(s.name) + '</span>' +
        '<span class="step-time">' + (s.duration_ms||0) + 'ms</span>' +
        (s.error ? '<span class="step-error-badge">ERROR</span>' : '') +
      '</div>' +
      '<div class="step-body" id="step-body-' + idx + '">' +
        '<div class="step-io"><div class="step-io-label">Input</div>' +
          '<div class="step-io-content">' + esc(formatJson(s.input)) + '</div></div>' +
        '<div class="step-io"><div class="step-io-label">Output</div>' +
          '<div class="step-io-content">' + esc(formatJson(s.output)) + '</div></div>' +
        (s.error ? '<div class="step-error">Error: ' + esc(s.error) + '</div>' : '') +
      '</div></div>';
  }).join("");

  // All labelers section
  var allLabelsHtml = "";
  if (run.all_labels && run.all_labels.length > 0) {
    allLabelsHtml = '<div class="all-labels">' +
      '<div class="all-labels-title">Labels (' + run.all_labels.length + ')</div>' +
      run.all_labels.map(function(l) {
        var lbc = l.label === "pass" ? "pass" : "fail";
        return '<div class="labeler-row">' +
          '<span class="labeler-name">' + esc(l.username) + '</span>' +
          '<span class="badge ' + lbc + '">' + l.label + '</span>' +
          (l.failure_category ? '<span style="font-size:10px;color:#8a8f98">' + esc(l.failure_category) + '</span>' : '') +
        '</div>' +
        (l.notes ? '<div style="font-size:11px;color:#5d6470;margin-left:80px;margin-bottom:4px;font-style:italic">' + esc(l.notes) + '</div>' : '');
      }).join("") +
    '</div>';
  }

  var cats = DATA.failure_categories;
  var catOpts = '<option value="">-- select --</option>' + cats.map(function(c) {
    return '<option value="' + esc(c) + '"' + (run.failure_category===c?" selected":"") + '>' + esc(c) + '</option>';
  }).join("");

  var myLabel = run.all_labels ? run.all_labels.find(function(l) {
    return DATA.current_user && l.user_id === DATA.current_user.user_id;
  }) : null;

  var removeBtn = myLabel ? '<button class="btn-remove" id="btn-remove">Remove my label</button>' : '';

  panel.innerHTML =
    '<div class="trace-header">' +
      '<div class="trace-task">' + esc(run.task) + '</div>' +
      '<div class="trace-meta">' +
        '<span>' + run.run_id + '</span><span>' + date + '</span>' +
        '<span>' + run.substeps.length + ' steps</span>' +
        '<span>' + run.total_runtime_ms + 'ms total</span>' +
        (run.group_id ? '<span>Group: ' + esc(run.group_id) + '</span>' : '') +
      '</div>' +
    '</div>' +
    '<div class="trace-body">' +
      '<div class="trace-steps">' +
        '<div class="final-answer">' +
          '<div class="final-answer-label">Final Answer</div>' +
          '<div class="final-answer-text">' + esc(run.final_answer || "(no answer)") + '</div>' +
        '</div>' +
        (stepsHtml || '<div style="color:#5d6470;font-size:12px">No substeps recorded.</div>') +
      '</div>' +
      '<div class="trace-label-panel">' +
        '<div class="label-panel">' +
          '<div class="label-panel-title">Label this run</div>' +
          allLabelsHtml +
          '<div class="label-verdict-row">' +
             '<button class="btn-pass" id="btn-verdict-pass"' + (myLabel&&myLabel.label==="pass"?' class="selected"':'') + '>Pass</button>' +
             '<button class="btn-fail" id="btn-verdict-fail"' + (myLabel&&myLabel.label==="fail"?' class="selected"':'') + '>Fail</button>' +
          '</div>' +
          '<div class="label-field" id="cat-field" style="display:' + (myLabel&&myLabel.label==="fail"?"block":"none") + '">' +
            '<label>Failure category</label><select id="label-cat">' + catOpts + '</select>' +
          '</div>' +
          '<div class="label-field">' +
            '<label>Notes</label>' +
            '<textarea id="label-notes" placeholder="optional note...">' + esc(myLabel?myLabel.notes:run.notes||"") + '</textarea>' +
          '</div>' +
          '<button class="btn-save" id="btn-save-label">Save label</button>' +
          removeBtn +
        '</div>' +
      '</div>' +
    '</div>';

  panel.querySelectorAll(".step-header").forEach(function(hdr) {
    hdr.addEventListener("click", function() {
      var body = document.getElementById("step-body-" + hdr.dataset.step);
      body.classList.toggle("open");
    });
  });

  document.getElementById("btn-verdict-pass").addEventListener("click", function() {
    currentLabelVerdict = "pass";
    this.classList.add("selected");
    document.getElementById("btn-verdict-fail").classList.remove("selected");
    document.getElementById("cat-field").style.display = "none";
  });
  document.getElementById("btn-verdict-fail").addEventListener("click", function() {
    currentLabelVerdict = "fail";
    this.classList.add("selected");
    document.getElementById("btn-verdict-pass").classList.remove("selected");
    document.getElementById("cat-field").style.display = "block";
  });

  document.getElementById("btn-save-label").addEventListener("click", function() {
    if (!currentLabelVerdict) { toast("Click Pass or Fail first", true); return; }
    var catEl = document.getElementById("label-cat");
    var cat = catEl ? catEl.value : null;
    if (currentLabelVerdict === "fail" && !cat) { toast("Select a failure category", true); return; }
    var notes = document.getElementById("label-notes").value;
    fetch("/api/label", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({run_id: run.run_id, label: currentLabelVerdict, failure_category: cat||null, notes: notes, task: run.task})
    }).then(function(r) { return r.json(); }).then(function(d) {
      if (d.ok) {
        toast("Saved \u2192 " + currentLabelVerdict + " (" + d.username + ")");
        refreshData(function() { renderAll(); selectRun(run.run_id); });
      } else { toast("Error: " + (d.error||"unknown"), true); }
    });
  });

  var removeEl = document.getElementById("btn-remove");
  if (removeEl) {
    removeEl.addEventListener("click", function() {
      fetch("/api/label/" + run.run_id, {method: "DELETE"}).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) { toast("Label removed"); refreshData(function() { renderAll(); selectRun(run.run_id); }); }
      });
    });
  }
}

function formatJson(s) {
  if (!s) return "";
  try { return JSON.stringify(typeof s === "string" ? JSON.parse(s) : s, null, 2); }
  catch(e) { return String(s); }
}

// ===== RUNS TAB =====

function populateCategoryFilter() {
  var sel = document.getElementById("runs-category");
  var current = sel.value;
  sel.innerHTML = '<option value="">All categories</option>';
  Object.keys(DATA.category_counts || {}).forEach(function(cat) {
    var opt = document.createElement("option");
    opt.value = cat; opt.textContent = cat + " (" + DATA.category_counts[cat] + ")";
    if (cat === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

function renderRunsTable() {
  var search = document.getElementById("runs-search").value.toLowerCase();
  var verdict = document.getElementById("runs-verdict").value;
  var category = document.getElementById("runs-category").value;
  var tbody = document.getElementById("runs-tbody");
  var filtered = DATA.runs.filter(function(r) {
    if (verdict && r.verdict !== verdict) return false;
    if (category && r.failure_category !== category) return false;
    if (search) {
      var h = (r.run_id + " " + r.task + " " + (r.started_at||"")).toLowerCase();
      if (h.indexOf(search) === -1) return false;
    }
    return true;
  });
  tbody.innerHTML = filtered.map(function(r) {
    var bc = (r.verdict==="pass"||r.verdict==="fail") ? r.verdict : "unlabeled";
    var ans = (r.final_answer||"").slice(0,80);
    var labelers = (r.all_labels||[]).map(function(l) {
      return '<span style="font-size:10px;color:#8a8f98;margin-right:4px">' + esc(l.username) + ': <span class="badge ' + l.label + '">' + l.label + '</span></span>';
    }).join("");
    return '<tr class="run-row" data-id="' + r.run_id + '">' +
      '<td class="td-task">' + esc(r.task) + '</td>' +
      '<td class="td-answer">' + esc(ans) + (r.final_answer&&r.final_answer.length>80?"...":"") + '</td>' +
      '<td><span class="badge ' + bc + '">' + bc + '</span></td>' +
      '<td style="font-size:11px;color:#8a8f98">' + esc(r.failure_category||"") + '</td>' +
      '<td>' + (labelers||'<span style="color:#5d6470;font-size:11px">none</span>') + '</td>' +
      '<td class="td-runtime">' + r.total_runtime_ms + 'ms</td>' +
      '<td style="font-family:monospace;color:#5d6470;font-size:10px">' + esc(r.group_id||"") + '</td>' +
      '<td class="td-id">' + r.run_id + '</td>' +
    '</tr>';
  }).join("");
  tbody.querySelectorAll(".run-row").forEach(function(row) {
    row.addEventListener("click", function() {
      document.querySelectorAll(".nav-tab").forEach(function(t) { t.classList.remove("active"); });
      document.querySelectorAll(".page").forEach(function(p) { p.classList.remove("active"); });
      document.querySelector('.nav-tab[data-tab="label"]').classList.add("active");
      document.getElementById("page-label").classList.add("active");
      document.getElementById("page-summary").classList.remove("active");
      selectRun(row.dataset.id);
    });
  });
}

// ===== TEST TAB =====

function renderQuestions() {
  var list = document.getElementById("questions-list");
  var questions = DATA.questions || [];
  list.innerHTML = questions.map(function(q, i) {
    return '<div class="question-item" data-idx="' + i + '">' +
      '<textarea rows="2" data-idx="' + i + '">' + esc(q) + '</textarea>' +
      '<button class="btn-del-q" data-idx="' + i + '">&times;</button>' +
    '</div>';
  }).join("");
  list.querySelectorAll(".btn-del-q").forEach(function(btn) {
    btn.addEventListener("click", function() {
      var idx = parseInt(btn.dataset.idx);
      var qs = getQuestionsFromUI(); qs.splice(idx, 1); saveQuestions(qs);
    });
  });
}

function getQuestionsFromUI() {
  var qs = [];
  document.querySelectorAll("#questions-list textarea").forEach(function(ta) {
    var v = ta.value.trim(); if (v) qs.push(v);
  });
  return qs;
}

function saveQuestions(questions) {
  fetch("/api/questions", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(questions)})
    .then(function(r){return r.json();}).then(function(){refreshData();});
}

// Save config
document.getElementById("btn-save-config").addEventListener("click", function() {
  var agentModel = document.getElementById("cfg-agent").value;
  var judgeModel = document.getElementById("cfg-judge").value;
  var statusEl = document.getElementById("config-status");
  var noteEl = document.getElementById("config-kappa-note");
  fetch("/api/config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({agent_model: agentModel, judge_model: judgeModel})
  }).then(function(r){return r.json();}).then(function(d) {
    if (d.ok) {
      statusEl.textContent = "Saved ✔";
      statusEl.style.color = "#4ade80";
      if (noteEl) noteEl.style.display = "block";
      setTimeout(function() { statusEl.textContent = ""; }, 3000);
    } else {
      statusEl.textContent = "Error: " + (d.error || "unknown");
      statusEl.style.color = "#f87171";
    }
  });
});

document.getElementById("btn-add-q").addEventListener("click", function() {
  var list = document.getElementById("questions-list");
  var idx = list.querySelectorAll(".question-item").length;
  var item = document.createElement("div");
  item.className = "question-item"; item.dataset.idx = idx;
  item.innerHTML = '<textarea rows="2" placeholder="Type your question here..."></textarea>' +
    '<button class="btn-del-q">&times;</button>';
  item.querySelector(".btn-del-q").addEventListener("click", function() {
    item.remove(); saveQuestions(getQuestionsFromUI());
  });
  list.appendChild(item);
  item.querySelector("textarea").focus();
});

document.getElementById("btn-run-batch").addEventListener("click", function() {
  var btn = this;
  var questions = getQuestionsFromUI().filter(function(q){return q.trim();});
  if (!questions.length) { toast("No questions to run", true); return; }
  fetch("/api/questions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(questions)})
  .then(function(){
    return fetch("/api/run_batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({questions:questions,batch_name:document.getElementById("batch-name-input").value||""})});
  }).then(function(r){return r.json();}).then(function(d){
    if (!d.ok){toast(d.error||"Failed",true);return;}
    btn.disabled=true; btn.textContent="Running...";
    var rb=document.getElementById("results-body");
    var sb=document.getElementById("run-status-bar");
    sb.classList.add("visible"); rb.innerHTML="";
    if(batchEventSource)batchEventSource.close();
    batchEventSource=new EventSource("/api/batch_stream");
    batchEventSource.onmessage=function(e){
      var msg=JSON.parse(e.data);
      if(msg.type==="ping")return;
      if(msg.type==="start"){
        sb.textContent="Starting "+msg.group_id+" ("+msg.total+" questions)...";
        questions.forEach(function(q,i){
          var card=document.createElement("div");card.className="result-card";card.id="result-card-"+(i+1);
          card.innerHTML='<div class="result-card-header"><span class="result-num">'+(i+1)+'</span><span class="result-question">'+esc(q)+'</span></div><div class="result-pending"><div class="spinner"></div>Waiting...</div>';
          rb.appendChild(card);
        });
      }
      if(msg.type==="progress"){
        sb.textContent="Running "+msg.i+"/"+questions.length+": "+msg.question.substring(0,60)+"...";
        var c=document.getElementById("result-card-"+msg.i);
        if(c)c.querySelector(".result-pending").innerHTML='<div class="spinner"></div>Running...';
      }
      if(msg.type==="result"){
        var c=document.getElementById("result-card-"+msg.i);
        if(c){
          var p=c.querySelector(".result-pending");if(p)p.remove();
          var ad=document.createElement("div");ad.className="result-answer";ad.textContent=msg.answer;c.appendChild(ad);
          var rs=document.createElement("span");rs.className="result-run-id";rs.textContent=msg.run_id;
          c.querySelector(".result-card-header").appendChild(rs);
        }
      }
      if(msg.type==="error"){
        var c=document.getElementById("result-card-"+msg.i);
        if(c){var p=c.querySelector(".result-pending");if(p)p.innerHTML='<span style="color:#f87171">Error: '+esc(msg.error)+'</span>';}
      }
      if(msg.type==="done"){
        sb.textContent="Batch complete! Group: "+msg.group_id;
        btn.disabled=false;btn.textContent="Run Batch";
        batchEventSource.close();refreshData();toast("Batch complete \u2014 "+questions.length+" runs");
      }
    };
    batchEventSource.onerror=function(){btn.disabled=false;btn.textContent="Run Batch";sb.textContent="Connection lost.";batchEventSource.close();};
  });
});

document.getElementById("questions-list").addEventListener("blur",function(e){
  if(e.target.tagName==="TEXTAREA")saveQuestions(getQuestionsFromUI());
},true);

var _passRateChart = null;

function renderPassRateChart() {
  if (!DATA) return;
  var runs = DATA.runs || [];
  var batchGroups = {};
  runs.forEach(function(r) {
    if (!r.group_id || r.group_id.indexOf("batch_") !== 0) return;
    if (!batchGroups[r.group_id]) batchGroups[r.group_id] = {pass:0, fail:0, ts:0};
    var m = r.group_id.match(/_(\d+)$/);
    if (m) batchGroups[r.group_id].ts = parseInt(m[1]);
    if (r.verdict === "pass") batchGroups[r.group_id].pass++;
    else if (r.verdict === "fail") batchGroups[r.group_id].fail++;
  });
  var sorted = Object.entries(batchGroups)
    .filter(function(e) { return e[1].pass + e[1].fail > 0; })
    .sort(function(a,b) { return a[1].ts - b[1].ts; });
  var canvas = document.getElementById("pass-rate-chart");
  var emptyEl = document.getElementById("pass-rate-empty");
  if (!canvas) return;
  if (sorted.length < 2) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.style.display = "block";
    if (_passRateChart) { _passRateChart.destroy(); _passRateChart = null; }
    return;
  }
  canvas.style.display = "block";
  if (emptyEl) emptyEl.style.display = "none";
  var labels = sorted.map(function(e) {
    var g = e[0];
    var m = g.match(/_(\d{10})$/);
    var dt = m ? new Date(parseInt(m[1])*1000) : null;
    var name = g.replace(/^batch_/, "").replace(/_\d{10}$/, "").replace(/_/g, " ").trim();
    var dateStr = dt ? dt.toISOString().slice(5,10) : "";
    if (name && name.length > 2 && name !== dateStr) return name;
    return dateStr || g.slice(-6);
  });
  var values = sorted.map(function(e) {
    var d = e[1];
    return Math.round((d.pass / (d.pass + d.fail)) * 100);
  });
  if (values.length >= 2) {
    var latest = values[values.length-1];
    var prev = values[values.length-2];
    var diff = latest - prev;
    var sign = diff > 0 ? "+" : "";
    var passD = document.getElementById("stat-pass-delta");
    var failD = document.getElementById("stat-fail-delta");
    if (passD) { passD.textContent = sign + diff + "pp vs prev batch"; passD.className = "stat-delta " + (diff > 0 ? "up" : diff < 0 ? "down" : "neutral"); }
    if (failD) { failD.textContent = (diff < 0 ? "+" : diff > 0 ? "-" : "") + Math.abs(diff) + "pp vs prev batch"; failD.className = "stat-delta " + (diff < 0 ? "up" : diff > 0 ? "down" : "neutral"); }
  }
  if (_passRateChart) _passRateChart.destroy();
  _passRateChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: labels,
      datasets: [{
        label: "Pass rate",
        data: values,
        borderColor: "#4F7CFF",
        backgroundColor: "rgba(79,124,255,0.08)",
        pointBackgroundColor: "#4F7CFF",
        pointBorderColor: "#0D1117",
        pointBorderWidth: 2,
        pointRadius: 5,
        pointHoverRadius: 7,
        tension: 0.35,
        fill: true,
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#0D1117",
          borderColor: "#1A2233",
          borderWidth: 1,
          titleColor: "#E2E8F0",
          bodyColor: "#8896A7",
          padding: 10,
          callbacks: { label: function(ctx) { return ctx.parsed.y + "% pass rate"; } }
        }
      },
      layout: { padding: { top: 2, bottom: 0, left: 0, right: 4 } },
      scales: {
        x: { grid: { color: "#1A2233" }, ticks: { color: "#8896A7", font: { size: 10 }, maxTicksLimit: 5 } },
        y: { min: 0, max: 100, grid: { color: "#1A2233" }, ticks: { color: "#8896A7", font: { size: 10 }, maxTicksLimit: 4, callback: function(v) { return v + "%"; } } }
      }
    }
  });
}

function renderSummary() {
  if (!DATA) return;
  var cats = Object.entries(DATA.category_counts || {});
  var catEl = document.getElementById("sum-categories");
  if (cats.length === 0) {
    catEl.innerHTML = "<span style='color:#5d6470;font-size:12px'>No labeled failures yet.</span>";
  } else {
    var maxC = Math.max.apply(null, cats.map(function(e){return e[1];}));
    catEl.innerHTML = cats.map(function(e) {
      return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;cursor:pointer" data-category="' + esc(e[0]) + '">' +
        '<span style="width:200px;color:#c5c8cc;font-size:11px;flex-shrink:0">' + esc(e[0]) + '</span>' +
        '<div style="flex:1;background:#1e2029;border-radius:4px;height:12px;overflow:hidden"><div style="background:#f87171;height:100%;border-radius:4px;width:' + ((e[1]/maxC)*100) + '%"></div></div>' +
        '<span style="width:20px;text-align:right;color:#8a8f98;font-size:11px">' + e[1] + '</span>' +
      '</div>';
    }).join("");
    catEl.querySelectorAll("[data-category]").forEach(function(row) {
      row.addEventListener("click", function() {
        categoryFilter = row.dataset.category;
        document.querySelector(".nav-tab[data-tab='runs']").click();
        document.getElementById("runs-verdict").value = "fail";
        renderRunsTable();
      });
    });
  }

  renderPassRateChart();

  var groups = Object.entries(DATA.group_counts || {});
  var grpEl = document.getElementById("sum-groups");
  if (groups.length === 0) {
    grpEl.innerHTML = "<span style='color:#5d6470;font-size:12px'>No groups yet.</span>";
  } else {
    grpEl.innerHTML = '<table style="width:100%;font-size:12px;border-collapse:collapse">' +
      groups.map(function(e) {
        var g = e[0];
        var m = g.match(/_(\d{10})$/);
        var dt = m ? new Date(parseInt(m[1])*1000) : null;
        var dateStr = dt ? dt.toISOString().slice(0,10) + " " + dt.toISOString().slice(11,16) : "";
        var label = g.replace(/^batch_/, "").replace(/_\d{10}$/, "").replace(/_/g, " ").trim() || g;
        var namePart = label && label !== dateStr ? label + " — " : "";
        var friendly = g.indexOf("batch_") === 0 ? (namePart + dateStr) : g;
        return '<tr>' +
          '<td style="padding:5px 6px;border-bottom:1px solid #1e2029;color:#c5c8cc;font-size:12px">' + esc(friendly) + '</td>' +
          '<td style="padding:5px 6px;border-bottom:1px solid #1e2029;color:#5d6470;font-family:monospace;font-size:10px">' + esc(g) + '</td>' +
          '<td style="padding:5px 6px;border-bottom:1px solid #1e2029;color:#8a8f98">' + e[1] + ' runs</td></tr>';
      }).join("") + '</table>';
  }

  var consistency = DATA.consistency_results || [];
  var conEl = document.getElementById("sum-consistency");
  if (consistency.length === 0) {
    conEl.innerHTML = "<span style='color:#5d6470;font-size:12px'>No consistency checks saved yet — run consistency_check.py with --save.</span>";
  } else {
    conEl.innerHTML = consistency.map(function(r) {
      var sc = r.is_consistent ? "pass" : "fail";
      var sl = r.is_consistent ? "CONSISTENT" : "INCONSISTENT";
      var claims = Object.entries(r.claim_counts || {}).map(function(e) {
        return '<li style="margin:2px 0">' + esc(e[0]) + ': ' + e[1] + '/' + r.n_runs + '</li>';
      }).join("");
      return '<div style="background:#0d0f14;border:1px solid #1e2029;border-radius:6px;padding:12px 14px;margin-bottom:8px">' +
        '<div style="font-size:12px;color:#c5c8cc;margin-bottom:8px">' + esc(r.question) + '</div>' +
        '<div style="display:flex;align-items:center;gap:10px"><span class="badge ' + sc + '">' + sl + '</span></div>' +
        (claims ? '<ul style="margin:8px 0 0 0;padding-left:16px;font-size:11px;color:#8a8f98">' + claims + '</ul>' : '') +
      '</div>';
    }).join("");
  }

  var regression = DATA.regression_results || [];
  var regEl = document.getElementById("sum-regression");
  if (regression.length === 0) {
    regEl.innerHTML = "<span style='color:#5d6470;font-size:12px'>No regression checks saved yet — run regression_check.py.</span>";
  } else {
    regEl.innerHTML = regression.map(function(r) {
      var sig = r["significant_at_0.05"];
      var ci = r.ci_95 || [0,0];
      var rawDiff = r.diff * 100;
      var diff = rawDiff.toFixed(1);
      var diffColor = rawDiff > 0 ? "#4ade80" : rawDiff < 0 ? "#f87171" : "#8896A7";
      var diffSign = rawDiff > 0 ? "+" : "";
      var sigBadge = sig ? '<span class="badge fail">SIGNIFICANT</span>' : '<span class="badge unlabeled">NOT SIGNIFICANT</span>';
      var beforePct = (r.before.pass_rate*100).toFixed(0);
      var afterPct = (r.after.pass_rate*100).toFixed(0);
      return '<div style="background:#080B0F;border:1px solid #1A2233;border-radius:8px;padding:14px 16px;margin-bottom:10px;">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap;">' +
          '<div style="display:flex;align-items:center;gap:8px;">' +
            '<div style="background:#131C2E;border-radius:6px;padding:6px 12px;text-align:center;">' +
              '<div style="font-size:10px;color:#8896A7;margin-bottom:2px;">BEFORE</div>' +
              '<div style="font-size:18px;font-weight:600;color:#E2E8F0;">' + beforePct + '%</div>' +
              '<div style="font-size:10px;color:#4A5568;">n=' + r.before.n + '</div>' +
            '</div>' +
            '<div style="font-size:18px;color:#4A5568;">&rarr;</div>' +
            '<div style="background:#131C2E;border-radius:6px;padding:6px 12px;text-align:center;">' +
              '<div style="font-size:10px;color:#8896A7;margin-bottom:2px;">AFTER</div>' +
              '<div style="font-size:18px;font-weight:600;color:#E2E8F0;">' + afterPct + '%</div>' +
              '<div style="font-size:10px;color:#4A5568;">n=' + r.after.n + '</div>' +
            '</div>' +
            '<div style="font-size:20px;font-weight:600;color:' + diffColor + ';margin-left:4px;">' + diffSign + diff + 'pp</div>' +
          '</div>' +
          sigBadge +
        '</div>' +
        '<div style="font-size:11px;color:#4A5568;">CI [' + (ci[0]*100).toFixed(1) + ', ' + (ci[1]*100).toFixed(1) + ']pp &bull; p = ' + r.fishers_p_value.toFixed(4) + '</div>' +
      '</div>';
    }).join("");
  }
}

function renderTools() {
  if (!DATA) return;
  var groups = Object.keys(DATA.group_counts || {});

  // Load current config and set dropdowns
  fetch("/api/config").then(function(r){return r.json();}).then(function(cfg) {
    var agentSel = document.getElementById("cfg-agent");
    var judgeSel = document.getElementById("cfg-judge");
    if (agentSel && cfg.agent_model) agentSel.value = cfg.agent_model;
    if (judgeSel && cfg.judge_model) judgeSel.value = cfg.judge_model;
  }).catch(function(){});

  // Populate question dropdown for consistency check
  var conQ = document.getElementById("con-question");
  if (conQ) {
    conQ.innerHTML = '<option value="">-- select --</option>' +
      (DATA.questions || []).map(function(q) {
        return '<option value="' + esc(q) + '">' + esc(q) + '</option>';
      }).join("");
  }

  // Populate group dropdowns for regression check
  var regB = document.getElementById("reg-before");
  var regA = document.getElementById("reg-after");
  if (regB && regA) {
    var batchGroups = groups.filter(function(g) { return g.indexOf("batch_") === 0; });
    var opts = '<option value="">-- select group --</option>' +
      batchGroups.map(function(g) {
        var label = g.replace(/^batch_/, "").replace(/_\d{10}$/, "").replace(/_/g, " ") || g;
        var m = g.match(/_(\d{10})$/);
        var dt = m ? new Date(parseInt(m[1])*1000) : null;
        var dateStr = dt ? dt.toISOString().slice(0,10) + " " + dt.toISOString().slice(11,16) + " UTC" : "";
        var namePart = label.trim();
        var display = (namePart && namePart !== dateStr ? namePart + " — " : "") + dateStr + " (" + DATA.group_counts[g] + " runs)";
        return '<option value="' + esc(g) + '">' + esc(display) + '</option>';
      }).join("");
    regB.innerHTML = opts;
    regA.innerHTML = opts;
  }
}

var _toolsEs = null;

function startToolsStream(onMsg) {
  if (_toolsEs) _toolsEs.close();
  _toolsEs = new EventSource("/api/tools_stream");
  _toolsEs.onmessage = function(e) {
    var msg = JSON.parse(e.data);
    if (msg.type === "ping") return;
    onMsg(msg);
    if (msg.type === "done" || msg.type === "error") {
      _toolsEs.close();
      _toolsEs = null;
      refreshData();
    }
  };
  _toolsEs.onerror = function() {
    _toolsEs.close();
    _toolsEs = null;
  };
}

document.addEventListener("DOMContentLoaded", function() {

  document.getElementById("btn-calibrate").addEventListener("click", function() {
    var btn = this;
    var resEl = document.getElementById("calibrate-result");
    btn.disabled = true;
    btn.textContent = "Running...";
    resEl.style.display = "block";
    resEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px"><div class="spinner"></div>Scoring runs...</div>';
    startToolsStream(function(msg) {
      if (msg.type === "done" && msg.tool === "calibrate") {
        var kappaColor = msg.kappa >= 0.6 ? "#4ade80" : msg.kappa >= 0.4 ? "#fbbf24" : "#f87171";
        resEl.innerHTML =
          '<div style="display:flex;gap:24px;align-items:center">' +
          '<div><div style="font-size:11px;color:#8a8f98;margin-bottom:3px">SCORED</div><div style="font-size:22px;font-weight:600;color:#e0e2e6">' + msg.n + '</div></div>' +
          '<div><div style="font-size:11px;color:#8a8f98;margin-bottom:3px">RAW AGREEMENT</div><div style="font-size:22px;font-weight:600;color:#e0e2e6">' + msg.raw + '%</div></div>' +
          '<div><div style="font-size:11px;color:#8a8f98;margin-bottom:3px">KAPPA (COHEN)</div><div style="font-size:22px;font-weight:600;color:' + kappaColor + '">' + msg.kappa + '</div></div>' +
          '<div><div style="font-size:11px;color:#8a8f98;margin-bottom:3px">MISMATCHES</div><div style="font-size:22px;font-weight:600;color:#f87171">' + msg.mismatches + '</div></div>' +
          '</div>';
        // Update kappa in nav
        var kappaEl = document.getElementById("nav-kappa-val");
        var kappaStat = document.getElementById("nav-kappa-stat");
        if (kappaEl) { kappaEl.textContent = msg.kappa; kappaEl.style.color = kappaColor; }
        if (kappaStat) kappaStat.style.display = "inline";
        if (document.getElementById("kappa-card")) { document.getElementById("kappa-card").style.display = "block"; if(document.getElementById("unlabeled-card")) document.getElementById("unlabeled-card").style.display = "none"; }
        if (document.getElementById("stat-kappa-val")) { document.getElementById("stat-kappa-val").textContent = msg.kappa; document.getElementById("stat-kappa-val").style.color = kappaColor; }
        if (document.getElementById("stat-kappa-desc")) { document.getElementById("stat-kappa-desc").textContent = msg.kappa >= 0.6 ? "good agreement" : msg.kappa >= 0.4 ? "moderate agreement" : "weak agreement"; }
        btn.disabled = false;
        btn.textContent = "Run Calibrate";
      } else if (msg.type === "error") {
        resEl.innerHTML = '<span style="color:#f87171">Error: ' + esc(msg.msg) + '</span>';
        btn.disabled = false;
        btn.textContent = "Run Calibrate";
      }
    });
    fetch("/api/run_calibrate", {method: "POST"});
  });

  document.getElementById("btn-consistency").addEventListener("click", function() {
    var btn = this;
    var resEl = document.getElementById("consistency-result");
    var q = document.getElementById("con-custom").value.trim() ||
            document.getElementById("con-question").value.trim();
    var n = parseInt(document.getElementById("con-n").value) || 5;
    if (!q) { toast("Enter or select a question first", true); return; }
    btn.disabled = true;
    btn.textContent = "Running...";
    resEl.style.display = "block";
    resEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px"><div class="spinner"></div>Running ' + n + ' times...</div>';
    startToolsStream(function(msg) {
      if (msg.type === "done" && msg.tool === "consistency") {
        var r = msg.result;
        var sc = r.is_consistent ? "pass" : "fail";
        var sl = r.is_consistent ? "CONSISTENT" : "INCONSISTENT";
        var claims = Object.entries(r.claim_counts || {}).map(function(e) {
          return '<li>' + esc(e[0]) + ': ' + e[1] + '/' + r.n_runs + '</li>';
        }).join("");
        resEl.innerHTML =
          '<div style="margin-bottom:8px"><span class="badge ' + sc + '">' + sl + '</span>' +
          '<span style="font-size:11px;color:#8a8f98;margin-left:10px">Saved to ' + esc(msg.saved_to) + '</span></div>' +
          (claims ? '<ul style="margin:0;padding-left:16px;font-size:12px;color:#aeb2b8">' + claims + '</ul>' : "");
        btn.disabled = false;
        btn.textContent = "Run Check";
      } else if (msg.type === "error") {
        resEl.innerHTML = '<span style="color:#f87171">Error: ' + esc(msg.msg) + '</span>';
        btn.disabled = false;
        btn.textContent = "Run Check";
      }
    });
    fetch("/api/run_consistency", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: q, n_runs: n})
    });
  });

  document.getElementById("btn-regression").addEventListener("click", function() {
    var btn = this;
    var resEl = document.getElementById("regression-result");
    var before = document.getElementById("reg-before").value;
    var after = document.getElementById("reg-after").value;
    if (!before || !after) { toast("Select both groups first", true); return; }
    if (before === after) { toast("Before and after groups must be different", true); return; }
    btn.disabled = true;
    btn.textContent = "Running...";
    resEl.style.display = "block";
    resEl.innerHTML = '<div style="display:flex;align-items:center;gap:8px"><div class="spinner"></div>Judging runs...</div>';
    startToolsStream(function(msg) {
      if (msg.type === "done" && msg.tool === "regression") {
        var r = msg.result;
        var sig = r["significant_at_0.05"];
        var ci = r.ci_95 || [0,0];
        var diff = (r.diff*100).toFixed(1);
        var sc = sig ? "fail" : "unlabeled";
        var sl = sig ? "SIGNIFICANT" : "NOT SIGNIFICANT";
        resEl.innerHTML =
          '<div style="margin-bottom:8px;font-size:13px;color:#c5c8cc">Before: ' +
          (r.before.pass_rate*100).toFixed(0) + '% (n=' + r.before.n + ') &rarr; After: ' +
          (r.after.pass_rate*100).toFixed(0) + '% (n=' + r.after.n + ')</div>' +
          '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
          '<span class="badge ' + sc + '">' + sl + '</span>' +
          '<span style="color:#8a8f98;font-size:12px">diff ' + (diff>0?"+":"") + diff +
          'pp &nbsp; CI [' + (ci[0]*100).toFixed(1) + ', ' + (ci[1]*100).toFixed(1) +
          '] &nbsp; p=' + r.fishers_p_value.toFixed(4) + '</span></div>' +
          '<div style="font-size:11px;color:#5d6470;margin-top:8px">Saved to ' + esc(msg.saved_to) + '</div>';
        btn.disabled = false;
        btn.textContent = "Run Check";
      } else if (msg.type === "error") {
        resEl.innerHTML = '<span style="color:#f87171">Error: ' + esc(msg.msg) + '</span>';
        btn.disabled = false;
        btn.textContent = "Run Check";
      }
    });
    fetch("/api/run_regression", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({before_group: before, after_group: after})
    });
  });

});

document.querySelectorAll(".nav-tab").forEach(function(tab){
  tab.addEventListener("click",function(){
    document.querySelectorAll(".nav-tab").forEach(function(t){t.classList.remove("active");});
    document.querySelectorAll(".page").forEach(function(p){p.classList.remove("active");});
    tab.classList.add("active");
    document.getElementById("page-"+tab.dataset.tab).classList.add("active");
    if(tab.dataset.tab==="runs")renderRunsTable();
    if(tab.dataset.tab==="summary")renderSummary();
    if(tab.dataset.tab==="tools")renderTools();
  });
});


try {
  document.querySelectorAll(".collapsible-header").forEach(function(hdr) {
    hdr.addEventListener("click", function() {
      var targetId = hdr.dataset.target;
      var body = document.getElementById(targetId);
      var arrow = hdr.querySelector(".collapse-arrow");
      if (!body) return;
      var isOpen = body.style.display !== "none";
      body.style.display = isOpen ? "none" : "block";
      if (arrow) arrow.classList.toggle("open", !isOpen);
    });
  });
} catch(e) { console.warn("Collapse init error:", e); }

refreshData();


</script>
</body>
</html>"""
def run_migration():
    conn = init_db(DB_PATH)
    if os.path.exists("ground_truth"):
        migrate_json_labels(conn, "ground_truth", "admin")


def ensure_env_admin():
    """If ADMIN_USERNAME and ADMIN_PASSWORD are set as environment variables
    and that user doesn't already exist, create it. Lets you set up a real
    admin account on hosts (like Render's free tier) that don't provide
    shell access, just by adding env vars and redeploying."""
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    conn = init_db(DB_PATH)
    if get_user_by_username(conn, username):
        print(f"Admin user '{username}' already exists, skipping creation.")
        return
    pw_hash = generate_password_hash(password)
    uid = create_user(conn, username, pw_hash, "admin")
    print(f"Created admin user '{username}' from environment variables (id: {uid})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-user", nargs="+", metavar=("USERNAME", "PASSWORD"),
                        help="Create a user: --create-user <username> <password> [admin|labeler]")
    parser.add_argument("--migrate", action="store_true",
                        help="Run JSON -> SQLite label migration only")
    args = parser.parse_args()

    if args.migrate:
        run_migration()
        sys.exit(0)

    if args.create_user:
        parts = args.create_user
        if len(parts) < 2:
            print("Usage: --create-user <username> <password> [admin|labeler]")
            sys.exit(1)
        username, password = parts[0], parts[1]
        role = parts[2] if len(parts) > 2 else "labeler"
        conn = init_db(DB_PATH)
        pw_hash = generate_password_hash(password)
        uid = create_user(conn, username, pw_hash, role)
        print(f"Created user '{username}' (role: {role}, id: {uid})")
        sys.exit(0)

    # Normal startup: run migration then start server
    run_migration()
    ensure_env_admin()
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting server on port {port}")
    print("Default login: admin / changeme  <-- CHANGE THIS if deploying publicly")
    print("Create more users: python server.py --create-user <name> <password> [admin|labeler]")
    print("Admin panel: /admin")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
