"""Config drift between machines (#307).

Each machine's ``orchestrator.yaml`` is its own, and nothing hands the
same one to every machine yet. A machine missing repos the others have
silently ignores issues they would take. This module lets the status page
show that, without sending the config itself:

* :func:`config_fingerprint` is this machine's fingerprint, published as
  ``node.config_fingerprint``: the repo names, and for each repo one hash
  per field of ``automation`` and of ``steps``, plus one hash per field of
  ``schedules``. Repo names and hashes only; never a value. Most repos
  share one configuration, so the per-repo hashes are published once per
  distinct set (a "profile"), and each repo names its profile's hash.
* :func:`clean_fingerprint` checks a peer's fingerprint, which is
  untrusted, and returns ``None`` (unknown) for anything not shaped like one.
* :func:`peer_drift` compares a peer's fingerprint with this machine's and
  names the differences, leaving out those listed in
  ``fleet.expected_drift``.

Hashes are per field, not per section, so a comparison can name the field
that differs ("automation.exclude_labels differs on 3 repos") and an
expected difference can leave out one field. A field only one side has
is not compared at all, so a config field added in a newer version does
not read as drift; whether the two sides run the same version is shown
separately, and a field only one side has is not reported either way.

The fingerprint covers only ``repos[].automation``, ``repos[].steps`` and
``schedules``, none of which holds a credential. A hash hides a value's
length and shape, not a value someone can guess: a policy such as ``auto``
can be confirmed by hashing it. Nothing secret is in these fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from ctrlrelay.core.config import Config

# 2: per-repo hashes deduplicated into profiles. A peer sending any other
# version is unknown, not compared.
FINGERPRINT_VERSION = 2
# The sections compared, each hashed field by field.
REPO_SECTIONS = ("automation", "steps")
SAME, DIFFERS, UNKNOWN = "same", "differs", "unknown"
# A peer's fingerprint is untrusted: bounds past which it is not one.
_MAX_REPOS = 5000
_MAX_PROFILES = 5000
_MAX_FIELDS = 200
_HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")
# Repo names shown in one line before "and N more".
_SHOW = 5


def _hash(value: Any) -> str:
    """A short hash of a JSON value, canonical: keys sorted, no spacing."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _field_hashes(model: BaseModel) -> dict[str, str]:
    return {name: _hash(value) for name, value in model.model_dump(mode="json").items()}


def _repos_hash(names: Iterable[str]) -> str:
    return _hash(sorted(name.lower() for name in names))


def config_fingerprint(config: Config) -> dict[str, Any]:
    """This machine's fingerprint. Repo names and hashes only."""
    profiles: dict[str, dict[str, dict[str, str]]] = {}
    repos: dict[str, str] = {}
    for r in config.repos:
        profile = {section: _field_hashes(getattr(r, section)) for section in REPO_SECTIONS}
        key = _hash(profile)
        profiles[key] = profile
        repos[r.name] = key
    return {
        "v": FINGERPRINT_VERSION,
        "repos_hash": _repos_hash(repos),
        "profiles": profiles,
        "repos": repos,
        "schedules": _field_hashes(config.schedules),
    }


def _clean_hashes(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict) or len(raw) > _MAX_FIELDS:
        return None
    for key, value in raw.items():
        if not isinstance(key, str) or not 0 < len(key) <= 100:
            return None
        if not isinstance(value, str) or not _HASH_RE.match(value):
            return None
    return dict(raw)


def clean_fingerprint(raw: Any) -> dict[str, Any] | None:
    """A peer's fingerprint, or ``None`` (unknown) when it is missing, of
    another format version, or holds anything not shaped like one: a
    fingerprint with a bad entry dropped would read as drift.

    It must also agree with itself: ``repos_hash`` is the hash of its own
    repo names, and each profile's key is the hash of that profile. A
    peer that sent ``repos: {}`` with some other ``repos_hash`` would
    otherwise make every repo here read as "only here". An empty repo
    list with the matching hash is a real one and is kept."""
    if not isinstance(raw, dict) or raw.get("v") != FINGERPRINT_VERSION:
        return None
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or len(profiles) > _MAX_PROFILES:
        return None
    clean_profiles: dict[str, dict[str, dict[str, str]]] = {}
    for key, sections in profiles.items():
        if not isinstance(key, str) or not isinstance(sections, dict):
            return None
        profile = {}
        for section in REPO_SECTIONS:
            hashes = _clean_hashes(sections.get(section))
            if hashes is None:
                return None
            profile[section] = hashes
        if _hash(profile) != key:
            return None
        clean_profiles[key] = profile
    repos = raw.get("repos")
    if not isinstance(repos, dict) or len(repos) > _MAX_REPOS:
        return None
    for name, key in repos.items():
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            return None
        if not isinstance(key, str) or key not in clean_profiles:
            return None
    schedules = _clean_hashes(raw.get("schedules"))
    repos_hash = raw.get("repos_hash")
    if schedules is None or repos_hash != _repos_hash(repos):
        return None
    return {
        "v": FINGERPRINT_VERSION, "repos_hash": repos_hash,
        "profiles": clean_profiles, "repos": dict(repos), "schedules": schedules,
    }


