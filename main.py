from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, ToolSet, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .core.runtime_invariant import (
    TypedRuntimeDecision,
    TypedTurnGate,
    typed_runtime_decision,
    validate_typed_turn,
)
from .core.action_planner import PlannedAction, PlannedActionAuthority, plan_action
from .core.accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnDisposition,
)
from .core.action_outcome import (
    ActionOutcomeAuthority,
    ActionOutcomeIntent,
    ActionOutcomeKind,
)
from .core.affect import (
    AffectAppraisal,
    AffectTrigger,
    appraise_affect,
    trusted_relationship_distance,
)
from .core.affect_state import (
    AffectAppraisalKind,
    AffectMutationResult,
    AffectMutationStatus,
    AffectStateBook,
    ModelAffectAppraisal,
    issue_affect_admission_evidence,
    issue_shio_receipt_evidence,
)
from .core.capability_policy import (
    build_guest_capability_policy,
    build_owner_capability_policy,
    classify_tool,
    decide_tool,
)
from .core.content_intent_builder import (
    ContentIntentSeed,
    attach_action_outcome,
    attach_grounding_facts,
    build_content_intent_seed,
)
from .core.context_builder import (
    clean_contexts,
)
from .core.context_assembler import (
    AssembledContext,
    FactSelection,
    ReferenceContext,
    ReplyTarget,
    assemble_context_views,
    ensure_direct_reply_target,
    ensure_reference_context,
)
from .core.conversation_ledger import (
    ConversationLedger,
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    PlatformGroupHistoryRead,
    PlatformGroupHistoryStatus,
    adapt_astrbot_history,
    has_verified_public_group_context,
    ledger_content_digest,
    read_astrbot_group_history,
    records_for_sender_thread,
)
from .core.conversation_runtime import ConversationRuntime
from .core.contracts import (
    ActionKind,
    AddressDecision,
    AddressKind,
    ContentIntent,
    ContractViolation,
    ExpressionIntent,
    ExpressionModality,
    IngressDisposition,
    KnowledgeGapDecision,
    KnowledgeNeed,
    ParticipationLevel,
    PluginEvidenceStatus,
    SenderKind,
)
from .core.conversation_event import (
    ConversationEvent,
    ConversationRevisionBook,
    PluginSourceEvidence,
    build_ingress_event,
)
from .core.group_scene import (
    GroupSceneBook,
    GroupSceneSnapshot,
    SceneEntrySource,
    SceneMutationResult,
    SceneMutationStatus,
)
from .core.identity import (
    PrincipalContext,
    TurnEnvelope,
    build_scope_key,
    build_sender_key,
    ensure_principal_context,
    ensure_turn_envelope,
)
from .core.generation_epoch import (
    GENERATION_EPOCH_EXTRA,
    GenerationEpochRegistry,
    ensure_event_generation_epoch,
    event_epoch_validation,
    event_generation_snapshot,
)
from .core.generation_cancellation import (
    GenerationTaskRegistry,
    SupersededGeneration,
    provider_supports_cancellation,
)
from .core.scope_concurrency import ScopeWorkKind, TurnScopeCoordinator
from .core.inference_budget import (
    InferenceBudgetAuthority,
    InferenceBudgetError,
    InferencePermit,
    InferencePurpose,
)
from .core.meme_presentation import (
    ExpressionIntentAuthority,
    MemeComplementCadence,
    MemeComplementDecision,
    MemeExecutionAuthority,
    MemeExecutionReceipt,
    MemeManagerConformanceStatus,
    MemeManagerConformanceCollector,
    decide_text_meme_complement,
    execute_meme_permit,
    production_meme_manager_runtime_profile,
)
from .core.learning_cluster import (
    LearningContext,
    make_learning_context,
)
from .core.name_wake_filter import (
    IngressWakeCandidate,
    NaturalNameWakeFilter,
    bind_name_wake_plugin,
    unbind_name_wake_plugin,
)
from .core.ingress_admission import (
    AdmissionResult,
    GateObservation,
    IngressAdmissionController,
)
from .core.trusted_bot_registry import (
    BotIdentityObservation,
    TrustedBotRegistry,
    TypedAdapterBotFlag,
)
from .core.temporal_context import TemporalContext, build_temporal_context
from .core.plugin_adapters.reneban import (
    inspect_reneban_hook,
)
from .core.plugin_adapters.livingmemory import (
    EXPECTED_PLUGIN_NAME as LIVINGMEMORY_PLUGIN_NAME,
    LivingMemoryAdapter,
    has_provided_recall,
)
from .core.memory_policy import MemoryPolicy, MemoryPolicyResult
from .core.pipeline_trace import (
    content_fingerprint,
    get_pipeline_trace,
    get_trace_context,
    get_trace_id,
    record_pipeline_stage,
    start_pipeline_trace,
)
from .core.pipeline_metrics import (
    PIPELINE_METRICS_EXTRA,
    PipelineMetricsSnapshot,
    store_pipeline_metrics,
)
from .core.performance_metrics import (
    LatencyKind,
    ModelCallKind,
    PerformanceWindow,
)
from .core.product_trace import (
    ProductOutcome,
    ProductStage,
    ProductTrace,
    ProductTracePayload,
    ProductTraceStatus,
)
from .core.observability import (
    diagnostic_digest,
    safe_exception_kind,
    structured_log,
)
from .core.name_wake import NameWakeDecision, classify_name_wake
from .core.astrbot_media_adapter import (
    AstrBotMediaAdaptation,
    adapt_astrbot_media,
    media_only_current_message,
)
from .core.astrbot_tool_executor import (
    SUPPORTED_SEALED_ACQUISITION_TOOL_NAMES,
    execute_sealed_acquisition,
)
from .core.current_question_anchor import (
    CurrentQuestionAnchor,
    build_current_question_anchor,
)
from .core.expression_retrieval import retrieve_expression_candidates
from .core.address_resolver import (
    AddressResolutionAuthority,
    StructuredMentionEvidence,
    StructuredReplyEvidence,
)
from .core.opportunity_attention import (
    OpportunityAttentionAuthority,
    OpportunityAttentionDecision,
)
from .core.participation_engine import (
    ParticipationAssessment,
    ParticipationAuthority,
)
from .core.participation_cadence import (
    ParticipationCadenceAuthority,
    ParticipationCadenceDecision,
)
from .core.participation_reaction import (
    ParticipationReactionAuthority,
    ParticipationReactionDecision,
)
from .core.proactive_policy import (
    ProactivePolicyConfig,
    ProactivePolicyState,
)
from .core.proactive_trigger import ProactiveTriggerAuthority
from .core.proactive_topic import ProactiveTopicAuthority
from .core.proactive_runtime import (
    ProactiveComposerRequest,
    ProactiveExecutionAuthority,
    ProactiveExecutionStatus,
    ProactiveSchedulerRuntime,
)
from .core.history_normalizer import (
    HistoryDisposition,
    HistoryNormalizationContext,
    normalize_assistant_history,
)
from .core.persona import PersonaPackage, load_persona_package
from .core.persona_expression import (
    PersonaExpressionPlan,
    build_persona_expression_plan,
)
from .core.response_guard import (
    contains_nonowner_identity_confusion,
    contains_internal_reasoning,
    contains_tool_protocol,
    extract_and_clean_internal_meme_references,
)
from .core.output_validator_v2 import (
    OutputValidationContext,
    _preflight_reply_composer_repair_candidate,
    build_output_validation_context,
    validate_reply_composer_output,
)
from .core.presentation_handoff import PresentationHandoff, build_presentation_handoff
from .core.repair_controller import (
    RepairAction,
    build_safe_direct_reply_fallback,
    build_single_repair_request,
    decide_output_repair,
)
from .core.semantic_guard import (
    SemanticGuardController,
    SemanticGuardContract,
    SemanticGuardPhase,
    SemanticValidationSeal,
    validate_semantic_media_guard,
)
from .core.reply_composer import (
    ReplyComposerRequest,
    build_reply_composer_request,
    parse_reply_composer_output,
)
from .core.relationship_state import (
    RelationshipMutationResult,
    RelationshipMutationStatus,
    RelationshipStateBook,
)
from .core.runtime_continuity import RuntimeContinuityStore
from .core.knowledge_gap import decide_knowledge_gap
from .core.tool_broker import (
    AcquisitionRequest,
    broker_tool_request,
    build_extract_request_shape,
    build_search_request_shape,
)
from .core.grounding_adapter import (
    EvidenceOutcome,
    EvidenceOutcomeKind,
    adapt_grounding_evidence,
)
from .core.send_receipt import (
    InternalSendReceiptLedger,
    ReplyObservationTracker,
    SegmentSendStatus,
)
from .core.owner_action_adapters import (
    AdapterConfig,
    ArtifactPathFlavor,
    ShellFamily,
)
from .core.owner_action_controller import (
    OwnerActionController,
)
from .core.owner_action_durable_finalize import (
    DurableFinalizeConsumer,
    OwnerActionDurableFinalizeAuthority,
)
from .core.owner_action_lifecycle import (
    LifecycleHandle,
    OwnerActionLifecycleStore,
)
from .core.owner_action_router import (
    OwnerActionRouteDecision,
    OwnerActionRouteStatus,
    OwnerActionRouter,
)
from .core.tool_result import (
    adapt_tool_call_results,
    tool_result_trace_metadata,
)


PLUGIN_NAME = "astrbot_plugin_shio"
SHIO_ACTIVE = "_shio_active"
SHIO_PAYLOAD = "_shio_retry_payload"
SHIO_IDENTITY_SCOPE = "_shio_identity_scope"
SHIO_SEND_OBSERVATION = "_shio_send_observation"
SHIO_NATURAL_WAKE = "_shio_natural_name_wake"
SHIO_TYPED_HISTORY_SOURCE = "_shio_typed_history_source"
SHIO_PLATFORM_GROUP_HISTORY = "_shio_platform_group_history_v1"

_CONFIG_GROUP_BY_KEY = {
    **dict.fromkeys(
        (
            "enabled",
            "persona_name",
            "replyer_provider_id",
            "enable_chat_bubbles",
            "chat_max_bubbles",
            "bubble_interval_min_ms",
            "bubble_interval_max_ms",
        ),
        "basic_settings",
    ),
    **dict.fromkeys(
        (
            "natural_name_wake_enabled",
            "natural_name_wake_mode",
            "natural_name_wake_aliases",
            "natural_name_wake_group_whitelist",
        ),
        "wake_settings",
    ),
    **dict.fromkeys(
        (
            "natural_group_participation_enabled",
            "natural_group_participation_allowlist",
            "natural_group_participation_min_context_messages",
            "natural_group_participation_cooldown_seconds",
            "natural_group_participation_window_minutes",
            "natural_group_participation_max_joins_per_window",
            "social_feedback_enabled",
            "social_feedback_window_minutes",
        ),
        "participation_settings",
    ),
    **dict.fromkeys(
        (
            "proactive_initiation_enabled",
            "proactive_group_allowlist",
            "proactive_active_hour_start",
            "proactive_active_hour_end",
            "proactive_timezone_offset_minutes",
            "proactive_observation_minutes",
            "proactive_idle_minutes",
            "proactive_cooldown_minutes",
            "proactive_daily_limit",
            "proactive_scheduler_interval_seconds",
        ),
        "proactive_settings",
    ),
    **dict.fromkeys(
        (
            "prefer_livingmemory_group_history",
            "max_context_messages",
            "max_context_chars",
            "inject_verified_context",
        ),
        "context_settings",
    ),
    **dict.fromkeys(
        (
            "meme_complement_enabled",
            "meme_complement_cadence_turns",
            "meme_complement_cooldown_turns",
        ),
        "meme_settings",
    ),
    **dict.fromkeys(
        (
            "owner_ids",
            "trusted_bot_identities",
            "permission_guard_enabled",
            "guest_allowed_tools",
            "permission_audit_log",
            "owner_action_enabled",
            "owner_action_artifact_read_exact_enabled",
            "owner_action_artifact_grep_enabled",
            "owner_action_memory_write_literal_enabled",
            "owner_action_sandbox_shell_once_enabled",
            "owner_action_artifact_root",
            "owner_action_artifact_path_flavor",
            "owner_action_shell_family",
        ),
        "permission_settings",
    ),
    **dict.fromkeys(
        (
            "inference_max_parallel",
            "inference_max_waiters",
            "inference_queue_timeout_seconds",
            "inference_active_timeout_seconds",
            "performance_window_samples",
            "continuity_max_scopes",
            "continuity_max_subjects",
            "debug_log",
        ),
        "performance_settings",
    ),
}
_MISSING_CONFIG_VALUE = object()
SHIO_TYPED_INBOUND_RECORDED = "_shio_typed_inbound_recorded"
SHIO_TYPED_INBOUND_RECORD = "_shio_typed_inbound_record_v1"
SHIO_ASSEMBLED_CONTEXT_V2 = "_shio_assembled_context_v2"
SHIO_CAPABILITY_POLICY = "_shio_capability_policy"
SHIO_TYPED_RUNTIME = "_shio_typed_runtime"
SHIO_TYPED_RUNTIME_DECISION = "_shio_typed_runtime_decision"
SHIO_TYPED_PIPELINE_ACTIVE = "_shio_typed_pipeline_active"
SHIO_PLANNED_ACTION = "_shio_planned_action_v1"
SHIO_CONTENT_INTENT = "_shio_content_intent_v1"
SHIO_EXPRESSION_INTENT = "_shio_expression_intent_v1"
SHIO_MEME_COMPLEMENT_DECISION = "_shio_meme_complement_decision_v1"
SHIO_MEME_PRESENTATION_RECEIPT = "_shio_meme_presentation_receipt_v1"
SHIO_PRESENTATION_SEND_EVIDENCE = "_shio_presentation_send_evidence_v1"
SHIO_AFFECT_APPRAISAL = "_shio_affect_appraisal_v1"
SHIO_PERSONA_EXPRESSION = "_shio_persona_expression_v1"
SHIO_ACQUISITION_REQUEST = "_shio_acquisition_request_v1"
SHIO_EVIDENCE_OUTCOME = "_shio_evidence_outcome_v1"
SHIO_ACTIVE_CAPABILITY_POLICY = "_shio_active_capability_policy"
SHIO_REPLY_COMPOSER_REQUEST = "_shio_reply_composer_request"
SHIO_OUTPUT_VALIDATION_CONTEXT = "_shio_output_validation_context"
SHIO_SEMANTIC_GUARD_CONTRACT = "_shio_semantic_guard_contract"
SHIO_SEMANTIC_VALIDATION_SEAL = "_shio_semantic_validation_seal"
SHIO_REPAIR_ATTEMPTS = "_shio_repair_attempts"
SHIO_PRESENTATION_HANDOFF = "_shio_presentation_handoff"
SHIO_LEDGER_OUTBOUND_IDS = "_shio_ledger_outbound_ids"
SHIO_INGRESS_CANDIDATE = "_shio_ingress_candidate"
SHIO_INGRESS_DECISION = "_shio_ingress_decision"
SHIO_ADMISSION_RESULT = "_shio_admission_result"
SHIO_CONVERSATION_EVENT = "_shio_conversation_event"
SHIO_PLUGIN_SOURCE_EVIDENCE = "_shio_plugin_source_evidence"
SHIO_TYPED_ADAPTER_BOT_FLAG = "_shio_typed_adapter_bot_flag"
SHIO_GATE_OBSERVATION = "_shio_gate_observation"
SHIO_PRODUCT_TRACE = "_shio_product_trace"
SHIO_RENEBAN_HOOK_EVIDENCE = "_shio_reneban_hook_evidence"
SHIO_MEDIA_ADAPTATION = "_shio_media_adaptation"
SHIO_GROUP_SCENE_MUTATION = "_shio_group_scene_mutation"
SHIO_GROUP_SCENE_SNAPSHOT = "_shio_group_scene_snapshot"
SHIO_LIVINGMEMORY_ADAPTER = "_shio_livingmemory_adapter_v1"
SHIO_MEMORY_POLICY_RESULT = "_shio_memory_policy_result_v1"
SHIO_MEMORY_READER_EVIDENCE = "_shio_memory_reader_evidence_v1"
SHIO_ADDRESS_DECISION = "_shio_address_decision"
SHIO_CURRENT_QUESTION_ANCHOR = "_shio_current_question_anchor_v1"
SHIO_ACCEPTED_TURN_DISPATCH = "_shio_accepted_turn_dispatch_v1"
SHIO_OWNER_ACTION_TICKET = "_shio_owner_action_ticket_v1"
SHIO_OPPORTUNITY_ATTENTION_TICKET = "_shio_opportunity_attention_ticket_v1"
SHIO_OPPORTUNITY_ATTENTION = "_shio_opportunity_attention_v1"
SHIO_PARTICIPATION_ASSESSMENT = "_shio_participation_assessment_v1"
SHIO_PARTICIPATION_CADENCE = "_shio_participation_cadence_v1"
SHIO_PARTICIPATION_REACTION = "_shio_participation_reaction_v1"
SHIO_OWNER_ACTION_ROUTE = "_shio_owner_action_route_v1"
SHIO_OWNER_ACTION_SOURCE = "_shio_owner_action_source_v1"
SHIO_ACTION_OUTCOME = "_shio_action_outcome_v1"
SHIO_OWNER_ACTION_LIFECYCLE_HANDLE = "_shio_owner_action_lifecycle_handle_v1"
SHIO_OWNER_ACTION_SEND_EVIDENCE = "_shio_owner_action_send_evidence_v1"
SHIO_AFFECT_STATE_MUTATION = "_shio_affect_state_mutation_v1"
SHIO_AFFECT_OUTBOUND_MUTATION = "_shio_affect_outbound_mutation_v1"
SHIO_AFFECT_RENDER_CONTEXT = "_shio_affect_render_context_v1"
SHIO_RELATIONSHIP_STATE_MUTATION = "_shio_relationship_state_mutation_v1"
SHIO_RELATIONSHIP_OUTBOUND_MUTATION = "_shio_relationship_outbound_mutation_v1"
SHIO_RELATIONSHIP_RENDER_CONTEXT = "_shio_relationship_render_context_v1"
SHIO_INFERENCE_PERMIT = "_shio_inference_permit_v1"
SHIO_PRIMARY_PROVIDER_STARTED_AT = "_shio_primary_provider_started_at_v1"
SHIO_PERFORMANCE_SNAPSHOT = "_shio_performance_snapshot_v1"

RECOVERABLE_QUESTION_RE = re.compile(
    r"[？?]|(?:怎么|如何|为啥|为什么|是不是|有没有|能不能|可不可以|什么|谁|哪里|多少|"
    r"帮我|告诉我|解释|讲讲|说说|看看|分析|排查|解决)"
)

_SENSITIVE_DEPENDENCY_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)


def _suppress_sensitive_dependency_debug_logs() -> None:
    """Keep third-party request bodies out of AstrBot's DEBUG root bridge."""

    for logger_name in _SENSITIVE_DEPENDENCY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


