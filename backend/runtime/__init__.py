"""The judgement side: locks, limits, the arbiter, the single commit path.

Nothing is re-exported here on purpose. Every name has exactly one import path, so a
submodule importing a sibling can never meet a half-initialised package.
"""
