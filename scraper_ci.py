"""
scraper_ci.py — GitHub Actions version of the PastTopic scraper
- Runs in current working directory (no /data/ prefix)
- Pushes PDFs to HF Dataset in batches
- Stops ~20 min before GitHub's 6-hour limit
- Resumes automatically on next run via progress.json in HF Dataset
- Skips fully-done subjects to avoid wasted HTTP calls on resume
"""

import os, re, time, json, logging, requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from huggingface_hub import HfApi, CommitOperationAdd

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_URL      = "https://pastopic.com"
FILES_CDN     = "https://files.pastopic.com"
OUTPUT_DIR    = Path("papers")
PROGRESS_FILE = Path("progress.json")
LOG_FILE      = Path("scraper.log")

HF_TOKEN     = os.environ.get("HF_TOKEN", "")
DATASET_REPO = os.environ.get("DATASET_REPO", "")

DOWNLOAD_DELAY  = 0.5
REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3
HF_PUSH_EVERY   = 40   # push to HF every N new files

# Stop 20 min before the 350-min GitHub Actions timeout
MAX_RUN_SECONDS = (350 - 20) * 60
START_TIME      = time.time()

SESSION_MAP = {
    "may-june": "May-June", "oct-nov": "Oct-Nov",
    "feb-march": "Feb-March", "march": "Feb-March",
}

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── HTTP ─────────────────────────────────────────────────────────────────────
http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0.0.0"})


def time_ok() -> bool:
    return (time.time() - START_TIME) < MAX_RUN_SECONDS


def fetch(url, stream=False):
    for attempt in range(1, MAX_RETRIES + 1):
        if not time_ok():
            return None
        try:
            r = http.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log.warning(f"  Retry {attempt}/{MAX_RETRIES} [{url[:60]}]: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return None


# ─── Progress ─────────────────────────────────────────────────────────────────
def load_progress() -> tuple[set, set]:
    """Returns (done_files: set, done_subjects: set)"""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        # Support old format (plain list) and new format (dict with files + subjects)
        if isinstance(data, list):
            done_files = set(data)
            done_subjects = set()
        else:
            done_files = set(data.get("files", []))
            done_subjects = set(data.get("subjects", []))
        log.info(f"Resumed — {len(done_files)} files done, {len(done_subjects)} subjects fully complete.")
        return done_files, done_subjects
    return set(), set()


def save_progress(done: set, done_subjects: set):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"files": sorted(done), "subjects": sorted(done_subjects)}, f)


# ─── HF Dataset push ─────────────────────────────────────────────────────────
def push_batch(api: HfApi, paths: list[Path], include_progress=False, msg_prefix=""):
    if not api or not DATASET_REPO:
        return
    try:
        ops = []
        for p in paths:
            rel = str(p.relative_to(OUTPUT_DIR)).replace("\\", "/")
            ops.append(CommitOperationAdd(path_in_repo=f"papers/{rel}",
                                          path_or_fileobj=str(p)))
        if include_progress and PROGRESS_FILE.exists():
            ops.append(CommitOperationAdd(path_in_repo="progress.json",
                                          path_or_fileobj=str(PROGRESS_FILE)))
        if not ops:
            return
        label = msg_prefix or f"Add {len(ops)} files"
        api.create_commit(
            repo_id=DATASET_REPO, repo_type="dataset",
            operations=ops,
            commit_message=f"{label} ({time.strftime('%Y-%m-%d %H:%M')})",
        )
        log.info(f"  [HF] Pushed {len(ops)} ops to {DATASET_REPO}")
    except Exception as e:
        log.warning(f"  [HF] Push error: {e}")


def push_progress_only(api: HfApi, done: set, done_subjects: set, label="Update progress"):
    """Push just the progress.json to HF so the dashboard refreshes."""
    if not api or not DATASET_REPO:
        return
    save_progress(done, done_subjects)
    try:
        api.create_commit(
            repo_id=DATASET_REPO, repo_type="dataset",
            operations=[CommitOperationAdd(path_in_repo="progress.json",
                                           path_or_fileobj=str(PROGRESS_FILE))],
            commit_message=f"{label} ({time.strftime('%Y-%m-%d %H:%M')} UTC)",
        )
        log.info(f"  [HF] Progress.json updated — {len(done)} files, {len(done_subjects)} subjects complete")
    except Exception as e:
        log.warning(f"  [HF] Progress push error: {e}")


# ─── Discovery ────────────────────────────────────────────────────────────────
def discover_all_subjects():
    log.info("Discovering all subjects …")
    resp = fetch(f"{BASE_URL}/papers")
    if not resp:
        raise RuntimeError("Cannot reach pastopic.com/papers")

    soup = BeautifulSoup(resp.text, "html.parser")
    pat  = re.compile(r"/cambridge/(.+?)-(\d{4})/papers$")
    subjects, seen = [], set()

    for a in soup.find_all("a", href=pat):
        m = pat.search(a["href"])
        if not m:
            continue
        slug, code = m.group(1), m.group(2)
        if code in seen:
            continue
        seen.add(code)

        level = "Other"
        for p in a.parents:
            h = p.find(re.compile(r"^h[1-6]$"))
            if h:
                t = h.get_text().lower()
                if "igcse"  in t: level = "IGCSE"
                elif "a level" in t or "as &" in t: level = "A-Level"
                elif "o level" in t: level = "O-Level"
                if level != "Other": break

        raw  = a.get_text(strip=True)
        name = re.sub(r"\d[\d\s]*papers.*$", "", raw, flags=re.I).strip()
        name = re.sub(r"\s+\d+$", "", name).strip() or slug.replace("-"," ").title()

        subjects.append({"level": level, "slug": slug, "code": code,
                          "name": name,
                          "url":  f"{BASE_URL}/cambridge/{slug}-{code}/papers"})

    log.info(f"  {len(subjects)} subjects found across all boards.")
    return subjects


