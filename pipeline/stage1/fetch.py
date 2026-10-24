"""
Stage 1a: download the open issues of a SonarCloud project.

The project key and organization default to sonar.projectKey and
sonar.organization in the target repository's sonar-project.properties.
Public projects need no token; set SONAR_TOKEN for private ones.

By default the main branch's latest analysis is read. Pass branch= or
pull_request= to read the analysis of a refactored branch instead; that branch
must have been analysed by SonarCloud (pushed and scanned by CI) first.

Run with: python -m pipeline fetch-issues --repo <path>
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SONARCLOUD_URL = 'https://sonarcloud.io'
PAGE_SIZE = 500
# SonarCloud refuses to page past 10,000 results.
MAX_RESULTS = 10_000
REQUEST_TIMEOUT_SECONDS = 30


def _get(url: str, token: Optional[str]) -> Dict[str, Any]:
    request = urllib.request.Request(url)
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def analysed_branches(project_key: str, token: Optional[str] = None) -> List[str]:
    """Names of the branches SonarCloud has analysed for the project."""
    url = (f"{SONARCLOUD_URL}/api/project_branches/list?"
           f"{urllib.parse.urlencode({'project': project_key})}")
    try:
        return [b['name'] for b in _get(url, token).get('branches', [])]
    except (urllib.error.URLError, OSError, KeyError):
        return []


def fetch_issues(project_key: str, organization: Optional[str], output_path: Path,
                 token: Optional[str] = None, branch: Optional[str] = None,
                 pull_request: Optional[str] = None) -> int:
    """Write every unresolved issue of the project to output_path; returns the count."""
    token = token if token is not None else os.environ.get('SONAR_TOKEN')
    if branch:
        known = analysed_branches(project_key, token)
        if known and branch not in known:
            raise SystemExit(
                f"SonarCloud has not analysed branch {branch!r} (analysed: "
                f"{', '.join(known)}). Push it and let CI run the SonarCloud scan first; "
                "see 'Measuring the refactored code' in pipeline/README.md.")
    issues: List[Dict[str, Any]] = []
    page = 1
    while True:
        params = {'componentKeys': project_key, 'resolved': 'false',
                  'ps': PAGE_SIZE, 'p': page}
        if organization:
            params['organization'] = organization
        if branch:
            params['branch'] = branch
        if pull_request:
            params['pullRequest'] = pull_request
        url = f"{SONARCLOUD_URL}/api/issues/search?{urllib.parse.urlencode(params)}"
        try:
            data = _get(url, token)
        except urllib.error.HTTPError as e:
            hint = '' if token else ' (private project? set SONAR_TOKEN)'
            raise SystemExit(f"SonarCloud returned HTTP {e.code} for {project_key}{hint}")
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"Could not reach SonarCloud: {e}")

        issues.extend(data.get('issues', []))
        total = data.get('paging', {}).get('total', data.get('total', 0))
        if len(issues) >= min(total, MAX_RESULTS) or not data.get('issues'):
            break
        page += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({'project': project_key, 'organization': organization,
                                       'branch': branch, 'pull_request': pull_request,
                                       'total': len(issues), 'issues': issues},
                                      indent=2, ensure_ascii=False), encoding='utf-8')
    where = f" (branch {branch})" if branch else f" (PR {pull_request})" if pull_request else ''
    logger.info("Saved %d issue(s) of %s%s to %s", len(issues), project_key, where, output_path)
    return len(issues)
