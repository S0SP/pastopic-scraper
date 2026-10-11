"""
app.py — PastTopic Scraper Dashboard
Beautiful real-time UI for Hugging Face Spaces (CPU Basic, free)
"""

import os
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime

import gradio as gr
import scraper as sc

DATA_DIR = Path("/data/papers")
ZIP_PATH = Path("/data/pastopic_papers.zip")

# ─── CSS ──────────────────────────────────────────────────────────────────────
CSS = """
/* ── Base ── */
body, .gradio-container { background: #0d1117 !important; font-family: 'Inter', sans-serif; }
.dark { --background-fill-primary: #0d1117; }

/* ── Stat cards ── */
.stat-card {
    background: linear-gradient(135deg, #161b22, #1c2128);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 18px 22px;
    text-align: center;
    transition: transform .2s, box-shadow .2s;
}
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,.4); }
.stat-num  { font-size: 2.2rem; font-weight: 700; line-height: 1.1; }
.stat-label { font-size: .8rem; color: #8b949e; margin-top: 4px; letter-spacing: .5px; }
.stat-green  .stat-num { color: #3fb950; }
.stat-yellow .stat-num { color: #d29922; }
.stat-red    .stat-num { color: #f85149; }
.stat-blue   .stat-num { color: #58a6ff; }
.stat-purple .stat-num { color: #bc8cff; }

/* ── Section headers ── */
.section-title {
    font-size: 1rem; font-weight: 600; color: #c9d1d9;
    border-bottom: 1px solid #30363d;
    padding-bottom: 8px; margin-bottom: 12px; margin-top: 4px;
    letter-spacing: .4px;
}

/* ── Log terminal ── */
.log-terminal textarea {
    background: #010409 !important;
    color: #c9d1d9 !important;
    font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
    font-size: 0.78rem !important;
    border: 1px solid #30363d !important;
    border-radius: 8px !important;
    line-height: 1.5 !important;
}

/* ── Buttons ── */
.btn-start { background: linear-gradient(135deg, #238636, #2ea043) !important; color: #fff !important; border: none !important; border-radius: 8px !important; font-weight: 600 !important; font-size: 1rem !important; }
.btn-stop  { background: linear-gradient(135deg, #b62324, #da3633) !important; color: #fff !important; border: none !important; border-radius: 8px !important; font-weight: 600 !important; }
.btn-zip   { background: linear-gradient(135deg, #1f6feb, #388bfd) !important; color: #fff !important; border: none !important; border-radius: 8px !important; font-weight: 600 !important; }

/* ── Status pill ── */
.status-pill {
    display: inline-block; padding: 4px 14px;
    border-radius: 20px; font-size: .8rem; font-weight: 600;
}
.pill-running { background: #0d4429; color: #3fb950; border: 1px solid #238636; }
.pill-stopped { background: #2d1515; color: #f85149; border: 1px solid #b62324; }
.pill-idle    { background: #1c2128; color: #8b949e; border: 1px solid #30363d; }

/* ── Progress bar ── */
.prog-bar-wrap { background: #21262d; border-radius: 99px; height: 8px; overflow: hidden; }
.prog-bar-fill { background: linear-gradient(90deg, #238636, #3fb950); height: 8px; border-radius: 99px; transition: width .6s ease; }

/* ── Subject table ── */
.subj-table { width: 100%; border-collapse: collapse; font-size: .82rem; color: #c9d1d9; }
.subj-table th { color: #8b949e; text-align: left; padding: 6px 10px; border-bottom: 1px solid #21262d; font-weight: 500; }
.subj-table td { padding: 6px 10px; border-bottom: 1px solid #161b22; }
.subj-table tr:last-child td { border-bottom: none; }
.badge { padding: 2px 8px; border-radius: 4px; font-size: .72rem; font-weight: 600; }
.badge-done { background: #0d4429; color: #3fb950; }
.badge-run  { background: #3b2300; color: #d29922; }
.badge-wait { background: #1c2128; color: #8b949e; }

/* ── Card container ── */
.card {
    background: #161b22; border: 1px solid #30363d;
    border-radius: 12px; padding: 20px;
}
"""

JS_AUTOSCROLL = """
function() {
    setTimeout(() => {
        const ta = document.querySelector('.log-terminal textarea');
        if (ta) { ta.scrollTop = ta.scrollHeight; }
    }, 100);
}
"""

# ─── State helpers ─────────────────────────────────────────────────────────────
def disk_count() -> int:
    return len(list(DATA_DIR.rglob("*.pdf"))) if DATA_DIR.exists() else 0


