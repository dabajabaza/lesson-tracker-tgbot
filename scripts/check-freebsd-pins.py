#!/usr/bin/env python3
"""Fail if a dependency that FreeBSD cannot install as a wheel is pinned to a
version its ports tree does not ship.

The production jail is FreeBSD, and PyPI publishes no wheels for that platform
at all. Anything without a pure-python wheel is therefore built from source
there — which for pydantic-core means Rust, a 1.27 GiB package the jail does
not carry. The jail installs those packages from py312-* ports instead, and pip
only accepts them in place of a pin when the version matches exactly.

So a lockfile bump that moves such a package away from the ports version does
not fail in review or in tests; it fails halfway through a deploy, inside a
wall of pip output. This turns that into a red CI check instead.

Usage: scripts/check-freebsd-pins.py [requirements.txt]
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PACKAGESITE = "https://pkg.freebsd.org/FreeBSD:15:amd64/latest/packagesite.pkg"
PYTHON_PKG_PREFIX = "py312-"
REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([0-9A-Za-z.]+)")


def fetch(url: str, timeout: int, attempts: int = 3) -> bytes:
    """GET with retries: a blip in the network must not read as a bad pin."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"{url}: unreachable after {attempts} tries ({exc})") from exc
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def pinned(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        found = (REQUIREMENT.match(line.strip()) for line in handle)
        return {m.group(1).lower(): m.group(2) for m in found if m}


def has_pure_wheel(name: str, version: str) -> bool:
    """Whether PyPI ships a wheel that installs anywhere, FreeBSD included."""
    data = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30))
    return any(f["filename"].endswith("-py3-none-any.whl") for f in data.get("urls", []))


def ports_versions() -> dict[str, str]:
    """Every py312-* package in the ports tree, keyed by its PyPI-style name.

    The index is ~9 MiB of zstd-compressed JSON lines. Unpacked with the system
    tar rather than the tarfile module: zstd support only arrived in Python
    3.14, and GNU tar has handled it for years. The port's own revision suffix
    (`2.46.4_2`) is dropped — it does not change the upstream version pip
    compares against.
    """
    with tempfile.NamedTemporaryFile(suffix=".pkg") as archive:
        archive.write(fetch(PACKAGESITE, timeout=120))
        archive.flush()
        index = subprocess.run(
            ["tar", "-xOf", archive.name, "packagesite.yaml"],
            check=True,
            capture_output=True,
        ).stdout
    versions: dict[str, str] = {}
    for line in index.splitlines():
        entry = json.loads(line)
        name = entry["name"]
        if not name.startswith(PYTHON_PKG_PREFIX):
            continue
        key = name[len(PYTHON_PKG_PREFIX) :].lower().replace("_", "-")
        versions[key] = entry["version"].split("_")[0]
    return versions


def main() -> int:
    requirements = sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"
    wanted = pinned(requirements)

    # One PyPI request per dependency, run concurrently: serially this takes
    # longer than the rest of CI put together.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        verdicts = pool.map(
            lambda item: (item[0], item[1], has_pure_wheel(*item)),
            sorted(wanted.items()),
        )
        needs_build = {name: version for name, version, pure in verdicts if not pure}
    if not needs_build:
        print("no dependency needs building on FreeBSD — nothing to check")
        return 0

    ports = ports_versions()
    problems = []
    for name, version in needs_build.items():
        # pydantic2 in ports is what PyPI calls pydantic; the rest match by name.
        port_version = ports.get(name) or ports.get(f"{name}2")
        if port_version is None:
            problems.append(
                f"{name}=={version}: no py312-{name} port — the jail would have to "
                f"build it, and cannot"
            )
        elif port_version != version:
            problems.append(
                f"{name}=={version}: ports ship {port_version} — pip would build "
                f"from source instead of using py312-{name}"
            )
        else:
            print(f"ok  {name}=={version} matches py312-{name}")

    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        print(
            "\nPin these in pyproject.toml under [tool.uv] constraint-dependencies,"
            "\nthen re-run: uv lock && uv export --format requirements-txt"
            " --no-hashes --no-dev -o requirements.txt",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        # Network trouble, not a verdict on the pins — say so plainly instead of
        # dumping a traceback that reads like the checker itself is broken.
        print(f"check-freebsd-pins: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
