"""Legacy mapping exports; domain modules own definitions on the shared Base."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Mapped

from .actions import (
    ActionPlanRecord,
    ActionStepRecord,
    PendingMutationRecord,
)
from .appearance import (
    AvatarInstanceRecord,
    AvatarPackRecord,
    PersonaAvatarBindingRecord,
    UiPreferenceRecord,
    UiThemeRecord,
)
from .assets import (
    AssetDerivationRecord,
    AssetRecord,
    AssetReferenceRecord,
)
from .base import (
    BIGINT_PK,
    Base,
)
from .calendar import (
    CalendarEventRecord,
    CalendarOAuthTokenRecord,
    ContactRecord,
)
from .cognition import (
    ActionResultRecord,
    CognitiveDecisionRecord,
    CognitiveFeedbackRecord,
    CognitiveGoalRecord,
    ProactiveQuotaEntryRecord,
    ReflectionCandidateRecord,
    SemanticEventAuditRecord,
)
from .configuration import (
    ConfigPointerRecord,
    ConfigVersionRecord,
    PersonaPointerRecord,
    PersonaVersionRecord,
)
from .conversation import (
    ConversationRecord,
    InteractionTurnRecord,
    MessageRecord,
    RuntimeLeaseRecord,
    UserModeRecord,
)
from .delivery import (
    HomeAssistantProactiveLogRecord,
    ProactiveDeliveryReceiptRecord,
    PushSubscriptionRecord,
)
from .devices import (
    DeviceClientRecord,
    DeviceCommandRecord,
    DevicePairingCodeRecord,
)
from .events import (
    ConsumerInboxRecord,
    DeadLetterRecord,
    EventRecord,
    OutboxRecord,
)
from .home import (
    HomeSceneRecord,
    SafetyAlertEscalationRecord,
    SafetyAlertRecord,
    SafetyAuthorizationRecord,
)
from .identity import (
    AppUserRecord,
    AuthCredentialRecord,
    AuthSessionRecord,
    ObservationOwnerBindingRecord,
)
from .jobs import (
    JobArtifactRecord,
    JobRecord,
    JobStepRecord,
)
from .mail import (
    MailAttachmentRecord,
    MailOutboxLogRecord,
)
from .meetings import (
    MeetingActionClaimRecord,
    MeetingRecord,
)
from .memory import (
    DeletionLedgerRecord,
    MemoryRecord,
    MemorySourceRecord,
    TimelineEventRecord,
)
from .runs import (
    ModelReservationRecord,
    TaskRunEventRecord,
    TaskRunRecord,
)
from .skills import (
    SkillConnectionRecord,
    SkillCredentialRecord,
    SkillDraftRecord,
    SkillRecord,
    SkillRunRecord,
    SkillSuggestionRecord,
    SkillVersionRecord,
)
from .tasks import (
    DailyBriefRecord,
    DailyReviewRecord,
    TaskItemRecord,
)
from .workflows import (
    WorkflowDraftRecord,
    WorkflowRecord,
)

__all__ = [
    "BIGINT_PK",
    "UUID",
    "ActionPlanRecord",
    "ActionResultRecord",
    "ActionStepRecord",
    "Any",
    "AppUserRecord",
    "AssetDerivationRecord",
    "AssetRecord",
    "AssetReferenceRecord",
    "AuthCredentialRecord",
    "AuthSessionRecord",
    "AvatarInstanceRecord",
    "AvatarPackRecord",
    "Base",
    "CalendarEventRecord",
    "CalendarOAuthTokenRecord",
    "CognitiveDecisionRecord",
    "CognitiveFeedbackRecord",
    "CognitiveGoalRecord",
    "ConfigPointerRecord",
    "ConfigVersionRecord",
    "ConsumerInboxRecord",
    "ContactRecord",
    "ConversationRecord",
    "DailyBriefRecord",
    "DailyReviewRecord",
    "DeadLetterRecord",
    "DeletionLedgerRecord",
    "DeviceClientRecord",
    "DeviceCommandRecord",
    "DevicePairingCodeRecord",
    "EventRecord",
    "HomeAssistantProactiveLogRecord",
    "HomeSceneRecord",
    "InteractionTurnRecord",
    "JobArtifactRecord",
    "JobRecord",
    "JobStepRecord",
    "MailAttachmentRecord",
    "MailOutboxLogRecord",
    "Mapped",
    "MeetingActionClaimRecord",
    "MeetingRecord",
    "MemoryRecord",
    "MemorySourceRecord",
    "MessageRecord",
    "ModelReservationRecord",
    "ObservationOwnerBindingRecord",
    "OutboxRecord",
    "PendingMutationRecord",
    "PersonaAvatarBindingRecord",
    "PersonaPointerRecord",
    "PersonaVersionRecord",
    "ProactiveDeliveryReceiptRecord",
    "ProactiveQuotaEntryRecord",
    "PushSubscriptionRecord",
    "ReflectionCandidateRecord",
    "RuntimeLeaseRecord",
    "SafetyAlertEscalationRecord",
    "SafetyAlertRecord",
    "SafetyAuthorizationRecord",
    "SemanticEventAuditRecord",
    "SkillConnectionRecord",
    "SkillCredentialRecord",
    "SkillDraftRecord",
    "SkillRecord",
    "SkillRunRecord",
    "SkillSuggestionRecord",
    "SkillVersionRecord",
    "TaskItemRecord",
    "TaskRunEventRecord",
    "TaskRunRecord",
    "TimelineEventRecord",
    "UiPreferenceRecord",
    "UiThemeRecord",
    "UserModeRecord",
    "WorkflowDraftRecord",
    "WorkflowRecord",
    "date",
    "datetime",
]
