"""
deploy_to_hf.py -- One-shot deployer for PastTopic Scraper Space
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
from huggingface_hub import HfApi

TOKEN      = os.environ.get("HF_TOKEN", "")   # set via: $env:HF_TOKEN="hf_..."
SPACE_NAME = "pastopic-scraper"
DATA_NAME  = "pastopic-papers"
FILES_DIR  = r"d:\work\content\pastopic-cloud"

api = HfApi(token=TOKEN)

# ── 1. Who am I? ──────────────────────────────────────────────────────────────
user = api.whoami()
username = user["name"]
print(f"✅ Logged in as: {username}")

space_id   = f"{username}/{SPACE_NAME}"
dataset_id = f"{username}/{DATA_NAME}"

# ── 2. Create Dataset repo (for persistent PDF storage) ───────────────────────
print(f"\n📦 Creating dataset repo: {dataset_id}")
try:
    api.create_repo(
        repo_id=DATA_NAME,
        repo_type="dataset",
        private=False,
        exist_ok=True,
    )
    print(f"   ✅ Dataset ready: https://huggingface.co/datasets/{dataset_id}")
except Exception as e:
    print(f"   ⚠️  Dataset create: {e}")

# ── 3. Create Space ────────────────────────────────────────────────────────────
print(f"\n🚀 Creating Space: {space_id}")
try:
    api.create_repo(
        repo_id=SPACE_NAME,
        repo_type="space",
        space_sdk="gradio",
        private=False,
        exist_ok=True,
    )
    print(f"   ✅ Space created: https://huggingface.co/spaces/{space_id}")
except Exception as e:
    print(f"   ⚠️  Space create: {e}")

# ── 4. Upload all app files ────────────────────────────────────────────────────
files = ["app.py", "scraper.py", "requirements.txt", "README.md"]
print(f"\n📤 Uploading {len(files)} files …")
for fname in files:
    local = os.path.join(FILES_DIR, fname)
    if not os.path.exists(local):
        print(f"   ⚠️  Missing: {local}")
        continue
    try:
        api.upload_file(
            path_or_fileobj=local,
            path_in_repo=fname,
            repo_id=space_id,
            repo_type="space",
            commit_message=f"Add {fname}",
        )
        print(f"   ✅ Uploaded: {fname}")
    except Exception as e:
        print(f"   ❌ Failed {fname}: {e}")

# ── 5. Set Space secrets ───────────────────────────────────────────────────────
print(f"\n🔐 Setting Space secrets …")
try:
    api.add_space_secret(space_id, "HF_TOKEN",     TOKEN)
    print(f"   ✅ HF_TOKEN set")
except Exception as e:
    print(f"   ⚠️  HF_TOKEN: {e}")

try:
    api.add_space_secret(space_id, "DATASET_REPO", dataset_id)
    print(f"   ✅ DATASET_REPO set → {dataset_id}")
except Exception as e:
    print(f"   ⚠️  DATASET_REPO: {e}")

# ── Done ──────────────────────────────────────────────────────────────────────
print(f"""
{'='*60}
🎉 DEPLOYMENT COMPLETE!

  Space URL  : https://huggingface.co/spaces/{space_id}
  Dataset URL: https://huggingface.co/datasets/{dataset_id}

  ➡  Open the Space URL, click  ▶ Start Scraping
  ➡  Watch live stats and log refresh every 5 seconds
  ➡  When done, click 🗜️ Create ZIP & Download
{'='*60}
""")
