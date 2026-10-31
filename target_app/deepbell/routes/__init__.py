"""HTTP routes, one APIRouter per area of the API."""

from . import chaos, demo, incidents, pages, reports, settings, snapshots, status, transactions

# The order in which main.py includes them, which is also the order of the OpenAPI schema.
ROUTERS = [
    pages.router,
    transactions.router,
    status.router,
    incidents.router,
    reports.router,
    snapshots.router,
    settings.router,
    demo.router,
    chaos.router,
]
