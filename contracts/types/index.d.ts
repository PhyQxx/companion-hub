// Generated from Pydantic JSON Schema. Do not edit by hand.

export type CheckedAt = string;
export type ReasonCode = string | null;
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterState".
 */
export type AdapterState =
  "installed" | "configured" | "starting" | "ready" | "degraded" | "stopping" | "stopped" | "failed";
export type AdapterId = string;
export type AdapterVersion = string;
export type Critical = string[];
export type ProactiveReachable = boolean;
export type SupportsAck = boolean;
export type SupportsCancel = boolean;
export type SupportsReplace = boolean;
export type InputParts = string[];
export type OutputParts = string[];
export type Emotions = string[];
export type Engine = string | null;
export type Gestures = string[];
export type Streaming = string[];
export type ConfigSchemaRef = string;
/**
 * @minItems 1
 * @maxItems 2
 */
export type Direction = [AdapterDirection] | [AdapterDirection, AdapterDirection];
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterDirection".
 */
export type AdapterDirection = "input" | "output";
export type Backpressure = "block" | "drop_oldest" | "sample" | "aggregate";
export type QueueLimit = number;
export type MaxTestedHubVersion = string;
export type MinHubVersion = string;
/**
 * @maxItems 32
 */
export type Permissions = string[];
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "PrivacyLevel".
 */
export type PrivacyLevel = "L0" | "L1" | "L2" | "L3";
export type RunsLocal = boolean;
export type ResourceProfile = string;
export type SchemaVersion = 1;
/**
 * @maxItems 16
 */
export type TransportBindings =
  | []
  | [string]
  | [string, string]
  | [string, string, string]
  | [string, string, string, string]
  | [string, string, string, string, string]
  | [string, string, string, string, string, string]
  | [string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ];
/**
 * @maxItems 16
 */
export type Actions =
  | []
  | [AgentAction]
  | [AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction, AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction]
  | [AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction, AgentAction]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ]
  | [
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction,
      AgentAction
    ];
export type Type = "expression" | "animation" | "sound" | "picture";
export type Value = string;
export type Emotion = "neutral" | "happy" | "sad" | "angry" | "surprised" | "thinking" | "concerned";
/**
 * @maxItems 8
 */
export type Expressions =
  | []
  | [string]
  | [string, string]
  | [string, string, string]
  | [string, string, string, string]
  | [string, string, string, string, string]
  | [string, string, string, string, string, string]
  | [string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string];
export type ParseStatus = "structured" | "fallback";
export type SchemaRef = "aria.agent-reply/1";
export type SchemaVersion1 = 1;
export type Text = string;
export type TtsText = string;
export type AdapterInstanceId = string;
export type Attempt = number;
export type DeadlineAt = string;
export type DeliveryId = string;
export type EndpointId = string;
export type GenerationId = string | null;
export type IdempotencyKey = string;
export type IntentId = string;
export type Emotion1 = string | null;
export type Gesture = string | null;
export type Intensity = number | null;
/**
 * @minItems 1
 * @maxItems 32
 */
export type SelectedContent = [
  TextPart | SpeechPart | AudioPart | ImagePart | VideoPart | FilePart | NotificationPart | DeviceCommandPart,
  ...(TextPart | SpeechPart | AudioPart | ImagePart | VideoPart | FilePart | NotificationPart | DeviceCommandPart)[]
];
export type Format = "plain" | "markdown";
export type Language = string | null;
export type Text1 = string;
export type Type1 = "text";
export type Language1 = string | null;
export type Text2 = string;
export type Type2 = "speech";
export type VoiceProfile = string | null;
export type AssetId = string;
export type ContentHash = string;
export type MediaType = string;
export type Codec = string | null;
export type SampleRate = number | null;
export type Type3 = "audio";
export type Height = number | null;
export type Type4 = "image";
export type Width = number | null;
export type DurationMs = number | null;
export type Type5 = "video";
export type DisplayName = string;
export type Type6 = "file";
export type Body = string;
export type Category = string;
export type Title = string;
export type Type7 = "notification";
export type Command = string;
export type ToolExecutionId = string;
export type Type8 = "device_command";
export type AdapterInstanceId1 = string;
export type Attempt1 = number;
export type DeliveryId1 = string;
export type EndpointId1 = string;
export type ExternalOperationId = string | null;
export type GenerationId1 = string | null;
export type IntentId1 = string;
export type OccurredAt = string;
export type ReasonCode1 = string | null;
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeliveryStatus".
 */