def disk_size_mb() -> float:
    if not DATA_DIR.exists():
        return 0.0
    return sum(f.stat().st_size for f in DATA_DIR.rglob("*.pdf")) / 1024 / 1024


def elapsed_str() -> str:
    s = sc.state
    if not s.get("start_time"):
        return "—"
    secs = int(time.time() - s["start_time"])
    h, m = divmod(secs, 3600)
    m, s2 = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s2:02d}"


def status_html() -> str:
    s = sc.state
    if s["running"]:
        return '<span class="status-pill pill-running">● RUNNING</span>'
    elif s.get("stop_requested"):
        return '<span class="status-pill pill-stopped">■ STOPPED</span>'
    return '<span class="status-pill pill-idle">○ IDLE</span>'


def build_stats_html() -> str:
    s   = sc.state
    cnt = disk_count()
    mb  = disk_size_mb()
    total = max(s["total"], 1)
    pct = min(100, int(s["downloaded"] / total * 100)) if s["total"] else 0
    subj_done = len(s["subjects_done"])
    subj_total = s.get("subjects_total", 0)

    cards = f"""
<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px">
  <div class="stat-card stat-green">
    <div class="stat-num">{s['downloaded']:,}</div>
    <div class="stat-label">Downloaded</div>
  </div>
  <div class="stat-card stat-yellow">
    <div class="stat-num">{s['skipped']:,}</div>
    <div class="stat-label">Skipped</div>
  </div>
  <div class="stat-card stat-red">
    <div class="stat-num">{s['failed']:,}</div>
    <div class="stat-label">Failed</div>
  </div>
  <div class="stat-card stat-blue">
    <div class="stat-num">{cnt:,}</div>
    <div class="stat-label">Total on Disk</div>
  </div>
  <div class="stat-card stat-purple">
    <div class="stat-num">{mb:.0f} MB</div>
    <div class="stat-label">Disk Used</div>
  </div>
</div>

<div style="margin-bottom:20px">
  <div style="display:flex;justify-content:space-between;margin-bottom:6px;font-size:.82rem;color:#8b949e">
    <span>Overall Progress — {subj_done}/{subj_total} subjects</span>
    <span>{pct}%</span>
  </div>
  <div class="prog-bar-wrap">
    <div class="prog-bar-fill" style="width:{pct}%"></div>
  </div>
</div>
"""
    return cards


def build_subject_table() -> str:
    s     = sc.state
    done  = set(s["subjects_done"])
    cur   = s["current"]
    all_s = s.get("all_subjects", [])

    if not all_s:
        return "<p style='color:#8b949e;font-size:.85rem'>Subjects load when scraper starts…</p>"

    rows = []
    for subj in all_s:
        name = subj["name"]
        code = subj["code"]
        lvl  = subj["level"]

        if name in done:
            badge = '<span class="badge badge-done">✓ Done</span>'
        elif cur and name in cur:
            badge = '<span class="badge badge-run">⚡ Active</span>'
        else:
            badge = '<span class="badge badge-wait">Queued</span>'

        # count files for this subject
        folder = DATA_DIR / lvl / f"{name.replace(' ','_')}_{code}"
        n = len(list(folder.rglob("*.pdf"))) if folder.exists() else 0

        rows.append(
            f"<tr><td>{name}</td><td style='color:#8b949e'>{code}</td>"
            f"<td>{lvl}</td><td style='text-align:center'>{n}</td><td>{badge}</td></tr>"
        )

    body = "\n".join(rows)
    return f"""
<table class="subj-table">
  <thead><tr>
    <th>Subject</th><th>Code</th><th>Level</th><th>Files</th><th>Status</th>
  </tr></thead>
  <tbody>{body}</tbody>
</table>"""


def build_current_html() -> str:
    s = sc.state
    cur = s["current"] or "—"
    et  = elapsed_str()
    rate = s.get("rate", 0)
    return f"""
<div style="display:flex;gap:20px;flex-wrap:wrap;font-size:.85rem;color:#c9d1d9">
  <div>📌 <b>Current:</b> {cur}</div>
  <div>⏱ <b>Elapsed:</b> {et}</div>
  <div>⚡ <b>Rate:</b> {rate:.1f} files/min</div>
</div>"""


def get_log() -> str:
    lines = sc.state["log"]
    return "\n".join(lines[-120:]) if lines else "Waiting for scraper to start…"


# ─── Actions ──────────────────────────────────────────────────────────────────
def start_scraper():
    if sc.state["running"]:
        return gr.update()
    sc.state["start_time"] = time.time()
    sc.state["stop_requested"] = False
    t = threading.Thread(target=sc.run_scraper, daemon=True)
    t.start()
    return gr.update()


