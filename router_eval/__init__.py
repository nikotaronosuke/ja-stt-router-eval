"""Routing of short Japanese utterances to a closed set of candidate ids.

The package holds the router contract, a keyword baseline, an evaluation-only
hosted router, the sealed-dataset loader and the evaluation runner. Nothing in
it generates free text: a router returns a candidate id or abstains.
"""