export type DeliveryStatus =
  "planned" | "sending" | "accepted" | "playing" | "delivered" | "dropped" | "failed" | "cancelled" | "unknown_outcome";
export type EndpointId2 = string;
export type ObservedAt = string;
export type Channel = string;
export type Content = TelemetryPart | AudioStreamPart | VideoStreamPart;
export type Channel1 = string;
export type Quality = number | null;
export type Type9 = "telemetry";
export type Unit = string | null;
export type Value1 = string | number | boolean | null;
export type Codec1 = string;
export type SampleRate1 = number;
export type StreamId = string;
export type Type10 = "audio_stream";
export type Codec2 = string;
export type Height1 = number;
export type StreamId1 = string;
export type Type11 = "video_stream";
export type Width1 = number;
export type ExpiresAt = string;
export type OccurredAt1 = string;
export type SignalId = string;
export type AdapterId1 = string;
export type AdapterInstanceId2 = string;
export type EndpointId3 = string;
export type CausationId = string | null;
/**
 * @minItems 1
 * @maxItems 32
 */
export type Content1 = [
  TextPart | AudioPart | ImagePart | VideoPart | FilePart | InteractionPart | ControlPart | AssetRefPart,
  ...(TextPart | AudioPart | ImagePart | VideoPart | FilePart | InteractionPart | ControlPart | AssetRefPart)[]
];
export type Name = string;
export type Target = string | null;
export type Type12 = "interaction";
export type Value2 = string | number | boolean | null;
export type Name1 = string;
export type Type13 = "control";
export type Type14 = "asset_ref";
export type ConversationId = string | null;
export type CorrelationId = string;
export type EventId = string;
export type Kind = string;
export type OccurredAt2 = string;
export type Priority = "low" | "normal" | "high" | "critical";
export type ProtoVersion = 1;
export type ReceivedAt = string;
export type SchemaRef1 = "aria.input-envelope/1";
export type TurnId = string | null;
export type UserId = string;
export type Mode = "user" | "admins" | "public";
export type CausationId1 = string | null;
/**
 * @minItems 1
 * @maxItems 32
 */
export type Content2 = [
  TextPart | SpeechPart | AudioPart | ImagePart | VideoPart | FilePart | NotificationPart | DeviceCommandPart,
  ...(TextPart | SpeechPart | AudioPart | ImagePart | VideoPart | FilePart | NotificationPart | DeviceCommandPart)[]
];
export type ConversationId1 = string | null;
export type CorrelationId1 = string;
export type CreatedAt = string;
export type AckPolicy = "none" | "accepted" | "delivered" | "played";
/**
 * @maxItems 16
 */
export type Fallback =
  | []
  | [string]
  | [string, string]
  | [string, string, string]
  | [string, string, string, string]
  | [string, string, string, string, string]
  | [string, string, string, string, string, string]
  | [string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ];
export type Interruptible = boolean;
export type Priority1 = "low" | "normal" | "high" | "critical";
export type TtlMs = number;
export type GenerationId2 = string | null;
export type IntentId2 = string;
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "OutputKind".
 */
export type OutputKind = "reply" | "proactive" | "system" | "alert";
export type PrivacyLevel1 = "L0" | "L1" | "L2" | "L3";
export type ProtoVersion1 = 1;
export type SchemaRef2 = "aria.output-intent/1";
/**
 * @maxItems 32
 */
export type EndpointIds = string[];
export type Mode1 = "best_available" | "all_compatible" | "explicit";
/**
 * @maxItems 16
 */
export type Prefer =
  | []
  | [string]
  | [string, string]
  | [string, string, string]
  | [string, string, string, string]
  | [string, string, string, string, string]
  | [string, string, string, string, string, string]
  | [string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ]
  | [
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string,
      string
    ];
export type TurnId1 = string | null;
export type UserId1 = string;
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "Priority".
 */
export type Priority2 = "low" | "normal" | "high" | "critical";

