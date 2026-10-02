"""Chat adapter for the shared owned maintenance worker."""

from app.db import Database
from app.jobs.maintenance import Handler, MaintenanceSourceGone, MaintenanceWorker

RESOURCE = "chat-postcommit"
PostcommitSourceGone = MaintenanceSourceGone


class PostcommitWorker(MaintenanceWorker):
    def __init__(self, database: Database, handler: Handler) -> None:
        super().__init__(database, handler, resource_class=RESOURCE, error_code="postcommit_failed")
