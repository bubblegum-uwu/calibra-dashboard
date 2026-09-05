"""
inspect_trace.py -- pretty-print one run's full trace from traces.db.

Usage:
    python inspect_trace.py <run_id>

Example:
    python inspect_trace.py 28eb33dc
"""

import sys

from tracing import Tracer

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python inspect_trace.py <run_id>")
    tracer = Tracer()
    tracer.pretty_print(sys.argv[1])
