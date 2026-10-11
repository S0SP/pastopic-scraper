"""
scraper.py — Core scraping engine for PastTopic.com
Saves PDFs locally under /data/papers/ and pushes to HF Dataset repo.

Env vars (set as HF Space Secrets):
  HF_TOKEN        — your Hugging Face write token
  DATASET_REPO    — e.g. "yourname/pastopic-papers"
"""

import os
import re
import time
import json
import logging
import threading
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from huggingface_hub import HfApi, CommitOperationAdd

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path("/data/papers")          # persistent HF Space storage
PROGRESS_FILE = Path("/data/progress.json")
LOG_FILE      = Path("/data/scraper.log")

BASE_URL  = "https://pastopic.com"
FILES_CDN = "https://files.pastopic.com"

DOWNLOAD_DELAY  = 0.5
REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3

# Push to HF Dataset every N files (batching reduces API calls)
HF_PUSH_EVERY = 50

SESSION_MAP = {
    "may-june":  "May-June",
    "oct-nov":   "Oct-Nov",
    "feb-march": "Feb-March",
    "march":     "Feb-March",
}

# ─── Shared state (read by Gradio UI) ────────────────────────────────────────
state = {
    "running":        False,
    "stop":           False,
    "stop_requested": False,
    "log":            [],
    "downloaded":     0,
    "skipped":        0,
    "failed":         0,
    "total":          0,
    "current":        "",
    "subjects_done":  [],
    "subjects_total": 0,
    "all_subjects":   [],   # list of {name,code,level} for the UI table
    "start_time":     0.0,
    "rate":           0.0,  # files/min (rolling)
}
_lock = threading.Lock()


def log_msg(msg: str, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    logging.log(getattr(logging, level), msg)
    with _lock:
        state["log"].append(line)
        if len(state["log"]) > 500:
            state["log"] = state["log"][-400:]


# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# ─── HTTP ─────────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
})


def fetch(url, stream=False):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if state["stop"]:
                return None
            log_msg(f"  Retry {attempt}/{MAX_RETRIES} [{url}]: {e}", "WARNING")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return None


# ─── Progress ────────────────────────────────────────────────────────────────
def load_progress() -> set:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()


def save_progress(done: set):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(sorted(done), f)


# ─── HF Dataset Push ─────────────────────────────────────────────────────────
def push_to_hf(pending_files: list[Path], api: HfApi, repo_id: str):
    """Upload a batch of files to the HF Dataset repo."""
    if not pending_files or not repo_id:
        return
    try:
        ops = []
        for p in pending_files:
            rel = p.relative_to(DATA_DIR)
            ops.append(CommitOperationAdd(
                path_in_repo=str(rel).replace("\\", "/"),
                path_or_fileobj=str(p),
            ))
        api.create_commit(
            repo_id=repo_id,
            repo_type="dataset",
            operations=ops,
            commit_message=f"Add {len(ops)} papers",
        )
        log_msg(f"  ☁️  Pushed {len(ops)} files to HF Dataset")
    except Exception as e:
        log_msg(f"  HF push error: {e}", "WARNING")


# ─── Discovery ────────────────────────────────────────────────────────────────
def discover_all_subjects() -> list[dict]:
    """Scrape /papers page to get EVERY subject from EVERY board."""
    log_msg("Discovering all subjects from pastopic.com/papers …")
    resp = fetch(f"{BASE_URL}/papers")
    if not resp:
        raise RuntimeError("Cannot reach pastopic.com/papers")

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(r"/cambridge/(.+?)-(\d{4})/papers$")
    subjects, seen = [], set()

    for a in soup.find_all("a", href=pattern):
        href = a["href"]
        m = pattern.search(href)
        if not m:
            continue
        slug, code = m.group(1), m.group(2)
        if code in seen:
            continue
        seen.add(code)

        # Determine curriculum level
        level = "Other"
        for parent in a.parents:
            h = parent.find(re.compile(r"^h[1-6]$"))
            if h:
                txt = h.get_text(strip=True).lower()
                if "igcse" in txt:
                    level = "IGCSE"
                elif "a level" in txt or "as &" in txt or "a-level" in txt:
                    level = "A-Level"
                elif "o level" in txt:
                    level = "O-Level"
                if level != "Other":
                    break

        # Clean subject name
        raw = a.get_text(strip=True)
        name = re.sub(r"\d[\d\s]*papers.*$", "", raw, flags=re.I).strip()
        name = re.sub(r"\s+\d+$", "", name).strip() or slug.replace("-", " ").title()

        subjects.append({
            "level": level,
            "slug":  slug,
            "code":  code,
            "name":  name,
            "url":   f"{BASE_URL}/cambridge/{slug}-{code}/papers",
        })

    log_msg(f"Found {len(subjects)} subjects across all boards.")
    return subjects


def get_paper_pages(subject: dict) -> list[dict]:
    resp = fetch(subject["url"])
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(r"/cambridge/.+-\d{4}/papers/(\d{4})/([^/]+)/(\w+)$")
    seen, papers = set(), []
    for a in soup.find_all("a", href=pattern):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        m = pattern.search(href)
        papers.append({
            "year":    m.group(1),
            "session": m.group(2),
            "url":     urljoin(BASE_URL, href),
        })
    papers.sort(key=lambda p: p["year"], reverse=True)
    return papers


