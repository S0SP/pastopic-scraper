"""
scraper_ci.py — GitHub Actions version of the PastTopic scraper
- Runs in current working directory (no /data/ prefix)
- Pushes PDFs to HF Dataset in batches
- Stops ~20 min before GitHub's 6-hour limit
- Resumes automatically on next run via progress.json in HF Dataset
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
def load_progress() -> set:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        log.info(f"Resumed from local progress.json — {len(data)} files done.")
        return set(data)
    return set()


def save_progress(done: set):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(sorted(done), f)


# ─── HF Dataset push ─────────────────────────────────────────────────────────
def push_batch(api: HfApi, paths: list[Path], include_progress=False):
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
        api.create_commit(
            repo_id=DATASET_REPO, repo_type="dataset",
            operations=ops,
            commit_message=f"Add {len(ops)} files ({time.strftime('%Y-%m-%d %H:%M')})",
        )
        log.info(f"  [HF] Pushed {len(ops)} files to {DATASET_REPO}")
    except Exception as e:
        log.warning(f"  [HF] Push error: {e}")


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


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("PastTopic Scraper — GitHub Actions CI run")
    log.info(f"Max runtime: {MAX_RUN_SECONDS//60} min")
    log.info("=" * 65)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    api  = HfApi(token=HF_TOKEN) if HF_TOKEN else None
    done = load_progress()

    subjects = discover_all_subjects()
    log.info(f"Starting — {len(subjects)} subjects, {len(done)} already done.\n")

    pending: list[Path] = []
    total_dl = 0

    for subj in subjects:
        if not time_ok():
            log.info("Time limit approaching — stopping gracefully.")
            break

        log.info(f"\n[{subj['level']}] {subj['name']} ({subj['code']})")
        pages = get_paper_pages(subj)
        log.info(f"  {len(pages)} paper pages")

        for page in pages:
            if not time_ok():
                break
            for pdf in get_pdfs(page["url"]):
                if not time_ok():
                    break

                sp  = save_path(subj, page["year"], page["session"], pdf["filename"])
                key = str(sp)

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
                    pending.append(sp)
                    log.info(f"  + {sp.relative_to(OUTPUT_DIR)}")
                    time.sleep(DOWNLOAD_DELAY)

                    if len(pending) >= HF_PUSH_EVERY:
                        save_progress(done)
                        push_batch(api, pending, include_progress=True)
                        pending.clear()

                except Exception as e:
                    log.warning(f"  Save error: {e}")
                    if sp.exists(): sp.unlink()

    # Final push
    save_progress(done)
    if pending:
        push_batch(api, pending, include_progress=True)
    elif api:
        # Push updated progress even if no new files
        push_batch(api, [], include_progress=True)

    log.info(f"\n{'='*65}")
    log.info(f"Done. Downloaded {total_dl} new files this run.")
    log.info(f"Total progress: {len(done)} files.")
    log.info(f"Elapsed: {(time.time()-START_TIME)/60:.1f} min")


if __name__ == "__main__":
    main()