class ShioPlugin(Star):
    """用 typed behavior → content → Persona Renderer 管线接管角色聊天。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        _suppress_sensitive_dependency_debug_logs()
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        assets_dir = Path(__file__).resolve().parent / "assets"
        self.runtime = ConversationRuntime(data_dir, logger)
        self.runtime_continuity = RuntimeContinuityStore(
            data_dir / "continuity",
            max_scopes=max(
                1,
                min(8192, self._config_int("continuity_max_scopes", 2048)),
            ),
            max_subjects=max(
                1,
                min(8192, self._config_int("continuity_max_subjects", 256)),
            ),
        )
        self.send_receipts = InternalSendReceiptLedger()
        self.generation_epochs = GenerationEpochRegistry()
        self.generation_tasks = GenerationTaskRegistry(self.generation_epochs)
        self.scope_concurrency = TurnScopeCoordinator(self.generation_epochs)
        self.conversation_revisions = ConversationRevisionBook(
            continuity_store=self.runtime_continuity,
        )
        self.ingress_admission = IngressAdmissionController(
            self.conversation_revisions,
        )
        self.accepted_turn_authority = AcceptedTurnAuthority(
            self.ingress_admission,
        )
        self.address_resolution_authority = AddressResolutionAuthority()
        self.ledger = ConversationLedger(
            state_path=data_dir / "public_group_ledger.json",
        )
        self.group_scenes = GroupSceneBook(
            revision_book=self.conversation_revisions,
            conversation_ledger=self.ledger,
        )
        self.opportunity_attention_authority = OpportunityAttentionAuthority(
            self.accepted_turn_authority,
            self.address_resolution_authority,
        )
        self.participation_authority = ParticipationAuthority(
            self.opportunity_attention_authority,
            self.group_scenes,
        )
        self.participation_cadence_authority = ParticipationCadenceAuthority(
            self.participation_authority,
            continuity_store=self.runtime_continuity,
            join_cooldown_seconds=float(
                max(
                    1,
                    min(
                        3600,
                        self._config_int(
                            "natural_group_participation_cooldown_seconds",
                            45,
                        ),
                    ),
                )
            ),
            window_seconds=float(
                max(
                    1,
                    min(
                        1440,
                        self._config_int(
                            "natural_group_participation_window_minutes",
                            5,
                        ),
                    ),
                )
                * 60
            ),
            max_joins_per_window=max(
                1,
                min(
                    20,
                    self._config_int(
                        "natural_group_participation_max_joins_per_window",
                        2,
                    ),
                ),
            ),
        )
        self.participation_reaction_authority = ParticipationReactionAuthority(
            self.participation_cadence_authority,
        )
        self.proactive_trigger_authority = ProactiveTriggerAuthority()
        self.proactive_policy_state = ProactivePolicyState(
            data_dir / "proactive",
            trigger_authority=self.proactive_trigger_authority,
            policy=self._build_proactive_policy_config(),
        )
        self.affect_states = AffectStateBook(
            accepted_turn_authority=self.accepted_turn_authority,
            revision_book=self.conversation_revisions,
        )
        self.relationship_states = RelationshipStateBook(
            accepted_turn_authority=self.accepted_turn_authority,
        )
        self.owner_action_router = OwnerActionRouter(
            self.accepted_turn_authority,
        )
        self.planned_action_authority = PlannedActionAuthority()
        self.inference_budget = InferenceBudgetAuthority(
            self.generation_epochs,
            self.planned_action_authority,
            max_active=max(
                1,
                min(16, self._config_int("inference_max_parallel", 4)),
            ),
            max_waiters=max(
                1,
                min(512, self._config_int("inference_max_waiters", 128)),
            ),
            queue_timeout_seconds=float(
                max(
                    1,
                    min(
                        300,
                        self._config_int("inference_queue_timeout_seconds", 30),
                    ),
                )
            ),
            active_timeout_seconds=float(
                max(
                    5,
                    min(
                        1800,
                        self._config_int("inference_active_timeout_seconds", 300),
                    ),
                )
            ),
        )
        self.performance_window = PerformanceWindow(
            max_samples_per_kind=max(
                16,
                min(
                    8192,
                    self._config_int("performance_window_samples", 512),
                ),
            )
        )
        self.expression_intent_authority = ExpressionIntentAuthority()
        self.meme_manager_conformance = MemeManagerConformanceCollector(
            production_meme_manager_runtime_profile(),
        )
        self.meme_complement_cadence = MemeComplementCadence(
            enabled=self._config_bool("meme_complement_enabled", True),
            ordinary_threshold=max(
                1,
                min(
                    16,
                    self._config_int("meme_complement_cadence_turns", 4),
                ),
            ),
            cooldown_turns=max(
                1,
                min(
                    32,
                    self._config_int("meme_complement_cooldown_turns", 4),
                ),
            ),
        )
        self.meme_execution_authority = MemeExecutionAuthority(
            planned_action_authority=self.planned_action_authority,
            expression_intent_authority=self.expression_intent_authority,
            generation_registry=self.generation_epochs,
            conformance_collector=self.meme_manager_conformance,
        )
        self.owner_action_controller = OwnerActionController(
            self.accepted_turn_authority,
            self.owner_action_router,
            self.planned_action_authority,
        )
        self.action_outcome_authority = ActionOutcomeAuthority.issue_for_runtime(
            self.owner_action_controller,
            self.planned_action_authority,
        )
        self.owner_action_enabled = self._config_bool(
            "owner_action_enabled",
            False,
        )
        self.owner_action_adapter_config = self._build_owner_action_adapter_config()
        self.owner_action_lifecycle_store: OwnerActionLifecycleStore | None = None
        self.owner_action_durable_authority: (
            OwnerActionDurableFinalizeAuthority | None
        ) = None
        self._owner_action_data_dir = data_dir / "owner_action"
        self._owner_action_runtime_lock = threading.RLock()
        self.memory_policy = MemoryPolicy()
        self.semantic_guard_controller = SemanticGuardController()
        self._admission_lock = threading.RLock()
        self._trusted_bot_registry, self._trusted_bot_config_valid = (
            self._build_trusted_bot_registry()
        )
        self._persona_packages = self._load_persona_packages(assets_dir / "personas")
        proactive_personas: list[PersonaPackage] = []
        for persona in self._persona_packages.values():
            if not any(persona is registered for registered in proactive_personas):
                proactive_personas.append(persona)
        self.proactive_topic_authority = ProactiveTopicAuthority(
            self.proactive_policy_state,
            self.group_scenes,
            tuple(proactive_personas),
        )
        self.proactive_execution_authority = ProactiveExecutionAuthority.issue_for_runtime(
            self.proactive_topic_authority,
        )
        self.proactive_scheduler_runtime = ProactiveSchedulerRuntime.issue_for_runtime(
            self.proactive_execution_authority,
            self.proactive_trigger_authority,
            self.proactive_policy_state,
            self.proactive_topic_authority,
        )
        self._proactive_scheduler_log_signature: tuple[tuple[str, object], ...] | None = None
        self._proactive_scheduler_task: asyncio.Task[None] | None = None
        bind_name_wake_plugin(self)
        self._ensure_proactive_scheduler_started()

    def _config(self, key: str, default: Any) -> Any:
        group_name = _CONFIG_GROUP_BY_KEY.get(key)
        if group_name:
            group = self.config.get(group_name, _MISSING_CONFIG_VALUE)
            if hasattr(group, "get"):
                value = group.get(key, _MISSING_CONFIG_VALUE)
                if value is not _MISSING_CONFIG_VALUE:
                    return default if value is None else value
        value = self.config.get(key, default)
        return default if value is None else value

    def _config_bool(self, key: str, default: bool = False) -> bool:
        value = self._config(key, default)
        return value if type(value) is bool else default

    def _config_int(self, key: str, default: int) -> int:
        value = self._config(key, default)
        return value if type(value) is int else default

    def _build_temporal_context(self, *, now: float) -> TemporalContext:
        """Build the sole chat/proactive wall-clock authority from frozen config."""

        offset = self._config_int("proactive_timezone_offset_minutes", 480)
        try:
            return build_temporal_context(
                now=now,
                timezone_offset_minutes=offset,
            )
        except ContractViolation as exc:
            structured_log(
                logger,
                "error",
                "temporal.context_config_invalid",
                failure_kind=safe_exception_kind(exc),
            )
            return build_temporal_context(
                now=now,
                timezone_offset_minutes=480,
            )

    def _build_proactive_policy_config(self) -> ProactivePolicyConfig:
        raw_allowlist = self._config("proactive_group_allowlist", [])
        if type(raw_allowlist) is str:
            raw_values: object = tuple(
                value for value in re.split(r"[,;，；\s]+", raw_allowlist) if value
            )
        elif type(raw_allowlist) in {list, tuple}:
            raw_values = tuple(raw_allowlist)
        else:
            raw_values = ()
        valid_allowlist = bool(
            type(raw_values) is tuple
            and all(
                type(value) is str
                and bool(value.strip())
                and value == value.strip()
                for value in raw_values
            )
        )
        allowlist = (
            tuple(sorted(set(raw_values)))
            if valid_allowlist
            else ()
        )
        try:
            return ProactivePolicyConfig(
                enabled=self._config_bool("proactive_initiation_enabled", False),
                group_allowlist=allowlist,
                active_hour_start=self._config_int("proactive_active_hour_start", 9),
                active_hour_end=self._config_int("proactive_active_hour_end", 23),
                timezone_offset_minutes=self._config_int(
                    "proactive_timezone_offset_minutes",
                    480,
                ),
                observation_seconds=self._config_int(
                    "proactive_observation_minutes",
                    30,
                )
                * 60,
                idle_seconds=self._config_int("proactive_idle_minutes", 20) * 60,
                cooldown_seconds=self._config_int(
                    "proactive_cooldown_minutes",
                    180,
                )
                * 60,
                daily_limit=self._config_int("proactive_daily_limit", 1),
            )
        except ContractViolation as exc:
            structured_log(
                logger,
                "error",
                "proactive.policy_config_invalid",
                failure_kind=safe_exception_kind(exc),
            )
            return ProactivePolicyConfig()

    def _build_owner_action_adapter_config(self) -> AdapterConfig:
        master = bool(getattr(self, "owner_action_enabled", False))
        # The retained UI flag is observed for audit/config-shape parity only.
        # It is deliberately discarded and can never reach AdapterConfig.
        self._config_bool("owner_action_sandbox_shell_once_enabled", False)
        path_flavor_value = str(
            self._config("owner_action_artifact_path_flavor", "") or ""
        ).strip()
        shell_family_value = str(
            self._config("owner_action_shell_family", "") or ""
        ).strip()
        path_flavor = next(
            (
                value
                for value in ArtifactPathFlavor
                if value.value == path_flavor_value
            ),
            None,
        )
        shell_family = next(
            (
                value
                for value in ShellFamily
                if value.value == shell_family_value
            ),
            None,
        )
        return AdapterConfig(
            artifact_read_exact_enabled=(
                master
                and self._config_bool(
                    "owner_action_artifact_read_exact_enabled",
                    False,
                )
            ),
            artifact_grep_enabled=(
                master
                and self._config_bool(
                    "owner_action_artifact_grep_enabled",
                    False,
                )
            ),
            memory_write_literal_enabled=(
                master
                and self._config_bool(
                    "owner_action_memory_write_literal_enabled",
                    False,
                )
            ),
            # Shell remains code-level hard-disabled even if a stale config has
            # the UI flag set.  The value is deliberately never propagated.
            sandbox_shell_once_enabled=False,
            artifact_root=str(
                self._config("owner_action_artifact_root", "") or ""
            ).strip(),
            path_flavor=path_flavor,
            shell_family=shell_family,
        )

    def _owner_action_secret(self) -> bytes:
        """Load or create the local lifecycle HMAC secret without logging it."""

        self._owner_action_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret_path = self._owner_action_data_dir / ".install_secret"
        try:
            value = secret_path.read_bytes()
        except FileNotFoundError:
            value = secrets.token_bytes(32)
            descriptor = -1
            try:
                descriptor = os.open(
                    secret_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                value = secret_path.read_bytes()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        if type(value) is not bytes or len(value) != 32:
            raise RuntimeError("owner_action_install_secret_invalid")
        if os.name != "nt":
            os.chmod(secret_path, 0o600)
        return value

    def _ensure_owner_action_durable_runtime(
        self,
    ) -> tuple[OwnerActionLifecycleStore, OwnerActionDurableFinalizeAuthority]:
        with self._owner_action_runtime_lock:
            store = self.owner_action_lifecycle_store
            authority = self.owner_action_durable_authority
            if store is not None and authority is not None and store.enabled:
                return store, authority
            if store is not None:
                store.close()
            store = OwnerActionLifecycleStore(
                self._owner_action_data_dir / "lifecycle",
                install_secret=self._owner_action_secret(),
                recovery_now=time.time(),
                max_records=512,
                tombstone_ttl_seconds=900.0,
            )
            if not store.enabled:
                raise RuntimeError("owner_action_lifecycle_unavailable")
            authority = OwnerActionDurableFinalizeAuthority.issue_for_runtime(
                store,
                self.owner_action_controller,
                self.action_outcome_authority,
                max_dispatches=512,
            )
            self.owner_action_lifecycle_store = store
            self.owner_action_durable_authority = authority
            return store, authority

    @staticmethod
    def _load_persona_packages(persona_dir: Path) -> dict[str, PersonaPackage]:
        """Load replaceable persona assets without making one role a core rule."""

        packages: dict[str, PersonaPackage] = {}
        for path in sorted(persona_dir.glob("*.json")):
            try:
                package = load_persona_package(path)
            except ValueError as exc:
                structured_log(
                    logger,
                    "warning",
                    "persona.load_failed",
                    package_digest=diagnostic_digest(path.name),
                    failure_kind=safe_exception_kind(exc),
                )
                continue
            for key in {
                package.package_id.casefold(),
                package.display_name.casefold(),
            }:
                if key:
                    packages.setdefault(key, package)
        return packages

    def _configured_persona_package(self) -> PersonaPackage | None:
        configured = str(self._config("persona_name", "亚托莉") or "").strip()
        if not configured:
            return None
        return self._persona_packages.get(configured.casefold())

    def _typed_turn_gate(
        self,
        *,
        envelope: TurnEnvelope,
        principal: PrincipalContext,
    ) -> TypedTurnGate:
        return validate_typed_turn(
            envelope=envelope,
            principal=principal,
        )

    def _typed_runtime_decision(
        self,
        event: AstrMessageEvent | None = None,
        *,
        envelope: TurnEnvelope | None = None,
        principal: PrincipalContext | None = None,
        turn_ready: bool = False,
        planned_action: PlannedAction | None = None,
        refresh: bool = False,
        turn_status: str = "not_evaluated",
        turn_reason_count: int = 0,
    ) -> TypedRuntimeDecision:
        """Return the event-local typed-only runtime decision."""

        if event is not None and not refresh:
            cached = event.get_extra(SHIO_TYPED_RUNTIME_DECISION, None)
            if isinstance(cached, TypedRuntimeDecision):
                return cached

        gate: TypedTurnGate | None = None
        if event is not None and envelope is not None and principal is not None:
            gate = self._typed_turn_gate(
                envelope=envelope,
                principal=principal,
            )
        activation_ready = bool(
            gate is not None
            and gate.activation_ready
            and turn_ready
        )
        decision = typed_runtime_decision(
            activation_ready=activation_ready,
            planned_action=planned_action,
        )
        if event is not None:
            metadata = decision.trace_metadata()
            if gate is not None:
                metadata.update(gate.trace_metadata())
            metadata.update(
                {
                    "typed_plan_status": turn_status,
                    "typed_plan_reason_count": max(0, int(turn_reason_count)),
                }
            )
            event.set_extra(SHIO_TYPED_RUNTIME_DECISION, decision)
            event.set_extra(SHIO_TYPED_RUNTIME, metadata)
        return decision

    def _owner_ids(self) -> set[str]:
        raw = self._config("owner_ids", [])
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        return {str(item).strip() for item in values if str(item).strip()}

    def _string_set(self, key: str, default: Any = None) -> set[str]:
        raw = self._config(key, [] if default is None else default)
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        return {str(item).strip() for item in values if str(item).strip()}

    def _natural_group_participation_enabled(self, envelope: TurnEnvelope) -> bool:
        """Keep opportunistic joining independent from cold-silence scheduling."""

        if envelope.chat_type != "group" or not self._config_bool(
            "natural_group_participation_enabled",
            False,
        ):
            return False
        allowlist = self._string_set("natural_group_participation_allowlist")
        return bool(allowlist and envelope.group_id in allowlist)

    def _build_trusted_bot_registry(self) -> tuple[TrustedBotRegistry, bool]:
        """Build an exact structural bot registry from ``platform|sender`` rows."""

        raw = self._config("trusted_bot_identities", [])
        if isinstance(raw, str):
            values: Any = [item for item in re.split(r"[,;，；\n]+", raw) if item]
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        pairs: list[tuple[str, str]] = []
        valid = True
        for value in values:
            if isinstance(value, dict):
                platform_id = str(value.get("platform_id", "") or "").strip()
                sender_id = str(value.get("sender_id", "") or "").strip()
            else:
                rendered = str(value or "").strip()
                parts = rendered.split("|", 1)
                if len(parts) != 2:
                    valid = False
                    continue
                platform_id, sender_id = (part.strip() for part in parts)
            if not platform_id or not sender_id or "*" in {platform_id, sender_id}:
                valid = False
                continue
            pairs.append((platform_id, sender_id))
        try:
            registry = TrustedBotRegistry.from_configured_pairs(pairs)
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "trusted_bot.config_invalid",
                failure_kind=safe_exception_kind(exc),
            )
            return TrustedBotRegistry.from_configured_pairs(()), False
        if not valid:
            structured_log(
                logger,
                "error",
                "trusted_bot.config_invalid",
                failure_kind="malformed_identity_pair",
            )
        return registry, valid

    def _name_wake_aliases(self) -> list[str]:
        raw = self._config(
            "natural_name_wake_aliases",
            ["亚托莉", "ATRI", "アトリ", "萝卜子"],
        )
        if isinstance(raw, str):
            values = re.split(r"[,;，；\n]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        aliases: list[str] = []
        for value in values:
            alias = str(value or "").strip()
            if alias and alias.casefold() not in {item.casefold() for item in aliases}:
                aliases.append(alias)
        persona_name = str(self._config("persona_name", "亚托莉") or "").strip()
        if persona_name and persona_name.casefold() not in {
            item.casefold() for item in aliases
        }:
            aliases.insert(0, persona_name)
        return aliases

    def _classify_natural_name_wake(
        self,
        event: AstrMessageEvent,
        message: str,
        group_id: str,
    ) -> NameWakeDecision:
        if not bool(self._config("natural_name_wake_enabled", True)):
            return NameWakeDecision("none", reason="功能已关闭")
        whitelist = self._string_set("natural_name_wake_group_whitelist", [])
        if whitelist and group_id not in whitelist:
            return NameWakeDecision("none", reason="当前群不在白名单")
        try:
            components = list(event.get_messages() or [])
        except Exception:
            components = []
        # 只有引用段里出现名字不算当前用户直呼；当前纯文本仍会正常参与判断。
        if not str(message or "").strip() and any(
            component.__class__.__name__ == "Reply" for component in components
        ):
            return NameWakeDecision("none", reason="名字只出现在引用内容中")
        return classify_name_wake(
            message,
            self._name_wake_aliases(),
            mode=str(self._config("natural_name_wake_mode", "natural")),
        )

    @staticmethod
    def _structured_address_evidence(
        event: AstrMessageEvent,
        envelope: TurnEnvelope,
    ) -> tuple[
        tuple[StructuredMentionEvidence, ...],
        StructuredReplyEvidence | None,
    ]:
        """Adapt only structural At/Reply fields; never infer from display names."""

        get_messages = getattr(event, "get_messages", None)
        try:
            components = list(get_messages() or ()) if callable(get_messages) else []
        except Exception:
            components = []
        mentions: list[StructuredMentionEvidence] = []
        reply_component: Any | None = None
        for component in components:
            raw_type = getattr(component, "type", "")
            component_type = str(
                getattr(raw_type, "value", raw_type) or ""
            ).strip().lower()
            class_name = component.__class__.__name__.strip().lower()
            if component_type == "at" or class_name in {"at", "atall"}:
                target_sender_id = str(getattr(component, "qq", "") or "").strip()
                if target_sender_id and target_sender_id.casefold() != "all":
                    mentions.append(
                        StructuredMentionEvidence(
                            source_message_id=envelope.message_id,
                            target_sender_id=target_sender_id,
                        )
                    )
            if component_type == "reply" or class_name == "reply":
                reply_component = component

        structured_reply = None
        if (
            reply_component is not None
            and envelope.reply_to_message_id
            and envelope.reply_to_sender_id
        ):
            quoted_content = str(
                getattr(reply_component, "message_str", "")
                or getattr(reply_component, "text", "")
                or ""
            )
            structured_reply = StructuredReplyEvidence(
                source_message_id=envelope.message_id,
                referenced_message_id=envelope.reply_to_message_id,
                referenced_sender_id=envelope.reply_to_sender_id,
                quoted_content=quoted_content,
            )
        return tuple(mentions), structured_reply

    def _effective_sender(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str]:
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or sender_id or "群友").strip()
        return sender_id, sender_name

    def _effective_principal(
        self,
        event: AstrMessageEvent,
    ) -> PrincipalContext:
        return ensure_principal_context(event, self._owner_ids())

    @staticmethod
    def _event_value(event: AstrMessageEvent, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            return str(method() or "").strip()
        except Exception:
            return ""

    @classmethod
    def _canonical_event_message(cls, event: AstrMessageEvent) -> str:
        """Return the same untruncated canonical text used for ingress binding."""

        text = cls._event_value(event, "get_message_str") or cls._event_value(
            event,
            "get_message_outline",
        )
        if text:
            return text
        get_messages = getattr(event, "get_messages", None)
        try:
            message_chain = get_messages() if callable(get_messages) else ()
        except Exception:
            message_chain = ()
        return media_only_current_message(message_chain)

    def _identity_scope(
        self,
        event: AstrMessageEvent,
        sender_id: str,
    ) -> dict[str, str]:
        envelope = ensure_turn_envelope(event)
        platform_id = envelope.platform_id
        platform_name = self._event_value(event, "get_platform_name")
        bot_id = envelope.bot_id
        group_id = envelope.group_id
        session_id = envelope.session_id
        chat_type = envelope.chat_type
        scope_key = build_scope_key(
            platform_id=platform_id,
            bot_id=bot_id,
            chat_type=chat_type,
            group_id=group_id,
            session_id=session_id,
        )
        identity_key = build_sender_key(scope_key, sender_id)
        return {
            "platform_id": platform_id,
            "platform_name": platform_name,
            "bot_id": bot_id,
            "chat_type": chat_type,
            "group_id": group_id,
            "session_id": session_id,
            "scope_key": scope_key,
            "identity_key": identity_key,
        }

    def _livingmemory_adapter(self, event: AstrMessageEvent) -> LivingMemoryAdapter:
        """Resolve only Shio-injected or AstrBot-public LivingMemory state.

        AstrBot's public star metadata verifies installation/activation, but it
        does not expose a supported recent-message reader. A reader therefore
        has to be injected through Shio's typed adapter boundary; an active
        plugin without one is an explicit interface degradation.
        """

        injected = event.get_extra(SHIO_LIVINGMEMORY_ADAPTER, None)
        if isinstance(injected, LivingMemoryAdapter):
            event.set_extra(
                SHIO_MEMORY_READER_EVIDENCE,
                {
                    "memory_recent_reader_status": injected.status.value,
                    "memory_recent_reader_reason_code": "injected_adapter",
                    "memory_recent_reader_degraded": (
                        injected.status is not PluginEvidenceStatus.VERIFIED
                    ),
                    "memory_provided_only": False,
                },
            )
            return injected

        if not bool(self._config("prefer_livingmemory_group_history", True)):
            adapter = LivingMemoryAdapter.degraded(
                PluginEvidenceStatus.DISABLED,
                reason_code="plugin_disabled",
            )
            event.set_extra(
                SHIO_MEMORY_READER_EVIDENCE,
                {
                    "memory_recent_reader_status": "disabled",
                    "memory_recent_reader_reason_code": "plugin_disabled",
                    "memory_recent_reader_degraded": True,
                    "memory_provided_only": False,
                },
            )
            return adapter

        get_registered_star = getattr(self.context, "get_registered_star", None)
        if not callable(get_registered_star):
            adapter = LivingMemoryAdapter.degraded(
                PluginEvidenceStatus.INTERFACE_CHANGED,
                reason_code="public_registry_unavailable",
            )
            event.set_extra(
                SHIO_MEMORY_READER_EVIDENCE,
                {
                    "memory_recent_reader_status": "interface_changed",
                    "memory_recent_reader_reason_code": "public_registry_unavailable",
                    "memory_recent_reader_degraded": True,
                    "memory_provided_only": False,
                },
            )
            return adapter
        reader_reason = "public_adapter_unavailable"
        try:
            metadata = get_registered_star(LIVINGMEMORY_PLUGIN_NAME)
        except TimeoutError:
            adapter = LivingMemoryAdapter.degraded(
                PluginEvidenceStatus.TIMEOUT,
                reason_code="public_registry_timeout",
            )
        except (AttributeError, TypeError):
            adapter = LivingMemoryAdapter.degraded(
                PluginEvidenceStatus.INTERFACE_CHANGED,
                reason_code="public_registry_interface_changed",
            )
        except Exception:
            adapter = LivingMemoryAdapter.degraded(
                PluginEvidenceStatus.ERROR,
                reason_code="public_registry_error",
            )
        else:
            if metadata is None:
                adapter = LivingMemoryAdapter.degraded(
                    PluginEvidenceStatus.MISSING,
                    reason_code="plugin_missing",
                )
            else:
                try:
                    plugin_name = str(getattr(metadata, "name", "") or "").strip()
                    activated = getattr(metadata, "activated")
                except (AttributeError, TypeError):
                    adapter = LivingMemoryAdapter.degraded(
                        PluginEvidenceStatus.INTERFACE_CHANGED,
                        reason_code="public_metadata_interface_changed",
                    )
                else:
                    if (
                        plugin_name != LIVINGMEMORY_PLUGIN_NAME
                        or type(activated) is not bool
                    ):
                        adapter = LivingMemoryAdapter.degraded(
                            PluginEvidenceStatus.INTERFACE_CHANGED,
                            reason_code="public_metadata_interface_changed",
                        )
                    elif not activated:
                        adapter = LivingMemoryAdapter.degraded(
                            PluginEvidenceStatus.DISABLED,
                            reason_code="plugin_disabled",
                        )
                    else:
                        reader_reason = "public_reader_unavailable"
                        adapter = LivingMemoryAdapter.degraded(
                            PluginEvidenceStatus.INTERFACE_CHANGED,
                            reason_code="public_reader_unavailable",
                        )
        event.set_extra(
            SHIO_MEMORY_READER_EVIDENCE,
            {
                "memory_recent_reader_status": adapter.status.value,
                "memory_recent_reader_reason_code": reader_reason,
                "memory_recent_reader_degraded": (
                    adapter.status is not PluginEvidenceStatus.VERIFIED
                ),
                "memory_provided_only": False,
            },
        )
        return adapter

    async def _ensure_memory_policy_result(
        self,
        *,
        event: AstrMessageEvent,
        request: ProviderRequest,
        admission: AdmissionResult,
        conversation_event: ConversationEvent,
    ) -> MemoryPolicyResult:
        if (
            not isinstance(admission, AdmissionResult)
            or not admission.decision.allows_state_mutation
            or not isinstance(conversation_event, ConversationEvent)
            or admission.decision.binding != conversation_event.binding
        ):
            raise ValueError("memory_policy_binding_invalid")

        existing = event.get_extra(SHIO_MEMORY_POLICY_RESULT, None)
        if isinstance(existing, MemoryPolicyResult):
            if existing.decision.binding != conversation_event.binding:
                raise ValueError("memory_policy_result_binding_mismatch")
            return existing

        try:
            configured_limit = int(self._config("max_context_messages", 16))
        except (TypeError, ValueError):
            configured_limit = 16
        semantic_required = has_provided_recall(request)
        adapter = self._livingmemory_adapter(event)
        reader_evidence = event.get_extra(SHIO_MEMORY_READER_EVIDENCE, {})
        if not isinstance(reader_evidence, dict):
            reader_evidence = {}
        include_recent = True
        policy_adapter = adapter
        if (
            semantic_required
            and reader_evidence.get("memory_recent_reader_reason_code")
            == "public_reader_unavailable"
        ):
            # The current ProviderRequest is itself the supported, bound recall
            # transport. Preserve it while separately tracing that no public
            # recent-reader contract exists; never recover through private state.
            policy_adapter = LivingMemoryAdapter.verified(reader=None)
            include_recent = False
            reader_evidence = {
                **reader_evidence,
                "memory_provided_only": True,
            }
            event.set_extra(SHIO_MEMORY_READER_EVIDENCE, reader_evidence)
        result = await self.memory_policy.decide(
            admission.decision,
            conversation_event,
            adapter=policy_adapter,
            provided_recall=request,
            include_recent=include_recent,
            semantic_required=semantic_required,
            max_results=5,
            recent_limit=min(20, max(2, configured_limit)),
        )
        event.set_extra(SHIO_MEMORY_POLICY_RESULT, result)
        if (
            result.plugin_evidence.status is not PluginEvidenceStatus.VERIFIED
            or bool(reader_evidence.get("memory_recent_reader_degraded", False))
        ):
            structured_log(
                logger,
                "warning",
                "memory.policy_degraded",
                trace_id=get_trace_id(event),
                **result.trace_metadata(),
                **reader_evidence,
            )
        return result

    async def _identity_aware_history(
        self,
        event: AstrMessageEvent,
        native_contexts: list[dict] | None,
        current_message: str,
        sender_id: str,
        group_id: str,
        max_messages: int,
        max_chars: int,
    ) -> tuple[list[dict[str, str]], str]:
        # request.contexts 只作兼容输入。群聊的主历史源是 AstrBot 公开的
        # message_history_manager；每条平台记录仍须与 Shio 已接纳的人类
        # inbound ledger 对齐，避免把自发消息、已拦用户或插件回声带入 Prompt。
        envelope = ensure_turn_envelope(event)
        platform_history = await read_astrbot_group_history(
            context=self.context,
            event=event,
            accepted_records=self.ledger.records(
                envelope.scope_key,
                source_kinds=(LedgerSourceKind.INBOUND,),
            ),
            scope_key=envelope.scope_key,
            session_id=envelope.session_id,
            max_records=max_messages,
        )
        event.set_extra(SHIO_PLATFORM_GROUP_HISTORY, platform_history)
        history_metadata = platform_history.trace_metadata()
        record_pipeline_stage(
            event,
            "group_history",
            **history_metadata,
        )
        structured_log(
            logger,
            "info",
            "group_history.read",
            trace_id=get_trace_id(event),
            **history_metadata,
        )
        event.set_extra(SHIO_TYPED_HISTORY_SOURCE, list(native_contexts or []))
        trusted = clean_contexts(
            event,
            native_contexts,
            current_message,
            max_messages,
            max_chars,
            group_id=group_id,
            current_sender_id=sender_id,
        )
        if platform_history.status is PlatformGroupHistoryStatus.VERIFIED:
            source = "astrbot_platform_history"
        elif trusted:
            source = "request_contexts_compat"
        else:
            source = platform_history.status.value
        return trusted, source

    def _record_typed_inbound_once(
        self,
        event: AstrMessageEvent,
        envelope: TurnEnvelope,
        current_message: str,
    ) -> LedgerRecord:
        existing = event.get_extra(SHIO_TYPED_INBOUND_RECORD, None)
        if type(existing) is LedgerRecord:
            if (
                existing.source_kind is not LedgerSourceKind.INBOUND
                or existing.role is not LedgerRole.USER
                or existing.scope_key != envelope.scope_key
                or existing.session_id != envelope.session_id
                or existing.message_id != envelope.message_id
                or existing.sender_key != envelope.sender_key
                or existing.content_digest != ledger_content_digest(current_message)
            ):
                raise ContractViolation("typed_inbound_record_corrupt")
            return existing
        record = self.ledger.record_inbound(
            envelope,
            current_message,
            unified_msg_origin=str(
                getattr(event, "unified_msg_origin", "") or ""
            ),
        )
        if envelope.reply_to_message_id:
            self.ledger.record_reference(envelope)
        event.set_extra(SHIO_TYPED_INBOUND_RECORD, record)
        event.set_extra(SHIO_TYPED_INBOUND_RECORDED, True)
        return record

    def _build_typed_context(
        self,
        *,
        event: AstrMessageEvent,
        envelope: TurnEnvelope,
        principal: PrincipalContext,
        reply_target: ReplyTarget | None,
        reference_context: ReferenceContext | None,
        current_message: str,
        scope_key: str,
        identity_scope: dict[str, str],
    ) -> dict[str, Any]:
        runtime_decision = self._typed_runtime_decision(event)
        metadata: dict[str, Any] = {
            **runtime_decision.trace_metadata(),
            "v2_typed_history_count": 0,
            "v2_sender_thread_count": 0,
            "v2_unknown_assistant_count": 0,
            "v2_inbound_recorded": False,
            "v2_fact_count": 0,
            "v2_current_subject_fact_count": 0,
            "v2_unknown_subject_fact_count": 0,
            "v2_must_include_fact_count": 0,
            "v2_other_subject_fact_count": 0,
            "v2_planner_record_count": 0,
            "v2_replyer_thread_count": 0,
            "v2_public_background_count": 0,
            "v2_history_candidate_count": 0,
            "v2_history_accepted_count": 0,
            "v2_history_dropped_count": 0,
            "v2_history_receipt_verified_count": 0,
        }
        event.set_extra(SHIO_ASSEMBLED_CONTEXT_V2, None)
        if not scope_key:
            return metadata

        admitted_event = event.get_extra(SHIO_CONVERSATION_EVENT, None)
        if (
            not isinstance(admitted_event, ConversationEvent)
            or admitted_event.envelope != envelope
            or admitted_event.binding.current_sender_key != principal.sender_key
        ):
            return metadata
        memory_result = event.get_extra(SHIO_MEMORY_POLICY_RESULT, None)
        if (
            not isinstance(memory_result, MemoryPolicyResult)
            or memory_result.decision.binding != admitted_event.binding
        ):
            return metadata

        raw_history = event.get_extra(SHIO_TYPED_HISTORY_SOURCE, [])
        platform_history = event.get_extra(SHIO_PLATFORM_GROUP_HISTORY, None)
        platform_records = (
            platform_history.records
            if type(platform_history) is PlatformGroupHistoryRead
            else ()
        )
        platform_matched_message_ids = (
            set(platform_history.matched_ledger_message_ids)
            if type(platform_history) is PlatformGroupHistoryRead
            else set()
        )
        legacy_history = tuple(
            record
            for record in adapt_astrbot_history(
                list(raw_history or []),
                scope_key=scope_key,
                session_id=str(identity_scope.get("session_id", "")),
                group_id=str(identity_scope.get("group_id", "")),
                bot_sender_key=build_sender_key(
                    scope_key,
                    str(identity_scope.get("bot_id", "")),
                ),
            )
            if not (
                record.timestamp > 0
                and envelope.timestamp > 0
                and record.timestamp > envelope.timestamp
            )
        )
        typed_facts = memory_result.decision.selected_facts
        fact_selection = FactSelection(
            must_include_candidates=memory_result.current_subject_facts,
            uncertain_target_facts=(),
            public_background=memory_result.public_background_facts,
            other_subject_facts=(),
        )

        current_inbound_record: LedgerRecord | None = None
        if envelope.scope_key == scope_key and envelope.message_id:
            current_inbound_record = self._record_typed_inbound_once(
                event,
                envelope,
                current_message,
            )

        ledger_records = self.ledger.records(scope_key)
        if platform_matched_message_ids:
            ledger_records = tuple(
                record
                for record in ledger_records
                if not (
                    record.source_kind is LedgerSourceKind.INBOUND
                    and record.message_id in platform_matched_message_ids
                )
            )
        if current_inbound_record is not None:
            ledger_records = tuple(
                record
                for record in ledger_records
                if record.sequence <= current_inbound_record.sequence
            )
        history_candidates = (
            *platform_records,
            *legacy_history,
            *ledger_records,
        )
        normalization_context = HistoryNormalizationContext(
            binding=admitted_event.binding,
            current_action_id=(
                f"turn_revision_{admitted_event.binding.conversation_revision}"
            ),
        )
        assistant_records = tuple(
            record
            for record in history_candidates
            if record.role is LedgerRole.ASSISTANT
        )
        receipt_ids: set[str] = set()
        receipts = []
        for record in assistant_records:
            if record.source_kind is not LedgerSourceKind.OUTBOUND:
                continue
            internal_reply_id = record.message_id.rsplit(":", 1)[0]
            if not internal_reply_id or internal_reply_id in receipt_ids:
                continue
            receipt = self.send_receipts.sent_reply_record(internal_reply_id)
            if receipt is not None:
                receipt_ids.add(internal_reply_id)
                receipts.append(receipt)
        assistant_decisions = normalize_assistant_history(
            assistant_records,
            receipts=receipts,
            context=normalization_context,
        )
        accepted_assistant_records = {
            record
            for record, decision in zip(
                assistant_records,
                assistant_decisions,
                strict=True,
            )
            if decision.disposition is HistoryDisposition.CHARACTER_THREAD
        }
        reference_message_id = (
            reference_context.message_id if reference_context is not None else ""
        )
        normalized_history: list[LedgerRecord] = []
        normalized_keys: set[tuple[str, str, str]] = set()
        public_group_context_allowed = envelope.chat_type == "group"
        for record in history_candidates:
            if record.role is LedgerRole.ASSISTANT:
                if record in accepted_assistant_records:
                    normalized_history.append(record)
                continue
            if record.role is not LedgerRole.USER:
                continue
            is_current_sender = record.sender_key == principal.sender_key
            is_exact_reference = bool(
                reference_message_id
                and record.message_id == reference_message_id
                and (
                    reference_context is None
                    or not reference_context.sender_key
                    or record.sender_key == reference_context.sender_key
                )
            )
            source_verified = (
                record.source_kind is LedgerSourceKind.INBOUND
                and record.attribution_status == "verified"
            ) or (
                record.source_kind is LedgerSourceKind.LEGACY_HISTORY
                and record.attribution_status == "verified_sender"
            ) or (
                record.source_kind is LedgerSourceKind.PLATFORM_GROUP_HISTORY
                and record.attribution_status
                == "verified_platform_sender_and_admission"
            )
            is_safe_public_group_context = bool(
                public_group_context_allowed
                and record.source_kind
                in {
                    LedgerSourceKind.INBOUND,
                    LedgerSourceKind.LEGACY_HISTORY,
                    LedgerSourceKind.PLATFORM_GROUP_HISTORY,
                }
                and record.message_id != envelope.message_id
            )
            if source_verified and (
                is_current_sender
                or is_exact_reference
                or is_safe_public_group_context
            ):
                normalized_key = (
                    record.message_id,
                    record.sender_key,
                    record.content_digest,
                )
                if normalized_key in normalized_keys:
                    continue
                normalized_keys.add(normalized_key)
                normalized_history.append(record)

        typed_history = tuple(normalized_history)
        sender_thread = records_for_sender_thread(typed_history, principal.sender_key)

        assembled = (
            assemble_context_views(
                typed_history,
                reply_target=reply_target,
                reference=reference_context,
                fact_selection=fact_selection,
            )
            if reply_target is not None
            else None
        )
        event.set_extra(SHIO_ASSEMBLED_CONTEXT_V2, assembled)

        metadata.update(
            {
                **memory_result.trace_metadata(),
                "v2_typed_history_count": len(typed_history),
                "v2_sender_thread_count": len(sender_thread),
                "v2_unknown_assistant_count": sum(
                    decision.disposition is HistoryDisposition.DROP
                    for decision in assistant_decisions
                ),
                "v2_inbound_recorded": bool(
                    event.get_extra(SHIO_TYPED_INBOUND_RECORDED, False)
                ),
                "v2_fact_count": len(typed_facts),
                "v2_current_subject_fact_count": sum(
                    1
                    for fact in typed_facts
                    if fact.subject_key and fact.subject_key == principal.sender_key
                ),
                "v2_unknown_subject_fact_count": sum(
                    1 for fact in typed_facts if not fact.subject_key
                ),
                "v2_must_include_fact_count": len(
                    fact_selection.must_include_candidates
                ),
                "v2_other_subject_fact_count": len(
                    fact_selection.other_subject_facts
                ),
                "v2_planner_record_count": (
                    len(assembled.planner_records) if assembled is not None else 0
                ),
                "v2_replyer_thread_count": (
                    len(assembled.replyer_thread) if assembled is not None else 0
                ),
                "v2_public_background_count": (
                    len(assembled.public_background) if assembled is not None else 0
                ),
                "v2_history_candidate_count": len(history_candidates),
                "v2_history_accepted_count": len(typed_history),
                "v2_history_dropped_count": (
                    len(history_candidates) - len(typed_history)
                ),
                "v2_history_receipt_verified_count": len(
                    accepted_assistant_records
                ),
            }
        )
        return metadata

    @staticmethod
    def _xml_attrs(values: dict[str, str]) -> str:
        return " ".join(
            f'{key}="{html.escape(str(value), quote=True)}"'
            for key, value in values.items()
        )

    def _guest_allowed_tool_names(self) -> list[str]:
        raw = self._config(
            "guest_allowed_tools",
            ["anysearch_search", "anysearch_extract"],
        )
        if isinstance(raw, str):
            values = re.split(r"[,;\s]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        result: list[str] = []
        for item in values:
            name = str(item or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    @staticmethod
    def _capability_policy_metadata(
        *,
        policy: Any,
        available_tools: list[Any],
    ) -> dict[str, str | int | bool]:
        """Record content-free metrics for the active capability policy."""

        classifications = [classify_tool(tool) for tool in available_tools]
        decisions = [decide_tool(policy, value) for value in classifications]
        allowed_count = sum(1 for decision in decisions if decision.allowed)
        return {
            "capability_policy_status": (
                f"{policy.policy_kind}_degraded"
                if policy.is_degraded
                else f"{policy.policy_kind}_active"
            ),
            "capability_available_count": len(available_tools),
            "capability_allowed_count": allowed_count,
            "capability_denied_count": len(decisions) - allowed_count,
            "capability_unknown_count": sum(
                1
                for value in classifications
                if value.capability.value == "unknown"
            ),
            "capability_untrusted_source_denied_count": sum(
                1
                for decision in decisions
                if not decision.allowed
                and decision.reason_code
                in {
                    "audited_name_source_mismatch",
                    "declared_capability_source_missing",
                    "classification_source_unattested",
                }
            ),
            "capability_policy_degraded": policy.is_degraded,
            "capability_policy_degradation_count": len(policy.degradation_reasons),
            "capability_external_tool_budget": policy.max_external_tool_calls,
            "capability_local_presentation_budget": (
                policy.max_local_presentation_calls
            ),
        }

    @staticmethod
    def _get_tools(tool_set: Any) -> list[Any]:
        if tool_set is None:
            return []
        tools = getattr(tool_set, "tools", None)
        if tools is None:
            tools = getattr(tool_set, "func_list", [])
        return list(tools or [])

    def _available_tools(self, request_tool_set: Any) -> list[Any]:
        """合并当前请求与 AstrBot 全局插件工具，按名称去重。"""
        candidates = self._get_tools(request_tool_set)
        try:
            manager = self.context.get_llm_tool_manager()
            global_tool_set = (
                manager.get_full_tool_set()
                if hasattr(manager, "get_full_tool_set")
                else manager
            )
            candidates.extend(self._get_tools(global_tool_set))
        except Exception as exc:
            if bool(self._config("debug_log", False)):
                structured_log(
                    logger,
                    "warning",
                    "tools.inventory_failed",
                    failure_kind=safe_exception_kind(exc),
                )

        tools_by_name: dict[str, Any] = {}
        for tool in candidates:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or not bool(getattr(tool, "active", True)):
                continue
            if name not in tools_by_name:
                tools_by_name[name] = tool
        return list(tools_by_name.values())

    @staticmethod
    def _get_tool_names(tool_set: Any) -> list[str]:
        if tool_set is None:
            return []
        names = getattr(tool_set, "names", None)
        if callable(names):
            try:
                return [str(name) for name in names()]
            except Exception:
                pass
        return [
            str(getattr(tool, "name", "unknown"))
            for tool in ShioPlugin._get_tools(tool_set)
        ]

    @staticmethod
    def _response_guard_tool_names(event: AstrMessageEvent) -> tuple[str, ...]:
        """Return the closed protocol taxonomy used only by the send guard.

        Final Persona rendering intentionally receives no tools.  Detection must
        therefore not depend on that empty renderer ToolSet or on the removed
        legacy payload inventory.  Seed it from the code-owned executors and add
        only the exact typed acquisition selected for this turn.
        """

        names = {
            "search_memes",
            *SUPPORTED_SEALED_ACQUISITION_TOOL_NAMES,
        }
        acquisition = event.get_extra(SHIO_ACQUISITION_REQUEST, None)
        if isinstance(acquisition, AcquisitionRequest):
            exact_name = str(acquisition.selection.tool_name or "").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", exact_name):
                names.add(exact_name)
        return tuple(sorted(names))

    @staticmethod
    def _collect_text_components(components: Any) -> list[Any]:
        """Collect mutable text components, including those nested in Node.

        Meme Manager may decorate an LLM reply as a merged-forward ``Node``.
        Looking only at the top-level chain leaves its nested ``Plain`` text
        outside the final output guard.
        """
        result: list[Any] = []
        visited: set[int] = set()

        def visit(component: Any) -> None:
            marker = id(component)
            if marker in visited:
                return
            visited.add(marker)
            if isinstance(getattr(component, "text", None), str):
                result.append(component)
            for attribute in ("content", "chain"):
                children = getattr(component, attribute, None)
                if isinstance(children, (list, tuple)):
                    for child in children:
                        visit(child)

        if isinstance(components, (list, tuple)):
            for component in components:
                visit(component)
        elif components is not None:
            visit(components)
        return result

    def _provider(self, provider_id: str, umo: str) -> Any:
        if provider_id.strip():
            provider = self.context.get_provider_by_id(provider_id.strip())
            if provider is not None and hasattr(provider, "text_chat"):
                return provider
            structured_log(
                logger,
                "warning",
                "provider.not_found",
                provider_digest=diagnostic_digest(provider_id),
            )
        return self.context.get_using_provider(umo)

    def log_name_wake_filter_error(self, exc: Exception) -> None:
        structured_log(
            logger,
            "warning",
            "name_wake.ingest_failed",
            failure_kind=safe_exception_kind(exc),
        )

    def prepare_ingress_candidate(self, event: AstrMessageEvent) -> bool:
        """Collect only event-local wake evidence during AstrBot WakingCheck."""

        if not bool(self._config("enabled", True)):
            return False
        group_id = self._event_value(event, "get_group_id")
        sender_id = self._event_value(event, "get_sender_id")
        bot_id = self._event_value(event, "get_self_id")
        platform_id = self._event_value(event, "get_platform_id")
        session_id = self._event_value(event, "get_session_id")
        if not sender_id or not bot_id or not platform_id or not (group_id or session_id):
            return False

        message = self._canonical_event_message(event)
        if not message:
            return False
        is_direct_wake = bool(getattr(event, "is_at_or_wake_command", False))
        name_wake = (
            NameWakeDecision("none")
            if is_direct_wake or not group_id
            else self._classify_natural_name_wake(event, message, group_id)
        )
        candidate = IngressWakeCandidate(
            should_observe=True,
            was_native_wake=is_direct_wake,
            natural_direct=name_wake.is_direct,
            alias=name_wake.alias,
            reason_code=name_wake.reason or "none",
        )
        event.set_extra(SHIO_INGRESS_CANDIDATE, candidate)
        return candidate.should_observe

    def ingest_name_wake_event(self, event: AstrMessageEvent) -> bool:
        """Compatibility shim: classify a wake candidate without side effects."""

        if not self.prepare_ingress_candidate(event):
            return False
        candidate = event.get_extra(SHIO_INGRESS_CANDIDATE, None)
        return bool(
            isinstance(candidate, IngressWakeCandidate)
            and candidate.natural_direct
        )

    def _ingress_sender_kind(
        self,
        event: AstrMessageEvent,
        envelope: TurnEnvelope,
    ) -> tuple[SenderKind, PluginSourceEvidence | None]:
        source_evidence = event.get_extra(SHIO_PLUGIN_SOURCE_EVIDENCE, None)
        if source_evidence is not None and not isinstance(
            source_evidence,
            PluginSourceEvidence,
        ):
            return SenderKind.UNKNOWN_AUTOMATION, None
        if isinstance(source_evidence, PluginSourceEvidence):
            return (
                SenderKind.SELF
                if source_evidence.source.value == "shio_reply"
                else SenderKind.PLUGIN_ECHO,
                source_evidence,
            )

        adapter_flag = event.get_extra(SHIO_TYPED_ADAPTER_BOT_FLAG, None)
        if adapter_flag is not None and not isinstance(
            adapter_flag,
            TypedAdapterBotFlag,
        ):
            return SenderKind.UNKNOWN_AUTOMATION, None
        try:
            trust = self._trusted_bot_registry.resolve(
                BotIdentityObservation(
                    platform_id=envelope.platform_id,
                    sender_id=envelope.sender_id,
                    current_adapter_self_id=envelope.bot_id,
                    adapter_bot_flag=adapter_flag,
                )
            )
        except Exception:
            return SenderKind.UNKNOWN, None
        if trust.is_trusted_bot:
            return trust.sender_kind, None
        if not self._trusted_bot_config_valid:
            return SenderKind.UNKNOWN, None
        return SenderKind.HUMAN, None

    def _reneban_arrival_gate(
        self,
        event: AstrMessageEvent,
        ingress_event: IngressEvent,
    ) -> GateObservation:
        """Bind successful priority-90 arrival to the already-run gate chain.

        ReNeBan owns the ban verdict and stops matching events at priority 114;
        this handler never re-reads its private state.  It verifies public
        AstrBot registration metadata before treating arrival as not-banned.
        """

        registry_loader = getattr(
            self.context,
            "shio_reneban_registry_loader",
            None,
        )
        evidence = inspect_reneban_hook(
            self.context,
            registry_loader=(registry_loader if callable(registry_loader) else None),
        )
        event.set_extra(SHIO_RENEBAN_HOOK_EVIDENCE, evidence)
        if not evidence.is_verified:
            structured_log(
                logger,
                "error",
                "ingress.reneban_gate_degraded",
                **evidence.trace_metadata(),
            )
            return self.ingress_admission.issue_gate_observation(
                ingress_event,
                status=evidence.status,
                banned=None,
            )
        return self.ingress_admission.issue_gate_observation(
            ingress_event,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        )

    @staticmethod
    def _record_ingress_product_trace(
        event: AstrMessageEvent,
        result: AdmissionResult,
    ) -> ProductTrace:
        accepted = result.decision.allows_state_mutation
        committed = result.conversation_event
        trace = ProductTrace(
            trace_id=result.ingress_event.trace_id,
            conversation_revision=(
                committed.binding.conversation_revision
                if committed is not None
                else 0
            ),
            generation_epoch=(
                committed.binding.generation_epoch
                if committed is not None
                else 0
            ),
        )
        disposition = result.decision.disposition
        degraded = disposition is IngressDisposition.DEGRADED_EXTERNAL_GATE
        reason_code = (
            result.decision.reason_codes[0]
            if result.decision.reason_codes
            else "ingress_accepted"
        )
        trace.append(
            ProductStage.INGRESS,
            elapsed_ms=0.0,
            payload=ProductTracePayload(
                status=(
                    ProductTraceStatus.ACCEPTED
                    if accepted
                    else (
                        ProductTraceStatus.DEGRADED
                        if degraded
                        else ProductTraceStatus.DROPPED
                    )
                ),
                reason_code=reason_code,
                source_verified=result.gate_status
                is PluginEvidenceStatus.VERIFIED,
                degraded=degraded,
            ),
        )
        trace.append(
            ProductStage.SENDER_SOURCE,
            elapsed_ms=0.0,
            payload=ProductTracePayload(
                status=(
                    ProductTraceStatus.ACCEPTED
                    if accepted
                    else ProductTraceStatus.DROPPED
                ),
                reason_code=(
                    "sender_source_bound"
                    if accepted
                    else reason_code
                ),
                current_subject_only=accepted,
                source_verified=result.decision.sender_kind
                not in {SenderKind.UNKNOWN, SenderKind.UNKNOWN_AUTOMATION},
                degraded=degraded,
            ),
        )
        if not accepted:
            trace.terminate(
                ProductOutcome.DROPPED,
                elapsed_ms=0.0,
                reason_code=reason_code,
            )
        event.set_extra(SHIO_PRODUCT_TRACE, trace)
        return trace

    def _bind_accepted_turn_authority(
        self,
        event: AstrMessageEvent,
        result: AdmissionResult,
        *,
        current_message: str,
    ) -> None:
        """Dispatch the raw proof once and terminalize every unused consumer."""

        conversation_event = result.conversation_event
        if not isinstance(conversation_event, ConversationEvent):
            raise RuntimeError("accepted_turn_event_required")
        dispatch = self.accepted_turn_authority.dispatch(result)
        binding = conversation_event.binding
        owner_ticket = self.accepted_turn_authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.OWNER_ACTION,
        )
        affect_ticket = self.accepted_turn_authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.AFFECT_STATE,
        )
        relationship_ticket = self.accepted_turn_authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.RELATIONSHIP_STATE,
        )
        attention_ticket = self.accepted_turn_authority.ticket_for(
            dispatch,
            AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
        )
        try:
            route = self.owner_action_router.route(
                owner_ticket,
                current_message=current_message,
            )
            affect_evidence = issue_affect_admission_evidence(
                affect_ticket,
                dispatch=dispatch,
                accepted_turn_authority=self.accepted_turn_authority,
            )
            envelope = ensure_turn_envelope(event)
            principal = ensure_principal_context(event, self._owner_ids())
            target = ensure_direct_reply_target(
                event,
                envelope,
                current_message,
            )
            appraisal = appraise_affect(
                principal=principal,
                reply_target=target,
                current_message=current_message,
                conversation_mode="direct_reply",
            )
            affect_mutation = self.affect_states.record_human(
                affect_evidence,
                appraisal=self._continuous_affect_appraisal(appraisal),
                now=time.time(),
            )
            event.set_extra(SHIO_AFFECT_STATE_MUTATION, affect_mutation)
            relationship_mutation = self.relationship_states.record_human(
                relationship_ticket,
                current_message=current_message,
                now=time.time(),
            )
            event.set_extra(
                SHIO_RELATIONSHIP_STATE_MUTATION,
                relationship_mutation,
            )
            if affect_mutation.status is not AffectMutationStatus.ACCEPTED_HUMAN:
                affect_context = self.accepted_turn_authority.context_for(
                    affect_ticket,
                    consumer=AcceptedTurnConsumer.AFFECT_STATE,
                    binding=binding,
                )
                self.accepted_turn_authority.finalize_unclaimed_ticket(
                    affect_ticket,
                    consumer=AcceptedTurnConsumer.AFFECT_STATE,
                    binding=binding,
                    context=affect_context,
                    disposition=AcceptedTurnDisposition.REJECTED,
                )
            if (
                relationship_mutation.status
                is not RelationshipMutationStatus.ACCEPTED_HUMAN
            ):
                relationship_context = self.accepted_turn_authority.context_for(
                    relationship_ticket,
                    consumer=AcceptedTurnConsumer.RELATIONSHIP_STATE,
                    binding=binding,
                )
                self.accepted_turn_authority.finalize_unclaimed_ticket(
                    relationship_ticket,
                    consumer=AcceptedTurnConsumer.RELATIONSHIP_STATE,
                    binding=binding,
                    context=relationship_context,
                    disposition=AcceptedTurnDisposition.REJECTED,
                )
            if route.status is not OwnerActionRouteStatus.MATCHED:
                self.owner_action_router.finalize_noop_route(
                    route,
                    ticket=owner_ticket,
                )
        except BaseException:
            try:
                self.accepted_turn_authority.finalize_unclaimed_turn(
                    dispatch,
                    binding=binding,
                    disposition=AcceptedTurnDisposition.ABORTED,
                )
            except Exception:
                pass
            raise
        event.set_extra(SHIO_ACCEPTED_TURN_DISPATCH, dispatch)
        event.set_extra(SHIO_OWNER_ACTION_TICKET, owner_ticket)
        event.set_extra(SHIO_OPPORTUNITY_ATTENTION_TICKET, attention_ticket)
        event.set_extra(SHIO_OWNER_ACTION_ROUTE, route)

    @staticmethod
    def _continuous_affect_appraisal(
        appraisal: AffectAppraisal,
    ) -> ModelAffectAppraisal:
        """Project the typed current-turn appraisal into the state impulse set."""

        kind_by_trigger = {
            AffectTrigger.PRAISE: AffectAppraisalKind.POSITIVE_SOCIAL,
            AffectTrigger.GRATITUDE: AffectAppraisalKind.POSITIVE_SOCIAL,
            AffectTrigger.DISAGREEMENT: AffectAppraisalKind.NEGATIVE_SOCIAL,
            AffectTrigger.PLAYFUL_PROVOCATION: AffectAppraisalKind.PLAYFUL,
            AffectTrigger.BEING_SEEN_THROUGH: AffectAppraisalKind.PLAYFUL,
            AffectTrigger.CONCERN_FOR_AGENT: AffectAppraisalKind.CARE,
            AffectTrigger.USER_NEEDS_CARE: AffectAppraisalKind.CARE,
            AffectTrigger.CORRECTION_OR_MISTAKE: AffectAppraisalKind.REPAIR,
            AffectTrigger.APOLOGY: AffectAppraisalKind.REPAIR,
        }
        return ModelAffectAppraisal(
            appraisal_hint=kind_by_trigger.get(
                appraisal.trigger,
                AffectAppraisalKind.NEUTRAL_FACT,
            ),
            confidence=appraisal.confidence,
        )

    def _settle_affect_after_send(
        self,
        event: AstrMessageEvent,
    ) -> AffectMutationResult | None:
        tracker = self._send_observation_tracker(event)
        if tracker is None:
            return None
        evidence = issue_shio_receipt_evidence(
            self.send_receipts,
            internal_reply_id=tracker.internal_reply_id,
        )
        mutation = self.affect_states.record_shio_receipt(
            evidence,
            now=time.time(),
        )
        event.set_extra(SHIO_AFFECT_OUTBOUND_MUTATION, mutation)
        relationship_mutation = self.relationship_states.record_shio_receipt(
            evidence,
            now=time.time(),
        )
        event.set_extra(
            SHIO_RELATIONSHIP_OUTBOUND_MUTATION,
            relationship_mutation,
        )
        return mutation

    def _finalize_owner_action_noop(self, event: AstrMessageEvent) -> None:
        route = event.get_extra(SHIO_OWNER_ACTION_ROUTE, None)
        ticket = event.get_extra(SHIO_OWNER_ACTION_TICKET, None)
        if not isinstance(route, OwnerActionRouteDecision) or ticket is None:
            return
        try:
            self.owner_action_router.finalize_noop_route(route, ticket=ticket)
        except Exception:
            return

    def _finalize_opportunity_attention_noop(
        self,
        event: AstrMessageEvent,
        *,
        disposition: AcceptedTurnDisposition = AcceptedTurnDisposition.REJECTED,
    ) -> None:
        ticket = event.get_extra(SHIO_OPPORTUNITY_ATTENTION_TICKET, None)
        conversation_event = event.get_extra(SHIO_CONVERSATION_EVENT, None)
        if ticket is None or not isinstance(conversation_event, ConversationEvent):
            return
        try:
            context = self.accepted_turn_authority.context_for(
                ticket,
                consumer=AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                binding=conversation_event.binding,
            )
            self.accepted_turn_authority.finalize_unclaimed_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                binding=conversation_event.binding,
                context=context,
                disposition=disposition,
            )
        except Exception:
            return

    async def _execute_proactive_request(
        self,
        request: ProactiveComposerRequest,
    ) -> ProactiveExecutionStatus:
        """Run one exact proactive request with zero tools and one send attempt."""

        return await self.scope_concurrency.run_proactive(
            request,
            self.proactive_execution_authority,
            work_factory=lambda: self._execute_proactive_request_serialized(request),
        )

    async def _run_observed_provider_call(
        self,
        *,
        call_kind: ModelCallKind,
        latency_kind: LatencyKind,
        work_factory: Any,
    ) -> Any:
        """Measure provider work only after all admission permits exist."""

        provider_started = time.perf_counter()
        self.performance_window.record_model_call(call_kind)
        try:
            return await work_factory()
        except BaseException:
            self.performance_window.record_model_failure()
            raise
        finally:
            self.performance_window.observe_latency(
                latency_kind,
                float((time.perf_counter() - provider_started) * 1000.0),
            )

    def _observe_inference_permit(self, permit: InferencePermit) -> None:
        self.inference_budget.inspect(permit)
        self.performance_window.observe_latency(
            LatencyKind.INFERENCE_QUEUE,
            float(permit.wait_seconds * 1000.0),
        )

    async def _execute_proactive_request_serialized(
        self,
        request: ProactiveComposerRequest,
    ) -> ProactiveExecutionStatus:
        """Execute after the exact proactive scope lane has been acquired."""

        try:
            self.proactive_execution_authority.inspect_request(request)
            provider = self._provider(
                str(self._config("replyer_provider_id", "")),
                request.plan.target.unified_msg_origin,
            )
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "proactive.provider_unavailable",
                failure_kind=safe_exception_kind(exc),
            )
            return ProactiveExecutionStatus.PROVIDER_UNAVAILABLE
        if provider is None:
            return ProactiveExecutionStatus.PROVIDER_UNAVAILABLE
        cancel_safe = provider_supports_cancellation(provider)
        try:
            self.proactive_scheduler_runtime.mark_provider_cancel_safe(
                request,
                cancel_safe=cancel_safe,
            )
            response = await self.inference_budget.run_proactive_call(
                request,
                self.proactive_execution_authority,
                work_factory=lambda: self._run_observed_provider_call(
                    call_kind=ModelCallKind.PROACTIVE,
                    latency_kind=LatencyKind.PROACTIVE_PROVIDER,
                    work_factory=lambda: provider.text_chat(
                        prompt=request.user_prompt,
                        contexts=[],
                        system_prompt=request.system_prompt,
                        image_urls=[],
                        audio_urls=[],
                        func_tool=None,
                        tool_calls_result=None,
                        request_max_retries=1,
                    ),
                ),
                permit_observer=self._observe_inference_permit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "proactive.provider_failed",
                failure_kind=safe_exception_kind(exc),
            )
            return ProactiveExecutionStatus.PROVIDER_FAILED
        if not self.proactive_scheduler_runtime.is_current(request):
            return ProactiveExecutionStatus.SUPERSEDED
        if getattr(response, "role", "assistant") == "err":
            return ProactiveExecutionStatus.PROVIDER_FAILED
        try:
            presentation = self.proactive_execution_authority.validate_output(
                request,
                str(getattr(response, "completion_text", "") or ""),
            )
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "proactive.output_rejected",
                failure_kind=safe_exception_kind(exc),
            )
            return ProactiveExecutionStatus.OUTPUT_REJECTED
        if not self.proactive_scheduler_runtime.claim_send(request):
            return ProactiveExecutionStatus.SUPERSEDED
        try:
            reply = self.send_receipts.begin_proactive_presentation_reply(
                presentation,
            )
            segment = reply.segments[0]
            self.send_receipts.mark_attempted(segment.segment_id)
            try:
                sent = await self.context.send_message(
                    request.plan.target.unified_msg_origin,
                    MessageChain().message(presentation.final_visible_text),
                )
            except Exception as exc:
                self.send_receipts.mark_failed(
                    segment.segment_id,
                    failure_kind=safe_exception_kind(exc),
                )
            else:
                if sent is True:
                    self.send_receipts.mark_succeeded(segment.segment_id)
                else:
                    self.send_receipts.mark_failed(
                        segment.segment_id,
                        failure_kind="ContextSendRejected",
                    )
            status = self.proactive_execution_authority.complete_send(
                presentation,
                ledger=self.send_receipts,
            )
            terminal_metadata = presentation.trace_metadata()
            if status is ProactiveExecutionStatus.SENT:
                scene_mutation = self.group_scenes.record_proactive_outbound(
                    presentation,
                    ledger=self.send_receipts,
                    internal_reply_id=reply.internal_reply_id,
                )
                if (
                    scene_mutation.status
                    is not SceneMutationStatus.ACCEPTED_SHIO_OUTBOUND
                ):
                    structured_log(
                        logger,
                        "warning",
                        "proactive.scene_outbound_rejected",
                        **scene_mutation.trace_metadata(),
                    )
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "proactive.send_failed",
                failure_kind=safe_exception_kind(exc),
            )
            return ProactiveExecutionStatus.SEND_FAILED
        structured_log(
            logger,
            "info",
            "proactive.terminal",
            outcome=status.value,
            **terminal_metadata,
        )
        return status

    async def _run_proactive_scheduler_once(self, *, now: float) -> int:
        persona = self._configured_persona_package()
        if persona is None:
            return 0
        requests = await self.proactive_scheduler_runtime.tick(
            now=now,
            persona=persona,
            temporal_context=self._build_temporal_context(now=now),
            executor=self._execute_proactive_request,
        )
        metadata = self.proactive_scheduler_runtime.trace_metadata()
        signature = tuple(
            sorted(
                (key, value)
                for key, value in metadata.items()
                if key.startswith("proactive_scheduler_last_")
                or key == "proactive_scheduler_terminal_error_count"
            )
        )
        if signature != self._proactive_scheduler_log_signature:
            self._proactive_scheduler_log_signature = signature
            structured_log(
                logger,
                "info",
                "proactive.scheduler_tick",
                **metadata,
            )
        return len(requests)

    async def _proactive_scheduler_loop(self) -> None:
        interval = max(
            15,
            min(3600, self._config_int("proactive_scheduler_interval_seconds", 60)),
        )
        while self.proactive_scheduler_runtime.operational:
            try:
                await self._run_proactive_scheduler_once(now=float(time.time()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    logger,
                    "error",
                    "proactive.scheduler_failed",
                    failure_kind=safe_exception_kind(exc),
                )
            await asyncio.sleep(float(interval))

    def _ensure_proactive_scheduler_started(self) -> bool:
        """Start exactly one scheduler for startup and plugin hot reload alike."""

        if not self.proactive_scheduler_runtime.operational:
            return False
        task = self._proactive_scheduler_task
        if task is not None and not task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._proactive_scheduler_task = loop.create_task(
            self._proactive_scheduler_loop(),
            name="shio-proactive-scheduler",
        )
        structured_log(
            logger,
            "info",
            "proactive.scheduler_started",
            scheduler_interval_seconds=max(
                15,
                min(
                    3600,
                    self._config_int("proactive_scheduler_interval_seconds", 60),
                ),
            ),
        )
        return True

    @filter.on_astrbot_loaded()
    async def start_proactive_scheduler(self) -> None:
        self._ensure_proactive_scheduler_started()

    def admit_ingress_event(self, event: AstrMessageEvent) -> AdmissionResult | None:
        """Commit a structurally admitted event exactly once after outer gates."""

        existing = event.get_extra(SHIO_ADMISSION_RESULT, None)
        if isinstance(existing, AdmissionResult):
            return existing
        if not bool(self._config("enabled", True)):
            return None
        candidate = event.get_extra(SHIO_INGRESS_CANDIDATE, None)
        if not isinstance(candidate, IngressWakeCandidate):
            if not self.prepare_ingress_candidate(event):
                return None
            candidate = event.get_extra(SHIO_INGRESS_CANDIDATE, None)
        if not isinstance(candidate, IngressWakeCandidate):
            return None

        message = self._canonical_event_message(event)
        envelope = ensure_turn_envelope(event)
        principal = ensure_principal_context(event, self._owner_ids())
        if not all(
            (
                message,
                envelope.scope_key,
                envelope.session_id,
                envelope.message_id,
                envelope.sender_key,
            )
        ):
            return None

        with self._admission_lock:
            existing = event.get_extra(SHIO_ADMISSION_RESULT, None)
            if isinstance(existing, AdmissionResult):
                return existing
            sender_kind, source_evidence = self._ingress_sender_kind(event, envelope)
            revision_candidate = self.conversation_revisions.peek(envelope.scope_key)
            generation_epoch = self.generation_epochs.next_epoch(envelope)
            ingress = build_ingress_event(
                envelope=envelope,
                principal=principal,
                sender_kind=sender_kind,
                content=message,
                revision_candidate=revision_candidate,
                plugin_source_evidence=source_evidence,
                generation_epoch=generation_epoch,
            )
            gate_observation = event.get_extra(SHIO_GATE_OBSERVATION, None)
            if not isinstance(gate_observation, GateObservation):
                gate_observation = self._reneban_arrival_gate(
                    event,
                    ingress,
                )
            result = self.ingress_admission.admit(
                ingress,
                gate_observation=gate_observation,
            )
            event.set_extra(SHIO_ADMISSION_RESULT, result)
            event.set_extra(SHIO_INGRESS_DECISION, result.decision)
            if not result.decision.allows_state_mutation:
                self._record_ingress_product_trace(event, result)
                structured_log(
                    logger,
                    "info",
                    "ingress.dropped",
                    **result.decision.trace_metadata(),
                )
                return result

            conversation_event = result.conversation_event
            if not isinstance(conversation_event, ConversationEvent):
                return result
            try:
                self._bind_accepted_turn_authority(
                    event,
                    result,
                    current_message=message,
                )
            except Exception as exc:
                structured_log(
                    logger,
                    "error",
                    "accepted_turn.dispatch_failed",
                    failure_kind=safe_exception_kind(exc),
                )
                return result
            event.set_extra(SHIO_CONVERSATION_EVENT, conversation_event)
            scene_ready = True
            address_ready = True
            participation_assessment: ParticipationAssessment | None = None
            participation_cadence: ParticipationCadenceDecision | None = None
            participation_reaction: ParticipationReactionDecision | None = None
            attention_ticket = event.get_extra(
                SHIO_OPPORTUNITY_ATTENTION_TICKET,
                None,
            )
            attention_context = None
            try:
                attention_context = self.accepted_turn_authority.context_for(
                    attention_ticket,
                    consumer=AcceptedTurnConsumer.OPPORTUNITY_ATTENTION,
                    binding=conversation_event.binding,
                )
            except Exception as exc:
                address_ready = False
                structured_log(
                    logger,
                    "error",
                    "attention.context_failed",
                    failure_kind=safe_exception_kind(exc),
                )
            if envelope.chat_type == "group":
                try:
                    scene_mutation = self.group_scenes.record_human(
                        conversation_event,
                        decision=result.decision,
                        public_content=message,
                    )
                except Exception as exc:
                    scene_ready = False
                    structured_log(
                        logger,
                        "error",
                        "scene.ingress_failed",
                        failure_kind=safe_exception_kind(exc),
                    )
                else:
                    scene_ready = (
                        scene_mutation.status
                        is SceneMutationStatus.ACCEPTED_HUMAN
                    )
                    event.set_extra(SHIO_GROUP_SCENE_MUTATION, scene_mutation)
                    event.set_extra(
                        SHIO_GROUP_SCENE_SNAPSHOT,
                        scene_mutation.snapshot,
                    )
                    if scene_ready:
                        try:
                            self._record_typed_inbound_once(
                                event,
                                envelope,
                                message,
                            )
                        except Exception as exc:
                            structured_log(
                                logger,
                                "error",
                                "ledger.inbound_failed",
                                failure_kind=safe_exception_kind(exc),
                            )
                    if not scene_ready:
                        structured_log(
                            logger,
                            "error",
                            "scene.ingress_rejected",
                            **scene_mutation.trace_metadata(),
                        )
                if scene_ready and address_ready:
                    try:
                        structured_mentions, structured_reply = (
                            self._structured_address_evidence(event, envelope)
                        )
                        address_decision = self.address_resolution_authority.resolve_group(
                            attention_context,
                            conversation_event,
                            ingress=result.decision,
                            message_text=message,
                            aliases=self._name_wake_aliases(),
                            structured_mentions=structured_mentions,
                            structured_reply=structured_reply,
                            name_wake_candidate=candidate,
                            group_scene=scene_mutation.snapshot,
                        )
                    except Exception as exc:
                        address_ready = False
                        structured_log(
                            logger,
                            "error",
                            "address.resolve_failed",
                            failure_kind=safe_exception_kind(exc),
                        )
                    else:
                        pass
                else:
                    address_ready = False
            else:
                if address_ready:
                    try:
                        address_decision = self.address_resolution_authority.resolve_private(
                            attention_context,
                            conversation_event,
                            result.decision,
                        )
                    except Exception as exc:
                        address_ready = False
                        structured_log(
                            logger,
                            "error",
                            "address.resolve_failed",
                            failure_kind=safe_exception_kind(exc),
                        )
            if address_ready:
                try:
                    opportunity_attention = (
                        self.opportunity_attention_authority.issue(
                            attention_ticket,
                            address_decision,
                        )
                    )
                except Exception as exc:
                    address_ready = False
                    structured_log(
                        logger,
                        "error",
                        "attention.issue_failed",
                        failure_kind=safe_exception_kind(exc),
                    )
                else:
                    event.set_extra(SHIO_ADDRESS_DECISION, address_decision)
                    event.set_extra(
                        SHIO_OPPORTUNITY_ATTENTION,
                        opportunity_attention,
                    )
                    try:
                        persona_package = self._configured_persona_package()
                        if persona_package is None:
                            raise RuntimeError("persona_package_unavailable")
                        participation_assessment = self.participation_authority.issue(
                            opportunity_attention,
                            persona=persona_package,
                            current_message=message,
                            scene=(
                                scene_mutation.snapshot
                                if envelope.chat_type == "group"
                                else None
                            ),
                            opportunistic_join_enabled=(
                                self._natural_group_participation_enabled(envelope)
                            ),
                            minimum_context_messages=max(
                                2,
                                min(
                                    16,
                                    self._config_int(
                                        "natural_group_participation_min_context_messages",
                                        2,
                                    ),
                                ),
                            ),
                        )
                        participation_cadence = (
                            self.participation_cadence_authority.issue(
                                participation_assessment,
                                now=float(time.monotonic()),
                            )
                        )
                        participation_reaction = (
                            self.participation_reaction_authority.issue(
                                participation_cadence,
                                current_message=message,
                            )
                        )
                    except Exception as exc:
                        address_ready = False
                        structured_log(
                            logger,
                            "error",
                            "participation.issue_failed",
                            failure_kind=safe_exception_kind(exc),
                        )
                    else:
                        event.set_extra(
                            SHIO_PARTICIPATION_ASSESSMENT,
                            participation_assessment,
                        )
                        event.set_extra(
                            SHIO_PARTICIPATION_CADENCE,
                            participation_cadence,
                        )
                        event.set_extra(
                            SHIO_PARTICIPATION_REACTION,
                            participation_reaction,
                        )
                        if participation_reaction.decision.level in {
                            ParticipationLevel.MUST_REPLY,
                            ParticipationLevel.MAY_JOIN,
                            ParticipationLevel.REACT_ONLY,
                        }:
                            try:
                                generation_snapshot = self.generation_epochs.advance(
                                    envelope,
                                    expected_epoch=(
                                        conversation_event.binding.generation_epoch
                                    ),
                                )
                            except Exception as exc:
                                address_ready = False
                                structured_log(
                                    logger,
                                    "error",
                                    "generation.advance_failed",
                                    failure_kind=safe_exception_kind(exc),
                                )
                            else:
                                event.set_extra(
                                    GENERATION_EPOCH_EXTRA,
                                    generation_snapshot,
                                )
                                self.generation_tasks.cancel_older(
                                    generation_snapshot
                                )
                        else:
                            try:
                                passive_snapshot = (
                                    self.generation_epochs.issue_passive(
                                        envelope,
                                        expected_epoch=(
                                            conversation_event.binding.generation_epoch
                                        ),
                                    )
                                )
                            except Exception as exc:
                                address_ready = False
                                structured_log(
                                    logger,
                                    "error",
                                    "generation.passive_issue_failed",
                                    failure_kind=safe_exception_kind(exc),
                                )
                            else:
                                event.set_extra(
                                    GENERATION_EPOCH_EXTRA,
                                    passive_snapshot,
                                )
            self._record_ingress_product_trace(event, result)

        if not address_ready or (
            envelope.chat_type == "group" and not scene_ready
        ):
            self._finalize_opportunity_attention_noop(event)
            self._finalize_owner_action_noop(event)
            return result
        if envelope.chat_type == "group":
            activity_at = float(getattr(event, "created_at", 0.0) or 0.0)
            if activity_at <= 0:
                activity_at = float(time.time())
            try:
                self.proactive_policy_state.observe_group_activity(
                    platform_id=envelope.platform_id,
                    bot_id=envelope.bot_id,
                    group_id=envelope.group_id,
                    observed_at=activity_at,
                )
            except Exception as exc:
                structured_log(
                    logger,
                    "error",
                    "proactive.activity_observation_failed",
                    failure_kind=safe_exception_kind(exc),
                )
            try:
                scene_snapshot = event.get_extra(
                    SHIO_GROUP_SCENE_SNAPSHOT,
                    None,
                )
                if not isinstance(scene_snapshot, GroupSceneSnapshot):
                    raise ContractViolation("proactive_scene_missing")
                self.proactive_scheduler_runtime.record_human_activity(
                    platform_id=envelope.platform_id,
                    bot_id=envelope.bot_id,
                    group_id=envelope.group_id,
                    unified_msg_origin=str(
                        getattr(event, "unified_msg_origin", "") or ""
                    ),
                    scene=scene_snapshot,
                )
            except Exception as exc:
                structured_log(
                    logger,
                    "error",
                    "proactive.runtime_observation_failed",
                    failure_kind=safe_exception_kind(exc),
                )
            self.runtime.ingest(
                platform_id=envelope.platform_id,
                bot_id=envelope.bot_id,
                group_id=envelope.group_id,
                unified_msg_origin=str(
                    getattr(event, "unified_msg_origin", "") or ""
                ),
                sender_id=envelope.sender_id,
                sender_name=self._event_value(event, "get_sender_name")
                or envelope.sender_id,
                text=message,
                is_owner=principal.is_owner,
                is_direct_wake=candidate.was_native_wake
                or candidate.natural_direct,
                message_id=envelope.message_id,
                reply_to_message_id=envelope.reply_to_message_id,
                observe_feedback=bool(self._config("social_feedback_enabled", True)),
                created_at=float(getattr(event, "created_at", 0.0) or 0.0)
                or None,
            )

        participation_should_promote = bool(
            type(participation_reaction) is ParticipationReactionDecision
            and participation_reaction.decision.level
            in {ParticipationLevel.MAY_JOIN, ParticipationLevel.REACT_ONLY}
        )
        if candidate.should_promote or participation_should_promote:
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra(
                SHIO_NATURAL_WAKE,
                {
                    "alias": candidate.alias,
                    "reason": (
                        (
                            "participation_react_only"
                            if participation_reaction.decision.level
                            is ParticipationLevel.REACT_ONLY
                            else "participation_may_join"
                        )
                        if participation_should_promote
                        and not candidate.should_promote
                        else candidate.reason_code
                    ),
                },
            )
            structured_log(
                logger,
                "info",
                "name_wake.promoted",
                group_digest=diagnostic_digest(envelope.group_id),
                subject_digest=diagnostic_digest(envelope.sender_id),
                alias_digest=diagnostic_digest(candidate.alias),
                reason_code=(
                    (
                        "participation_react_only"
                        if participation_reaction.decision.level
                        is ParticipationLevel.REACT_ONLY
                        else "participation_may_join"
                    )
                    if participation_should_promote
                    and not candidate.should_promote
                    else candidate.reason_code
                ),
            )
        return result

    @filter.custom_filter(NaturalNameWakeFilter, False, priority=90)
    async def admit_inbound_event(self, event: AstrMessageEvent):
        """Run after external gates; never emit or globally stop an event."""

        self.admit_ingress_event(event)

        if False:  # 保持 AstrBot 对异步生成器过滤器的调用契约。
            yield event
        return

    def _set_inactive(self, event: AstrMessageEvent) -> None:
        self._discard_semantic_validation_seal(event)
        event.set_extra(SHIO_ACTIVE, False)
        event.set_extra(SHIO_PAYLOAD, None)
        event.set_extra(SHIO_TYPED_RUNTIME_DECISION, None)
        event.set_extra(SHIO_TYPED_RUNTIME, None)
        event.set_extra(SHIO_TYPED_PIPELINE_ACTIVE, False)
        event.set_extra(SHIO_PLANNED_ACTION, None)
        event.set_extra(SHIO_CONTENT_INTENT, None)
        event.set_extra(SHIO_EXPRESSION_INTENT, None)
        event.set_extra(SHIO_MEME_COMPLEMENT_DECISION, None)
        event.set_extra(SHIO_MEME_PRESENTATION_RECEIPT, None)
        event.set_extra(SHIO_PRESENTATION_SEND_EVIDENCE, None)
        event.set_extra(SHIO_AFFECT_APPRAISAL, None)
        event.set_extra(SHIO_PERSONA_EXPRESSION, None)
        event.set_extra(SHIO_ACQUISITION_REQUEST, None)
        event.set_extra(SHIO_EVIDENCE_OUTCOME, None)
        event.set_extra(SHIO_ACTIVE_CAPABILITY_POLICY, None)
        event.set_extra(SHIO_REPLY_COMPOSER_REQUEST, None)
        event.set_extra(SHIO_OUTPUT_VALIDATION_CONTEXT, None)
        event.set_extra(SHIO_SEMANTIC_GUARD_CONTRACT, None)
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        event.set_extra(SHIO_REPAIR_ATTEMPTS, 0)
        event.set_extra(SHIO_PRESENTATION_HANDOFF, None)
        event.set_extra(SHIO_LEDGER_OUTBOUND_IDS, set())
        event.set_extra(SHIO_MEDIA_ADAPTATION, None)
        event.set_extra(SHIO_MEMORY_POLICY_RESULT, None)
        event.set_extra(SHIO_MEMORY_READER_EVIDENCE, None)
        event.set_extra(SHIO_CURRENT_QUESTION_ANCHOR, None)
        event.set_extra(SHIO_INFERENCE_PERMIT, None)
        event.set_extra(SHIO_PRIMARY_PROVIDER_STARTED_AT, None)
        event.set_extra(SHIO_PERFORMANCE_SNAPSHOT, None)

    def _discard_semantic_validation_seal(self, event: AstrMessageEvent) -> None:
        self.semantic_guard_controller.discard(
            event.get_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        )
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)

    def _owner_action_denial_reason(self, planned_action: PlannedAction) -> str:
        if not self.owner_action_enabled:
            return "owner_action_disabled"
        operation = planned_action.action.operation_intent
        enabled_by_operation = {
            "artifact_read_exact": (
                self.owner_action_adapter_config.artifact_read_exact_enabled
            ),
            "artifact_grep": self.owner_action_adapter_config.artifact_grep_enabled,
            "memory_write_literal": (
                self.owner_action_adapter_config.memory_write_literal_enabled
            ),
            "sandbox_shell_once": False,
        }
        if operation is None or not enabled_by_operation.get(operation.value, False):
            return "owner_action_adapter_disabled"
        # The audited production runtime allowlist is intentionally empty.  A UI
        # flag alone can never create tool authority.
        return "owner_action_runtime_conformance_unavailable"

    @staticmethod
    def _deterministic_owner_action_fallback(
        composer_request: ReplyComposerRequest,
    ) -> str | None:
        """Return the only production-local owner-action fallback E5 can send.

        The current production graph can issue a displayable owner-action
        outcome only for an all-off denial.  If model output violates that
        outcome, do not ask the model to improvise the security result again:
        provide one content-free sentence and still pass it through the exact
        REPAIR validator, presentation handoff and final-send seal.
        """

        outcome = composer_request.action_outcome
        if (
            type(outcome) is ActionOutcomeIntent
            and outcome.kind is ActionOutcomeKind.DENIED
            and outcome.has_output is False
        ):
            return "这个我不能替你做，所以没动。"
        return None

    def _prepare_disabled_owner_action_outcome(
        self,
        *,
        event: AstrMessageEvent,
        planned_action: PlannedAction,
        content_seed: ContentIntentSeed,
    ) -> ContentIntentSeed:
        """Create a canonical denial; E5 never opens an adapter or executor."""

        ticket = event.get_extra(SHIO_OWNER_ACTION_TICKET, None)
        route = event.get_extra(SHIO_OWNER_ACTION_ROUTE, None)
        if (
            ticket is None
            or not isinstance(route, OwnerActionRouteDecision)
            or route.status is not OwnerActionRouteStatus.MATCHED
        ):
            raise RuntimeError("owner_action_route_unavailable")
        store, _durable = self._ensure_owner_action_durable_runtime()
        action_route = self.owner_action_controller.seal_action_route(
            ticket,
            planned_action,
            route,
        )
        now = time.time()
        denial = self.owner_action_controller.deny_action_route(
            ticket,
            action_route,
            denied_at=now,
            reason_codes=(self._owner_action_denial_reason(planned_action),),
        )
        inspection = self.owner_action_controller.inspect_denial_lineage(denial)
        reservation = store.reserve(
            inspection.operation,
            request_digest=denial.denial_digest,
            now=now,
        )
        store.mark_terminal(
            reservation.handle,
            status=denial.status,
            effect_state=denial.effect_state,
            attempted=False,
            now=now + 0.001,
        )
        outcome = self.action_outcome_authority.issue_from_denial(
            current_planned_action=planned_action,
            denial=denial,
        )
        attached = attach_action_outcome(
            content_seed,
            outcome,
            self.action_outcome_authority,
            current_planned_action=planned_action,
        )
        event.set_extra(SHIO_OWNER_ACTION_SOURCE, denial)
        event.set_extra(SHIO_ACTION_OUTCOME, outcome)
        event.set_extra(
            SHIO_OWNER_ACTION_LIFECYCLE_HANDLE,
            reservation.handle,
        )
        return attached

    async def _execute_react_presentation(
        self,
        *,
        event: AstrMessageEvent,
        planned_action: PlannedAction,
        expression_intent: ExpressionIntent,
    ) -> MemeExecutionReceipt | None:
        """Execute the sole zero-text REACT presentation path."""

        # Explicitly disable the legacy semantic-tool selection state for this
        # turn. The compatibility executor below receives only a closed
        # expression marker and never exposes model query/candidate material.
        for key, value in (
            ("meme_manager_semantic_active", False),
            ("meme_manager_semantic_mode", ""),
            ("meme_manager_semantic_selected_ids", []),
            ("meme_manager_semantic_search_completed", False),
        ):
            event.set_extra(key, value)
        generation = event_generation_snapshot(event)
        if generation is None:
            record_pipeline_stage(
                event,
                "meme_presentation",
                meme_runtime_status="generation_missing",
                meme_execution_status="suppressed",
            )
            return None
        conformance = self.meme_manager_conformance.collect(self.context)
        receipt: MemeExecutionReceipt
        if conformance.status is not MemeManagerConformanceStatus.VERIFIED:
            reason_by_status = {
                MemeManagerConformanceStatus.MISSING: "runtime_missing",
                MemeManagerConformanceStatus.DISABLED: "runtime_disabled",
                MemeManagerConformanceStatus.INTERFACE_CHANGED: (
                    "runtime_interface_changed"
                ),
                MemeManagerConformanceStatus.BUILD_CHANGED: "runtime_build_changed",
                MemeManagerConformanceStatus.ERROR: "runtime_error",
            }
            receipt = self.meme_execution_authority.issue_suppressed(
                planned_action=planned_action,
                expression_intent=expression_intent,
                generation=generation,
                reason_code=reason_by_status[conformance.status],
            )
        else:
            evidence = conformance.evidence
            if evidence is None:
                raise ContractViolation("meme_runtime_evidence_required")
            lease = self.meme_execution_authority.prepare(
                planned_action=planned_action,
                expression_intent=expression_intent,
                generation=generation,
                runtime_evidence=evidence,
            )
            permit = self.meme_execution_authority.claim(
                lease,
                generation=generation,
            )
            receipt = await self.scope_concurrency.run_event(
                generation,
                self._effective_principal(event),
                kind=ScopeWorkKind.REACT,
                work_factory=lambda: execute_meme_permit(
                    self.meme_execution_authority,
                    permit,
                    event=event,
                    generation=generation,
                ),
            )
        self.meme_execution_authority.inspect_receipt(receipt)
        event.set_extra(SHIO_MEME_PRESENTATION_RECEIPT, receipt)
        record_pipeline_stage(
            event,
            "meme_presentation",
            **conformance.trace_metadata(),
            **receipt.trace_metadata(),
        )
        return receipt

    async def _execute_text_meme_complement_after_send(
        self,
        *,
        event: AstrMessageEvent,
        tracker: ReplyObservationTracker,
    ) -> MemeExecutionReceipt | None:
        """Run one optional Meme only after the exact text presentation succeeded."""

        planned_action = event.get_extra(SHIO_PLANNED_ACTION, None)
        expression_intent = event.get_extra(SHIO_EXPRESSION_INTENT, None)
        if (
            type(planned_action) is not PlannedAction
            or type(expression_intent) is not ExpressionIntent
            or planned_action.kind is not ActionKind.REPLY
            or expression_intent.modality is not ExpressionModality.TEXT_AND_MEME
        ):
            return None
        existing = event.get_extra(SHIO_MEME_PRESENTATION_RECEIPT, None)
        if type(existing) is MemeExecutionReceipt:
            return self.meme_execution_authority.inspect_receipt(existing)
        presentation = event.get_extra(SHIO_PRESENTATION_HANDOFF, None)
        if type(presentation) is not PresentationHandoff:
            raise ContractViolation("meme_complement_presentation_required")
        send_evidence = self.send_receipts.issue_presentation_send_terminal_evidence(
            presentation,
            internal_reply_id=tracker.internal_reply_id,
        )
        event.set_extra(SHIO_PRESENTATION_SEND_EVIDENCE, send_evidence)
        generation = event_generation_snapshot(event)
        if generation is None:
            raise ContractViolation("meme_generation_not_canonical")
        if not event_epoch_validation(event, self.generation_epochs).is_current:
            receipt = self.meme_execution_authority.issue_stale_complement(
                planned_action=planned_action,
                expression_intent=expression_intent,
                generation=generation,
                presentation=presentation,
                send_ledger=self.send_receipts,
                send_evidence=send_evidence,
            )
            conformance = None
        else:
            conformance = self.meme_manager_conformance.collect(self.context)
            if conformance.status is not MemeManagerConformanceStatus.VERIFIED:
                reason_by_status = {
                    MemeManagerConformanceStatus.MISSING: "runtime_missing",
                    MemeManagerConformanceStatus.DISABLED: "runtime_disabled",
                    MemeManagerConformanceStatus.INTERFACE_CHANGED: (
                        "runtime_interface_changed"
                    ),
                    MemeManagerConformanceStatus.BUILD_CHANGED: (
                        "runtime_build_changed"
                    ),
                    MemeManagerConformanceStatus.ERROR: "runtime_error",
                }
                receipt = self.meme_execution_authority.issue_suppressed(
                    planned_action=planned_action,
                    expression_intent=expression_intent,
                    generation=generation,
                    reason_code=reason_by_status[conformance.status],
                )
            else:
                runtime_evidence = conformance.evidence
                if runtime_evidence is None:
                    raise ContractViolation("meme_runtime_evidence_required")
                lease = self.meme_execution_authority.prepare(
                    planned_action=planned_action,
                    expression_intent=expression_intent,
                    generation=generation,
                    runtime_evidence=runtime_evidence,
                    presentation=presentation,
                    send_ledger=self.send_receipts,
                    send_evidence=send_evidence,
                )
                permit = self.meme_execution_authority.claim(
                    lease,
                    generation=generation,
                )
                receipt = await self.scope_concurrency.run_event(
                    generation,
                    self._effective_principal(event),
                    kind=ScopeWorkKind.REACT,
                    work_factory=lambda: execute_meme_permit(
                        self.meme_execution_authority,
                        permit,
                        event=event,
                        generation=generation,
                    ),
                )
        self.meme_execution_authority.inspect_receipt(receipt)
        event.set_extra(SHIO_MEME_PRESENTATION_RECEIPT, receipt)
        trace_values = receipt.trace_metadata()
        if conformance is not None:
            trace_values = {**conformance.trace_metadata(), **trace_values}
        record_pipeline_stage(
            event,
            "meme_presentation",
            text_terminal=True,
            **trace_values,
        )
        structured_log(
            logger,
            "info",
            "meme.presentation_terminal",
            trace_id=get_trace_id(event),
            **receipt.trace_metadata(),
        )
        return receipt

    def _block_typed_turn(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        *,
        reason_code: str,
    ) -> None:
        """Fail closed when a direct v2 turn cannot be bound safely.

        A typed-only deployment must never leak back into AstrBot's raw request.
        """

        self._finalize_owner_action_noop(event)
        req.system_prompt = ""
        req.contexts = []
        req.prompt = ""
        req.extra_user_content_parts = []
        req.image_urls = []
        req.audio_urls = []
        req.func_tool = ToolSet([])
        req.tool_calls_result = None
        event.set_extra(SHIO_ACTIVE, False)
        event.set_extra(SHIO_TYPED_PIPELINE_ACTIVE, False)
        event.set_extra(
            SHIO_PAYLOAD,
            {"typed_pipeline_active": False, "blocked_reason": reason_code},
        )
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            stop()
        structured_log(
            logger,
            "error",
            "typed_reply.fail_closed",
            trace_id=get_trace_id(event),
            reason_code=reason_code,
        )

    def _activate_planned_reply_request(
        self,
        *,
        event: AstrMessageEvent,
        req: ProviderRequest,
        envelope: TurnEnvelope,
        principal: PrincipalContext,
        identity_scope: dict[str, str],
        current_message: str,
        sender_id: str,
        sender_name: str,
        scope_key: str,
        planned_action: PlannedAction,
        content_seed: ContentIntentSeed,
        capability_policy: Any,
        persona_package: PersonaPackage,
        recent_replies: list[str],
        evidence_outcome: EvidenceOutcome | None = None,
    ) -> bool:
        """Build the sole final Persona request after content and evidence settle."""

        assembled = event.get_extra(SHIO_ASSEMBLED_CONTEXT_V2, None)
        media_adaptation = event.get_extra(SHIO_MEDIA_ADAPTATION, None)
        current_question_anchor = event.get_extra(
            SHIO_CURRENT_QUESTION_ANCHOR,
            None,
        )
        target = planned_action.action.reply_target
        if (
            planned_action.kind
            not in {
                ActionKind.REPLY,
                ActionKind.USE_TOOL,
                ActionKind.EXECUTE_ACTION,
            }
            or target is None
            or not isinstance(assembled, AssembledContext)
            or not isinstance(media_adaptation, AstrBotMediaAdaptation)
            or not isinstance(current_question_anchor, CurrentQuestionAnchor)
            or assembled.reply_target != target
            or content_seed.binding != planned_action.binding
        ):
            return False

        try:
            product_trace = event.get_extra(SHIO_PRODUCT_TRACE, None)
            if isinstance(product_trace, ProductTrace):
                product_trace.append(
                    ProductStage.CONTENT_INTENT,
                    elapsed_ms=0.0,
                    payload=ProductTracePayload(
                        status=ProductTraceStatus.READY,
                        item_count=len(content_seed.intent.required_atoms),
                        target_bound=True,
                        current_subject_only=True,
                        source_verified=True,
                    ),
                )
            appraisal = appraise_affect(
                principal=principal,
                reply_target=target,
                current_message=current_message,
                conversation_mode=capability_policy.conversation_mode,
            )
            affect_mutation = event.get_extra(SHIO_AFFECT_STATE_MUTATION, None)
            continuous_affect = self.affect_states.issue_render_context(
                affect_mutation,
                binding=planned_action.binding,
                now=time.time(),
            )
            event.set_extra(SHIO_AFFECT_RENDER_CONTEXT, continuous_affect)
            relationship_mutation = event.get_extra(
                SHIO_RELATIONSHIP_STATE_MUTATION,
                None,
            )
            if not isinstance(relationship_mutation, RelationshipMutationResult):
                return False
            if (
                relationship_mutation.status
                is not RelationshipMutationStatus.ACCEPTED_HUMAN
            ):
                return False
            relationship_context = self.relationship_states.issue_render_context(
                relationship_mutation,
                binding=planned_action.binding,
            )
            event.set_extra(
                SHIO_RELATIONSHIP_RENDER_CONTEXT,
                relationship_context,
            )
            if isinstance(product_trace, ProductTrace):
                product_trace.append(
                    ProductStage.AFFECT,
                    elapsed_ms=0.0,
                    payload=ProductTracePayload(
                        status=ProductTraceStatus.READY,
                        item_count=1,
                        target_bound=True,
                        current_subject_only=True,
                        source_verified=True,
                    ),
                )
            persona_expression = build_persona_expression_plan(
                persona_package,
                appraisal,
                principal=principal,
                conversation_mode=capability_policy.conversation_mode,
                recent_visible_replies=tuple(recent_replies),
            )
            feedback_scores = (
                self.runtime.expression_feedback_scores(
                    scope_key=scope_key,
                    persona_key=persona_package.package_id,
                    situation_id=persona_expression.trigger.value,
                    relationship_scope=principal.relationship_role,
                )
                if (
                    bool(self._config("social_feedback_enabled", True))
                    and persona_expression.is_actionable
                )
                else {}
            )
            retrieval = retrieve_expression_candidates(
                persona_package,
                persona_expression,
                feedback_scores=feedback_scores,
                max_candidates=3,
            )
            emotion_tags = tuple(
                dict.fromkeys(
                    value
                    for value in (
                        appraisal.trigger.value,
                        appraisal.surface_emotion.value,
                        (
                            appraisal.secondary_emotion.value
                            if appraisal.secondary_emotion is not None
                            else ""
                        ),
                        appraisal.hidden_concern.value,
                    )
                    if value
                )
            )
            recent_sender_messages = tuple(
                record.content
                for record in assembled.replyer_thread
                if record.role is LedgerRole.USER and record.content
            )[-4:]
            meme_complement = decide_text_meme_complement(
                cadence=self.meme_complement_cadence,
                planned_action_authority=self.planned_action_authority,
                planned_action=planned_action,
                current_message=current_message,
                social_act=persona_expression.topic_return,
                emotion_tags=emotion_tags,
                relationship_distance=appraisal.relationship_distance,
                recent_sender_messages=recent_sender_messages,
            )
            expression_intent = self.expression_intent_authority.issue(
                planned_action_authority=self.planned_action_authority,
                planned_action=planned_action,
                modality=(
                    ExpressionModality.TEXT_AND_MEME
                    if meme_complement.eligible
                    else ExpressionModality.TEXT
                ),
                social_act=persona_expression.topic_return,
                emotion_tags=emotion_tags,
                **(
                    {
                        "current_message": current_message,
                        "relationship_distance": appraisal.relationship_distance,
                        "recent_sender_messages": recent_sender_messages,
                    }
                    if meme_complement.eligible
                    else {}
                ),
                max_bubbles=min(
                    3,
                    max(1, int(self._config("chat_max_bubbles", 3))),
                ),
                meme_executor=("meme_manager" if meme_complement.eligible else ""),
                max_meme_calls=(1 if meme_complement.eligible else 0),
                reason_codes=("typed_text_renderer",),
            )
            event.set_extra(SHIO_MEME_COMPLEMENT_DECISION, meme_complement)
            structured_log(
                logger,
                "info",
                "meme.complement_decision",
                trace_id=get_trace_id(event),
                eligible=meme_complement.eligible,
                reason_code=meme_complement.reason_code,
                category=(
                    meme_complement.category.value
                    if meme_complement.category is not None
                    else "none"
                ),
            )
            if isinstance(product_trace, ProductTrace):
                product_trace.append(
                    ProductStage.EXPRESSION,
                    elapsed_ms=0.0,
                    payload=ProductTracePayload(
                        status=ProductTraceStatus.READY,
                        item_count=len(retrieval.candidates),
                        target_bound=True,
                        current_subject_only=True,
                        source_verified=True,
                    ),
                )
            composer_request = build_reply_composer_request(
                planned_action=planned_action,
                content_seed=content_seed,
                expression_intent=expression_intent,
                affect_appraisal=appraisal,
                continuous_affect=continuous_affect,
                relationship_context=relationship_context,
                persona_expression=persona_expression,
                expression_candidates=retrieval.candidates,
                persona_package=persona_package,
                capability_policy=capability_policy,
                current_message=current_message,
                sender_name=sender_name,
                current_question_anchor=current_question_anchor,
                assembled_context=assembled,
                media_context=media_adaptation.context,
                media_prompt_evidence=(
                    media_adaptation.transport.safe_prompt_evidence
                ),
                evidence_outcome=evidence_outcome,
                temporal_context=self._build_temporal_context(
                    now=float(time.time()),
                ),
            )
            semantic_contract = SemanticGuardContract(
                composer_request=composer_request,
                planned_action=planned_action,
                content_intent=content_seed.intent,
                current_question_anchor=current_question_anchor,
                media_context=media_adaptation.context,
                current_message=current_message,
                evidence_outcome=evidence_outcome,
                action_outcome=composer_request.action_outcome,
                action_outcome_authority=(
                    composer_request.action_outcome_authority
                ),
            )
            validation_context = build_output_validation_context(
                composer_request=composer_request,
                current_message=current_message,
                expected_target_message_id=target.message_id,
                expected_target_sender_key=principal.sender_key,
                is_owner=principal.is_owner,
                current_question_anchor=current_question_anchor,
                semantic_contract=semantic_contract,
            )
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "typed_reply.prepare_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            return False

        learning_context = make_learning_context(
            persona_key=persona_package.package_id,
            situation_id=appraisal.trigger.value,
            relationship_scope=principal.relationship_role,
            behavior_ids=tuple(
                candidate.material_id for candidate in retrieval.candidates
            ),
        )
        media_adaptation.restore_request_media(req)
        req.system_prompt = composer_request.system_prompt
        req.contexts = []
        req.prompt = composer_request.user_prompt
        req.extra_user_content_parts = []
        req.func_tool = ToolSet([])
        req.tool_calls_result = None

        event.set_extra(SHIO_TYPED_PIPELINE_ACTIVE, True)
        event.set_extra(SHIO_PLANNED_ACTION, planned_action)
        event.set_extra(SHIO_CONTENT_INTENT, content_seed.intent)
        event.set_extra(SHIO_EXPRESSION_INTENT, expression_intent)
        event.set_extra(SHIO_AFFECT_APPRAISAL, appraisal)
        event.set_extra(SHIO_PERSONA_EXPRESSION, persona_expression)
        event.set_extra(SHIO_EVIDENCE_OUTCOME, evidence_outcome)
        event.set_extra(SHIO_ACTIVE_CAPABILITY_POLICY, capability_policy)
        event.set_extra(SHIO_REPLY_COMPOSER_REQUEST, composer_request)
        event.set_extra(SHIO_OUTPUT_VALIDATION_CONTEXT, validation_context)
        event.set_extra(SHIO_SEMANTIC_GUARD_CONTRACT, semantic_contract)
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        event.set_extra(SHIO_REPAIR_ATTEMPTS, 0)
        event.set_extra(SHIO_ACTIVE, True)
        event.set_extra(
            SHIO_PAYLOAD,
            {
                "typed_pipeline_active": True,
                "system_prompt": composer_request.system_prompt,
                "contexts": [],
                "recent_assistant_replies": list(recent_replies),
                "prompt": composer_request.user_prompt,
                "current_message": current_message,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "platform_id": str(identity_scope.get("platform_id", "")),
                "bot_id": str(identity_scope.get("bot_id", "")),
                "chat_type": str(identity_scope.get("chat_type", "private")),
                "group_id": str(identity_scope.get("group_id", "")),
                "identity_key": principal.sender_key,
                "is_owner": principal.is_owner,
                "conversation_mode": capability_policy.conversation_mode,
                "scope_key": scope_key,
                "target_sequence": 0,
                "history_source": "typed_assembled",
                "expression_ids": [
                    candidate.material_id for candidate in retrieval.candidates
                ],
                "learning_context": learning_context,
                "reply_shape": composer_request.reply_shape,
                "chat_max_bubbles": composer_request.max_bubbles,
                "action_kind": planned_action.kind.value,
            },
        )
        record_pipeline_stage(
            event,
            "action",
            **planned_action.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "content_intent",
            **content_seed.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "expression_intent",
            **expression_intent.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "plan",
            target_model="typed_action_content_renderer",
            has_target_message_id=True,
            target_actionable=True,
            has_reference_context=assembled.reference is not None,
            reference_sender_verified=bool(
                assembled.reference is not None and assembled.reference.sender_key
            ),
            fact_model="typed_grounding",
            fact_count=len(validation_context.grounding_facts),
            expression_count=len(retrieval.candidates),
            reply_shape=composer_request.reply_shape,
            use_allowed_tools=False,
            planner_provider_candidate_count=0,
            allowed_tool_count=0,
            planner_call_count=0,
            planner_failure_count=0,
            v2_plan_status="typed_action_planner",
            v2_plan_fact_model="typed_grounding",
            v2_plan_fact_count=len(validation_context.grounding_facts),
            v2_plan_replan_required=False,
            v2_plan_rejection_code="",
            final_generation_budget=composer_request.call_budget.total_model_calls,
        )
        structured_log(
            logger,
            "info",
            "typed_reply.prepared",
            trace_id=get_trace_id(event),
            source_kind=envelope.source_kind,
            subject_digest=diagnostic_digest(principal.sender_key),
            target_digest=diagnostic_digest(target.message_id),
            action_kind=planned_action.kind.value,
            context_record_count=composer_request.context_record_count,
            grounding_fact_count=composer_request.grounding_fact_count,
            expression_count=composer_request.candidate_count,
            planner_call_count=0,
            final_generation_budget=1,
        )
        return True

    @filter.on_llm_request(priority=-sys.maxsize)
    async def enforce_agent_permission(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """内置权限守卫：主人保留全部工具，普通用户只保留精确白名单。"""
        if not bool(self._config("permission_guard_enabled", True)):
            return

        ensure_turn_envelope(event)
        sender_id, _ = self._effective_sender(event)
        principal = self._effective_principal(event)
        is_owner = principal.is_owner
        identity_scope = self._identity_scope(event, sender_id)
        event.set_extra(SHIO_IDENTITY_SCOPE, identity_scope)
        configured_allowed = set(self._guest_allowed_tool_names())
        available_tools = self._available_tools(req.func_tool)
        active_policy = (
            build_owner_capability_policy(
                principal,
                conversation_mode="direct_reply",
            )
            if is_owner
            else build_guest_capability_policy(
                principal,
                configured_tool_names=configured_allowed,
                conversation_mode="direct_reply",
            )
        )
        allowed_tools = [
            tool
            for tool in available_tools
            if decide_tool(active_policy, classify_tool(tool)).allowed
        ]
        allowed_tool_names = [str(getattr(tool, "name", "")) for tool in allowed_tools]
        if not sender_id:
            allowed_tools = []
            allowed_tool_names = []
        event.set_extra(
            SHIO_CAPABILITY_POLICY,
            self._capability_policy_metadata(
                policy=active_policy,
                available_tools=available_tools,
            ),
        )

        allowed_name_set = set(allowed_tool_names)
        removed_tool_names = [
            name
            for name in self._get_tool_names(req.func_tool)
            if name not in allowed_name_set
        ]
        req.func_tool = ToolSet(allowed_tools)

        if bool(self._config("inject_verified_context", True)):
            access_mode = (
                "owner_entitled_typed_actions"
                if is_owner
                else ("limited_read_only" if allowed_tool_names else "chat_only")
            )
            verified_values = {
                "source": "shio",
                "platform_id": identity_scope["platform_id"],
                "bot_id": identity_scope["bot_id"] or "unknown",
                "chat_type": identity_scope["chat_type"],
                "group_id": identity_scope["group_id"] or "private",
                "sender_id": sender_id or "unknown",
                "identity_key": identity_scope["identity_key"],
                "owner": "true" if is_owner else "false",
                "mode": access_mode,
            }
            if isinstance(event.get_extra(SHIO_NATURAL_WAKE, None), dict):
                verified_values["wake_reason"] = "natural_name"
            if allowed_tool_names and not is_owner:
                verified_values["allowed_tools"] = ",".join(allowed_tool_names)
                verified_values["external_writes"] = "disabled"
            elif not is_owner:
                verified_values["tools"] = "disabled"
                verified_values["external_actions"] = "disabled"
            context_text = (
                "<verified_access_control "
                + self._xml_attrs(verified_values)
                + " />"
            )
            parts = req.extra_user_content_parts
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(TextPart(text=context_text).mark_as_temp())

        if (
            not is_owner
            and removed_tool_names
            and bool(self._config("permission_audit_log", True))
        ):
            structured_log(
                logger,
                "info",
                "capability.blocked",
                group_digest=diagnostic_digest(identity_scope["group_id"]),
                subject_digest=diagnostic_digest(sender_id),
                blocked_tool_count=len(removed_tool_names),
            )

    # 内置权限守卫先裁决，角色回复链随后清理后台注入并构造纯聊天请求。
    # 内置权限守卫先裁决，角色回复链随后构造唯一的 typed Composer 请求。
    @filter.on_llm_request(priority=-sys.maxsize - 100)
    async def build_persona_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """接管启用状态下的所有角色聊天；失败时闭锁，绝不回退旧链。"""
        if not bool(self._config("enabled", True)):
            return

        admission = self.admit_ingress_event(event)
        if (
            not isinstance(admission, AdmissionResult)
            or not admission.decision.allows_state_mutation
        ):
            self._set_inactive(event)
            self._block_typed_turn(
                event,
                req,
                reason_code="ingress_not_admitted",
            )
            return

        # AstrBot's internal tool loop does not re-enter this hook.  A value
        # arriving here is therefore legacy/unbound input, never evidence for
        # the current action.  Drop it and build the current turn from the
        # admitted event; the sealed acquisition path below owns all results.
        req.tool_calls_result = None
        self._set_inactive(event)
        sender_id, sender_name = self._effective_sender(event)
        turn_envelope = ensure_turn_envelope(event)
        generation_snapshot = event_generation_snapshot(event)
        if (
            generation_snapshot is None
            or generation_snapshot.epoch
            != admission.decision.binding.generation_epoch
            or generation_snapshot.scope_key != turn_envelope.scope_key
        ):
            self._block_typed_turn(
                event,
                req,
                reason_code="generation_epoch_unbound",
            )
            return

        wake_metadata = event.get_extra(SHIO_NATURAL_WAKE, None)
        wake_reason = (
            str(wake_metadata.get("reason", "") or "").strip()
            if isinstance(wake_metadata, dict)
            else ""
        )
        conversation_mode = (
            "group_join"
            if turn_envelope.chat_type == "group"
            and wake_reason
            in {"participation_may_join", "participation_react_only"}
            else "direct_reply"
        )
        principal = self._effective_principal(event)
        identity_scope = event.get_extra(SHIO_IDENTITY_SCOPE, None)
        if not isinstance(identity_scope, dict):
            identity_scope = self._identity_scope(event, sender_id)
        admitted_current_message = self._canonical_event_message(event)
        current_message = admitted_current_message

        reply_target = ensure_direct_reply_target(event, turn_envelope, current_message)
        reference_context = ensure_reference_context(event, turn_envelope)
        admitted_event = event.get_extra(SHIO_CONVERSATION_EVENT, None)
        if not isinstance(admitted_event, ConversationEvent):
            self._block_typed_turn(
                event,
                req,
                reason_code="conversation_event_unavailable",
            )
            return
        start_pipeline_trace(
            event,
            current_message=current_message,
            sender_id=sender_id,
            group_id=str(identity_scope.get("group_id", "")),
            chat_type=str(identity_scope.get("chat_type", "private")),
            is_owner=principal.is_owner,
            envelope_metadata=turn_envelope.trace_metadata(),
            trace_id=admitted_event.binding.trace_id,
        )
        affect_state_mutation = event.get_extra(
            SHIO_AFFECT_STATE_MUTATION,
            None,
        )
        if (
            not isinstance(affect_state_mutation, AffectMutationResult)
            or affect_state_mutation.status
            is not AffectMutationStatus.ACCEPTED_HUMAN
            or affect_state_mutation.state is None
            or affect_state_mutation.state.conversation_revision
            != admitted_event.binding.conversation_revision
        ):
            self._block_typed_turn(
                event,
                req,
                reason_code="affect_state_unbound",
            )
            return
        record_pipeline_stage(
            event,
            "affect_state_ingress",
            **affect_state_mutation.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "target",
            has_target=reply_target is not None,
            target_actionable=bool(reply_target is not None and reply_target.is_actionable),
            has_target_message_id=bool(reply_target is not None and reply_target.message_id),
            has_target_sender_key=bool(reply_target is not None and reply_target.sender_key),
            target_degradation_count=(
                len(reply_target.degradation_reasons)
                if reply_target is not None
                else 1
            ),
            target_message_digest=diagnostic_digest(
                reply_target.message_id if reply_target is not None else ""
            ),
            target_subject_digest=diagnostic_digest(
                reply_target.sender_key if reply_target is not None else ""
            ),
        )
        if turn_envelope.chat_type == "group":
            scene_mutation = event.get_extra(SHIO_GROUP_SCENE_MUTATION, None)
            if (
                not isinstance(scene_mutation, SceneMutationResult)
                or scene_mutation.status
                is not SceneMutationStatus.ACCEPTED_HUMAN
                or scene_mutation.snapshot.scope_key
                != admitted_event.binding.scope_key
                or scene_mutation.snapshot.conversation_revision
                != admitted_event.binding.conversation_revision
            ):
                self._block_typed_turn(
                    event,
                    req,
                    reason_code="group_scene_unbound",
                )
                return
            event.set_extra(
                SHIO_GROUP_SCENE_SNAPSHOT,
                scene_mutation.snapshot,
            )
            record_pipeline_stage(
                event,
                "scene_ingress",
                **scene_mutation.trace_metadata(),
            )
        address_decision = event.get_extra(SHIO_ADDRESS_DECISION, None)
        if (
            not isinstance(address_decision, AddressDecision)
            or address_decision.binding != admitted_event.binding
        ):
            self._block_typed_turn(
                event,
                req,
                reason_code="address_unbound",
            )
            return
        record_pipeline_stage(
            event,
            "address",
            **address_decision.trace_metadata(),
        )
        get_messages = getattr(event, "get_messages", None)
        try:
            message_chain = list(get_messages() or ()) if callable(get_messages) else ()
            media_adaptation = adapt_astrbot_media(
                binding=admitted_event.binding,
                provider_request=req,
                message_chain=message_chain,
                sender_key_resolver=(
                    lambda value: build_sender_key(
                        admitted_event.binding.scope_key,
                        value,
                    )
                ),
            )
        except Exception as exc:
            structured_log(
                logger,
                "warning",
                "media.adaptation_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="media_context_invalid",
            )
            return
        event.set_extra(SHIO_MEDIA_ADAPTATION, media_adaptation)
        record_pipeline_stage(
            event,
            "media",
            **media_adaptation.trace_metadata(),
        )
        try:
            current_question_anchor = build_current_question_anchor(
                admitted_event.binding,
                admitted_current_message,
                media_item_ids=tuple(
                    item.item_id for item in media_adaptation.context.items
                ),
            )
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "current_anchor.build_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="current_question_anchor_invalid",
            )
            return
        event.set_extra(
            SHIO_CURRENT_QUESTION_ANCHOR,
            current_question_anchor,
        )
        record_pipeline_stage(
            event,
            "current_anchor",
            **current_question_anchor.trace_metadata(),
        )

        try:
            memory_result = await self._ensure_memory_policy_result(
                event=event,
                request=req,
                admission=admission,
                conversation_event=admitted_event,
            )
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "memory.policy_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="memory_policy_invalid",
            )
            return
        reader_evidence = event.get_extra(SHIO_MEMORY_READER_EVIDENCE, {})
        if not isinstance(reader_evidence, dict):
            reader_evidence = {}
        record_pipeline_stage(
            event,
            "memory_policy",
            **memory_result.trace_metadata(),
            **reader_evidence,
        )

        max_messages = max(2, int(self._config("max_context_messages", 16)))
        max_context_chars = max(1000, int(self._config("max_context_chars", 9000)))
        clean_history, history_source = await self._identity_aware_history(
            event,
            req.contexts,
            current_message,
            sender_id,
            str(identity_scope.get("group_id", "")),
            max_messages,
            max_context_chars,
        )
        scope_key = str(
            identity_scope.get("scope_key", "")
            or self.runtime.group_scope(
                str(identity_scope.get("platform_id", "")),
                str(identity_scope.get("bot_id", "")),
                str(identity_scope.get("group_id", "")),
            )
        )
        context_metadata = self._build_typed_context(
            event=event,
            envelope=turn_envelope,
            principal=principal,
            reply_target=reply_target,
            reference_context=reference_context,
            current_message=current_message,
            scope_key=scope_key,
            identity_scope=identity_scope,
        )
        assembled_context = event.get_extra(SHIO_ASSEMBLED_CONTEXT_V2, None)
        prior_group_join_records = (
            tuple(
                record
                for record in (
                    *assembled_context.replyer_thread,
                    *assembled_context.public_background,
                )
                if not (
                    record.role is LedgerRole.USER
                    and record.message_id
                    and record.message_id == reply_target.message_id
                )
            )
            if isinstance(assembled_context, AssembledContext)
            else ()
        )
        if conversation_mode == "group_join" and (
            not isinstance(assembled_context, AssembledContext)
            or not has_verified_public_group_context(prior_group_join_records)
        ):
            structured_log(
                logger,
                "info",
                "participation.context_unavailable",
                trace_id=get_trace_id(event),
                conversation_mode=conversation_mode,
                verified_public_context_count=0,
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="participation_verified_context_unavailable",
            )
            return
        replyer_history = (
            assembled_context.replyer_thread
            if isinstance(assembled_context, AssembledContext)
            else ()
        )
        recent_replies = [
            record.content
            for record in replyer_history
            if record.role is LedgerRole.ASSISTANT and record.content
        ][-6:]

        turn_gate = self._typed_turn_gate(
            envelope=turn_envelope,
            principal=principal,
        )
        persona_package = self._configured_persona_package()
        if not turn_gate.activation_ready:
            self._block_typed_turn(
                event,
                req,
                reason_code=turn_gate.reason_codes[0]
                if turn_gate.reason_codes
                else "typed_identity_denied",
            )
            return
        if persona_package is None:
            self._block_typed_turn(
                event,
                req,
                reason_code="persona_package_unavailable",
            )
            return

        active_capability_policy = (
            build_owner_capability_policy(
                principal,
                conversation_mode=conversation_mode,
            )
            if principal.is_owner
            else build_guest_capability_policy(
                principal,
                configured_tool_names=self._guest_allowed_tool_names(),
                conversation_mode=conversation_mode,
            )
        )
        opportunity_attention = event.get_extra(
            SHIO_OPPORTUNITY_ATTENTION,
            None,
        )
        participation_assessment = event.get_extra(
            SHIO_PARTICIPATION_ASSESSMENT,
            None,
        )
        participation_cadence = event.get_extra(
            SHIO_PARTICIPATION_CADENCE,
            None,
        )
        participation_reaction = event.get_extra(
            SHIO_PARTICIPATION_REACTION,
            None,
        )
        try:
            if not isinstance(opportunity_attention, OpportunityAttentionDecision):
                raise RuntimeError("opportunity_attention_unavailable")
            if type(participation_assessment) is not ParticipationAssessment:
                raise RuntimeError("participation_assessment_unavailable")
            self.participation_authority.inspect(participation_assessment)
            if type(participation_cadence) is not ParticipationCadenceDecision:
                raise RuntimeError("participation_cadence_unavailable")
            self.participation_cadence_authority.inspect(participation_cadence)
            if type(participation_reaction) is not ParticipationReactionDecision:
                raise RuntimeError("participation_reaction_unavailable")
            self.participation_reaction_authority.inspect(participation_reaction)
            if (
                participation_assessment.opportunity is not opportunity_attention
                or participation_assessment.binding is not admission.decision.binding
                or participation_cadence.base is not participation_assessment
                or participation_cadence.binding is not admission.decision.binding
                or participation_reaction.base is not participation_cadence
                or participation_reaction.binding is not admission.decision.binding
                or participation_assessment.persona_package_id
                != persona_package.package_id
            ):
                raise RuntimeError("participation_assessment_binding_mismatch")
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "attention.inspect_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="opportunity_attention_unbound",
            )
            return
        attention = participation_assessment.attention
        participation = participation_reaction.decision
        try:
            content_seed = build_content_intent_seed(
                anchor=current_question_anchor,
                reply_target=reply_target,
                memory_result=memory_result,
                media_context=media_adaptation.context,
            )
            knowledge_gap = (
                KnowledgeGapDecision(
                    binding=content_seed.binding,
                    need=KnowledgeNeed.NONE,
                    requires_evidence=False,
                    requested_capability=None,
                    max_tool_calls=0,
                    reason_codes=("opportunity_join_zero_tool",),
                )
                if participation.level
                in {ParticipationLevel.MAY_JOIN, ParticipationLevel.REACT_ONLY}
                else decide_knowledge_gap(
                    content_seed=content_seed,
                    current_message=current_message,
                )
            )
            owner_action_ticket = event.get_extra(
                SHIO_OWNER_ACTION_TICKET,
                None,
            )
            owner_action_route = event.get_extra(
                SHIO_OWNER_ACTION_ROUTE,
                None,
            )
            owner_action_matched = bool(
                isinstance(owner_action_route, OwnerActionRouteDecision)
                and owner_action_route.status is OwnerActionRouteStatus.MATCHED
            )
            planned_action = plan_action(
                ingress=admission.decision,
                address=address_decision,
                attention=attention,
                participation=participation,
                reply_target=reply_target,
                knowledge_gap=knowledge_gap,
                capability_policy=active_capability_policy,
                owner_action_router=(
                    self.owner_action_router if owner_action_matched else None
                ),
                owner_action_ticket=(
                    owner_action_ticket if owner_action_matched else None
                ),
                owner_action_route=(
                    owner_action_route if owner_action_matched else None
                ),
                planned_action_authority=self.planned_action_authority,
                now=time.monotonic(),
            )
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "typed_action.prepare_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            self._block_typed_turn(
                event,
                req,
                reason_code="typed_action_invalid",
            )
            return

        runtime_decision = self._typed_runtime_decision(
            event,
            envelope=turn_envelope,
            principal=principal,
            turn_ready=True,
            planned_action=planned_action,
            refresh=True,
            turn_status=planned_action.kind.value,
            turn_reason_count=len(planned_action.planner_reason_codes),
        )
        capability_metadata = event.get_extra(SHIO_CAPABILITY_POLICY, {})
        if not isinstance(capability_metadata, dict):
            capability_metadata = {}
        record_pipeline_stage(
            event,
            "context",
            planner_message_count=len(clean_history),
            replyer_message_count=len(replyer_history),
            history_source=history_source,
            memory_subject_model="typed_provenance",
            **context_metadata,
            **capability_metadata,
        )
        record_pipeline_stage(
            event,
            "attention",
            **opportunity_attention.trace_metadata(),
            **attention.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "participation",
            **participation_assessment.trace_metadata(),
            **{
                key: value
                for key, value in participation_cadence.trace_metadata().items()
                if key != "schema_version"
            },
            **{
                key: value
                for key, value in participation_reaction.trace_metadata().items()
                if key != "schema_version"
            },
        )
        record_pipeline_stage(
            event,
            "knowledge_gap",
            **knowledge_gap.trace_metadata(),
        )
        event.set_extra(SHIO_PLANNED_ACTION, planned_action)
        event.set_extra(SHIO_CONTENT_INTENT, content_seed.intent)

        if planned_action.kind is ActionKind.EXECUTE_ACTION:
            try:
                content_seed = self._prepare_disabled_owner_action_outcome(
                    event=event,
                    planned_action=planned_action,
                    content_seed=content_seed,
                )
                event.set_extra(SHIO_CONTENT_INTENT, content_seed.intent)
            except Exception as exc:
                structured_log(
                    logger,
                    "error",
                    "owner_action.denial_prepare_failed",
                    trace_id=get_trace_id(event),
                    failure_kind=safe_exception_kind(exc),
                )
                self._block_typed_turn(
                    event,
                    req,
                    reason_code="owner_action_denial_unavailable",
                )
                return

        if planned_action.kind is ActionKind.REACT:
            reaction_target = planned_action.action.reply_target
            if reaction_target is None:
                self._block_typed_turn(
                    event,
                    req,
                    reason_code="reaction_target_unavailable",
                )
                return
            expression_intent = self.expression_intent_authority.issue(
                planned_action_authority=self.planned_action_authority,
                planned_action=planned_action,
                modality=ExpressionModality.REACTION,
                social_act=planned_action.action.expression_intent,
                emotion_tags=(),
                current_message=current_message,
                relationship_distance=trusted_relationship_distance(
                    principal,
                    conversation_mode,
                ),
                max_bubbles=0,
                reason_codes=("participation_react_only",),
            )
            event.set_extra(SHIO_EXPRESSION_INTENT, expression_intent)
            await self._execute_react_presentation(
                event=event,
                planned_action=planned_action,
                expression_intent=expression_intent,
            )

        if planned_action.kind in {
            ActionKind.NO_ACTION,
            ActionKind.WAIT,
            ActionKind.REACT,
        }:
            req.system_prompt = ""
            req.contexts = []
            req.prompt = ""
            req.extra_user_content_parts = []
            req.image_urls = []
            req.audio_urls = []
            req.func_tool = ToolSet([])
            req.tool_calls_result = None
            event.set_extra(SHIO_TYPED_PIPELINE_ACTIVE, True)
            event.set_extra(
                SHIO_PAYLOAD,
                {
                    "typed_pipeline_active": True,
                    "action_kind": planned_action.kind.value,
                    "scope_key": scope_key,
                    "identity_key": principal.sender_key,
                },
            )
            event.set_extra(SHIO_ACTIVE, False)
            record_pipeline_stage(
                event,
                "action",
                **planned_action.trace_metadata(),
                terminal_without_text=True,
            )
            stop = getattr(event, "stop_event", None)
            if callable(stop):
                stop()
            return

        evidence_outcome: EvidenceOutcome | None = None
        if planned_action.kind is ActionKind.USE_TOOL:
            acquisition_clock = time.monotonic()
            runtime_tools = self._available_tools(req.func_tool)
            configured_names = (
                tuple(
                    str(getattr(tool, "name", "") or "").strip()
                    for tool in runtime_tools
                    if str(getattr(tool, "name", "") or "").strip()
                )
                if principal.is_owner
                else tuple(self._guest_allowed_tool_names())
            )
            url_match = re.search(
                r"https?://[^\s<>'\"，。；;]+",
                current_message,
                re.IGNORECASE,
            )
            try:
                request_shape = (
                    build_extract_request_shape(
                        admitted_event.binding,
                        url=url_match.group(0),
                    )
                    if url_match is not None
                    else build_search_request_shape(
                        admitted_event.binding,
                        query=current_message,
                    )
                )
                acquisition_request = broker_tool_request(
                    planned_action=planned_action,
                    knowledge_gap=knowledge_gap,
                    capability_policy=active_capability_policy,
                    runtime_tools=tuple(classify_tool(tool) for tool in runtime_tools),
                    configured_tool_names=configured_names,
                    request_shape=request_shape,
                    now=acquisition_clock,
                )
                event.set_extra(
                    SHIO_ACQUISITION_REQUEST,
                    acquisition_request,
                )
                sealed_execution = await self.scope_concurrency.run_event(
                    generation_snapshot,
                    principal,
                    kind=ScopeWorkKind.DIRECT,
                    work_factory=lambda: execute_sealed_acquisition(
                        request=acquisition_request,
                        runtime_tools=tuple(runtime_tools),
                        plugin_context=self.context,
                        event=event,
                        clock=time.monotonic,
                        epoch_current=(
                            lambda binding: (
                                binding == admitted_event.binding
                                and event_epoch_validation(
                                    event,
                                    self.generation_epochs,
                                ).is_current
                            )
                        ),
                    ),
                )
                record_pipeline_stage(
                    event,
                    "sealed_acquisition",
                    **sealed_execution.trace_metadata(),
                )
                observed_at = time.monotonic()
                typed_results = (
                    adapt_tool_call_results(
                        sealed_execution.batch,
                        tools=tuple(runtime_tools),
                        scope_key=admitted_event.binding.scope_key,
                        target_sender_key=(
                            admitted_event.binding.current_sender_key
                        ),
                        acquisition_request=acquisition_request,
                        observed_at=observed_at,
                    )
                    if (
                        sealed_execution.succeeded
                        and sealed_execution.batch is not None
                        and observed_at <= acquisition_request.selection.deadline
                        and event_epoch_validation(
                            event,
                            self.generation_epochs,
                        ).is_current
                    )
                    else ()
                )
                record_pipeline_stage(
                    event,
                    "tool_result",
                    **tool_result_trace_metadata(typed_results),
                )
                evidence_outcome = adapt_grounding_evidence(
                    request=acquisition_request,
                    results=typed_results,
                    current_binding=admitted_event.binding,
                    now=observed_at,
                )
            except Exception as exc:
                structured_log(
                    logger,
                    "warning",
                    "acquisition.prepare_failed",
                    trace_id=get_trace_id(event),
                    failure_kind=safe_exception_kind(exc),
                )
                evidence_outcome = EvidenceOutcome(
                    binding=admitted_event.binding,
                    action_id=planned_action.action_id,
                    kind=EvidenceOutcomeKind.INVALID_RESULT,
                    facts=(),
                    result_count=0,
                    reason_codes=("acquisition_unavailable",),
                )
            event.set_extra(SHIO_EVIDENCE_OUTCOME, evidence_outcome)
            if evidence_outcome.kind is EvidenceOutcomeKind.ACCEPTED:
                content_seed = attach_grounding_facts(
                    content_seed,
                    evidence_outcome.facts,
                )
                event.set_extra(SHIO_CONTENT_INTENT, content_seed.intent)
            record_pipeline_stage(
                event,
                "evidence",
                **evidence_outcome.trace_metadata(),
            )

        if (
            runtime_decision.activate_typed_pipeline
            and self._activate_planned_reply_request(
                event=event,
                req=req,
                envelope=turn_envelope,
                principal=principal,
                identity_scope=identity_scope,
                current_message=current_message,
                sender_id=sender_id,
                sender_name=sender_name,
                scope_key=scope_key,
                planned_action=planned_action,
                content_seed=content_seed,
                capability_policy=active_capability_policy,
                persona_package=persona_package,
                recent_replies=recent_replies,
                evidence_outcome=evidence_outcome,
            )
        ):
            try:
                permit = await self.inference_budget.acquire_event(
                    generation_snapshot,
                    principal,
                    planned_action,
                    purpose=InferencePurpose.PRIMARY,
                )
            except InferenceBudgetError as exc:
                self._set_inactive(event)
                self._block_typed_turn(
                    event,
                    req,
                    reason_code="inference_budget_unavailable",
                )
                structured_log(
                    logger,
                    "warning",
                    "typed_reply.inference_budget_rejected",
                    trace_id=get_trace_id(event),
                    failure_kind=safe_exception_kind(exc),
                )
                return
            event.set_extra(SHIO_INFERENCE_PERMIT, permit)
            trace_context = get_trace_context(event)
            if trace_context is not None:
                self.performance_window.observe_latency(
                    LatencyKind.LOCAL_ORCHESTRATION,
                    float((time.perf_counter() - trace_context.started_at) * 1000.0),
                )
            self._observe_inference_permit(permit)
            self.performance_window.record_model_call(ModelCallKind.PRIMARY)
            event.set_extra(
                SHIO_PRIMARY_PROVIDER_STARTED_AT,
                float(time.perf_counter()),
            )
            record_pipeline_stage(
                event,
                "inference_budget",
                **permit.trace_metadata(),
            )
            return

        self._block_typed_turn(
            event,
            req,
            reason_code="typed_prepare_failed",
        )
        return

    async def _guard_typed_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
        payload: dict[str, Any],
    ) -> None:
        """Validate one typed Composer result and allow at most one repair call."""

        self.semantic_guard_controller.discard(
            event.get_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        )
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        event.set_extra(SHIO_PRESENTATION_HANDOFF, None)
        composer_request = event.get_extra(SHIO_REPLY_COMPOSER_REQUEST, None)
        validation_context = event.get_extra(SHIO_OUTPUT_VALIDATION_CONTEXT, None)
        semantic_contract = event.get_extra(SHIO_SEMANTIC_GUARD_CONTRACT, None)
        planned_action = event.get_extra(SHIO_PLANNED_ACTION, None)
        content_intent = event.get_extra(SHIO_CONTENT_INTENT, None)
        expression_intent = event.get_extra(SHIO_EXPRESSION_INTENT, None)
        affect_appraisal = event.get_extra(SHIO_AFFECT_APPRAISAL, None)
        persona_expression = event.get_extra(SHIO_PERSONA_EXPRESSION, None)
        capability_policy = event.get_extra(SHIO_ACTIVE_CAPABILITY_POLICY, None)
        media_adaptation = event.get_extra(SHIO_MEDIA_ADAPTATION, None)
        if (
            not isinstance(composer_request, ReplyComposerRequest)
            or not isinstance(validation_context, OutputValidationContext)
            or not isinstance(semantic_contract, SemanticGuardContract)
            or not isinstance(planned_action, PlannedAction)
            or not isinstance(content_intent, ContentIntent)
            or not isinstance(expression_intent, ExpressionIntent)
            or not isinstance(affect_appraisal, AffectAppraisal)
            or not isinstance(persona_expression, PersonaExpressionPlan)
            or not isinstance(media_adaptation, AstrBotMediaAdaptation)
            or capability_policy is None
            or content_intent.binding != planned_action.binding
            or expression_intent.binding != planned_action.binding
            or composer_request.action_id != planned_action.action_id
            or validation_context.semantic_contract is not semantic_contract
            or semantic_contract.planned_action is not planned_action
            or semantic_contract.content_intent is not content_intent
            or semantic_contract.current_question_anchor
            is not composer_request.current_question_anchor
            or semantic_contract.media_context is not media_adaptation.context
            or semantic_contract.evidence_outcome
            is not event.get_extra(SHIO_EVIDENCE_OUTCOME, None)
        ):
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            structured_log(
                logger,
                "error",
                "typed_reply.state_missing",
                trace_id=get_trace_id(event),
            )
            return

        initial_raw_output = str(response.completion_text or "")
        raw_output = initial_raw_output
        record_pipeline_stage(
            event,
            "raw_reply",
            content_fingerprint=content_fingerprint(raw_output),
            content_chars=len(raw_output),
        )
        result = parse_reply_composer_output(composer_request, raw_output)
        report = validate_reply_composer_output(
            request=composer_request,
            result=result,
            raw_output=raw_output,
            context=validation_context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        attempts = max(0, int(event.get_extra(SHIO_REPAIR_ATTEMPTS, 0) or 0))
        decision = decide_output_repair(
            report,
            repair_attempts_used=attempts,
        )
        record_pipeline_stage(
            event,
            "guard",
            guard_hit=not report.is_valid,
            guard_hit_count=len(report.issues),
            severe_guard_hit=not report.is_valid,
            repair_attempted=decision.action is RepairAction.GENERATE_ONCE,
            called_tool_count=(
                1 if event.get_extra(SHIO_ACQUISITION_REQUEST, None) is not None else 0
            ),
            typed_tool_result_count=len(content_intent.grounding_facts),
            validator="output_validator_v2",
            **report.trace_metadata(),
        )

        if decision.action is RepairAction.BLOCK:
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            structured_log(
                logger,
                "warning",
                "typed_reply.blocked",
                trace_id=get_trace_id(event),
                issue_count=len(report.issues),
                repair_attempt_count=attempts,
            )
            return

        if decision.action is RepairAction.GENERATE_ONCE:
            repair_call_count = 0
            try:
                repair_request = build_single_repair_request(
                    original_request=composer_request,
                    rejected_visible_text=result.visible_text,
                    report=report,
                    repair_attempts_used=attempts,
                    semantic_contract=semantic_contract,
                )
                event.set_extra(SHIO_REPAIR_ATTEMPTS, attempts + 1)
                owner_action_fallback = (
                    self._deterministic_owner_action_fallback(composer_request)
                )
                direct_reply_fallback = build_safe_direct_reply_fallback(
                    composer_request
                )
                used_fallback = owner_action_fallback is not None
                repair_failure_kind = ""
                if owner_action_fallback is not None:
                    raw_output = owner_action_fallback
                else:
                    try:
                        provider = self._provider(
                            str(self._config("replyer_provider_id", "")),
                            event.unified_msg_origin,
                        )
                        snapshot = event_generation_snapshot(event)
                        if provider is None or snapshot is None:
                            raise RuntimeError(
                                "typed repair provider or generation snapshot unavailable"
                            )
                        repair_media = media_adaptation.for_repair()
                        expected_media_ids = tuple(
                            item.item_id
                            for item in semantic_contract.media_context.items
                        )
                        if (
                            repair_request.semantic_contract is not semantic_contract
                            or repair_request.media_item_ids != expected_media_ids
                            or repair_media is not media_adaptation
                            or repair_media.context
                            is not semantic_contract.media_context
                        ):
                            raise RuntimeError(
                                "typed repair semantic/media contract mismatch"
                            )
                        repair_call_count = 1
                        repaired = await self.scope_concurrency.run_event(
                            snapshot,
                            self._effective_principal(event),
                            kind=(
                                ScopeWorkKind.ACTION
                                if planned_action.kind is ActionKind.EXECUTE_ACTION
                                else ScopeWorkKind.DIRECT
                            ),
                            work_factory=lambda: self.inference_budget.run_event_call(
                                snapshot,
                                self._effective_principal(event),
                                planned_action,
                                purpose=InferencePurpose.REPAIR,
                                work_factory=lambda: self._run_observed_provider_call(
                                    call_kind=ModelCallKind.REPAIR,
                                    latency_kind=LatencyKind.REPAIR_PROVIDER,
                                    work_factory=lambda: self.generation_tasks.run(
                                        snapshot,
                                        provider.text_chat(
                                            prompt=repair_request.user_prompt,
                                            contexts=[],
                                            system_prompt=repair_request.system_prompt,
                                            image_urls=list(
                                                repair_media.transport.image_urls
                                            ),
                                            audio_urls=list(
                                                repair_media.transport.audio_urls
                                            ),
                                            func_tool=None,
                                            request_max_retries=1,
                                        ),
                                        cancel_safe=provider_supports_cancellation(
                                            provider
                                        ),
                                    ),
                                ),
                                permit_observer=self._observe_inference_permit,
                            ),
                        )
                        raw_output = str(repaired.completion_text or "")
                    except SupersededGeneration:
                        raise
                    except Exception as exc:
                        repair_failure_kind = safe_exception_kind(exc)
                        raw_output = ""

                    candidate_result = parse_reply_composer_output(
                        composer_request,
                        raw_output,
                    )
                    candidate_is_valid = _preflight_reply_composer_repair_candidate(
                        request=composer_request,
                        result=candidate_result,
                        raw_output=raw_output,
                        context=validation_context,
                    )
                    if not candidate_is_valid and direct_reply_fallback is not None:
                        raw_output = direct_reply_fallback
                        used_fallback = True
                result = parse_reply_composer_output(composer_request, raw_output)
                report = validate_reply_composer_output(
                    request=composer_request,
                    result=result,
                    raw_output=raw_output,
                    context=validation_context,
                    semantic_phase=SemanticGuardPhase.REPAIR,
                    repair_request=repair_request,
                )
                record_pipeline_stage(
                    event,
                    "repair",
                    repair_call_count=repair_call_count,
                    repair_failure_count=0 if report.is_valid else 1,
                    outcome=(
                        "fallback_succeeded"
                        if used_fallback and report.is_valid
                        else "fallback_rejected"
                        if used_fallback
                        else "succeeded"
                        if report.is_valid
                        else "rejected"
                    ),
                    **(
                        {"failure_kind": repair_failure_kind}
                        if repair_failure_kind
                        else {}
                    ),
                    **report.trace_metadata(),
                )
            except SupersededGeneration:
                response.role = "assistant"
                response.completion_text = ""
                event.set_extra("meme_manager_semantic_selected_ids", [])
                record_pipeline_stage(
                    event,
                    "stale_generation_drop",
                    reason_code="typed_repair_superseded",
                )
                return
            except Exception as exc:
                record_pipeline_stage(
                    event,
                    "repair",
                    repair_call_count=repair_call_count,
                    repair_failure_count=1,
                    outcome="failed",
                    failure_kind=safe_exception_kind(exc),
                )

            post_decision = decide_output_repair(
                report,
                repair_attempts_used=max(
                    1,
                    int(event.get_extra(SHIO_REPAIR_ATTEMPTS, 0) or 0),
                ),
            )
            if post_decision.action is not RepairAction.SEND:
                response.role = "assistant"
                response.completion_text = ""
                event.set_extra("meme_manager_semantic_selected_ids", [])
                structured_log(
                    logger,
                    "warning",
                    "typed_reply.repair_rejected",
                    trace_id=get_trace_id(event),
                    issue_count=len(report.issues),
                    primary_issue_code=(
                        report.issue_codes[0]
                        if report.issue_codes
                        else "repair_output_invalid"
                    ),
                    repair_attempt_count=1,
                )
                return

        final_text = result.visible_text
        presentation = build_presentation_handoff(
            composer_request=composer_request,
            semantic_contract=semantic_contract,
            action_outcome=semantic_contract.action_outcome,
            action_outcome_authority=(
                semantic_contract.action_outcome_authority
            ),
            planned_action=planned_action,
            expression_intent=expression_intent,
            affect_appraisal=affect_appraisal,
            result=result,
            validation=report,
            capability_policy=capability_policy,
        )
        semantic_report = report.semantic_guard_report
        if (
            not isinstance(presentation, PresentationHandoff)
            or not presentation.eligible
            or semantic_report is None
            or not semantic_report.is_valid
        ):
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            return
        try:
            semantic_seal = self.semantic_guard_controller.issue(
                contract=semantic_contract,
                phase=semantic_report.phase,
                visible_text=presentation.final_visible_text,
                presentation=presentation,
                presentation_digest=presentation.final_text_digest,
            )
        except ValueError:
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            return
        event.set_extra(SHIO_PRESENTATION_HANDOFF, presentation)
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, semantic_seal)
        response.role = "assistant"
        response.completion_text = final_text
        record_pipeline_stage(
            event,
            "presentation_handoff",
            **presentation.trace_metadata(),
        )
        record_pipeline_stage(
            event,
            "final_reply",
            content_fingerprint=content_fingerprint(final_text),
            content_chars=len(final_text),
            bubble_count=len(result.bubbles),
            changed_from_raw=final_text != initial_raw_output,
            final_generation_count=1
            + int(event.get_extra(SHIO_REPAIR_ATTEMPTS, 0) or 0),
            **report.trace_metadata(),
        )

    @filter.on_llm_response(priority=100)
    async def guard_persona_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """只接受绑定到当前 typed 请求的 Composer 输出。"""
        primary_started = event.get_extra(
            SHIO_PRIMARY_PROVIDER_STARTED_AT,
            None,
        )
        event.set_extra(SHIO_PRIMARY_PROVIDER_STARTED_AT, None)
        if type(primary_started) is float:
            self.performance_window.observe_latency(
                LatencyKind.PRIMARY_PROVIDER,
                float((time.perf_counter() - primary_started) * 1000.0),
            )
            if response.role == "err":
                self.performance_window.record_model_failure()
        permit = event.get_extra(SHIO_INFERENCE_PERMIT, None)
        if type(permit) is InferencePermit:
            event.set_extra(SHIO_INFERENCE_PERMIT, None)
            try:
                released = await self.inference_budget.release(permit)
                if not released:
                    raise InferenceBudgetError("inference_active_timeout")
            except InferenceBudgetError as exc:
                response.role = "assistant"
                response.completion_text = ""
                event.set_extra("meme_manager_semantic_selected_ids", [])
                structured_log(
                    logger,
                    "error",
                    "typed_reply.inference_permit_rejected",
                    trace_id=get_trace_id(event),
                    failure_kind=safe_exception_kind(exc),
                )
                return
        elif event.get_extra(SHIO_ACTIVE, False):
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            structured_log(
                logger,
                "error",
                "typed_reply.inference_permit_missing",
                trace_id=get_trace_id(event),
            )
            return
        if not event.get_extra(SHIO_ACTIVE, False):
            return

        payload = event.get_extra(SHIO_PAYLOAD, None)
        epoch_validation = event_epoch_validation(event, self.generation_epochs)
        if not epoch_validation.is_current:
            self._discard_semantic_validation_seal(event)
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(
                event,
                "stale_generation_drop",
                reason_code=epoch_validation.reason_code,
                current_epoch=epoch_validation.current_epoch,
            )
            self._emit_pipeline_metrics(event)
            return

        if response.role == "err":
            self._discard_semantic_validation_seal(event)
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(
                event,
                "replyer_failed",
                failure_kind="ProviderError",
                replyer_failure_count=1,
            )
            structured_log(
                logger,
                "warning",
                "typed_reply.provider_failed",
                trace_id=get_trace_id(event),
            )
            self._emit_pipeline_metrics(event)
            return

        if (
            not isinstance(payload, dict)
            or not bool(payload.get("typed_pipeline_active", False))
            or not bool(event.get_extra(SHIO_TYPED_PIPELINE_ACTIVE, False))
        ):
            self._discard_semantic_validation_seal(event)
            response.role = "assistant"
            response.completion_text = ""
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(
                event,
                "guard",
                guard_hit=True,
                guard_hit_count=1,
                severe_guard_hit=True,
                repair_attempted=False,
                validator="typed_state_binding",
            )
            structured_log(
                logger,
                "error",
                "typed_reply.non_typed_blocked",
                trace_id=get_trace_id(event),
            )
            self._emit_pipeline_metrics(event)
            return

        await self._guard_typed_reply(event, response, payload)

    @filter.on_decorating_result(priority=-100)
    async def dispatch_chat_bubbles(self, event: AstrMessageEvent) -> None:
        """闲聊按自然句逐条发送；内容型回答保持完整排版。"""
        if not event.get_extra(SHIO_ACTIVE, False):
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            self._discard_semantic_validation_seal(event)
            return
        epoch_validation = event_epoch_validation(event, self.generation_epochs)
        if not epoch_validation.is_current:
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(
                event,
                "stale_generation_drop",
                reason_code=epoch_validation.reason_code,
                current_epoch=epoch_validation.current_epoch,
            )
            self._emit_pipeline_metrics(event)
            logger.info(
                "[星汐/并发守卫] reason=%s 发送前发现 generation 过时，已清空结果。",
                epoch_validation.reason_code,
            )
            return
        try:
            if not result.is_llm_result():
                self._discard_semantic_validation_seal(event)
                return
        except Exception:
            self._discard_semantic_validation_seal(event)
            return

        text_components = self._collect_text_components(result.chain)
        if not text_components:
            self._discard_semantic_validation_seal(event)
            return
        payload = event.get_extra(SHIO_PAYLOAD, {})
        active_tool_names = self._response_guard_tool_names(event)
        final_meme_references: list[str] = []
        removed_visible_tool_artifact = False
        aggregate_source = "\n".join(component.text for component in text_components)
        aggregate_had_protocol = contains_tool_protocol(
            aggregate_source,
            active_tool_names,
        )
        aggregate_cleaned, aggregate_references = (
            extract_and_clean_internal_meme_references(
                aggregate_source,
                active_tool_names,
            )
        )
        if (
            aggregate_had_protocol
            and aggregate_cleaned != aggregate_source.strip()
            and not contains_tool_protocol(aggregate_cleaned, active_tool_names)
        ):
            text_components[0].text = aggregate_cleaned
            for component in text_components[1:]:
                component.text = ""
            removed_visible_tool_artifact = True
            final_meme_references.extend(aggregate_references)
        else:
            for component in text_components:
                original_component_text = component.text
                cleaned_text, references = extract_and_clean_internal_meme_references(
                    original_component_text,
                    active_tool_names,
                )
                component.text = cleaned_text
                if (
                    cleaned_text != original_component_text.strip()
                    and contains_tool_protocol(
                        original_component_text,
                        active_tool_names,
                    )
                    and not contains_tool_protocol(cleaned_text, active_tool_names)
                ):
                    removed_visible_tool_artifact = True
                for reference in references:
                    if reference not in final_meme_references:
                        final_meme_references.append(reference)
        if removed_visible_tool_artifact:
            logger.warning(
                "[星汐/表达守卫] 发送前已从消息节点移除伪造的工具文本调用。"
            )
        if final_meme_references:
            structured_log(
                logger,
                "warning",
                "presentation.reference_blocked_before_send",
                trace_id=get_trace_id(event),
                reference_count=len(final_meme_references),
            )
        visible_text = "\n".join(
            comp.text for comp in text_components if str(comp.text or "").strip()
        ).strip()
        if removed_visible_tool_artifact and not visible_text:
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(event, "final_send_blocked", reason_code="protocol_only_output")
            self._emit_pipeline_metrics(event)
            return
        if contains_tool_protocol(visible_text, active_tool_names):
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/协议守卫] 发送前再次发现内部工具协议，已阻断。"
            )
            record_pipeline_stage(event, "final_send_blocked", reason_code="tool_protocol")
            self._emit_pipeline_metrics(event)
            return
        if contains_internal_reasoning(visible_text):
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/规划守卫] 发送前再次发现内部规划或推理，已阻断。"
            )
            record_pipeline_stage(event, "final_send_blocked", reason_code="internal_reasoning")
            self._emit_pipeline_metrics(event)
            return
        if (
            isinstance(payload, dict)
            and not bool(payload.get("is_owner", False))
            and contains_nonowner_identity_confusion(visible_text)
        ):
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            logger.warning(
                "[星汐/身份守卫] 发送前再次发现普通群友被归因为主人，已阻断。"
            )
            record_pipeline_stage(event, "final_send_blocked", reason_code="identity_confusion")
            self._emit_pipeline_metrics(event)
            return

        semantic_contract = event.get_extra(SHIO_SEMANTIC_GUARD_CONTRACT, None)
        semantic_seal = event.get_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        validation_context = event.get_extra(SHIO_OUTPUT_VALIDATION_CONTEXT, None)
        composer_request = event.get_extra(SHIO_REPLY_COMPOSER_REQUEST, None)
        planned_action = event.get_extra(SHIO_PLANNED_ACTION, None)
        content_intent = event.get_extra(SHIO_CONTENT_INTENT, None)
        media_adaptation = event.get_extra(SHIO_MEDIA_ADAPTATION, None)
        evidence_outcome = event.get_extra(SHIO_EVIDENCE_OUTCOME, None)
        presentation = event.get_extra(SHIO_PRESENTATION_HANDOFF, None)
        stage_state_valid = bool(
            isinstance(semantic_contract, SemanticGuardContract)
            and isinstance(semantic_seal, SemanticValidationSeal)
            and isinstance(validation_context, OutputValidationContext)
            and isinstance(composer_request, ReplyComposerRequest)
            and isinstance(planned_action, PlannedAction)
            and isinstance(content_intent, ContentIntent)
            and isinstance(media_adaptation, AstrBotMediaAdaptation)
            and isinstance(presentation, PresentationHandoff)
            and presentation.eligible
            and validation_context.composer_request is composer_request
            and validation_context.semantic_contract is semantic_contract
            and validation_context.current_question_anchor
            is semantic_contract.current_question_anchor
            and composer_request.current_question_anchor
            is semantic_contract.current_question_anchor
            and composer_request.planned_action is planned_action
            and semantic_contract.composer_request is composer_request
            and semantic_contract.planned_action is planned_action
            and semantic_contract.content_intent is content_intent
            and semantic_contract.media_context is media_adaptation.context
            and semantic_contract.evidence_outcome is evidence_outcome
            and semantic_contract.action_outcome
            is composer_request.action_outcome
            and semantic_contract.action_outcome_authority
            is composer_request.action_outcome_authority
            and presentation.composer_request is composer_request
            and presentation.semantic_contract is semantic_contract
            and presentation.action_outcome
            is semantic_contract.action_outcome
            and presentation.action_outcome_authority
            is semantic_contract.action_outcome_authority
            and presentation.target_message_id
            == semantic_contract.content_intent.binding.current_message_id
            and bool(presentation.final_text_digest)
        )
        if not stage_state_valid:
            self._discard_semantic_validation_seal(event)
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            record_pipeline_stage(
                event,
                "final_send_blocked",
                reason_code="semantic_stage_seal_missing_or_mismatched",
            )
            self._emit_pipeline_metrics(event)
            return
        # PresentationHandoff is the sole canonical segment boundary authority.
        # Re-splitting guarded bytes here would create a second presentation
        # path and could move a negation or causal correction to a later bubble.
        final_segments = presentation.final_segments
        final_semantic_report = validate_semantic_media_guard(
            contract=semantic_contract,
            visible_text=visible_text,
            visible_segments=final_segments,
            phase=SemanticGuardPhase.FINAL_SEND,
        )
        validated_digest = presentation.final_text_digest
        seal_consumed = self.semantic_guard_controller.consume_final(
            seal=semantic_seal,
            contract=semantic_contract,
            visible_text=visible_text,
            visible_segments=final_segments,
            presentation=presentation,
            presentation_digest=validated_digest,
        )
        event.set_extra(SHIO_SEMANTIC_VALIDATION_SEAL, None)
        record_pipeline_stage(
            event,
            "final_semantic_guard",
            final_text_changed=bool(
                validated_digest
                and validated_digest != final_semantic_report.visible_digest
            ),
            semantic_stage_seal_consumed=seal_consumed,
            **final_semantic_report.trace_metadata(),
        )
        if not final_semantic_report.is_valid or not seal_consumed:
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            structured_log(
                logger,
                "warning",
                "typed_reply.final_semantic_blocked",
                trace_id=get_trace_id(event),
                issue_count=len(final_semantic_report.issues),
            )
            record_pipeline_stage(
                event,
                "final_send_blocked",
                reason_code=(
                    "semantic_guard_final_send"
                    if not final_semantic_report.is_valid
                    else "semantic_stage_seal_invalid"
                ),
            )
            self._emit_pipeline_metrics(event)
            return

        if not bool(self._config("enable_chat_bubbles", True)):
            if not self._prepare_automatic_send_observation(
                event,
                payload,
                visible_text,
            ):
                result.chain.clear()
            return
        expression_intent = event.get_extra(SHIO_EXPRESSION_INTENT, None)
        if (
            not isinstance(composer_request, ReplyComposerRequest)
            or not isinstance(expression_intent, ExpressionIntent)
            or composer_request.reply_shape != "chat_bubbles"
        ):
            if not self._prepare_automatic_send_observation(
                event,
                payload,
                visible_text,
            ):
                result.chain.clear()
            return
        bubbles = list(final_segments)
        if len(bubbles) <= 1:
            if not self._prepare_automatic_send_observation(
                event,
                payload,
                visible_text,
            ):
                result.chain.clear()
            return

        min_delay = max(0, int(self._config("bubble_interval_min_ms", 450))) / 1000
        max_delay = max(0, int(self._config("bubble_interval_max_ms", 1200))) / 1000
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay

        sent_count = 0
        for bubble in bubbles[:-1]:
            if not event_epoch_validation(event, self.generation_epochs).is_current:
                result.chain.clear()
                event.set_extra("meme_manager_semantic_selected_ids", [])
                return
            planned = self._plan_send_segment(event, payload, bubble)
            if planned is None:
                result.chain.clear()
                record_pipeline_stage(
                    event,
                    "final_send_blocked",
                    reason_code="presentation_segment_untracked",
                )
                self._emit_pipeline_metrics(event)
                return
            self.send_receipts.mark_attempted(planned)
            try:
                record_pipeline_stage(
                    event,
                    "send_attempt",
                    automatic=False,
                    visible_chars=len(bubble),
                )
                send_snapshot = event_generation_snapshot(event)
                if send_snapshot is None:
                    raise RuntimeError("send_generation_snapshot_missing")
                await self.scope_concurrency.run_event(
                    send_snapshot,
                    self._effective_principal(event),
                    kind=(
                        ScopeWorkKind.ACTION
                        if planned_action.kind is ActionKind.EXECUTE_ACTION
                        else ScopeWorkKind.DIRECT
                    ),
                    work_factory=lambda bubble=bubble: event.send(
                        event.plain_result(bubble)
                    ),
                )
            except Exception as exc:
                if planned is not None:
                    record = self.send_receipts.get_reply(
                        planned.rsplit(":", 1)[0]
                    )
                    if (
                        record is not None
                        and any(
                            segment.segment_id == planned
                            and segment.status == SegmentSendStatus.ATTEMPTED
                            for segment in record.segments
                        )
                    ):
                        self.send_receipts.mark_failed(
                            planned,
                            failure_kind=type(exc).__name__,
                        )
                record_pipeline_stage(
                    event,
                    "send_failed",
                    automatic=False,
                    failure_kind=type(exc).__name__,
                )
                structured_log(
                    logger,
                    "warning",
                    "send.manual_failed",
                    trace_id=get_trace_id(event),
                    failure_kind=safe_exception_kind(exc),
                )
                # A failed exact segment is terminal. Falling through to
                # AstrBot's aggregate automatic send would retry the same bytes
                # outside the canonical presentation transaction.
                result.chain.clear()
                self._emit_pipeline_metrics(event)
                return
            sent_count += 1
            if planned is not None:
                self.send_receipts.mark_succeeded(planned)
                self._observe_successful_send_segment(event, bubble)
            record_pipeline_stage(
                event,
                "send_success",
                automatic=False,
                visible_chars=len(bubble),
            )
            if max_delay > 0:
                await asyncio.sleep(random.uniform(min_delay, max_delay))

        if sent_count == 0:
            if not self._prepare_automatic_send_observation(
                event,
                payload,
                visible_text,
            ):
                result.chain.clear()
            return
        remaining = bubbles[sent_count:]
        if not event_epoch_validation(event, self.generation_epochs).is_current:
            result.chain.clear()
            event.set_extra("meme_manager_semantic_selected_ids", [])
            return
        text_components[0].text = "\n".join(remaining)
        for comp in text_components[1:]:
            comp.text = ""
        if not self._prepare_automatic_send_observation(
            event,
            payload,
            text_components[0].text,
        ):
            result.chain.clear()

    def _send_observation_tracker(
        self,
        event: AstrMessageEvent,
    ) -> ReplyObservationTracker | None:
        tracker = event.get_extra(SHIO_SEND_OBSERVATION, None)
        return tracker if isinstance(tracker, ReplyObservationTracker) else None

    def _emit_pipeline_metrics(
        self,
        event: AstrMessageEvent,
    ) -> PipelineMetricsSnapshot:
        snapshot = store_pipeline_metrics(event)
        performance = self.performance_window.snapshot()
        event.set_extra(SHIO_PERFORMANCE_SNAPSHOT, performance)
        if bool(self._config("debug_log", False)):
            structured_log(
                logger,
                "info",
                "pipeline.metrics",
                trace_id=snapshot.trace_id,
                **snapshot.trace_metadata(),
                **performance.trace_metadata(),
                **self.runtime_continuity.trace_metadata(),
            )
        return snapshot

    def _plan_send_segment(
        self,
        event: AstrMessageEvent,
        payload: Any,
        visible_text: str,
    ) -> str | None:
        if (
            not isinstance(payload, dict)
            or payload.get("chat_type") not in {"group", "private"}
        ):
            return None
        text = str(visible_text or "").strip()
        if not text:
            return None
        tracker = self._send_observation_tracker(event)
        if tracker is None:
            envelope = ensure_turn_envelope(event)
            target = ensure_direct_reply_target(
                event,
                envelope,
                str(payload.get("current_message", "")),
            )
            try:
                presentation = event.get_extra(SHIO_PRESENTATION_HANDOFF, None)
                expression_ids = (
                    payload.get("expression_ids", [])
                    if bool(self._config("social_feedback_enabled", True))
                    else []
                )
                if not isinstance(presentation, PresentationHandoff):
                    raise ValueError("presentation_required")
                record = self.send_receipts.begin_presentation_reply(
                    presentation,
                    expression_ids=expression_ids,
                    trace_id=get_trace_id(event),
                )
                if record.segments[0].visible_text != text:
                    raise ValueError("presentation_segment_order_mismatch")
            except Exception as exc:
                if bool(self._config("debug_log", False)):
                    structured_log(
                        logger,
                        "warning",
                        "send.observation_skipped",
                        trace_id=get_trace_id(event),
                        failure_kind=safe_exception_kind(exc),
                    )
                return None
            tracker = ReplyObservationTracker(
                internal_reply_id=record.internal_reply_id,
                target_sender_id=str(payload.get("sender_id", "")),
                expression_ids=tuple(
                    str(value)
                    for value in (
                        payload.get("expression_ids", [])
                        if bool(self._config("social_feedback_enabled", True))
                        else []
                    )
                    if str(value)
                )[:3],
                target_sequence=int(payload.get("target_sequence", 0) or 0),
                feedback_window_seconds=max(
                    60,
                    int(self._config("social_feedback_window_minutes", 10)) * 60,
                ),
                trace_id=get_trace_id(event),
                learning_context=(
                    payload.get("learning_context")
                    if isinstance(payload.get("learning_context"), LearningContext)
                    else None
                ),
            )
            event.set_extra(SHIO_SEND_OBSERVATION, tracker)
            record_pipeline_stage(
                event,
                "bubble_planned",
                segment_index=0,
                visible_chars=len(text),
            )
            return record.segments[0].segment_id
        presentation = event.get_extra(SHIO_PRESENTATION_HANDOFF, None)
        if isinstance(presentation, PresentationHandoff):
            record = self.send_receipts.get_reply(tracker.internal_reply_id)
            if record is None:
                return None
            matches = tuple(
                segment
                for segment in record.segments
                if segment.status is SegmentSendStatus.PLANNED
                and segment.visible_text == text
            )
            if len(matches) != 1:
                return None
            segment = matches[0]
            record_pipeline_stage(
                event,
                "bubble_planned",
                segment_index=segment.segment_index,
                visible_chars=len(text),
            )
            return segment.segment_id
        segment = self.send_receipts.append_segment(
            tracker.internal_reply_id,
            visible_text=text,
        )
        record_pipeline_stage(
            event,
            "bubble_planned",
            segment_index=segment.segment_index,
            visible_chars=len(text),
        )
        return segment.segment_id

    def _prepare_automatic_send_observation(
        self,
        event: AstrMessageEvent,
        payload: Any,
        visible_text: str,
    ) -> bool:
        tracker = self._send_observation_tracker(event)
        if tracker is not None and tracker.pending_automatic_segment_id:
            return True
        segment_id = self._plan_send_segment(event, payload, visible_text)
        if segment_id is None:
            return False
        self.send_receipts.mark_attempted(segment_id)
        record_pipeline_stage(
            event,
            "send_handoff",
            automatic=True,
            visible_chars=len(str(visible_text or "").strip()),
        )
        tracker = self._send_observation_tracker(event)
        if tracker is not None:
            tracker.pending_automatic_segment_id = segment_id
            tracker.pending_automatic_text = str(visible_text or "").strip()
        return tracker is not None

    def _observe_successful_send_segment(
        self,
        event: AstrMessageEvent,
        visible_text: str,
    ) -> None:
        tracker = self._send_observation_tracker(event)
        payload = event.get_extra(SHIO_PAYLOAD, {})
        if tracker is None or not isinstance(payload, dict):
            return
        text = str(visible_text or "").strip()
        if not text:
            return
        tracker.successful_segments.append(text)
        if len(tracker.successful_segments) == 1:
            trace_context = get_trace_context(event)
            if trace_context is not None:
                self.performance_window.observe_latency(
                    LatencyKind.FIRST_BUBBLE,
                    float((time.perf_counter() - trace_context.started_at) * 1000.0),
                )
        sent_record = self.send_receipts.sent_reply_record(tracker.internal_reply_id)
        self.runtime.record_bot_reply(
            scope_key=str(payload.get("scope_key", "")),
            target_sender_id=tracker.target_sender_id,
            reply_text=tracker.successful_visible_text,
            expression_ids=list(tracker.expression_ids),
            target_sequence=tracker.target_sequence,
            feedback_window_seconds=tracker.feedback_window_seconds,
            internal_reply_id=tracker.internal_reply_id,
            sent_platform_message_ids=(
                tuple(
                    segment.platform_message_id
                    for segment in sent_record.successful_segments
                    if segment.platform_message_id
                )
                if sent_record is not None
                else ()
            ),
            learning_context=tracker.learning_context,
            trace_id=tracker.trace_id,
        )
        envelope = ensure_turn_envelope(event)
        ledger_ids = event.get_extra(SHIO_LEDGER_OUTBOUND_IDS, set())
        if not isinstance(ledger_ids, set):
            ledger_ids = set()
        segment_index = len(tracker.successful_segments) - 1
        internal_message_id = f"{tracker.internal_reply_id}:{segment_index}"
        if internal_message_id not in ledger_ids:
            try:
                self.ledger.record_outbound(
                    scope_key=str(payload.get("scope_key", "")),
                    session_id=envelope.session_id,
                    message_id=internal_message_id,
                    bot_sender_key=build_sender_key(
                        str(payload.get("scope_key", "")),
                        str(payload.get("bot_id", "")),
                    ),
                    target_sender_key=str(
                        payload.get("identity_key", "")
                        or build_sender_key(
                            str(payload.get("scope_key", "")),
                            str(payload.get("sender_id", "")),
                        )
                    ),
                    reply_to_message_id=envelope.message_id,
                    timestamp=time.time(),
                    content=text,
                )
                ledger_ids.add(internal_message_id)
                event.set_extra(SHIO_LEDGER_OUTBOUND_IDS, ledger_ids)
            except ValueError as exc:
                if bool(self._config("debug_log", False)):
                    structured_log(
                        logger,
                        "warning",
                        "ledger.outbound_skipped",
                        trace_id=tracker.trace_id,
                        failure_kind=safe_exception_kind(exc),
                    )
        if bool(self._config("debug_log", False)):
            envelope = ensure_turn_envelope(event)
            trace = get_pipeline_trace(event)
            latest = trace.get("stages", [])[-1] if trace.get("stages") else {}
            structured_log(
                logger,
                "info",
                "send.succeeded",
                trace_id=tracker.trace_id,
                source_kind=envelope.source_kind,
                subject_digest=diagnostic_digest(envelope.sender_key),
                target_digest=diagnostic_digest(envelope.message_id),
                segment_count=len(tracker.successful_segments),
                visible_chars=len(text),
                latency_ms=float(latest.get("elapsed_ms", 0.0) or 0.0),
            )

    def _finalize_owner_action_delivery(self, event: AstrMessageEvent) -> None:
        outcome = event.get_extra(SHIO_ACTION_OUTCOME, None)
        if not isinstance(outcome, ActionOutcomeIntent):
            return
        source = event.get_extra(SHIO_OWNER_ACTION_SOURCE, None)
        lifecycle_handle = event.get_extra(
            SHIO_OWNER_ACTION_LIFECYCLE_HANDLE,
            None,
        )
        planned_action = event.get_extra(SHIO_PLANNED_ACTION, None)
        composer_request = event.get_extra(SHIO_REPLY_COMPOSER_REQUEST, None)
        presentation = event.get_extra(SHIO_PRESENTATION_HANDOFF, None)
        tracker = self._send_observation_tracker(event)
        store = self.owner_action_lifecycle_store
        durable_authority = self.owner_action_durable_authority
        if (
            source is None
            or not isinstance(lifecycle_handle, LifecycleHandle)
            or not isinstance(planned_action, PlannedAction)
            or not isinstance(composer_request, ReplyComposerRequest)
            or not isinstance(presentation, PresentationHandoff)
            or tracker is None
            or store is None
            or durable_authority is None
        ):
            raise RuntimeError("owner_action_delivery_lineage_missing")
        evidence = self.send_receipts.issue_owner_action_send_terminal_evidence(
            presentation,
            internal_reply_id=tracker.internal_reply_id,
        )
        self.action_outcome_authority.acknowledge_delivery_terminal(
            outcome,
            planned_action,
            consumer=composer_request,
        )
        dispatch = durable_authority.acknowledge_delivered(
            lifecycle_handle=lifecycle_handle,
            source=source,
            outcome=outcome,
            current_planned_action=planned_action,
            composer_request=composer_request,
            send_evidence=evidence,
            send_ledger=self.send_receipts,
            presentation=presentation,
            now=time.time(),
        )
        controller_ticket = durable_authority.ticket_for(
            dispatch,
            DurableFinalizeConsumer.CONTROLLER_RELEASE,
        )
        outcome_ticket = durable_authority.ticket_for(
            dispatch,
            DurableFinalizeConsumer.OUTCOME_RETIRE,
        )
        self.owner_action_controller.release_terminal_lineage(
            source,
            outcome=outcome,
            outcome_authority=self.action_outcome_authority,
            ticket=controller_ticket,
            durable_authority=durable_authority,
            reclaimed_at=time.time(),
        )
        self.action_outcome_authority.finalize_reclaimed_outcome(
            outcome,
            planned_action,
            ticket=outcome_ticket,
            durable_authority=durable_authority,
        )
        durable_authority.complete(controller_ticket)
        event.set_extra(SHIO_OWNER_ACTION_SEND_EVIDENCE, evidence)
        event.set_extra(SHIO_OWNER_ACTION_SOURCE, None)
        event.set_extra(SHIO_ACTION_OUTCOME, None)
        event.set_extra(SHIO_OWNER_ACTION_LIFECYCLE_HANDLE, None)

    @filter.after_message_sent(priority=-100)
    async def confirm_automatic_send_observation(
        self,
        event: AstrMessageEvent,
    ) -> None:
        tracker = self._send_observation_tracker(event)
        if tracker is None or not tracker.pending_automatic_segment_id:
            return
        segment_id = tracker.pending_automatic_segment_id
        visible_text = tracker.pending_automatic_text
        self.send_receipts.mark_succeeded(segment_id)
        record_pipeline_stage(
            event,
            "send_success",
            automatic=True,
            visible_chars=len(visible_text),
        )
        tracker.pending_automatic_segment_id = ""
        tracker.pending_automatic_text = ""
        self._observe_successful_send_segment(event, visible_text)
        trace_context = get_trace_context(event)
        if trace_context is not None:
            self.performance_window.observe_latency(
                LatencyKind.FULL_REPLY,
                float((time.perf_counter() - trace_context.started_at) * 1000.0),
            )
        try:
            await self._execute_text_meme_complement_after_send(
                event=event,
                tracker=tracker,
            )
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "meme.presentation_finalize_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
            record_pipeline_stage(
                event,
                "meme_presentation",
                meme_execution_status="failed_closed",
                failure_kind=safe_exception_kind(exc),
            )
        try:
            affect_mutation = self._settle_affect_after_send(event)
            if (
                isinstance(affect_mutation, AffectMutationResult)
                and affect_mutation.status is AffectMutationStatus.REJECTED
            ):
                structured_log(
                    logger,
                    "warning",
                    "affect.outbound_rejected",
                    trace_id=get_trace_id(event),
                    **affect_mutation.trace_metadata(),
                )
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "affect.outbound_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
        try:
            self._finalize_owner_action_delivery(event)
        except Exception as exc:
            structured_log(
                logger,
                "error",
                "owner_action.delivery_finalize_failed",
                trace_id=get_trace_id(event),
                failure_kind=safe_exception_kind(exc),
            )
        payload = event.get_extra(SHIO_PAYLOAD, {})
        if isinstance(payload, dict) and payload.get("chat_type") == "group":
            sent_record = self.send_receipts.sent_reply_record(
                tracker.internal_reply_id
            )
            if sent_record is not None:
                scene_mutation = self.group_scenes.record_outbound(
                    sent_record,
                    source=SceneEntrySource.SHIO_OUTBOUND,
                )
                event.set_extra(SHIO_GROUP_SCENE_MUTATION, scene_mutation)
                event.set_extra(
                    SHIO_GROUP_SCENE_SNAPSHOT,
                    scene_mutation.snapshot,
                )
                record_pipeline_stage(
                    event,
                    "scene_outbound",
                    **scene_mutation.trace_metadata(),
                )
                if (
                    scene_mutation.status
                    is not SceneMutationStatus.ACCEPTED_SHIO_OUTBOUND
                ):
                    structured_log(
                        logger,
                        "error",
                        "scene.outbound_rejected",
                        trace_id=get_trace_id(event),
                        **scene_mutation.trace_metadata(),
                    )
        self._emit_pipeline_metrics(event)

    async def terminate(self) -> None:
        unbind_name_wake_plugin(self)
        task = self._proactive_scheduler_task
        self._proactive_scheduler_task = None
        if task is not None and not task.done():
            task.cancel("plugin_terminated")
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.proactive_scheduler_runtime.shutdown()
        await self.generation_tasks.close()
        await self.scope_concurrency.close()
        await self.inference_budget.close()
        with self._owner_action_runtime_lock:
            if self.owner_action_lifecycle_store is not None:
                self.owner_action_lifecycle_store.close()
                self.owner_action_lifecycle_store = None
                self.owner_action_durable_authority = None
        self.runtime_continuity.close()
        self.ledger.flush()
        self.runtime.flush()
