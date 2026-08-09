"""Expose the Skill-local 1.0 suite to root unittest discovery.

The implementation suite lives beside the split Skill so the source and installed
payload exercise the same tests. Root discovery loads that suite without keeping a
second brittle copy of Harness internals.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "skill" / "scripts" / "test_harness.py"
SPEC = importlib.util.spec_from_file_location("skill_local_test_harness", SUITE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def load_tests(loader: unittest.TestLoader, _tests: unittest.TestSuite,
               _pattern: str | None) -> unittest.TestSuite:
    return loader.loadTestsFromModule(MODULE)
