"""Read-only views over the ledger. Nothing in this package judges, locks, or executes.

The judging modules live in backend/runtime, backend/intake and backend/api, and are held to
"never read who drew the route" (tests/test_drafter RuntimeNeverReadsTheDrafterTest). A report
is the opposite kind of code: it reads the ledger back to people, provenance included, and
changes nothing. That grep now walks all of backend and skips this directory by name, so the
exemption stays structural: a new report file here is exempt automatically, and no judging file
can slip it by being named reports.py.
"""
