"""
dashboard.py -- generate a static HTML dashboard from traces.db + labels.

v2: full substep visibility, runtime stats, run grouping (group_id), search
and verdict filtering, and a tabbed Summary / All Runs layout. Still no
server -- all data is embedded as JSON in the page and rendered client-side
with plain JS, which is what makes search and expand/collapse possible
without a backend.

Labeling still happens by hand in ground_truth/*.json -- this version adds
a "copy label template" button per unlabeled run so you don't have to
write the JSON skeleton by hand, but it doesn't write files directly (a
static HTML file can't do that; true click-to-label-in-place would need a
small local server, which is a separate, bigger change from this one).

Run:
    python dashboard.py
Then open dashboard.html in your browser.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter
from datetime import datetime

from tracing import Tracer


def load_labels() -> dict:
    labels = {}
    for path in glob.glob(os.path.join("ground_truth", "*.json")):
        with open(path) as f:
            labels.update(json.load(f))
    return labels


def load_consistency_results() -> list[dict]:
    results = []
    for path in glob.glob("consistency_*.json"):
        with open(path) as f:
            results.append(json.load(f))
    return results


def load_regression_results() -> list[dict]:
    results = []
    for path in glob.glob("regression_*.json"):
        with open(path) as f:
            results.append(json.load(f))
    return results


# Plain string, NOT an f-string -- CSS and JS are full of literal { } and
# this avoids the entire class of brace-escaping bugs that comes with
# trying to embed them inside a Python f-string. Only two tokens get
# substituted, via .replace(), at the very end.
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agent Eval Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #0f1117; color: #e3e5e8; margin: 0; padding: 32px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #8a8f98; font-size: 13px; margin-bottom: 20px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #262a33; }
  .tab { padding: 10px 18px; cursor: pointer; color: #8a8f98; font-size: 13px; border-bottom: 2px solid transparent; }
  .tab.active { color: #e3e5e8; border-bottom-color: #f87171; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .stats { display: flex; gap: 16px; margin-bottom: 32px; }
  .stat-card { background: #1a1d24; border-radius: 8px; padding: 16px 20px; flex: 1; }
  .stat-number { font-size: 28px; font-weight: 600; }
  .stat-label { color: #8a8f98; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  section { margin-bottom: 36px; }
  h2 { font-size: 15px; color: #c5c8cc; border-bottom: 1px solid #262a33; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8a8f98; font-size: 11px; text-transform: uppercase; padding: 8px; border-bottom: 1px solid #262a33; }
  td { padding: 10px 8px; border-bottom: 1px solid #1d2027; vertical-align: top; }
  tr.run-row { cursor: pointer; }
  tr.run-row:hover { background: #161920; }
  td.task { max-width: 260px; }
  td.answer { max-width: 300px; color: #aeb2b8; }
  td.run_id { color: #5d6470; font-family: monospace; font-size: 11px; }
  td.runtime { color: #8a8f98; font-family: monospace; font-size: 12px; white-space: nowrap; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.pass { background: #143324; color: #4ade80; }
  .badge.fail { background: #3a1a1a; color: #f87171; }
  .badge.unlabeled { background: #262a33; color: #8a8f98; }
  .bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 13px; cursor: pointer; }
  .bar-row:hover .bar-label { color: #fff; }
  .bar-label { width: 220px; color: #c5c8cc; }
  .bar-track { flex: 1; background: #1d2027; border-radius: 4px; height: 14px; overflow: hidden; }
  .bar-fill { background: #f87171; height: 100%; }
  .bar-count { width: 24px; text-align: right; color: #8a8f98; }
  .consistency-card { background: #1a1d24; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px; }
  .consistency-question { font-size: 13px; margin-bottom: 8px; color: #c5c8cc; }
  .consistency-card ul { margin: 8px 0 0 0; padding-left: 18px; font-size: 12px; color: #aeb2b8; }
  .controls { display: flex; gap: 10px; margin-bottom: 16px; }
  .controls input { background: #1a1d24; border: 1px solid #262a33; color: #e3e5e8; padding: 8px 12px; border-radius: 6px; font-size: 13px; flex: 1; }
  .controls select { background: #1a1d24; border: 1px solid #262a33; color: #e3e5e8; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
  .substeps { display: none; background: #0a0b0f; }
  .substeps.open { display: table-row; }
  .substep-box { padding: 12px 16px; }
  .substep { border-left: 2px solid #262a33; padding: 8px 0 8px 14px; margin-bottom: 6px; font-size: 12px; }
  .substep-name { color: #c5c8cc; font-weight: 600; }
  .substep-time { color: #5d6470; font-family: monospace; margin-left: 8px; }
  .substep-io { color: #8a8f98; font-family: monospace; font-size: 11px; margin-top: 4px; word-break: break-word; white-space: pre-wrap; max-height: 140px; overflow-y: auto; }
  .substep-error { color: #f87171; margin-top: 4px; }
  .copy-btn { background: #262a33; border: none; color: #c5c8cc; font-size: 11px; padding: 3px 8px; border-radius: 4px; cursor: pointer; }
  .copy-btn:hover { background: #353a45; }
  .group-tag { color: #5d6470; font-family: monospace; font-size: 10px; }
  .empty { color: #5d6470; }
</style>
</head>
<body>
  <h1>Agent Eval Dashboard</h1>
  <div class="subtitle">Generated __GENERATED_AT__</div>

  <div class="tabs">
    <div class="tab active" data-tab="summary">Summary</div>
    <div class="tab" data-tab="runs">All Runs</div>
  </div>

  <div class="tab-panel active" id="panel-summary">
    <div class="stats">
      <div class="stat-card"><div class="stat-number" id="stat-total"></div><div class="stat-label">Total runs</div></div>
      <div class="stat-card"><div class="stat-number" id="stat-pass"></div><div class="stat-label">Pass</div></div>
      <div class="stat-card"><div class="stat-number" id="stat-fail"></div><div class="stat-label">Fail</div></div>
      <div class="stat-card"><div class="stat-number" id="stat-unlabeled"></div><div class="stat-label">Unlabeled</div></div>
    </div>

    <section>
      <h2>Failure categories (click a bar to filter All Runs)</h2>
      <div id="category-bars"></div>
    </section>

    <section>
      <h2>Run groups</h2>
      <div id="group-list"></div>
    </section>

    <section>
      <h2>Consistency checks</h2>
      <div id="consistency-list"></div>
    </section>

    <section>
      <h2>Regression checks</h2>
      <div id="regression-list"></div>
    </section>
  </div>

  <div class="tab-panel" id="panel-runs">
    <div class="controls">
      <input type="text" id="search-box" placeholder="Search by run ID, task, or date (e.g. 2026-06-24)...">
      <select id="verdict-filter">
        <option value="">All verdicts</option>
        <option value="pass">Pass</option>
        <option value="fail">Fail</option>
        <option value="unlabeled">Unlabeled</option>
      </select>
      <button class="copy-btn" id="clear-filters">Clear filters</button>
    </div>
    <table>
      <tr><th></th><th>Task</th><th>Final answer</th><th>Verdict</th><th>Category</th><th>Runtime</th><th>Group</th><th>Run ID</th></tr>
      <tbody id="runs-tbody"></tbody>
    </table>
  </div>

<script>
const DATA = __PAYLOAD_JSON__;
let categoryFilter = null;

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderStats() {
  document.getElementById("stat-total").textContent = DATA.stats.total;
  document.getElementById("stat-pass").textContent = DATA.stats.pass_count;
  document.getElementById("stat-fail").textContent = DATA.stats.fail_count;
  document.getElementById("stat-unlabeled").textContent = DATA.stats.unlabeled;
}

function renderCategoryBars() {
  const entries = Object.entries(DATA.category_counts);
  const container = document.getElementById("category-bars");
  if (entries.length === 0) {
    container.innerHTML = "<p class='empty'>No labeled failures yet.</p>";
    return;
  }
  const maxCount = Math.max.apply(null, entries.map(function (e) { return e[1]; }));
  container.innerHTML = entries.map(function (entry) {
    var cat = entry[0], count = entry[1];
    return '<div class="bar-row" data-category="' + escapeHtml(cat) + '">' +
      '<span class="bar-label">' + escapeHtml(cat) + '</span>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + ((count / maxCount) * 100) + '%"></div></div>' +
      '<span class="bar-count">' + count + '</span>' +
    '</div>';
  }).join("");
  container.querySelectorAll(".bar-row").forEach(function (row) {
    row.addEventListener("click", function () {
      categoryFilter = row.dataset.category;
      document.querySelector('.tab[data-tab="runs"]').click();
      document.getElementById("search-box").value = "";
      document.getElementById("verdict-filter").value = "fail";
      renderRuns();
    });
  });
}

function renderGroups() {
  const entries = Object.entries(DATA.group_counts);
  const container = document.getElementById("group-list");
  if (entries.length === 0) {
    container.innerHTML = "<p class='empty'>No grouped runs yet -- group_id is set automatically by run_batch.py and consistency_check.py going forward; older runs predate this feature.</p>";
    return;
  }
  var rows = entries.map(function (entry) {
    return '<tr><td class="run_id">' + escapeHtml(entry[0]) + '</td><td>' + entry[1] + '</td></tr>';
  }).join("");
  container.innerHTML = '<table><tr><th>Group ID</th><th>Runs</th></tr>' + rows + '</table>';
}

function renderConsistency() {
  const container = document.getElementById("consistency-list");
  if (DATA.consistency_results.length === 0) {
    container.innerHTML = "<p class='empty'>No consistency checks saved yet -- run consistency_check.py with --save.</p>";
    return;
  }
  container.innerHTML = DATA.consistency_results.map(function (r) {
    var statusClass = r.is_consistent ? "pass" : "fail";
    var statusLabel = r.is_consistent ? "CONSISTENT" : "INCONSISTENT";
    var claims = Object.entries(r.claim_counts || {}).map(function (entry) {
      return '<li>' + escapeHtml(entry[0]) + ': ' + entry[1] + '/' + r.n_runs + '</li>';
    }).join("");
    return '<div class="consistency-card">' +
      '<div class="consistency-question">' + escapeHtml(r.question) + '</div>' +
      '<span class="badge ' + statusClass + '">' + statusLabel + '</span>' +
      '<ul>' + claims + '</ul>' +
    '</div>';
  }).join("");
}

function renderRegression() {
  const container = document.getElementById("regression-list");
  if (DATA.regression_results.length === 0) {
    container.innerHTML = "<p class='empty'>No regression checks saved yet -- run regression_check.py.</p>";
    return;
  }
  container.innerHTML = DATA.regression_results.map(function (r) {
    var sig = r["significant_at_0.05"];
    var sigLabel = sig ? "SIGNIFICANT" : "NOT SIGNIFICANT";
    var sigClass = sig ? "fail" : "unlabeled";
    var diffPct = (r.diff * 100).toFixed(1);
    var ci = r.ci_95 || [0, 0];
    return '<div class="consistency-card">' +
      '<div class="consistency-question">Before: ' + (r.before.pass_rate * 100).toFixed(0) + '% pass (n=' + r.before.n + ') &rarr; After: ' + (r.after.pass_rate * 100).toFixed(0) + '% pass (n=' + r.after.n + ')</div>' +
      '<span class="badge ' + sigClass + '">' + sigLabel + '</span>' +
      '<span style="color:#8a8f98; font-size:12px"> diff ' + (diffPct > 0 ? "+" : "") + diffPct + 'pp, 95% CI [' + (ci[0]*100).toFixed(1) + ', ' + (ci[1]*100).toFixed(1) + ']pp, p=' + r.fishers_p_value.toFixed(4) + '</span>' +
    '</div>';
  }).join("");
}

function copyLabelTemplate(runId, task) {
  var obj = {};
  obj[runId] = { task: task, label: "pass", failure_category: null, notes: "" };
  var template = JSON.stringify(obj, null, 2);
  navigator.clipboard.writeText(template).then(function () {
    alert("Label template copied -- paste it into a ground_truth/*.json file and fill in the verdict.");
  });
}

function renderRuns() {
  const search = document.getElementById("search-box").value.toLowerCase();
  const verdict = document.getElementById("verdict-filter").value;
  const tbody = document.getElementById("runs-tbody");

  const filtered = DATA.runs.filter(function (r) {
    if (categoryFilter && r.failure_category !== categoryFilter) return false;
    if (verdict && r.verdict !== verdict) return false;
    if (search) {
      var haystack = (r.run_id + " " + r.task + " " + (r.started_at || "")).toLowerCase();
      if (haystack.indexOf(search) === -1) return false;
    }
    return true;
  });

  tbody.innerHTML = filtered.map(function (r, i) {
    var answerText = r.final_answer || "";
    var shortAnswer = escapeHtml(answerText.slice(0, 110)) + (answerText.length > 110 ? "..." : "");
    var badgeClass = (r.verdict === "pass" || r.verdict === "fail") ? r.verdict : "unlabeled";
    var labelBtn = badgeClass === "unlabeled"
      ? '<button class="copy-btn" data-copy-idx="' + i + '">copy label template</button>'
      : "";
    var substepsHtml = r.substeps.map(function (s) {
      return '<div class="substep">' +
        '<span class="substep-name">Step ' + s.step_index + ': ' + escapeHtml(s.name) + '</span>' +
        '<span class="substep-time">' + s.duration_ms + 'ms</span>' +
        '<div class="substep-io"><strong>input:</strong> ' + escapeHtml(s.input) + '</div>' +
        '<div class="substep-io"><strong>output:</strong> ' + escapeHtml(s.output) + '</div>' +
        (s.error ? '<div class="substep-error">ERROR: ' + escapeHtml(s.error) + '</div>' : '') +
      '</div>';
    }).join("");
    return (
      '<tr class="run-row" data-idx="' + i + '">' +
        '<td>\u25B8</td>' +
        '<td class="task">' + escapeHtml(r.task) + '</td>' +
        '<td class="answer">' + shortAnswer + '</td>' +
        '<td><span class="badge ' + badgeClass + '">' + escapeHtml(r.verdict) + '</span> ' + labelBtn + '</td>' +
        '<td>' + escapeHtml(r.failure_category || "") + '</td>' +
        '<td class="runtime">' + r.total_runtime_ms + 'ms / ' + r.substeps.length + ' steps</td>' +
        '<td class="group-tag">' + escapeHtml(r.group_id || "") + '</td>' +
        '<td class="run_id">' + r.run_id + '</td>' +
      '</tr>' +
      '<tr class="substeps" id="substeps-' + i + '"><td colspan="8"><div class="substep-box">' +
        (substepsHtml || "<span class='empty'>No substeps recorded.</span>") +
      '</div></td></tr>'
    );
  }).join("");

  tbody.querySelectorAll(".run-row").forEach(function (row) {
    row.addEventListener("click", function () {
      var idx = row.dataset.idx;
      document.getElementById("substeps-" + idx).classList.toggle("open");
    });
  });

  tbody.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var idx = btn.dataset.copyIdx;
      var r = filtered[idx];
      copyLabelTemplate(r.run_id, r.task);
    });
  });
}

document.querySelectorAll(".tab").forEach(function (tab) {
  tab.addEventListener("click", function () {
    document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
    document.querySelectorAll(".tab-panel").forEach(function (p) { p.classList.remove("active"); });
    tab.classList.add("active");
    document.getElementById("panel-" + tab.dataset.tab).classList.add("active");
  });
});

document.getElementById("search-box").addEventListener("input", renderRuns);
document.getElementById("verdict-filter").addEventListener("change", function () { categoryFilter = null; renderRuns(); });
document.getElementById("clear-filters").addEventListener("click", function () {
  categoryFilter = null;
  document.getElementById("search-box").value = "";
  document.getElementById("verdict-filter").value = "";
  renderRuns();
});

renderStats();
renderCategoryBars();
renderGroups();
renderConsistency();
renderRegression();
renderRuns();
</script>
</body>
</html>"""