def unknown(reason: str) -> dict[str, Any]:
    return {
        "status": UNKNOWN, "reason": reason, "only_here": None, "only_there": None,
        "fields": None, "lines": [],
    }


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:_SHOW])
    more = len(names) - _SHOW
    return shown + (f" and {more} more" if more > 0 else "")


def _repos(n: int) -> str:
    return f"{n} repo" if n == 1 else f"{n} repos"


def compare(
    mine: dict[str, Any], theirs: dict[str, Any], expected: Iterable[str] = (),
) -> dict[str, Any]:
    """What differs between two clean fingerprints, leaving out what
    ``expected`` names: a repo (``owner/name``, case-insensitive), a whole
    section (``automation``, ``steps``, ``schedules``) or one field
    (``automation.exclude_labels``)."""
    expected = list(expected)
    skip_repos = {e.lower() for e in expected if "/" in e}
    skip_paths = {e for e in expected if "/" not in e}

    def skipped(path: str) -> bool:
        return path in skip_paths or path.split(".", 1)[0] in skip_paths

    def by_key(fp: dict[str, Any]) -> dict[str, tuple[str, dict]]:
        return {
            name.lower(): (name, fp["profiles"][key]) for name, key in fp["repos"].items()
            if name.lower() not in skip_repos
        }

    here, there = by_key(mine), by_key(theirs)
    only_here = sorted(here[k][0] for k in here.keys() - there.keys())
    only_there = sorted(there[k][0] for k in there.keys() - here.keys())
    fields: dict[str, list[str]] = {}
    for key in sorted(here.keys() & there.keys()):
        name, a = here[key]
        b = there[key][1]
        for section in REPO_SECTIONS:
            sa, sb = a.get(section, {}), b.get(section, {})
            for field in sorted(sa.keys() & sb.keys()):
                path = f"{section}.{field}"
                if not skipped(path) and sa[field] != sb[field]:
                    fields.setdefault(path, []).append(name)
    sa, sb = mine["schedules"], theirs["schedules"]
    for field in sorted(sa.keys() & sb.keys()):
        path = f"schedules.{field}"
        if not skipped(path) and sa[field] != sb[field]:
            fields[path] = []

    lines = []
    if only_here:
        lines.append(f"{_repos(len(only_here))} only here: {_names(only_here)}")
    if only_there:
        lines.append(f"{_repos(len(only_there))} only there: {_names(only_there)}")
    for path in sorted(fields):
        repos = fields[path]
        if not path.startswith("schedules."):
            lines.append(f"{path} differs on {_repos(len(repos))}: {_names(repos)}")
        else:
            lines.append(f"{path} differs")
    return {
        "status": DIFFERS if lines else SAME,
        "reason": None,
        "only_here": only_here,
        "only_there": only_there,
        "fields": fields,
        "lines": lines,
    }


def peer_drift(
    entry: dict[str, Any], mine: dict[str, Any] | None, expected: Iterable[str] = (),
) -> dict[str, Any]:
    """The ``drift`` item for one ``fleet.peers`` entry. Unknown, never
    drift, whenever either fingerprint cannot be had: a peer not read, not
    answering, or on a version that does not send one."""
    if mine is None:
        return unknown("this machine's config fingerprint could not be built")
    if entry.get("reachable") is not True:
        return unknown("not read yet" if entry.get("reachable") is None
                        else "not reachable from here")
    node = entry.get("node")
    theirs = node.get("config_fingerprint") if isinstance(node, dict) else None
    if theirs is None:
        return unknown(
            "it sends no config fingerprint this version can read (an older version?)"
        )
    return compare(mine, theirs, expected)
