"""GitHub submission service for course-organizer.

Handles validation of GitHub repository URLs, commit existence checks,
and fetching commit/diff information via the GitHub REST API.

Supports both public repositories (anonymous API access) and private
repositories (via optional GitHub personal access token).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

# GitHub API base
GITHUB_API = "https://api.github.com"


def _resolve_token(github_token: Optional[str] = None) -> Optional[str]:
    token = (github_token or "").strip() or (os.getenv("GITHUB_TOKEN") or "").strip()
    return token or None


def _auth_headers(github_token: Optional[str] = None) -> dict[str, str]:
    """Build request headers, using provided token or GITHUB_TOKEN from env."""
    headers = {"Accept": "application/vnd.github+json"}
    token = _resolve_token(github_token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_get(url: str, github_token: Optional[str] = None, timeout: int = 15) -> requests.Response:
    """GET from GitHub API. If auth fails (401), retry once without token for public access."""
    token = _resolve_token(github_token)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise GitHubError(f"Could not reach GitHub API: {exc}") from exc

    # Bad/expired token: retry anonymously so public repos still work
    if resp.status_code == 401 and token:
        try:
            resp = requests.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
        except requests.RequestException as exc:
            raise GitHubError(f"Could not reach GitHub API: {exc}") from exc
    return resp

# Regex for GitHub repository URLs (https://github.com/owner/repo or http://...)
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)(?:/)?$",
    re.IGNORECASE,
)


class GitHubError(Exception):
    """Raised when the GitHub API cannot validate a repo, commit, or branch."""


@dataclass
class RepoInfo:
    """Parsed GitHub repository information."""

    owner: str
    repo: str
    html_url: str
    clone_url: str
    default_branch: str
    is_private: bool
    size_kb: int


@dataclass
class CommitInfo:
    """Information about a single commit."""

    sha: str
    short_sha: str
    message: str
    author_name: str
    author_date: str
    url: str
    html_url: str


@dataclass
class CommitFileChange:
    """A single file change within a commit diff."""

    filename: str
    status: str  # added, modified, deleted, renamed
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None


@dataclass
class CommitDiff:
    """The full diff of a commit."""

    sha: str
    files: list[CommitFileChange]
    total_additions: int
    total_deletions: int
    total_changes: int


def _parse_repo_url(url: str, github_token: Optional[str] = None) -> RepoInfo:
    """Parse and validate a GitHub repository URL, returning metadata.

    Makes a live API call to verify the repository exists and is accessible.
    Raises GitHubError if the repo cannot be found or is inaccessible.
    """
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        raise GitHubError(
            "Invalid GitHub repository URL. Expected format: "
            "https://github.com/owner/repo"
        )

    owner, repo = m.group(1), m.group(2)
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = _github_get(api_url, github_token=github_token, timeout=15)

    if resp.status_code == 404:
        raise GitHubError(
            f"Repository '{owner}/{repo}' not found or is not publicly accessible. "
            "If it is a private repository, provide a GitHub personal access token."
        )
    if resp.status_code == 401:
        raise GitHubError(
            "GitHub authentication failed (401). The provided/configured token may be invalid or expired. "
            "Update GITHUB_TOKEN in .env or enter a valid token, then try again."
        )
    if resp.status_code == 403:
        raise GitHubError(
            "GitHub API rate limit exceeded or access forbidden. "
            "Try again later or provide a GitHub personal access token."
        )
    if resp.status_code != 200:
        raise GitHubError(f"GitHub API returned status {resp.status_code}.")

    data = resp.json()
    return RepoInfo(
        owner=owner,
        repo=repo,
        html_url=data.get("html_url", f"https://github.com/{owner}/{repo}"),
        clone_url=data.get("clone_url", f"https://github.com/{owner}/{repo}.git"),
        default_branch=data.get("default_branch", "main"),
        is_private=data.get("private", False),
        size_kb=data.get("size", 0),
    )


def _validate_commit(
    repo_owner: str, repo_name: str, commit_sha: str, github_token: Optional[str] = None
) -> CommitInfo:
    """Validate that a commit SHA exists in the repository and return metadata.

    Raises GitHubError if the commit cannot be found.
    """
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/commits/{commit_sha}"
    resp = _github_get(api_url, github_token=github_token, timeout=15)

    if resp.status_code == 404:
        raise GitHubError(
            f"Commit '{commit_sha[:7]}...' not found in repository "
            f"'{repo_owner}/{repo_name}'."
        )
    if resp.status_code == 401:
        raise GitHubError(
            "GitHub authentication failed (401). The provided/configured token may be invalid or expired. "
            "Update GITHUB_TOKEN in .env or enter a valid token, then try again."
        )
    if resp.status_code == 403:
        raise GitHubError(
            "GitHub API rate limit exceeded or access forbidden."
        )
    if resp.status_code != 200:
        raise GitHubError(f"GitHub API returned status {resp.status_code}.")

    data = resp.json()
    return CommitInfo(
        sha=data["sha"],
        short_sha=data["sha"][:7],
        message=data["commit"]["message"],
        author_name=data["commit"]["author"]["name"],
        author_date=data["commit"]["author"]["date"],
        url=data["url"],
        html_url=data["html_url"],
    )


def _get_commit_diff(
    repo_owner: str, repo_name: str, commit_sha: str, github_token: Optional[str] = None
) -> CommitDiff:
    """Fetch the full diff for a commit from the GitHub API."""
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/commits/{commit_sha}"
    resp = _github_get(api_url, github_token=github_token, timeout=15)

    if resp.status_code == 401:
        raise GitHubError(
            "GitHub authentication failed (401). The provided/configured token may be invalid or expired. "
            "Update GITHUB_TOKEN in .env or enter a valid token, then try again."
        )
    if resp.status_code != 200:
        raise GitHubError(f"GitHub API returned status {resp.status_code}.")

    data = resp.json()
    files = []
    for item in data.get("files", []):
        files.append(
            CommitFileChange(
                filename=item.get("filename", ""),
                status=item.get("status", ""),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
                changes=item.get("changes", 0),
                patch=item.get("patch"),
            )
        )

    return CommitDiff(
        sha=data["sha"],
        files=files,
        total_additions=data.get("stats", {}).get("additions", 0),
        total_deletions=data.get("stats", {}).get("deletions", 0),
        total_changes=data.get("stats", {}).get("total", 0),
    )


def validate_and_fetch_commit(
    repo_url: str,
    commit_sha: str,
    branch: Optional[str] = None,
    github_token: Optional[str] = None,
) -> CommitInfo:
    """Validate a repository URL and commit SHA, returning commit info.

    Performs:
    1. Validates the repo URL format
    2. Verifies the repository exists and is accessible
    3. Validates the commit SHA exists in the repo
    4. Optionally validates the branch (if provided)

    Raises GitHubError on any failure.
    """
    repo = _parse_repo_url(repo_url, github_token=github_token)
    return _validate_commit(repo.owner, repo.repo, commit_sha, github_token=github_token)


def get_commit_diff(
    repo_url: str,
    commit_sha: str,
    github_token: Optional[str] = None,
) -> CommitDiff:
    """Fetch the full diff for a commit from the GitHub API."""
    repo = _parse_repo_url(repo_url, github_token=github_token)
    return _get_commit_diff(repo.owner, repo.repo, commit_sha, github_token=github_token)


def validate_repo_accessible(repo_url: str, github_token: Optional[str] = None) -> RepoInfo:
    """Validate that a GitHub repository exists and is accessible.

    Returns repo metadata including whether it is private.
    """
    return _parse_repo_url(repo_url, github_token=github_token)