export interface AriaContracts {
  AdapterHealth?: AdapterHealth;
  AdapterManifest?: AdapterManifest;
  AgentReply?: AgentReply;
  DeliveryPlan?: DeliveryPlan;
  DeliveryReceipt?: DeliveryReceipt;
  EndpointCapabilities?: EndpointCapabilities;
  EphemeralSignal?: EphemeralSignal;
  InputEnvelope?: InputEnvelope;
  OutputIntent?: OutputIntent;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterHealth".
 */
export interface AdapterHealth {
  checked_at: CheckedAt;
  reason_code?: ReasonCode;
  state: AdapterState;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterManifest".
 */
export interface AdapterManifest {
  adapter_id: AdapterId;
  adapter_version: AdapterVersion;
  capabilities: AdapterCapabilities;
  config_schema_ref: ConfigSchemaRef;
  direction: Direction;
  input_policy?: InputPolicy;
  max_tested_hub_version: MaxTestedHubVersion;
  min_hub_version: MinHubVersion;
  permissions?: Permissions;
  privacy: AdapterPrivacy;
  resource_profile: ResourceProfile;
  schema_version?: SchemaVersion;
  transport_bindings?: TransportBindings;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterCapabilities".
 */
export interface AdapterCapabilities {
  critical?: Critical;
  delivery?: DeliveryCapabilities;
  input_parts?: InputParts;
  output_parts?: OutputParts;
  presentation?: PresentationCapabilities;
  streaming?: Streaming;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeliveryCapabilities".
 */
export interface DeliveryCapabilities {
  proactive_reachable?: ProactiveReachable;
  supports_ack?: SupportsAck;
  supports_cancel?: SupportsCancel;
  supports_replace?: SupportsReplace;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "PresentationCapabilities".
 */
export interface PresentationCapabilities {
  emotions?: Emotions;
  engine?: Engine;
  gestures?: Gestures;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "InputPolicy".
 */
export interface InputPolicy {
  backpressure?: Backpressure;
  queue_limit?: QueueLimit;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AdapterPrivacy".
 */
export interface AdapterPrivacy {
  max_input_level: PrivacyLevel;
  max_output_level: PrivacyLevel;
  runs_local: RunsLocal;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AgentReply".
 */
export interface AgentReply {
  actions?: Actions;
  emotion?: Emotion;
  expressions?: Expressions;
  parse_status?: ParseStatus;
  schema_ref?: SchemaRef;
  schema_version?: SchemaVersion1;
  text: Text;
  tts_text: TtsText;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AgentAction".
 */
export interface AgentAction {
  type: Type;
  value: Value;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeliveryPlan".
 */
export interface DeliveryPlan {
  adapter_instance_id: AdapterInstanceId;
  attempt?: Attempt;
  deadline_at: DeadlineAt;
  delivery_id: DeliveryId;
  endpoint_id: EndpointId;
  generation_id?: GenerationId;
  idempotency_key: IdempotencyKey;
  intent_id: IntentId;
  presentation?: Presentation;
  privacy_level: PrivacyLevel;
  selected_content: SelectedContent;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "Presentation".
 */
export interface Presentation {
  emotion?: Emotion1;
  gesture?: Gesture;
  intensity?: Intensity;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "TextPart".
 */
export interface TextPart {
  format?: Format;
  language?: Language;
  text: Text1;
  type?: Type1;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "SpeechPart".
 */
export interface SpeechPart {
  language?: Language1;
  text: Text2;
  type?: Type2;
  voice_profile?: VoiceProfile;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AudioPart".
 */
export interface AudioPart {
  asset_ref: AssetPointer;
  codec?: Codec;
  sample_rate?: SampleRate;
  type?: Type3;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AssetPointer".
 */
export interface AssetPointer {
  asset_id: AssetId;
  content_hash: ContentHash;
  media_type: MediaType;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "ImagePart".
 */
export interface ImagePart {
  asset_ref: AssetPointer;
  height?: Height;
  type?: Type4;
  width?: Width;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "VideoPart".
 */
export interface VideoPart {
  asset_ref: AssetPointer;
  duration_ms?: DurationMs;
  type?: Type5;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "FilePart".
 */
export interface FilePart {
  asset_ref: AssetPointer;
  display_name: DisplayName;
  type?: Type6;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "NotificationPart".
 */
export interface NotificationPart {
  body: Body;
  category: Category;
  title: Title;
  type?: Type7;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeviceCommandPart".
 */
export interface DeviceCommandPart {
  args_redacted?: ArgsRedacted;
  command: Command;
  tool_execution_id: ToolExecutionId;
  type?: Type8;
}
export interface ArgsRedacted {
  [k: string]: string | number | boolean | (string | number | boolean | null)[] | null;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeliveryReceipt".
 */
export interface DeliveryReceipt {
  adapter_instance_id: AdapterInstanceId1;
  attempt: Attempt1;
  delivery_id: DeliveryId1;
  endpoint_id: EndpointId1;
  external_operation_id?: ExternalOperationId;
  generation_id?: GenerationId1;
  intent_id: IntentId1;
  occurred_at: OccurredAt;
  reason_code?: ReasonCode1;
  status: DeliveryStatus;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "EndpointCapabilities".
 */
export interface EndpointCapabilities {
  capabilities: AdapterCapabilities;
  endpoint_id: EndpointId2;
  observed_at: ObservedAt;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "EphemeralSignal".
 */
export interface EphemeralSignal {
  channel: Channel;
  content: Content;
  expires_at: ExpiresAt;
  occurred_at: OccurredAt1;
  privacy_level: PrivacyLevel;
  signal_id: SignalId;
  source: SourceRef;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "TelemetryPart".
 */
export interface TelemetryPart {
  channel: Channel1;
  quality?: Quality;
  type?: Type9;
  unit?: Unit;
  value: Value1;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AudioStreamPart".
 */
export interface AudioStreamPart {
  codec: Codec1;
  sample_rate: SampleRate1;
  stream_id: StreamId;
  type?: Type10;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "VideoStreamPart".
 */
export interface VideoStreamPart {
  codec: Codec2;
  height: Height1;
  stream_id: StreamId1;
  type?: Type11;
  width: Width1;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "SourceRef".
 */
export interface SourceRef {
  adapter_id: AdapterId1;
  adapter_instance_id: AdapterInstanceId2;
  endpoint_id: EndpointId3;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "InputEnvelope".
 */
export interface InputEnvelope {
  causation_id?: CausationId;
  content: Content1;
  conversation_id?: ConversationId;
  correlation_id: CorrelationId;
  event_id: EventId;
  extensions?: Extensions;
  kind: Kind;
  occurred_at: OccurredAt2;
  priority?: Priority;
  privacy_level: PrivacyLevel;
  proto_version?: ProtoVersion;
  received_at: ReceivedAt;
  schema_ref?: SchemaRef1;
  source: SourceRef;
  turn_id?: TurnId;
  user_id: UserId;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "InteractionPart".
 */
export interface InteractionPart {
  name: Name;
  target?: Target;
  type?: Type12;
  value?: Value2;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "ControlPart".
 */
export interface ControlPart {
  args?: Args;
  name: Name1;
  type?: Type13;
}
export interface Args {
  [k: string]: string | number | boolean | (string | number | boolean | null)[] | null;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "AssetRefPart".
 */
export interface AssetRefPart {
  asset_ref: AssetPointer;
  type?: Type14;
}
export interface Extensions {
  [k: string]: {
    [k: string]: unknown;
  };
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "OutputIntent".
 */
export interface OutputIntent {
  audience?: Audience;
  causation_id?: CausationId1;
  content: Content2;
  conversation_id?: ConversationId1;
  correlation_id: CorrelationId1;
  created_at: CreatedAt;
  delivery?: DeliveryPolicy;
  generation_id?: GenerationId2;
  intent_id: IntentId2;
  kind: OutputKind;
  presentation?: Presentation;
  privacy_level?: PrivacyLevel1;
  proto_version?: ProtoVersion1;
  schema_ref?: SchemaRef2;
  target_selector?: TargetSelector;
  turn_id?: TurnId1;
  user_id: UserId1;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "Audience".
 */
export interface Audience {
  mode?: Mode;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "DeliveryPolicy".
 */
export interface DeliveryPolicy {
  ack_policy?: AckPolicy;
  fallback?: Fallback;
  interruptible?: Interruptible;
  priority?: Priority1;
  ttl_ms?: TtlMs;
}
/**
 * This interface was referenced by `AriaContracts`'s JSON-Schema
 * via the `definition` "TargetSelector".
 */
export interface TargetSelector {
  endpoint_ids?: EndpointIds;
  mode?: Mode1;
  prefer?: Prefer;
}
