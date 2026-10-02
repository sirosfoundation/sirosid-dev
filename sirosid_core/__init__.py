"""sirosid_core - the part of sirosid-dev that is not a command line.

scripts/ is the CLI: argument parsing, reading the developer's local files,
printing. This package is what a CLI and a service can both call: a description
of an instance (spec.py) and, in later steps, the state, naming and Fly
orchestration behind deploying it. Nothing in here reads argv, takes configuration
from environment variables or reads a developer's gitignored files - callers hand
it content. (FlyClient does pass the ambient environment through to the flyctl it
runs, since flyctl needs PATH and HOME; that is plumbing, not configuration.)
"""
