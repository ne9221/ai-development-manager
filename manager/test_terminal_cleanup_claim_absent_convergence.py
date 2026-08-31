"""Regression test suite for Terminal Cleanup Claim-Absent Convergence and
Terminal Monotonicity Truth Preservation (Round 46 and Round 38 exact shapes)."""

from manager.test_terminal_monotonicity_cleanup_convergence import (
    TerminalMonotonicityAndCleanupTruthTests,
)

__all__ = ["TerminalMonotonicityAndCleanupTruthTests"]

if __name__ == "__main__":
    import unittest
    unittest.main()
