"""
repo_indexer.py

Runs after a user connects the GitHub App. It:
  1. gets a short-lived installation token,
  2. lists the repos the installation can access and upserts them into `repositories`,
  3. fetches each repo's full file tree (ONE API call per repo),
  4. upserts folders/files into `repo_files` (only changed rows are touched),
  5. tombstones files that no longer exist (deleted_at), never physically deletes,
  6. hands changed .py files to the symbol indexer (Tree-sitter, Person 1).

Install:  pip install httpx "psycopg[binary]" pyjwt cryptography
Env vars: DATABASE_URL, GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM text)

Call `index_user_installation(installation_id)` from a BACKGROUND task
(FastAPI BackgroundTasks first, Celery/RQ later). Never inside the login request.
"""
import os
import posixpath
import time

import httpx
import jwt
import psycopg

GITHUB_API = "https://api.github.com"
APP_ID = os.environ["GITHUB_APP_ID"]
PRIVATE_KEY = os.environ["GITHUB_APP_PRIVATE_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".idea"}

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".html": "html", ".css": "css", ".json": "json",
    ".md": "markdown", ".txt": "text", ".yml": "yaml", ".yaml": "yaml",
    ".java": "java", ".go": "go", ".rs": "rust", ".sql": "sql",
}


# ---------------------------------------------------------------- GitHub auth
def _app_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": APP_ID}
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


def get_installation_token(installation_id: int) -> str:
    r = httpx.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["token"]


def _gh(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GITHUB_API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=60,
    )


# ---------------------------------------------------------------- Step 1: repos
def sync_installation_repos(conn, token: str, installation_id: int) -> list[int]:
    """Upsert every repo the installation can access. Returns local repositories.id values."""
    local_ids, page = [], 1
    with _gh(token) as gh:
        while True:
            r = gh.get("/installation/repositories", params={"per_page": 100, "page": page})
            r.raise_for_status()
            repos = r.json()["repositories"]
            if not repos:
                break
            for repo in repos:
                row = conn.execute(
                    """
                    INSERT INTO repositories (github_repo_id, owner, name, default_branch, installation_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (github_repo_id) DO UPDATE
                      SET owner = EXCLUDED.owner, name = EXCLUDED.name,
                          default_branch = EXCLUDED.default_branch,
                          installation_id = EXCLUDED.installation_id
                    RETURNING id
                    """,
                    (repo["id"], repo["owner"]["login"], repo["name"],
                     repo["default_branch"], installation_id),
                ).fetchone()
                local_ids.append(row[0])
            page += 1
    return local_ids


# ---------------------------------------------------------------- Step 2: file tree
def fetch_tree(token: str, owner: str, name: str, branch: str):
    """Returns (commit_sha, tree_entries) using a single recursive tree call."""
    with _gh(token) as gh:
        c = gh.get(f"/repos/{owner}/{name}/commits/{branch}")
        c.raise_for_status()
        commit_sha = c.json()["sha"]
        tree_sha = c.json()["commit"]["tree"]["sha"]

        t = gh.get(f"/repos/{owner}/{name}/git/trees/{tree_sha}", params={"recursive": "1"})
        t.raise_for_status()
        data = t.json()
        if data.get("truncated"):
            # Very large repo: GitHub cut the list. Fallback = walk folder by folder
            # (non-recursive) instead. Not needed for student-scale projects.
            print(f"WARNING: tree truncated for {owner}/{name}")
        return commit_sha, data["tree"]


