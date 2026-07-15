"""Alembic migration directory for the fleet observability entities.

Registered via the ``kestrel_entities.migrations`` entry point so the shared
``kestrel-entities`` CLI (``kestrel-entities upgrade head``) finds the revision
files under ``versions/``. The Alembic env + script template live in the
``kestrel-feature-entities`` package; only the revisions live here.
"""
