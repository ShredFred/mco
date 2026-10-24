"""Run the current lifecycle regressions with fake providers and temporary state.

The original audit observations are preserved in probe-output.txt and REPORT.txt.
Use Python 3.10+; this module does not dispatch real providers.
"""
import unittest


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName('tests.test_session_hardening'))
    raise SystemExit(not result.wasSuccessful())
