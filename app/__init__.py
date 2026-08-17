"""OpsBridge365 cloud layer.

Two entrypoints share one Graph client:

* ``python -m app.sync`` - run-and-exit job that pushes device data into the
  SharePoint Assets list (Azure Container Apps Job).
* ``app.main:app`` - FastAPI service exposing ``/healthz`` and ``/metrics``
  (Azure Container App, scale-to-zero).

Importing this package never reads configuration; see :mod:`app.config`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