def get_paper_pages(subject):
    resp = fetch(subject["url"])
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    pat  = re.compile(r"/cambridge/.+-\d{4}/papers/(\d{4})/([^/]+)/(\w+)$")
    seen, pages = set(), []
    for a in soup.find_all("a", href=pat):
        h = a["href"]
        if h in seen: continue
        seen.add(h)
        m = pat.search(h)
        pages.append({"year": m.group(1), "session": m.group(2),
                      "url":  urljoin(BASE_URL, h)})
    pages.sort(key=lambda p: p["year"], reverse=True)
    return pages


def get_pdfs(page_url):
    resp = fetch(page_url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    pdfs, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.I)):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(FILES_CDN, href)
        if href in seen: continue
        seen.add(href)
        fname = sanitize(href.split("/")[-1].lower())
        pdfs.append({"filename": fname, "url": href})
    return pdfs


def sanitize(s):
    return re.sub(r'[<>:"/\\|?*]', "_", s).strip()


def save_path(subject, year, session_slug, filename):
    sess   = SESSION_MAP.get(session_slug.lower(), session_slug.title())
    folder = sanitize(f"{subject['name'].replace(' ','_')}_{subject['code']}")
    return OUTPUT_DIR / subject["level"] / folder / year / sess / filename


def build_subject_prefix(subject) -> str:
    """Return the folder prefix for a subject so we can check done coverage."""
    folder = sanitize(f"{subject['name'].replace(' ','_')}_{subject['code']}")
    return f"{OUTPUT_DIR}/{subject['level']}/{folder}/".replace("\\", "/")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("PastTopic Scraper — GitHub Actions CI run")
    log.info(f"Max runtime: {MAX_RUN_SECONDS//60} min")
    log.info("=" * 65)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    api  = HfApi(token=HF_TOKEN) if HF_TOKEN else None
    done, done_subjects = load_progress()

    # Push progress.json immediately so dashboard shows latest count
    if api and done:
        log.info("Pushing current progress to HF for dashboard refresh …")
        push_progress_only(api, done, done_subjects, label="Run started — current progress")

    subjects = discover_all_subjects()
    log.info(f"Starting — {len(subjects)} subjects, {len(done)} files done, {len(done_subjects)} subjects fully skippable.\n")

    # Pre-compute done file counts per subject prefix for display
    done_prefixes = {}
    for key in done:
        slash_key = key.replace("\\", "/")
        parts = slash_key.split("/")
        if len(parts) >= 3:
            prefix = "/".join(parts[:3]) + "/"
            done_prefixes[prefix] = done_prefixes.get(prefix, 0) + 1

    pending: list[Path] = []
    total_dl = 0
    subjects_skipped = 0

    for subj in subjects:
        if not time_ok():
            log.info("Time limit approaching — stopping gracefully.")
            break

        subj_key = f"{subj['level']}/{subj['code']}"
        prefix   = build_subject_prefix(subj)
        done_count = done_prefixes.get(prefix, 0)

        # ── FAST SKIP: subject fully confirmed complete from a prior run ──
        if subj_key in done_subjects:
            log.info(f"[{subj['level']}] {subj['name']} ({subj['code']}) — SKIPPED ({done_count} files, confirmed complete)")
            subjects_skipped += 1
            continue

        log.info(f"\n[{subj['level']}] {subj['name']} ({subj['code']}) — {done_count} done locally (checking for new files)")
        pages = get_paper_pages(subj)
        log.info(f"  {len(pages)} paper pages")

        subj_new = 0

        for page in pages:
            if not time_ok():
                break
            pdfs = get_pdfs(page["url"])

            for pdf in pdfs:
                if not time_ok():
                    break

                sp  = save_path(subj, page["year"], page["session"], pdf["filename"])
                key = str(sp).replace("\\", "/")

                if key in done or sp.exists():
                    done.add(key)
                    continue

                sp.parent.mkdir(parents=True, exist_ok=True)
                resp = fetch(pdf["url"], stream=True)
                if not resp:
                    continue

                try:
                    with open(sp, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    done.add(key)
                    total_dl += 1
                    subj_new += 1
                    pending.append(sp)
                    log.info(f"  + {sp.relative_to(OUTPUT_DIR)}")
                    time.sleep(DOWNLOAD_DELAY)

                    if len(pending) >= HF_PUSH_EVERY:
                        save_progress(done, done_subjects)
                        push_batch(api, pending, include_progress=True)
                        pending.clear()

                except Exception as e:
                    log.warning(f"  Save error: {e}")
                    if sp.exists(): sp.unlink()

        if subj_new == 0:
            log.info(f"  ✓ Subject complete — marking for fast-skip on future runs")
            done_subjects.add(subj_key)
            subjects_skipped += 1

    # Final push
    save_progress(done, done_subjects)
    if pending:
        push_batch(api, pending, include_progress=True)
    elif api:
        # Push updated progress (with done_subjects) even if no new files
        push_progress_only(api, done, done_subjects, label="Run complete — final progress")

    log.info(f"\n{'='*65}")
    log.info(f"Done. Downloaded {total_dl} new files this run.")
    log.info(f"Total progress: {len(done)} files.")
    log.info(f"Subjects fully complete: {subjects_skipped}/{len(subjects)}")
    log.info(f"Elapsed: {(time.time()-START_TIME)/60:.1f} min")


if __name__ == "__main__":
    main()
