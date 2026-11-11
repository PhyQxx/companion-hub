"""迁移期兼容重导出：jobs 映射已迁至 app.jobs.persistence。"""

from app.jobs.persistence import JobArtifactRecord, JobRecord, JobStepRecord

__all__ = ["JobArtifactRecord", "JobRecord", "JobStepRecord"]