def _skip(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/"))


def _to_row(repo_id: int, entry: dict, commit_sha: str):
    path = entry["path"]
    ext = posixpath.splitext(path)[1].lower()
    is_dir = entry["type"] == "tree"
    return (
        repo_id,
        path,
        posixpath.dirname(path),            # '' for root-level items
        posixpath.basename(path),
        "dir" if is_dir else "file",
        None if is_dir else LANGUAGES.get(ext),
        entry.get("size"),                  # None for dirs
        entry["sha"],                       # blob SHA (file) or tree SHA (dir)
        commit_sha,
    )


# ---------------------------------------------------------------- Step 3: index one repo
def index_repo(conn, token: str, repo_id: int) -> None:
    owner, name, branch = conn.execute(
        "SELECT owner, name, default_branch FROM repositories WHERE id = %s", (repo_id,)
    ).fetchone()

    commit_sha, tree = fetch_tree(token, owner, name, branch)
    entries = [e for e in tree if e["type"] in ("blob", "tree") and not _skip(e["path"])]
    rows = [_to_row(repo_id, e, commit_sha) for e in entries]
    live_paths = [r[1] for r in rows]

    with conn.cursor() as cur:
        # Upsert. The WHERE clause means unchanged rows are NOT rewritten.
        cur.executemany(
            """
            INSERT INTO repo_files
              (repo_id, path, parent_path, name, node_type, language, size_bytes, content_hash, indexed_sha)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (repo_id, path) DO UPDATE
              SET parent_path  = EXCLUDED.parent_path,
                  name         = EXCLUDED.name,
                  node_type    = EXCLUDED.node_type,
                  language     = EXCLUDED.language,
                  size_bytes   = EXCLUDED.size_bytes,
                  content_hash = EXCLUDED.content_hash,
                  indexed_sha  = EXCLUDED.indexed_sha,
                  deleted_at   = NULL,
                  updated_at   = now()
              WHERE repo_files.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                 OR repo_files.deleted_at IS NOT NULL
            """,
            rows,
        )

        # Tombstone files that disappeared from GitHub (history is kept).
        cur.execute(
            """
            UPDATE repo_files SET deleted_at = now(), updated_at = now()
            WHERE repo_id = %s AND deleted_at IS NULL AND NOT (path = ANY(%s))
            """,
            (repo_id, live_paths),
        )

        cur.execute("UPDATE repositories SET last_indexed_sha = %s WHERE id = %s", (commit_sha, repo_id))

    # Hand changed Python files to the Tree-sitter indexer (Person 1).
    changed = conn.execute(
        """
        SELECT id, path FROM repo_files
        WHERE repo_id = %s AND language = 'python' AND deleted_at IS NULL AND indexed_sha = %s
        """,
        (repo_id, commit_sha),
    ).fetchall()
    index_symbols(conn, token, repo_id, owner, name, commit_sha, changed)


def index_symbols(conn, token, repo_id, owner, name, commit_sha, files) -> None:
    """
    PLACEHOLDER for the Tree-sitter step (code_symbols + symbol_edges).
    For each (file_id, path) in `files`:
      1. download the file:  GET /repos/{owner}/{name}/contents/{path}?ref={commit_sha}
      2. parse functions/classes with Tree-sitter
      3. upsert into code_symbols on (file_id, qualified_name, kind)
      4. delete + re-insert that file's outgoing symbol_edges
      5. re-resolve edges where target_symbol_id IS NULL
    """
    pass


# ---------------------------------------------------------------- Entry point
def index_user_installation(installation_id: int) -> None:
    """
    Call this in a background task:
      - right after the user installs the GitHub App / finishes login
      - on the `installation` (created) and `installation_repositories` (added) webhooks
    """
    token = get_installation_token(installation_id)
    with psycopg.connect(DATABASE_URL) as conn:
        repo_ids = sync_installation_repos(conn, token, installation_id)
        conn.commit()
        for repo_id in repo_ids:
            try:
                index_repo(conn, token, repo_id)
                conn.commit()
            except Exception as exc:        # one bad repo must not stop the others
                conn.rollback()
                print(f"index failed for repo {repo_id}: {exc}")


# ---------------------------------------------------------------- Wiring example (FastAPI)
# from fastapi import BackgroundTasks
#
# @router.get("/auth/github/callback")
# def callback(..., background: BackgroundTasks):
#     ...  # upsert user, create session, upsert user_installations / selected_repositories
#     background.add_task(index_user_installation, installation_id)
#     return RedirectResponse("/dashboard")
