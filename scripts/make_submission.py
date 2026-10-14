#!/usr/bin/env python3
"""
scripts/make_submission.py - Packaging and Submission Verification Script.
Performs:
  1. Quick sanity test run.
  2. PDF page count check (HARD LIMIT: <= 2 pages).
  3. Secret & token security scan (aborts if real API keys/credentials found).
  4. Packaging into MayaCardio_Submission_<name>.zip excluding caches, venvs, DBs, git.
  5. Displays zip manifest and file size.
"""

import argparse
import os
import re
import subprocess
import sys
import zipfile
import pymupdf

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def step_quick_tests() -> bool:
    print("--> Step 1: Running fast sanity tests...")
    cmd = [sys.executable, "-m", "unittest", "tests.test_mock_standin", "tests.test_phase4_differential_processing"]
    res = subprocess.run(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if res.returncode != 0:
        print("FAIL: Sanity tests failed!")
        print(res.stdout)
        return False
    print("    [PASS] Sanity tests passed.")
    return True


def step_check_pdf() -> bool:
    print("--> Step 2: Checking docs/design-note.pdf page count (hard limit <= 2)...")
    pdf_path = os.path.join(REPO_ROOT, "docs", "design-note.pdf")
    if not os.path.exists(pdf_path):
        print("FAIL: docs/design-note.pdf not found! Running generate_design_note_pdf.py...")
        subprocess.run([sys.executable, "scripts/generate_design_note_pdf.py"], cwd=REPO_ROOT, check=True)

    doc = pymupdf.open(pdf_path)
    pages = len(doc)
    doc.close()
    if pages > 2:
        print(f"FAIL: docs/design-note.pdf has {pages} pages! STRICT HARD LIMIT is <= 2 pages.")
        return False
    print(f"    [PASS] docs/design-note.pdf is exactly {pages} pages (<= 2).")
    return True


def step_secret_scan() -> bool:
    print("--> Step 3: Scanning repository for real secrets and credentials...")
    secret_patterns = [
        re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key
        re.compile(r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----"),
        re.compile(r"ghp_[0-9a-zA-Z]{36}"),  # GitHub Personal Access Token
        re.compile(r"sk-[a-zA-Z0-9]{48}"),  # OpenAI API Key
    ]

    violations = []
    for root, dirs, files in os.walk(REPO_ROOT):
        # Skip git and cache
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv", ".pytest_cache")]
        for file in files:
            if file.endswith((".py", ".md", ".sh", ".toml", ".json", ".txt")):
                fpath = os.path.join(root, file)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        for pat in secret_patterns:
                            if pat.search(content):
                                violations.append((os.path.relpath(fpath, REPO_ROOT), pat.pattern))
                except Exception:
                    pass

    if violations:
        print("FAIL: Found suspicious credentials / secrets in tree:")
        for path, pat in violations:
            print(f"  - {path} matched pattern {pat}")
        return False
    print("    [PASS] Zero secrets or private keys detected in repository.")
    return True


def step_create_zip(candidate_name: str) -> str:
    print(f"--> Step 4: Packaging submission archive for '{candidate_name}'...")
    zip_filename = f"MayaCardio_Submission_{candidate_name}.zip"
    zip_path = os.path.join(REPO_ROOT, zip_filename)

    exclude_patterns = [
        ".git", "__pycache__", ".pytest_cache", ".venv", "venv",
        ".coverage", "htmlcov", ".db", ".db-wal", ".db-shm", ".sqlite",
        ".zip", ".tar.gz", "reserve.dat"
    ]

    def should_exclude(relpath: str) -> bool:
        normalized = relpath.replace("\\", "/")
        for part in normalized.split("/"):
            if part in (".git", "__pycache__", ".venv", "venv", ".pytest_cache"):
                return True
        for pat in exclude_patterns:
            if normalized.endswith(pat) or pat in normalized:
                return True
        return False

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(REPO_ROOT):
            # Prune directory search
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv", ".pytest_cache")]
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, REPO_ROOT)
                if should_exclude(rel_path):
                    continue
                # Zip entry path inside MayaCardio_Submission_<name>/
                archive_name = os.path.join(f"MayaCardio_Submission_{candidate_name}", rel_path).replace("\\", "/")
                zf.write(full_path, archive_name)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    size_kb = os.path.getsize(zip_path) / 1024
    print(f"    [PASS] Created {zip_filename} ({size_kb:.1f} KB / {size_mb:.2f} MB)")
    return zip_path


def print_manifest(zip_path: str):
    print("\n======================================================================")
    print("SUBMISSION ZIP MANIFEST")
    print("======================================================================")
    with zipfile.ZipFile(zip_path, "r") as zf:
        infolist = zf.infolist()
        infolist.sort(key=lambda x: x.filename)
        for info in infolist:
            print(f"  {info.file_size:7d} B  {info.filename}")
    print("======================================================================")
    print(f"Total files in archive: {len(infolist)}")
    print(f"Final zip size: {os.path.getsize(zip_path) / 1024:.1f} KB")
    print(f"Archive path: {zip_path}")
    print("======================================================================")


def main():
    parser = argparse.ArgumentParser(description="Create MayaCardio candidate submission zip")
    parser.add_argument("--name", default="Karan_Rai", help="Candidate name for zip filename")
    args = parser.parse_args()

    if not step_quick_tests():
        sys.exit(1)
    if not step_check_pdf():
        sys.exit(1)
    if not step_secret_scan():
        sys.exit(1)

    zip_path = step_create_zip(args.name)
    print_manifest(zip_path)
    print("\nNext step: Record video/demo.mp4 (<= 5 min) and drop into archive as per HUMAN_TODO.md.")


if __name__ == "__main__":
    main()