def build_html(runs: list[dict], labels: dict, consistency_results: list[dict], regression_results: list[dict]) -> str:
    for run in runs:
        label = labels.get(run["run_id"], {})
        run["verdict"] = label.get("label", "unlabeled")
        run["failure_category"] = label.get("failure_category")

    total = len(runs)
    pass_count = sum(1 for r in runs if r["verdict"] == "pass")
    fail_count = sum(1 for r in runs if r["verdict"] == "fail")
    unlabeled = total - pass_count - fail_count

    category_counts = Counter(r["failure_category"] for r in runs if r["failure_category"])
    group_counts = Counter(r["group_id"] for r in runs if r["group_id"])

    payload = {
        "runs": runs,
        "consistency_results": consistency_results,
        "regression_results": regression_results,
        "stats": {
            "total": total,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "unlabeled": unlabeled,
        },
        "category_counts": dict(category_counts.most_common()),
        "group_counts": dict(group_counts.most_common()),
    }

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Defensive: if any embedded text literally contains "</script>", it would
    # otherwise break out of the <script> tag in the browser.
    payload_json = json.dumps(payload).replace("</script", "<\\/script")

    return HTML_TEMPLATE.replace("__GENERATED_AT__", generated_at).replace("__PAYLOAD_JSON__", payload_json)


if __name__ == "__main__":
    tracer = Tracer()
    runs = tracer.get_all_runs_full()
    labels = load_labels()
    consistency_results = load_consistency_results()
    regression_results = load_regression_results()

    html = build_html(runs, labels, consistency_results, regression_results)

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(
        f"Generated dashboard.html with {len(runs)} runs, {len(labels)} labels, "
        f"{len(consistency_results)} consistency check(s), {len(regression_results)} regression check(s)."
    )
    print("Open dashboard.html in your browser to view it.")