def get_pdfs(page_url: str) -> list[dict]:
    resp = fetch(page_url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    pdfs, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(FILES_CDN, href)
        if href in seen:
            continue
        seen.add(href)
        fname = href.split("/")[-1].lower()
        dtype = (
            "qp" if "_qp_" in fname else
            "ms" if "_ms_" in fname else
            "er" if "_er_" in fname else
            "gt" if "_gt_" in fname else
            "in" if "_in_" in fname else
            "other"
        )
        pdfs.append({"type": dtype, "filename": sanitize(fname), "url": href})
    return pdfs


def sanitize(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", s).strip()


def save_path(subject: dict, year: str, session_slug: str, filename: str) -> Path:
    sess   = SESSION_MAP.get(session_slug.lower(), session_slug.title())
    folder = sanitize(f"{subject['name'].replace(' ', '_')}_{subject['code']}")
    return DATA_DIR / subject["level"] / folder / year / sess / filename


# ─── Main scrape loop (runs in thread) ───────────────────────────────────────
def run_scraper():
    state["running"]        = True
    state["stop"]           = False
    state["stop_requested"] = False
    state["downloaded"]     = 0
    state["skipped"]        = 0
    state["failed"]         = 0
    state["total"]          = 0
    state["subjects_done"]  = []
    state["start_time"]     = __import__('time').time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    hf_token  = os.environ.get("HF_TOKEN", "")
    repo_id   = os.environ.get("DATASET_REPO", "")
    api       = HfApi(token=hf_token) if hf_token else None

    done = load_progress()
    state["downloaded"] = len(done)
    log_msg(f"Resuming — {len(done)} files already in progress.")

    try:
        subjects = discover_all_subjects()
        state["all_subjects"]   = subjects
        state["subjects_total"] = len(subjects)
    except Exception as e:
        log_msg(f"Discovery failed: {e}", "ERROR")
        state["running"] = False
        return

    pending_push: list[Path] = []

    for subj in subjects:
        if state["stop"]:
            break
        state["current"] = f"[{subj['level']}] {subj['name']} ({subj['code']})"
        log_msg(f"\n{'─'*55}\n  {state['current']}\n{'─'*55}")

        papers = get_paper_pages(subj)
        log_msg(f"  {len(papers)} paper pages found")

        for paper in papers:
            if state["stop"]:
                break
            pdfs = get_pdfs(paper["url"])

            for pdf in pdfs:
                if state["stop"]:
                    break
                sp  = save_path(subj, paper["year"], paper["session"], pdf["filename"])
                key = str(sp)
                state["total"] += 1

                if key in done or sp.exists():
                    state["skipped"] += 1
                    done.add(key)
                    continue

                sp.parent.mkdir(parents=True, exist_ok=True)
                resp = fetch(pdf["url"], stream=True)
                if not resp:
                    state["failed"] += 1
                    continue

                try:
                    with open(sp, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    done.add(key)
                    state["downloaded"] += 1
                    rel = sp.relative_to(DATA_DIR)
                    log_msg(f"  ✓ {rel}")
                    pending_push.append(sp)
                    # rolling rate (files/min)
                    elapsed = time.time() - state["start_time"]
                    if elapsed > 0:
                        state["rate"] = round(state["downloaded"] / elapsed * 60, 1)
                    time.sleep(DOWNLOAD_DELAY)

                    # Batch push to HF Dataset
                    if api and repo_id and len(pending_push) >= HF_PUSH_EVERY:
                        push_to_hf(pending_push, api, repo_id)
                        pending_push.clear()

                except Exception as e:
                    log_msg(f"  ✗ Save error: {e}", "ERROR")
                    state["failed"] += 1
                    if sp.exists():
                        sp.unlink()

        state["subjects_done"].append(subj["name"])
        save_progress(done)

    # Final push
    if api and repo_id and pending_push:
        push_to_hf(pending_push, api, repo_id)

    save_progress(done)
    generate_index()
    log_msg("\n✅ Scraping complete!")
    state["running"] = False


def generate_index():
    idx = DATA_DIR / "INDEX.txt"
    total = 0
    with open(idx, "w") as f:
        f.write("PastTopic — All Boards Paper Index\n" + "="*60 + "\n\n")
        for lvl in sorted(DATA_DIR.iterdir()):
            if not lvl.is_dir():
                continue
            f.write(f"\n[{lvl.name}]\n")
            for subj_dir in sorted(lvl.iterdir()):
                if not subj_dir.is_dir():
                    continue
                count = len(list(subj_dir.rglob("*.pdf")))
                total += count
                f.write(f"  {subj_dir.name:<55} ({count} files)\n")
                for yr in sorted(subj_dir.iterdir(), reverse=True):
                    if not yr.is_dir():
                        continue
                    for sess in sorted(yr.iterdir()):
                        if not sess.is_dir():
                            continue
                        files = sorted(sess.glob("*.pdf"))
                        f.write(f"    {yr.name} / {sess.name:<20} ({len(files)} PDFs)\n")
        f.write(f"\nTotal: {total} PDF files\n")
    log_msg(f"Index written → {idx}")