def stop_scraper():
    sc.state["stop"] = True
    sc.state["stop_requested"] = True
    return gr.update()


def make_zip():
    if not DATA_DIR.exists() or not any(DATA_DIR.rglob("*.pdf")):
        return None, "❌ No files downloaded yet."
    sc.log_msg("🗜️  Creating ZIP archive — this may take a while …")
    shutil.make_archive(str(ZIP_PATH.with_suffix("")), "zip", DATA_DIR)
    mb = ZIP_PATH.stat().st_size / 1024 / 1024
    sc.log_msg(f"✅ ZIP ready — {mb:.1f} MB")
    return str(ZIP_PATH), f"✅ ZIP ready — {mb:.1f} MB  ·  Click file above to download"


def full_refresh():
    return (
        build_stats_html(),
        build_subject_table(),
        build_current_html(),
        status_html(),
        get_log(),
    )


# ─── App ──────────────────────────────────────────────────────────────────────
with gr.Blocks(
    title="📚 PastTopic Paper Scraper",
    css=CSS,
    theme=gr.themes.Base(),
) as demo:

    # ── Header ──────────────────────────────────────────────────────────────
    gr.HTML("""
    <div style="text-align:center;padding:28px 0 16px">
      <h1 style="font-size:2rem;font-weight:800;color:#c9d1d9;margin:0">
        📚 PastTopic Paper Scraper
      </h1>
      <p style="color:#8b949e;margin:8px 0 0;font-size:.95rem">
        Cambridge IGCSE · A-Level · O-Level · All Subjects · All Boards
        &nbsp;|&nbsp; Question Papers · Mark Schemes · Examiner Reports · Grade Thresholds
      </p>
    </div>
    """)

    # ── Status bar ──────────────────────────────────────────────────────────
    with gr.Row():
        with gr.Column(scale=4):
            cur_html = gr.HTML(build_current_html())
        with gr.Column(scale=1, min_width=120):
            status_html_out = gr.HTML(status_html())

    # ── Stat cards + progress ────────────────────────────────────────────────
    stats_html_out = gr.HTML(build_stats_html())

    # ── Main layout ─────────────────────────────────────────────────────────
    with gr.Row():

        # Left — controls + subject table
        with gr.Column(scale=1, min_width=320):
            gr.HTML('<div class="section-title">⚙️ Controls</div>')
            with gr.Row():
                start_btn = gr.Button("▶  Start Scraping", elem_classes="btn-start")
                stop_btn  = gr.Button("⏹  Stop",           elem_classes="btn-stop")

            gr.HTML('<div class="section-title" style="margin-top:20px">📦 Download</div>')
            zip_btn    = gr.Button("🗜️  Create ZIP & Download", elem_classes="btn-zip")
            zip_status = gr.Textbox(label="", interactive=False, lines=1, container=False)
            zip_file   = gr.File(label="ZIP Download", interactive=False)

            gr.HTML('<div class="section-title" style="margin-top:20px">📚 Subject Progress</div>')
            subj_table_html = gr.HTML(build_subject_table())

        # Right — log terminal
        with gr.Column(scale=2):
            gr.HTML('<div class="section-title">🖥️ Live Log</div>')
            log_box = gr.Textbox(
                value=get_log(),
                lines=38,
                max_lines=38,
                interactive=False,
                show_label=False,
                elem_classes="log-terminal",
                autoscroll=True,
            )
            with gr.Row():
                refresh_btn = gr.Button("🔄 Refresh Now", size="sm")
                gr.Markdown(
                    "<span style='color:#8b949e;font-size:.8rem'>Auto-refreshes every 5 s</span>",
                    elem_id="refresh-hint",
                )

    # ── Footer ──────────────────────────────────────────────────────────────
    gr.HTML("""
    <div style="text-align:center;padding:20px 0 8px;color:#484f58;font-size:.78rem">
      Files saved to <code>/data/papers/</code> (persistent) ·
      Pushed to HF Dataset · Progress auto-saved ·
      Safe to restart — resumes from last file
    </div>
    """)

    # ── Wire callbacks ───────────────────────────────────────────────────────
    outputs = [stats_html_out, subj_table_html, cur_html, status_html_out, log_box]

    start_btn.click(start_scraper).then(full_refresh, outputs=outputs)
    stop_btn.click(stop_scraper).then(full_refresh, outputs=outputs)
    zip_btn.click(make_zip, outputs=[zip_file, zip_status])
    refresh_btn.click(full_refresh, outputs=outputs)

    # Auto-refresh every 5 seconds
    demo.load(full_refresh, outputs=outputs, every=5)


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
