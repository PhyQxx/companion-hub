"""迁移期兼容重导出：assets 映射已迁至 app.jobs.persistence（资产归 jobs 域）。"""

from app.jobs.persistence import AssetDerivationRecord, AssetRecord, AssetReferenceRecord

__all__ = ["AssetDerivationRecord", "AssetRecord", "AssetReferenceRecord"]
