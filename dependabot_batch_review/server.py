"""
Local HTTP server that serves the dashboard and exposes a JSON API
for fetching Dependabot data directly from GitHub.
"""

import dataclasses
import json
import os
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .alerts import Vulnerability, fetch_alerts
from .github_client import GitHubClient
from .review import (
    CheckStatus,
    DependencyUpdatePR,
    analyze_risk,
    fetch_dependency_prs,
    map_risk_to_priority,
    merge_pr,
)


def _serialize(obj: Any) -> Any:
    """Make dataclasses / enums JSON-serialisable."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, CheckStatus):
        return obj.description
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _pr_to_alert_dict(pr: DependencyUpdatePR) -> dict[str, Any]:
    """Convert a DependencyUpdatePR into the flat alert dict the dashboard expects."""
    url_parts = pr.url.split("/")
    repo_owner = url_parts[-4]
    repo_name = url_parts[-3]
    pr_number = url_parts[-1]

    risk = analyze_risk(pr)
    priority = map_risk_to_priority(risk.level)

    return {
        "repo": f"{repo_owner}/{repo_name}",
        "package": pr.group_name,
        "ecosystem": pr.package_type,
        "severity": risk.level,
        "priority": priority,
        "ghsa": pr.ghsa_id or "",
        "advisorySummary": pr.advisory_summary or "",
        "advisoryUrl": pr.advisory_url or "",
        "prUrl": pr.url,
        "prNumber": int(pr_number),
        "prId": pr.id,
        "checkStatus": pr.check_status.description,
        "mergeMethod": pr.merge_method,
        "merged": "No",
        "notes": "\n".join(risk.reasons),
        "updates": [
            {
                "name": u.name,
                "from": u.from_version,
                "to": u.to_version,
            }
            for u in pr.updates
        ],
    }


def _vuln_to_dict(vuln: Vulnerability, organization: str) -> dict[str, Any]:
    """Convert a Vulnerability into a JSON-friendly dict."""
    return {
        "repo": f"{organization}/{vuln.repo}",
        "packageName": vuln.package_name,
        "ecosystem": vuln.ecosystem,
        "severity": vuln.severity,
        "title": vuln.title,
        "url": vuln.url,
        "pr": vuln.pr,
        "versionRange": vuln.version_range,
        "createdAt": vuln.created_at,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the Dependabot dashboard."""

    gh_client: GitHubClient
    default_org: str
    project_root: str

    def __init__(
        self,
        *args: Any,
        gh_client: GitHubClient,
        default_org: str,
        project_root: str,
        **kwargs: Any,
    ) -> None:
        self.gh_client = gh_client
        self.default_org = default_org
        self.project_root = project_root
        super().__init__(*args, directory=project_root, **kwargs)

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self._serve_dashboard()
        elif path == "/api/alerts":
            self._handle_alerts(params)
        elif path == "/api/vulnerabilities":
            self._handle_vulnerabilities(params)
        else:
            # Fall through to serve static files (CSS / JS / etc.)
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/merge":
            self._handle_merge()
        else:
            self._send_json({"error": "Not found"}, status=404)

    # --------------------------------------------------------- route handlers

    def _serve_dashboard(self) -> None:
        dashboard_path = os.path.join(self.project_root, "dependabot-dashboard.html")
        try:
            with open(dashboard_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json({"error": "Dashboard HTML not found"}, status=404)

    def _handle_alerts(self, params: dict[str, list[str]]) -> None:
        org = params.get("org", [self.default_org])[0]
        repo_filter: str | None = params.get("repo", [None])[0]
        pkg_type: str | None = params.get("type", [None])[0]

        try:
            prs = fetch_dependency_prs(
                self.gh_client,
                organization=org,
                repo_filter=repo_filter,
            )

            if pkg_type:
                prs = [p for p in prs if p.package_type == pkg_type]

            alerts = [_pr_to_alert_dict(pr) for pr in prs]
            self._send_json({"alerts": alerts, "organization": org})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_vulnerabilities(self, params: dict[str, list[str]]) -> None:
        org = params.get("org", [self.default_org])[0]

        try:
            vulns = fetch_alerts(self.gh_client, organization=org)
            data = [_vuln_to_dict(v, org) for v in vulns]
            self._send_json({"vulnerabilities": data, "organization": org})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_merge(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            pr_id: str = body["prId"]
            merge_method: str = body.get("mergeMethod", "MERGE")
            merge_pr(self.gh_client, pr_id=pr_id, merge_method=merge_method)
            self._send_json({"success": True})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    # -------------------------------------------------------------- helpers

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Prefix log messages with a tag for clarity."""
        print(f"[dashboard] {format % args}")


def start_server(
    organization: str,
    port: int = 8080,
    repo_filter: str | None = None,
) -> None:
    """Start the dashboard HTTP server."""
    gh_client = GitHubClient.init()
    project_root = str(Path(__file__).resolve().parent.parent)

    handler = partial(
        DashboardHandler,
        gh_client=gh_client,
        default_org=organization,
        project_root=project_root,
    )

    server = HTTPServer(("", port), handler)
    print(f"Dashboard server running at http://localhost:{port}")
    print(f"Default organization: {organization}")
    print("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.shutdown()
