"""
SonarQube rule descriptions for the "## Rule description" section of every
prompt.

Lookup order for each rule:
  1. The local cache (cache/sonar_rules.json in the run folder).
  2. The SonarCloud API. The rule name, type and impacts are public; the
     description body is only returned to an authenticated caller, so set
     SONAR_TOKEN to get the official text.
  3. data/rule_descriptions.yaml, a curated summary, when the API gave no
     description.

The source used is recorded as RuleInfo.description_source, so the write-up
can state exactly what the model was shown.
"""

import html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .models import RuleInfo

logger = logging.getLogger(__name__)

SONARCLOUD_URL = 'https://sonarcloud.io'
REQUEST_TIMEOUT_SECONDS = 15

# Description sections worth showing the model, in display order.
USEFUL_SECTIONS = ('introduction', 'root_cause', 'how_to_fix')
MAX_DESCRIPTION_CHARS = 1500


def html_to_text(markup: str) -> str:
    """Flatten SonarCloud's HTML rule description to plain text."""
    text = re.sub(r'(?is)<(script|style).*?</\1>', '', markup)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</h\d>|</pre>', '\n', text)
    text = re.sub(r'(?i)<li[^>]*>', '- ', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


class RuleCatalog:
    def __init__(self, cache_path: Path, organization: Optional[str],
                 token: Optional[str] = None, offline: bool = False,
                 curated_path: Optional[Path] = None):
        from .config import RULE_DESCRIPTIONS_FILE
        self.cache_path = cache_path
        self.organization = organization
        self.token = token if token is not None else os.environ.get('SONAR_TOKEN')
        self.offline = offline
        self.curated = self._load_curated(curated_path or RULE_DESCRIPTIONS_FILE)
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()
        self._dirty = False

    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_curated(path: Path) -> Dict[str, str]:
        with path.open('r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return {key: str(value).strip() for key, value in data.items()}

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        if not self.cache_path.is_file():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable rule cache %s", self.cache_path)
            return {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=2, ensure_ascii=False),
                                   encoding='utf-8')
        logger.info("Cached %d rule description(s) in %s", len(self.cache), self.cache_path)

    # ------------------------------------------------------------------ #

    def get(self, rule_key: str) -> RuleInfo:
        if rule_key in self.cache:
            return RuleInfo(**self.cache[rule_key])

        info = None if self.offline else self._fetch(rule_key)
        if info is None:
            info = RuleInfo(key=rule_key, name=rule_key)
        if not info.description and rule_key in self.curated:
            info.description = self.curated[rule_key]
            info.description_source = 'curated'
        if not info.description:
            info.description = info.name
            logger.warning("No description available for rule %s; using its name", rule_key)

        # Only cache what came from the network, so an offline run can be
        # upgraded by a later online one.
        if not self.offline:
            self.cache[rule_key] = info.to_dict()
            self._dirty = True
        return info

    def _fetch(self, rule_key: str) -> Optional[RuleInfo]:
        if not self.organization:
            logger.warning("sonar.organization is not set; cannot fetch rule %s", rule_key)
            return None
        query = urllib.parse.urlencode({'key': rule_key, 'organization': self.organization})
        request = urllib.request.Request(f"{SONARCLOUD_URL}/api/rules/show?{query}")
        if self.token:
            request.add_header('Authorization', f'Bearer {self.token}')
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                rule = json.load(resp)['rule']
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
            logger.warning("Could not fetch rule %s from SonarCloud: %s", rule_key, e)
            return None

        sections = {s.get('key'): s.get('content', '')
                    for s in rule.get('descriptionSections') or []}
        parts = [html_to_text(sections[k]) for k in USEFUL_SECTIONS if sections.get(k)]
        if not parts and rule.get('htmlDesc'):
            parts = [html_to_text(rule['htmlDesc'])]
        description = '\n\n'.join(p for p in parts if p)[:MAX_DESCRIPTION_CHARS].strip()

        return RuleInfo(
            key=rule_key,
            name=rule.get('name', rule_key),
            type=rule.get('type', ''),
            impacts=[f"{i.get('softwareQuality')}/{i.get('severity')}"
                     for i in rule.get('impacts') or []],
            description=description,
            description_source='sonarcloud' if description else 'name_only',
        )
