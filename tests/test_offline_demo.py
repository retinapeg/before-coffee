"""The offline demo: `python scripts/demo.py` on a store of invented vacancies.

It must run on a clean clone with nothing but the standard library, say plainly that
everything in it is synthetic, print the real census and the real digest, and never
reach the network or Gmail.
"""
from __future__ import annotations

import importlib.util
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "scripts" / "demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("before_coffee_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a connection or resolve a name fails the test."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("the offline demo tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


@pytest.fixture
def demo_output(no_network, monkeypatch, capsys):
    from careerops import digest_delivery

    def no_gmail(*args, **kwargs):
        raise AssertionError("the offline demo tried to build a Gmail client")

    monkeypatch.setattr(digest_delivery, "gmail_service", no_gmail)
    assert _load_demo().main([]) == 0
    assert no_network == []
    return capsys.readouterr().out


def test_the_demo_says_it_is_synthetic_before_and_after(demo_output):
    lines = demo_output.splitlines()
    assert lines[0].startswith("SYNTHETIC DEMO")
    assert lines[-1].startswith("SYNTHETIC DEMO")
    assert "Nothing is sent and Gmail is not contacted" in lines[0]


def test_the_demo_prints_the_census_and_both_digest_sections(demo_output):
    for heading in ("JOBS BY SOURCE TYPE", "BOARDS BY TYPE", "TOTALS",
                    "--- would send to reader@example.invalid ---",
                    "LONDON\n------", "INTERNATIONAL\n-------------"):
        assert heading in demo_output
    assert demo_output.index("LONDON\n") < demo_output.index("INTERNATIONAL\n")


def test_every_employer_and_link_in_the_demo_is_invented(demo_output):
    urls = re.findall(r"https?://\S+", demo_output)
    assert urls and all(re.match(r"https://[a-z.]*example\.invalid/", u) for u in urls)
    employers = re.findall(r"^    (\S.*?)  \|  ", demo_output, re.M)
    assert len(employers) == 5
    assert all(name.endswith("(synthetic)") for name in employers)


def test_the_demo_counts_line_accounts_for_every_role(demo_output):
    line = next(l for l in demo_output.splitlines() if l.startswith("considered "))
    n = {label: int(value) for label, value in
         (part.strip().rsplit(" ", 1) for part in line.split("|"))}
    assert n["considered"] == 7
    assert n["considered"] == (n["qualifying"] + n["in an earlier digest"]
                               + n["below the criteria"] + n["outside configured locations"])
    assert n["qualifying"] == n["selected"] + n["held back"]
    assert n["outside configured locations"] == 1 and n["below the criteria"] == 1
    # The two excluded roles are not listed.
    assert "Shanghai" not in demo_output and "Veterinary Surgeon" not in demo_output


def test_the_demo_refuses_to_overwrite_an_existing_store(tmp_path):
    existing = tmp_path / "careerops.sqlite3"
    existing.write_bytes(b"not to be touched")
    with pytest.raises(SystemExit):
        _load_demo().main(["--data", str(existing)])
    assert existing.read_bytes() == b"not to be touched"


def test_the_demo_runs_as_a_plain_command():
    result = subprocess.run([sys.executable, str(DEMO)], cwd=REPO, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("SYNTHETIC DEMO")
    assert "CareerOps digest: 5 roles to look at" in result.stdout
