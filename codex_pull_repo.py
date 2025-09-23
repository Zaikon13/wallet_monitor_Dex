# codex_pull_repo.py
from __future__ import annotations
import os, sys, json, base64, requests, textwrap

OWNER = "Zaikon13"
REPO  = "wallet_monitor_Dex"
REF   = None  # use default branch; or set "main"

API = "https://api.github.com"

def h():
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "codex-pull/1.0"}
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    return hdr

def jget(url, params=None):
    r = requests.get(url, headers=h(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def resolve_ref():
    if REF:
        return REF
    info = jget(f"{API}/repos/{OWNER}/{REPO}")
    return info.get("default_branch") or "main"

def get_tree(branch: str):
    # Resolve branch -> commit sha -> tree (recursive)
    try:
        ref = jget(f"{API}/repos/{OWNER}/{REPO}/git/refs/heads/{branch}")
        sha = ref["object"]["sha"]
    except requests.HTTPError:
        # Maybe direct SHA or tag
        sha = branch
    tree = jget(f"{API}/repos/{OWNER}/{REPO}/git/trees/{sha}", params={"recursive": "1"})
    return sha, tree

def get_blob(sha: str):
    return jget(f"{API}/repos/{OWNER}/{REPO}/git/blobs/{sha}")

def print_header(title: str):
    print("\n" + "="*len(title))
    print(title)
    print("="*len(title))

def show_tree(tree_json: dict):
    rows = []
    for n in tree_json.get("tree", []):
        typ = n["type"]
        path = n["path"]
        sha  = n.get("sha","")[:7]
        size = n.get("size")
        if typ == "blob":
            rows.append(f"{typ:4} {size or 0:>10}  {sha:7}  {path}")
        else:
            rows.append(f"{typ:4} {'-':>10}  {sha:7}  {path}")
    rows.sort(key=lambda s: s.split()[-1])
    print_header("FULL REPO TREE")
    for line in rows:
        print(line)

def sample_file(path: str, sha: str, max_chars: int = 1600):
    try:
        blob = get_blob(sha)
        if blob.get("encoding") != "base64":
            return None
        raw = base64.b64decode(blob.get("content",""))
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw[:max_chars].decode("utf-8", errors="replace")
        return text[:max_chars]
    except Exception as e:
        return f"<<error reading {path}: {e}>>"

def show_heads(tree_json: dict):
    # Critical files we care about
    wanted = {
        "main.py",
        "requirements.txt",
        "core/tz.py",
        "core/config.py",
        "core/signals/server.py",
        "core/wallet_monitor.py",
        "core/watch.py",
        "core/holdings.py",
        "core/guards.py",
        "core/providers/cronos.py",
        "reports/scheduler.py",
        "reports/day_report.py",
        "telegram/api.py",
        "telegram/dispatcher.py",
        "telegram/formatters.py",
    }
    index = { n["path"]: n for n in tree_json.get("tree", []) if n["type"]=="blob" }
    present = [p for p in wanted if p in index]
    missing = [p for p in wanted if p not in index]

    print_header("CRITICAL FILES — PRESENCE")
    for p in present:
        sz = index[p].get("size")
        sha = (index[p].get("sha") or "")[:7]
        print(f"[OK]  {p}  ({sz} bytes, {sha})")
    for p in missing:
        print(f"[MISS] {p}")

    if present:
        print_header("CRITICAL FILE HEADS (first lines)")
    for p in present:
        sha = index[p]["sha"]
        head = sample_file(p, sha)
        print(f"\n----- {p} -----")
        if not head:
            print("<<no preview>>")
        else:
            print(head)

def main():
    print_header(f"FETCHING {OWNER}/{REPO}")
    branch = resolve_ref()
    print(f"Branch: {branch}")
    sha, tree = get_tree(branch)
    print(f"Commit: {sha[:12]}")
    show_tree(tree)
    show_heads(tree)
    print("\nDONE.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"<<fatal: {e}>>", file=sys.stderr)
        sys.exit(1)
