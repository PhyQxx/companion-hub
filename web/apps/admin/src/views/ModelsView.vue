<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

type ModelKind = "text" | "vision" | "image_generation" | "video_generation";

interface HubModel {
  name?: string;
  kind?: ModelKind;
  provider: string;
  model: string;
  enabled: boolean;
  runs_local: boolean;
  max_privacy_level: string;
  base_url: string;
  secret_ref?: string | null;
  secret_value?: string | null;
  supports_json_mode?: boolean;
  thinking_mode?: "provider_default" | "enabled" | "disabled";
  timeout_ms?: number;
  max_retries?: number;
  max_tokens?: number | null;
  max_context_tokens?: number;
  input_cost_per_million?: number;
  output_cost_per_million?: number;
}

function selectModel(index: number) {
  activeModelTab.value = index;
  editMode.value = "visual";
  connectionTest.value = null;
}

function localModelsProbeUrl(baseUrl: string): string {
  try {
    const url = new URL(baseUrl);
    return `${url.protocol}//${url.host}/api/v1/models`;
  } catch {
    return "http://127.0.0.1:1234/api/v1/models";
  }
}

async function testModelConnection() {
  const model = draft.value.models[activeModelTab.value];
  if (!model) return;
  if (!model.base_url.trim()) {
    ElMessage.warning("请先填写 Base URL");
    return;
  }
  if (!model.model.trim()) {
    ElMessage.warning("请先填写模型名");
    return;
  }
  testingConnection.value = true;
  connectionTest.value = null;
  try {
    const result = await api.request<ModelConnectionTestResult>("/api/v1/admin/config/models/test", {
      method: "POST",
      body: JSON.stringify({ endpoint: draftModelToEndpoint(model) }),
    });
    connectionTest.value = result;
    if (!result.ok) {
      ElMessage.error(`连接失败：${result.message}`);
    } else if (result.model_available === false || result.model_loaded === false) {
      ElMessage.warning(result.message);
    } else {
      ElMessage.success(result.message);
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : "连接测试失败";
    ElMessage.error(message);
    emit("status", message, true);
  } finally {
    testingConnection.value = false;
  }
}

function draftModelToEndpoint(m: DraftModel): HubModel {
  const endpoint: HubModel = {
    enabled: m.enabled,
    kind: m.kind,
    provider: m.provider,
    model: m.model,
    base_url: m.base_url,
    runs_local: m.runs_local,
    max_privacy_level: m.max_privacy_level,
    supports_json_mode: m.supports_json_mode,
    thinking_mode: m.thinking_mode,
    timeout_ms: m.timeout_ms,
    max_retries: m.max_retries,
    max_tokens: m.max_tokens ?? null,
    max_context_tokens: m.max_context_tokens,
    input_cost_per_million: m.input_cost_per_million,
    output_cost_per_million: m.output_cost_per_million,
  };
  if (m.secret_mode === "value" && m.secret_value) {
    endpoint.secret_value = m.secret_value;
    endpoint.secret_ref = null;
  } else if (m.secret_mode === "ref" && m.secret_ref) {
    endpoint.secret_ref = m.secret_ref;
    endpoint.secret_value = null;
  } else {
    endpoint.secret_ref = null;
    endpoint.secret_value = null;
  }
  return endpoint;
}

interface ModelProbeItem {
  key: string;
  display_name: string;
  type: string;
  loaded_instances: number;
}

interface ModelConnectionTestResult {
  ok: boolean;
  probe_kind: string;
  target_url: string;
  model: string;
  latency_ms: number;
  message: string;
  error_type?: string | null;
  model_available?: boolean | null;
  model_loaded?: boolean | null;
  models: ModelProbeItem[];
}
interface VoiceAsrEnvironmentCheckResult {
  ok: boolean;
  provider: string;
  model: string;
  runs_local: boolean;
  dependency_available?: boolean | null;
  package_version?: string | null;
  model_load_checked: boolean;
  message: string;
}
interface HubConfig {
  schema_version: number;
  models: Record<string, HubModel>;
  routes: Record<string, { primary: string; fallbacks?: string[]; timeout_ms?: number | null }>;
  capability_models?: {
    vision?: string | null;
    image_generation?: string | null;
    video_generation?: string | null;
  };
  voice?: HubVoiceConfig | null;
  observability: { log_level: string; trace_sample_rate: number; retain_days: number };
}
interface HubVoiceAsr {
  provider: "mimo" | "faster_whisper";
  model: string;
  base_url?: string | null;
  language: "auto" | "zh" | "en";
  secret_ref?: string | null;
  secret_value?: string | null;
  device?: "auto" | "cpu" | "cuda";
  compute_type?: string;
  runs_local?: boolean;
}
interface HubVoiceTts {
  provider: "mimo" | "edge_tts";
  model: string;
  base_url?: string | null;
  voice: string;
  enabled: boolean;
  secret_ref?: string | null;
  secret_value?: string | null;
}
interface HubVoiceConfig {
  asr: HubVoiceAsr | null;
  tts: HubVoiceTts[];
}
interface DraftModel {
  key: string;
  enabled: boolean;
  kind: ModelKind;
  provider: string;
  model: string;
  base_url: string;
  secret_mode: "value" | "ref" | "none";
  secret_value: string;
  secret_ref: string;
  runs_local: boolean;
  max_privacy_level: string;
  supports_json_mode: boolean;
  thinking_mode: "provider_default" | "enabled" | "disabled";
  timeout_ms: number;
  max_retries: number;
  max_tokens: number;
  max_context_tokens: number;
  input_cost_per_million: number;
  output_cost_per_million: number;
}

interface DraftRoute {
  primary: string;
  fallbacks: string[];
  timeout_ms: number | null;
}

interface DraftVoiceAsr {
  enabled: boolean;
  provider: "mimo" | "faster_whisper";
  model: string;
  base_url: string;
  language: "auto" | "zh" | "en";
  device: "auto" | "cpu" | "cuda";
  compute_type: string;
  secret_mode: "value" | "ref" | "none";
  secret_value: string;
  secret_ref: string;
}

interface DraftVoiceTts {
  provider: "mimo" | "edge_tts";
  model: string;
  base_url: string;
  voice: string;
  enabled: boolean;
  secret_mode: "value" | "ref" | "none";
  secret_value: string;
  secret_ref: string;
}

interface DraftVoice {
  asr: DraftVoiceAsr;
  tts: DraftVoiceTts[];
}

interface DraftState {
  schema_version: number;
  models: DraftModel[];
  routes: Record<string, DraftRoute>;
  capability_models: {
    vision: string;
    image_generation: string;
    video_generation: string;
  };
  voice: DraftVoice;
  observability: { log_level: string; trace_sample_rate: number; retain_days: number };
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; content_hash: string; published_at: string; config: HubConfig } | null>(null);
const draftJson = ref("");
const editMode = ref<"visual" | "json">("visual");
const activeSection = ref<"basic" | "connection" | "runtime">("basic");
const busy = ref(false);
const testingConnection = ref(false);
const connectionTest = ref<ModelConnectionTestResult | null>(null);
const testingVoiceAsr = ref(false);
const voiceAsrCheck = ref<VoiceAsrEnvironmentCheckResult | null>(null);
const activeModelTab = ref(0);
const activeMainTab = ref("routes");

const defaultModel = (): DraftModel => ({
  key: "",
  enabled: true,
  kind: "text",
  provider: "openai_compatible",
  model: "",
  base_url: "https://token.sensenova.cn/v1",
  secret_mode: "value",
  secret_value: "",
  secret_ref: "env:API_KEY",
  runs_local: false,
  max_privacy_level: "L1",
  supports_json_mode: false,
  thinking_mode: "provider_default",
  timeout_ms: 12000,
  max_retries: 1,
  max_tokens: 2048,
  max_context_tokens: 131072,
  input_cost_per_million: 0,
  output_cost_per_million: 0,
});

const defaultRoute = (): DraftRoute => ({ primary: "", fallbacks: [], timeout_ms: null });
const defaultCapabilityModels = () => ({ vision: "", image_generation: "", video_generation: "" });

const defaultObservability = () => ({ log_level: "INFO", trace_sample_rate: 1.0, retain_days: 14 });

const defaultVoiceAsr = (): DraftVoiceAsr => ({
  enabled: false,
  provider: "mimo",
  model: "mimo-v2.5-asr",
  base_url: "https://api.xiaomimimo.com/v1",
  language: "auto",
  device: "auto",
  compute_type: "default",
  secret_mode: "value",
  secret_value: "",
  secret_ref: "env:MIMO_API_KEY",
});

const defaultVoiceTts = (provider: "mimo" | "edge_tts" = "mimo"): DraftVoiceTts => ({
  provider,
  model: provider === "mimo" ? "mimo-v2.5-tts" : "edge-tts",
  base_url: provider === "mimo" ? "https://api.xiaomimimo.com/v1" : "",
  voice: provider === "mimo" ? "冰糖" : "zh-CN-XiaoxiaoNeural",
  enabled: true,
  secret_mode: "value",
  secret_value: "",
  secret_ref: "env:MIMO_API_KEY",
});

const defaultVoice = (): DraftVoice => ({ asr: defaultVoiceAsr(), tts: [] });

function onAsrProviderChange(provider: "mimo" | "faster_whisper") {
  voiceAsrCheck.value = null;
  const asr = draft.value.voice.asr;
  asr.provider = provider;
  if (provider === "faster_whisper") {
    if (asr.model.startsWith("mimo-")) asr.model = "small";
    asr.base_url = "";
    asr.device = "auto";
    asr.compute_type = "default";
    asr.secret_mode = "none";
    asr.secret_value = "";
    return;
  }
  if (!asr.model || ["tiny", "base", "small", "medium", "large-v3"].includes(asr.model)) {
    asr.model = "mimo-v2.5-asr";
  }
  asr.base_url = "https://api.xiaomimimo.com/v1";
  asr.secret_mode = asr.secret_value ? "value" : asr.secret_ref ? "ref" : "value";
}

async function checkLocalAsrEnvironment() {
  const asr = draft.value.voice.asr;
  if (asr.provider !== "faster_whisper") return;
  if (!asr.model.trim()) {
    ElMessage.warning("请先填写 faster-whisper 模型名");
    return;
  }
  testingVoiceAsr.value = true;
  voiceAsrCheck.value = null;
  try {
    const result = await api.request<VoiceAsrEnvironmentCheckResult>(
      "/api/v1/admin/config/voice/asr/check",
      {
        method: "POST",
        body: JSON.stringify({
          asr: {
            provider: "faster_whisper",
            model: asr.model,
            base_url: null,
            language: asr.language,
            device: asr.device,
            compute_type: asr.compute_type,
            runs_local: true,
            secret_ref: null,
            secret_value: null,
          },
        }),
      },
    );
    voiceAsrCheck.value = result;
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.warning(result.message);
  } catch (error) {
    const message = error instanceof Error ? error.message : "本地 ASR 自检失败";
    ElMessage.error(message);
  } finally {
    testingVoiceAsr.value = false;
  }
}

const draft = ref<DraftState>({
  schema_version: 1,
  models: [],
  routes: { dialogue: defaultRoute(), utility: defaultRoute(), private: defaultRoute() },
  capability_models: defaultCapabilityModels(),
  voice: defaultVoice(),
  observability: defaultObservability(),
});

function hubConfigToDraft(config: HubConfig | undefined | null): DraftState {
  if (!config) {
    return {
      schema_version: 1,
      models: [],
      routes: { dialogue: defaultRoute(), utility: defaultRoute(), private: defaultRoute() },
      capability_models: defaultCapabilityModels(),
      voice: defaultVoice(),
      observability: defaultObservability(),
    };
  }
  const models: DraftModel[] = Object.entries(config.models).map(([key, m]) => {
    let secretMode: "value" | "ref" | "none" = "none";
    if (m.secret_value) secretMode = "value";
    else if (m.secret_ref) secretMode = "ref";
    return {
      key,
      enabled: m.enabled ?? true,
      kind: m.kind ?? "text",
      provider: m.provider ?? "openai_compatible",
      model: m.model ?? "",
      base_url: m.base_url ?? "",
      secret_mode: secretMode,
      secret_value: m.secret_value ?? "",
      secret_ref: m.secret_ref ?? "",
      runs_local: m.runs_local ?? false,
      max_privacy_level: m.max_privacy_level ?? "L1",
      supports_json_mode: m.supports_json_mode ?? false,
      thinking_mode: m.thinking_mode ?? "provider_default",
      timeout_ms: m.timeout_ms ?? 12000,
      max_retries: m.max_retries ?? 1,
      max_tokens: m.max_tokens ?? 2048,
      max_context_tokens: m.max_context_tokens ?? 131072,
      input_cost_per_million: m.input_cost_per_million ?? 0,
      output_cost_per_million: m.output_cost_per_million ?? 0,
    };
  });
  const routes: Record<string, DraftRoute> = {};
  for (const [name, r] of Object.entries(config.routes ?? {})) {
    routes[name] = {
      primary: r.primary ?? "",
      fallbacks: r.fallbacks ?? [],
      timeout_ms: r.timeout_ms ?? null,
    };
  }
  const asr = config.voice?.asr;
  const asrSecretMode = asr?.secret_value ? "value" : asr?.secret_ref ? "ref" : "none";
  const draftAsr: DraftVoiceAsr = asr
    ? {
        enabled: true,
        provider: asr.provider ?? "mimo",
        model: asr.model ?? (asr.provider === "faster_whisper" ? "small" : "mimo-v2.5-asr"),
        base_url: asr.base_url ?? "",
        language: asr.language ?? "auto",
        device: asr.device ?? "auto",
        compute_type: asr.compute_type ?? "default",
        secret_mode: asrSecretMode,
        secret_value: asr.secret_value ?? "",
        secret_ref: asr.secret_ref ?? "env:MIMO_API_KEY",
      }
    : defaultVoiceAsr();
  const draftTts: DraftVoiceTts[] = (config.voice?.tts ?? []).map((p) => ({
    provider: p.provider,
    model: p.model ?? (p.provider === "mimo" ? "mimo-v2.5-tts" : "edge-tts"),
    base_url: p.base_url ?? "",
    voice: p.voice ?? (p.provider === "mimo" ? "冰糖" : "zh-CN-XiaoxiaoNeural"),
    enabled: p.enabled ?? true,
    secret_mode: p.secret_value ? "value" : p.secret_ref ? "ref" : "none",
    secret_value: p.secret_value ?? "",
    secret_ref: p.secret_ref ?? "env:MIMO_API_KEY",
  }));
  return {
    schema_version: config.schema_version ?? 1,
    models,
    routes,
    capability_models: {
      vision: config.capability_models?.vision ?? "",
      image_generation: config.capability_models?.image_generation ?? "",
      video_generation: config.capability_models?.video_generation ?? "",
    },
    voice: { asr: draftAsr, tts: draftTts },
    observability: config.observability ?? defaultObservability(),
  };
}

function draftToHubConfig(d: DraftState): HubConfig {
  const models: Record<string, HubModel> = {};
  for (const m of d.models) {
    const endpoint: HubModel = {
      enabled: m.enabled,
      kind: m.kind,
      provider: m.provider,
      model: m.model,
      base_url: m.base_url,
      runs_local: m.runs_local,
      max_privacy_level: m.max_privacy_level,
      supports_json_mode: m.supports_json_mode,
      thinking_mode: m.thinking_mode,
      timeout_ms: m.timeout_ms,
      max_retries: m.max_retries,
      max_tokens: m.max_tokens ?? null,
      max_context_tokens: m.max_context_tokens,
      input_cost_per_million: m.input_cost_per_million,
      output_cost_per_million: m.output_cost_per_million,
    };
    if (m.secret_mode === "value" && m.secret_value) {
      endpoint.secret_value = m.secret_value;
      endpoint.secret_ref = null;
    } else if (m.secret_mode === "ref" && m.secret_ref) {
      endpoint.secret_ref = m.secret_ref;
      endpoint.secret_value = null;
    } else {
      endpoint.secret_ref = null;
      endpoint.secret_value = null;
    }
    models[m.key] = endpoint;
  }
  const routes: Record<string, { primary: string; fallbacks: string[]; timeout_ms: number | null }> = {};
  for (const [name, r] of Object.entries(d.routes)) {
    routes[name] = { primary: r.primary, fallbacks: r.fallbacks, timeout_ms: r.timeout_ms };
  }
  const voiceAsr: HubVoiceAsr | null = d.voice.asr.enabled
    ? d.voice.asr.provider === "faster_whisper"
      ? {
          provider: "faster_whisper",
          model: d.voice.asr.model,
          base_url: null,
          language: d.voice.asr.language,
          device: d.voice.asr.device,
          compute_type: d.voice.asr.compute_type,
          runs_local: true,
          secret_ref: null,
          secret_value: null,
        }
      : {
          provider: "mimo",
          model: d.voice.asr.model,
          base_url: d.voice.asr.base_url,
          language: d.voice.asr.language,
          device: "auto",
          compute_type: "default",
          runs_local: false,
          ...(assembleSecret(d.voice.asr)),
        }
    : null;
  const voiceTts: HubVoiceTts[] = d.voice.tts.map((p) => ({
    provider: p.provider,
    model: p.model,
    base_url: p.provider === "mimo" ? p.base_url : null,
    voice: p.voice,
    enabled: p.enabled,
    ...(p.provider === "mimo" ? assembleSecret(p) : { secret_ref: null, secret_value: null }),
  }));
  return {
    schema_version: d.schema_version,
    models,
    routes,
    capability_models: {
      vision: d.capability_models.vision || null,
      image_generation: d.capability_models.image_generation || null,
      video_generation: d.capability_models.video_generation || null,
    },
    voice: { asr: voiceAsr, tts: voiceTts },
    observability: d.observability,
  };
}

interface SecretCarrier {
  secret_mode: "value" | "ref" | "none";
  secret_value: string;
  secret_ref: string;
}

function assembleSecret(carrier: SecretCarrier): { secret_ref: string | null; secret_value: string | null } {
  if (carrier.secret_mode === "value" && carrier.secret_value) {
    return { secret_value: carrier.secret_value, secret_ref: null };
  }
  if (carrier.secret_mode === "ref" && carrier.secret_ref) {
    return { secret_ref: carrier.secret_ref, secret_value: null };
  }
  return { secret_ref: null, secret_value: null };
}

const enabledTextModelNames = computed(() => draft.value.models.filter((m) => m.enabled && m.kind === "text").map((m) => m.key));
const localTextModelNames = computed(() => draft.value.models.filter((m) => m.enabled && m.kind === "text" && m.runs_local).map((m) => m.key));
const visionModelNames = computed(() => draft.value.models.filter((m) => m.enabled && m.kind === "vision").map((m) => m.key));
const imageModelNames = computed(() => draft.value.models.filter((m) => m.enabled && m.kind === "image_generation").map((m) => m.key));
const videoModelNames = computed(() => draft.value.models.filter((m) => m.enabled && m.kind === "video_generation").map((m) => m.key));

const modelKindLabels: Record<ModelKind, string> = {
  text: "文本",
  vision: "视觉理解",
  image_generation: "图片生成",
  video_generation: "视频生成",
};
const modelKindOptions: Array<{ value: ModelKind; label: string }> = (
  Object.entries(modelKindLabels) as Array<[ModelKind, string]>
).map(([value, label]) => ({ value, label }));

function modelUsageLabels(modelKey: string): string[] {
  const labels: string[] = [];
  const routeLabels: Record<string, string> = {
    dialogue: "对话",
    utility: "工具",
    private: "私密",
  };
  for (const [routeName, policy] of Object.entries(draft.value.routes)) {
    const label = routeLabels[routeName] ?? routeName;
    if (policy.primary === modelKey) labels.push(`${label}主模型`);
    if (policy.fallbacks.includes(modelKey)) labels.push(`${label}备用`);
  }
  if (draft.value.capability_models.vision === modelKey) labels.push("视觉能力");
  if (draft.value.capability_models.image_generation === modelKey) labels.push("生图能力");
  if (draft.value.capability_models.video_generation === modelKey) labels.push("视频能力");
  return labels.length ? labels : ["未参与路由"];
}
const enabledModelCount = computed(() => draft.value.models.filter((m) => m.enabled).length);
const configuredRouteCount = computed(() => Object.values(draft.value.routes).filter((r) => r.primary).length);
const localModelCount = computed(() => draft.value.models.filter((m) => m.enabled && m.runs_local).length);

function addModel() {
  const m = defaultModel();
  m.key = `new_model_${draft.value.models.length + 1}`;
  draft.value.models.push(m);
  activeModelTab.value = draft.value.models.length - 1;
}

function removeModel(index: number) {
  draft.value.models.splice(index, 1);
  if (activeModelTab.value >= draft.value.models.length) {
    activeModelTab.value = Math.max(0, draft.value.models.length - 1);
  }
}

function syncDraftJson() {
  draftJson.value = JSON.stringify(draftToHubConfig(draft.value), null, 2);
}

function syncFromDraftJson() {
  try {
    const parsed = JSON.parse(draftJson.value) as HubConfig;
    draft.value = hubConfigToDraft(parsed);
  } catch {
    // ignore parse error while typing
  }
}

async function load() {
  try {
    const config = await api.request<typeof current.value>("/api/v1/admin/config/current");
    current.value = config;
    draft.value = hubConfigToDraft(config?.config);
    syncDraftJson();
  } catch (error) {
    const message = error instanceof Error ? error.message : "加载失败";
    emit("status", message, true);
    ElMessage.error(message);
  }
}

const TOKEN_PATTERN = /^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$/;
const SECRET_REF_PATTERN = /^env:[A-Z][A-Z0-9_]{2,127}$/;

/**
 * 保存前的本地预校验，规则与后端 HubConfig 校验对齐，
 * 返回中文错误提示；通过则返回 null，交由后端做最终裁决。
 */
function validateCandidate(config: HubConfig): string | null {
  const models = config.models ?? {};
  const entries = Object.entries(models);
  if (!entries.length) return "至少需要保留一个模型";
  for (const [key, m] of entries) {
    if (!TOKEN_PATTERN.test(key)) {
      return `配置名称「${key || "未命名"}」不合法：需以小写字母开头，仅可用小写字母、数字、下划线和中划线`;
    }
    if (!m.model?.trim()) return `模型「${key}」未填写模型名`;
    if (!/^https?:\/\//.test((m.base_url ?? "").trim())) {
      return `模型「${key}」的 Base URL 需以 http:// 或 https:// 开头`;
    }
    if (m.secret_ref && !SECRET_REF_PATTERN.test(m.secret_ref)) {
      return `模型「${key}」的环境变量引用格式应为 env:大写环境变量名，如 env:SENSENOVA_API_KEY`;
    }
    if (typeof m.timeout_ms === "number" && (m.timeout_ms < 100 || m.timeout_ms > 120000)) {
      return `模型「${key}」的超时需在 100–120000 ms 之间`;
    }
    if (typeof m.max_retries === "number" && m.max_retries > 3) {
      return `模型「${key}」的重试次数不能超过 3`;
    }
    if (typeof m.max_tokens === "number" && (m.max_tokens < 1 || m.max_tokens > 131072)) {
      return `模型「${key}」的 max_tokens 需在 1–131072 之间`;
    }
    if (typeof m.max_context_tokens === "number" && m.max_context_tokens < 1) {
      return `模型「${key}」的上下文 tokens 需大于 0`;
    }
  }
  const routeLabels: Record<string, string> = { dialogue: "对话", utility: "工具", private: "私密" };
  const routes = config.routes ?? {};
  for (const name of ["dialogue", "utility", "private"]) {
    const route = routes[name];
    const label = routeLabels[name] ?? name;
    if (!route?.primary) return `路由「${label}」未选择首选模型`;
    if (typeof route.timeout_ms === "number" && (route.timeout_ms < 100 || route.timeout_ms > 120000)) {
      return `路由「${label}」的超时需在 100–120000 ms 之间`;
    }
    for (const endpointName of [route.primary, ...(route.fallbacks ?? [])]) {
      const endpoint = models[endpointName];
      if (!endpoint) return `路由「${label}」引用了不存在的模型：${endpointName}`;
      if (endpoint.enabled === false) return `路由「${label}」引用了已停用的模型：${endpointName}`;
      if ((endpoint.kind ?? "text") !== "text") return `路由「${label}」只能选择文本类型的模型：${endpointName}`;
      if (name === "private" && !endpoint.runs_local) return `私密路由只能选择本地运行的模型：${endpointName}`;
    }
  }
  const capabilityFields: Array<["vision" | "image_generation" | "video_generation", ModelKind, string]> = [
    ["vision", "vision", "视觉理解"],
    ["image_generation", "image_generation", "图片生成"],
    ["video_generation", "video_generation", "视频生成"],
  ];
  const capability = config.capability_models ?? {};
  for (const [field, kind, label] of capabilityFields) {
    const name = capability[field];
    if (!name) continue;
    const endpoint = models[name];
    if (!endpoint) return `${label}能力引用了不存在的模型：${name}`;
    if (endpoint.enabled === false) return `${label}能力引用了已停用的模型：${name}`;
    if ((endpoint.kind ?? "text") !== kind) return `${label}能力需要选择「${modelKindLabels[kind]}」类型的模型：${name}`;
  }
  const voice = config.voice ?? { asr: null, tts: [] };
  if (voice.asr) {
    if (!voice.asr.model?.trim()) return "语音识别未填写模型名";
    if (voice.asr.provider === "mimo") {
      if (!/^https?:\/\//.test((voice.asr.base_url ?? "").trim())) {
        return "MiMo 语音识别的 Base URL 需以 http:// 或 https:// 开头";
      }
      if (!voice.asr.secret_value && !voice.asr.secret_ref) {
        return "MiMo 语音识别需要配置 API Key（直接填写或使用环境变量引用）";
      }
      if (voice.asr.secret_ref && !SECRET_REF_PATTERN.test(voice.asr.secret_ref)) {
        return "语音识别的环境变量引用格式应为 env:大写环境变量名，如 env:MIMO_API_KEY";
      }
    } else if (!voice.asr.compute_type?.trim()) {
      return "faster-whisper 需要填写 compute type";
    }
  }
  if (voice.tts.length > 4) return "语音合成链最多 4 个提供方";
  for (const [index, p] of voice.tts.entries()) {
    if (!p.voice?.trim()) return `语音合成第 ${index + 1} 项未填写音色`;
    if (p.provider === "mimo") {
      if (!/^https?:\/\//.test((p.base_url ?? "").trim())) {
        return `语音合成第 ${index + 1} 项（MiMo）的 Base URL 需以 http:// 或 https:// 开头`;
      }
      if (!p.secret_value && !p.secret_ref) {
        return `语音合成第 ${index + 1} 项（MiMo）需要配置 API Key`;
      }
    }
  }
  return null;
}

function addVoiceTts(provider: "mimo" | "edge_tts") {
  if (draft.value.voice.tts.length >= 4) {
    ElMessage.warning("语音合成链最多 4 个提供方");
    return;
  }
  draft.value.voice.tts.push(defaultVoiceTts(provider));
}

function moveVoiceTts(index: number, offset: number) {
  const list = draft.value.voice.tts;
  const target = index + offset;
  if (target < 0 || target >= list.length) return;
  [list[index], list[target]] = [list[target], list[index]];
}

function removeVoiceTts(index: number) {
  draft.value.voice.tts.splice(index, 1);
}

function onTtsProviderChange(p: DraftVoiceTts) {
  const fresh = defaultVoiceTts(p.provider);
  p.model = fresh.model;
  p.base_url = fresh.base_url;
  p.voice = fresh.voice;
  p.secret_mode = fresh.secret_mode;
}

const mimoVoiceOptions = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"];
const edgeVoiceOptions = [
  "zh-CN-XiaoxiaoNeural",
  "zh-CN-YunxiNeural",
  "zh-CN-YunyangNeural",
  "zh-CN-XiaoyiNeural",
];

async function saveConfig() {
  busy.value = true;
  try {
    if (editMode.value === "visual") {
      syncDraftJson();
    }
    const candidate = JSON.parse(draftJson.value) as HubConfig;
    const problem = validateCandidate(candidate);
    if (problem) {
      emit("status", problem, true);
      ElMessage.error(problem);
      return;
    }
    const saved = await api.request<typeof current.value>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(candidate),
    });
    current.value = saved;
    draft.value = hubConfigToDraft(saved?.config);
    syncDraftJson();
    const message = "配置已保存并生效";
    emit("status", message);
    ElMessage.success(message);
  } catch (error) {
    const message = error instanceof Error ? error.message : "保存失败";
    emit("status", message, true);
    ElMessage.error(message);
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section v-if="current" class="models-page">
    <div class="page-intro">
      <div>
        <h1>模型与路由</h1>
        <p>管理 AI 模型配置与路由策略</p>
      </div>
      <div class="intro-meta">
        <div><span>配置哈希</span><code>{{ current.content_hash.slice(0, 12) }}</code></div>
        <div><span>状态</span><strong class="live-status">● 已生效</strong></div>
        <el-button plain @click="syncDraftJson(); editMode = editMode === 'json' ? 'visual' : 'json'">{{ editMode === 'json' ? '返回可视化' : '配置 JSON' }}</el-button>
        <el-button @click="load">重置全部修改</el-button>
        <el-button type="primary" :loading="busy" @click="saveConfig">保存并生效</el-button>
      </div>
    </div>

    <div class="summary-grid">
      <article><span class="summary-icon">▱</span><div><small>启用模型</small><strong>{{ enabledModelCount }} 个</strong></div><b>运行中</b></article>
      <article><span class="summary-icon">⌁</span><div><small>路由策略</small><strong>{{ configuredRouteCount }} 条</strong></div></article>
      <article><span class="summary-icon">⌂</span><div><small>本地模型</small><strong>{{ localModelCount }} 个</strong></div></article>
      <article><span class="summary-icon">◎</span><div><small>日志等级</small><strong>{{ draft.observability.log_level }}</strong></div></article>
    </div>

    <div v-if="editMode === 'json'" class="global-card json-card">
      <div class="global-head"><div><h2>完整配置 JSON</h2><p>这里编辑的是模型、路由和日志在内的整份配置。</p></div><el-tag type="warning" effect="plain">高级模式</el-tag></div>
      <div class="json-editor"><el-input v-model="draftJson" type="textarea" :rows="20" resize="vertical" spellcheck="false" @input="syncFromDraftJson" /></div>
    </div>

    <el-tabs v-else v-model="activeMainTab" class="main-tabs">
      <el-tab-pane label="路由与能力" name="routes">
        <div class="global-card">
          <div class="global-head"><div><h2>全局路由策略</h2><p>作用于整个 Hub，不属于某一个模型。决定不同任务优先调用哪个模型以及失败后的备用链。</p></div><el-tag effect="plain">全局配置</el-tag></div>
          <div class="route-list">
            <div v-for="routeName in ['dialogue', 'utility', 'private']" :key="routeName" class="route-card">
              <div class="route-name"><strong>{{ routeName === 'dialogue' ? '对话' : routeName === 'utility' ? '工具' : '私密' }}</strong><span>{{ routeName }}</span></div>
              <label class="field"><span>首选模型</span><el-select v-model="draft.routes[routeName].primary" placeholder="请选择"><el-option v-for="name in routeName === 'private' ? localTextModelNames : enabledTextModelNames" :key="name" :label="name" :value="name" /></el-select></label>
              <label class="field"><span>备用模型</span><el-select v-model="draft.routes[routeName].fallbacks" multiple collapse-tags collapse-tags-tooltip placeholder="选择备用模型"><el-option v-for="name in routeName === 'private' ? localTextModelNames : enabledTextModelNames" :key="name" :label="name" :value="name" :disabled="name === draft.routes[routeName].primary" /></el-select></label>
              <label class="field"><span>路由超时（ms）</span><el-input-number v-model="draft.routes[routeName].timeout_ms" :min="100" :max="120000" controls-position="right" placeholder="模型默认" /></label>
            </div>
          </div>
        </div>

        <div class="global-card compact-global">
          <div class="global-head">
            <div><h2>多模态与生成能力</h2><p>这些能力与文本对话路由分开选择，避免视觉或生成模型被误用为聊天模型。</p></div>
            <el-tag effect="plain">能力模型</el-tag>
          </div>
          <div class="form-grid three global-fields">
            <label class="field"><span>视觉理解</span><el-select v-model="draft.capability_models.vision" clearable placeholder="请选择视觉模型"><el-option v-for="name in visionModelNames" :key="name" :label="name" :value="name" /></el-select></label>
            <label class="field"><span>图片生成</span><el-select v-model="draft.capability_models.image_generation" clearable placeholder="请选择生图模型"><el-option v-for="name in imageModelNames" :key="name" :label="name" :value="name" /></el-select></label>
            <label class="field"><span>视频生成</span><el-select v-model="draft.capability_models.video_generation" clearable placeholder="请选择视频模型"><el-option v-for="name in videoModelNames" :key="name" :label="name" :value="name" /></el-select></label>
          </div>
          <div class="capability-hint">
            推荐免费组合：GLM-4.6V-Flash 负责视觉理解，CogView-3-Flash 负责生图，CogVideoX-Flash 负责视频生成。
          </div>
        </div>

        <div class="global-card compact-global">
          <div class="global-head"><div><h2>日志与可观测性</h2><p>全局日志策略，对所有模型和路由生效。</p></div><el-tag effect="plain">全局配置</el-tag></div>
          <div class="form-grid three global-fields">
            <label class="field"><span>日志等级</span><el-select v-model="draft.observability.log_level"><el-option v-for="level in ['DEBUG','INFO','WARNING','ERROR']" :key="level" :label="level" :value="level" /></el-select></label>
            <label class="field"><span>Trace 采样率</span><el-input-number v-model="draft.observability.trace_sample_rate" :min="0" :max="1" :step="0.1" controls-position="right" /></label>
            <label class="field"><span>保留天数</span><el-input-number v-model="draft.observability.retain_days" :min="1" :max="365" controls-position="right" /></label>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="语音" name="voice">
        <div class="global-card">
          <div class="global-head">
            <div>
              <h2>语音识别（ASR）</h2>
              <p>语音转文字的提供方。保存即生效，无需重启服务；关闭后语音通道将提示「识别未配置」。</p>
            </div>
            <el-switch v-model="draft.voice.asr.enabled" active-text="启用" />
          </div>
          <template v-if="draft.voice.asr.enabled">
            <div class="form-grid three global-fields">
              <label class="field">
                <span>提供方</span>
                <el-select v-model="draft.voice.asr.provider" @change="onAsrProviderChange">
                  <el-option label="小米 MiMo · 云端" value="mimo" />
                  <el-option label="faster-whisper · 本地" value="faster_whisper" />
                </el-select>
              </label>
              <label class="field"><span>模型</span><el-input v-model="draft.voice.asr.model" :placeholder="draft.voice.asr.provider === 'mimo' ? 'mimo-v2.5-asr' : 'small / large-v3'" /></label>
              <label class="field"><span>识别语种</span><el-select v-model="draft.voice.asr.language"><el-option label="自动检测" value="auto" /><el-option label="中文" value="zh" /><el-option label="英文" value="en" /></el-select></label>
            </div>
            <div v-if="draft.voice.asr.provider === 'mimo'" class="form-grid three global-fields">
              <label class="field"><span>Base URL</span><el-input v-model="draft.voice.asr.base_url" placeholder="https://api.xiaomimimo.com/v1" /></label>
              <label class="field"><span>密钥方式</span><el-select v-model="draft.voice.asr.secret_mode"><el-option label="直接填写" value="value" /><el-option label="环境变量引用" value="ref" /><el-option label="暂不配置" value="none" /></el-select></label>
              <label class="field">
                <span>API Key</span>
                <el-input
                  v-if="draft.voice.asr.secret_mode === 'value'"
                  v-model="draft.voice.asr.secret_value"
                  type="password"
                  show-password
                  placeholder="MiMo API Key"
                />
                <el-input v-else-if="draft.voice.asr.secret_mode === 'ref'" v-model="draft.voice.asr.secret_ref" placeholder="env:MIMO_API_KEY" />
                <el-input v-else model-value="—" disabled />
              </label>
            </div>
            <div v-else class="form-grid three global-fields">
              <label class="field">
                <span>设备</span>
                <el-select v-model="draft.voice.asr.device">
                  <el-option label="自动" value="auto" />
                  <el-option label="CPU" value="cpu" />
                  <el-option label="CUDA" value="cuda" />
                </el-select>
              </label>
              <label class="field"><span>Compute type</span><el-input v-model="draft.voice.asr.compute_type" placeholder="default / int8 / float16" /></label>
              <div class="capability-hint">
                <span>模型在第一次识别时延迟加载；L2 音频只会走该本地识别器。</span>
                <el-button size="small" :loading="testingVoiceAsr" @click="checkLocalAsrEnvironment">检查本地环境</el-button>
              </div>
            </div>
            <div
              v-if="draft.voice.asr.provider === 'faster_whisper' && voiceAsrCheck"
              class="connection-result"
              :class="{ success: voiceAsrCheck.ok, warning: !voiceAsrCheck.ok }"
            >
              <div class="connection-result-head">
                <strong>{{ voiceAsrCheck.message }}</strong>
                <el-tag size="small" :type="voiceAsrCheck.ok ? 'success' : 'warning'">
                  {{ voiceAsrCheck.ok ? '依赖可用' : '待安装' }}
                </el-tag>
              </div>
              <div class="connection-result-meta">
                <span>模型：{{ voiceAsrCheck.model }}</span>
                <span v-if="voiceAsrCheck.package_version">faster-whisper {{ voiceAsrCheck.package_version }}</span>
                <span>模型加载：未执行（自检不会下载/加载模型）</span>
              </div>
            </div>
          </template>
          <div v-else class="capability-hint">语音识别未启用：/ws/voice 通道收到语音会返回 voice.asr_unavailable。</div>
        </div>

        <div class="global-card compact-global">
          <div class="global-head">
            <div>
              <h2>语音合成（TTS）故障转移链</h2>
              <p>按顺序尝试：排在前面的为主通道，首段音频失败或空流时自动切换到下一家并冷却 60 秒。每句的输出格式由实际提供方声明，客户端逐句自适应。</p>
            </div>
            <div>
              <el-button size="small" @click="addVoiceTts('mimo')">+ MiMo</el-button>
              <el-button size="small" @click="addVoiceTts('edge_tts')">+ edge-tts</el-button>
            </div>
          </div>
          <div v-if="!draft.voice.tts.length" class="capability-hint">
            未配置语音合成：语音回合会退化为纯文字（voice.tts_unavailable）。建议至少保留一家兜底（edge-tts 免费且无需密钥）。
          </div>
          <div v-for="(p, index) in draft.voice.tts" :key="index" class="route-card voice-tts-card">
            <div class="route-name">
              <strong>#{{ index + 1 }} {{ p.provider === 'mimo' ? 'MiMo' : 'edge-tts' }}</strong>
              <el-tag v-if="index === 0" size="small" effect="plain">主通道</el-tag>
              <el-tag v-if="!p.enabled" size="small" type="info" effect="plain">已停用</el-tag>
            </div>
            <div class="form-grid three global-fields">
              <label class="field"><span>提供方</span><el-select v-model="p.provider" @change="onTtsProviderChange(p)"><el-option label="小米 MiMo（PCM 直出）" value="mimo" /><el-option label="edge-tts（免费兜底）" value="edge_tts" /></el-select></label>
              <label class="field"><span>音色</span>
                <el-select v-if="p.provider === 'mimo'" v-model="p.voice" filterable allow-create default-first-option placeholder="选择或输入音色"><el-option v-for="v in mimoVoiceOptions" :key="v" :label="v" :value="v" /></el-select>
                <el-select v-else v-model="p.voice" filterable allow-create default-first-option placeholder="选择或输入音色"><el-option v-for="v in edgeVoiceOptions" :key="v" :label="v" :value="v" /></el-select>
              </label>
              <label class="field"><span>启用</span><el-switch v-model="p.enabled" /></label>
            </div>
            <div v-if="p.provider === 'mimo'" class="form-grid three global-fields">
              <label class="field"><span>模型</span><el-input v-model="p.model" placeholder="mimo-v2.5-tts" /></label>
              <label class="field"><span>Base URL</span><el-input v-model="p.base_url" placeholder="https://api.xiaomimimo.com/v1" /></label>
              <label class="field"><span>密钥方式</span><el-select v-model="p.secret_mode"><el-option label="直接填写" value="value" /><el-option label="环境变量引用" value="ref" /></el-select></label>
            </div>
            <div v-if="p.provider === 'mimo' && p.secret_mode === 'value'" class="form-grid three global-fields">
              <label class="field"><span>API Key</span><el-input v-model="p.secret_value" type="password" show-password placeholder="MiMo API Key" /></label>
              <span class="field" />
              <span class="field" />
            </div>
            <div v-else-if="p.provider === 'mimo' && p.secret_mode === 'ref'" class="form-grid three global-fields">
              <label class="field"><span>环境变量引用</span><el-input v-model="p.secret_ref" placeholder="env:MIMO_API_KEY" /></label>
              <span class="field" />
              <span class="field" />
            </div>
            <div class="voice-tts-actions">
              <el-button size="small" :disabled="index === 0" @click="moveVoiceTts(index, -1)">上移</el-button>
              <el-button size="small" :disabled="index === draft.voice.tts.length - 1" @click="moveVoiceTts(index, 1)">下移</el-button>
              <el-button size="small" type="danger" plain @click="removeVoiceTts(index)">删除</el-button>
            </div>
          </div>
          <div class="capability-hint">
            隐私约束：MiMo 与 edge-tts 均为云端服务，不会接收 L2（私密）内容——L2 语音需要本地提供方（后续批次接入）。
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="模型列表" name="models">
        <div class="workspace-card">
          <aside class="models-column">
            <div class="column-head">
              <h2>模型列表</h2>
              <el-button type="primary" size="small" @click="addModel">＋ 添加模型</el-button>
            </div>
            <el-menu class="model-list" :default-active="String(activeModelTab)" @select="key => selectModel(Number(key))">
              <el-menu-item
                v-for="(m, i) in draft.models"
                :key="i"
                :index="String(i)"
                class="model-row"
                :class="{ active: activeModelTab === i }"
              >
                <span class="state-dot" :class="{ off: !m.enabled }"></span>
                <span class="model-copy">
                  <strong>{{ m.key || `模型 ${i + 1}` }}</strong>
                  <small>{{ m.model || '未填写模型名' }}</small>
                  <span class="model-roles">
                    <el-tag size="small" effect="plain" type="info">{{ modelKindLabels[m.kind] }}</el-tag>
                    <el-tag
                      v-for="role in modelUsageLabels(m.key)"
                      :key="role"
                      size="small"
                      effect="plain"
                      :type="role === '未参与路由' ? 'info' : role.includes('备用') ? 'warning' : 'primary'"
                    >{{ role }}</el-tag>
                  </span>
                </span>
                <span class="privacy-pill">{{ m.max_privacy_level }}</span>
              </el-menu-item>
            </el-menu>
            <div class="model-count">共 {{ draft.models.length }} 个模型</div>
          </aside>

          <main v-if="draft.models.length" class="editor">
            <div class="editor-head">
              <div class="editor-title">
                <span class="model-glyph">◇</span>
                <div>
                  <h2>{{ draft.models[activeModelTab].key || `模型 ${activeModelTab + 1}` }}</h2>
                  <p>{{ draft.models[activeModelTab].model || '未填写模型名' }}</p>
                </div>
              </div>
              <div class="editor-actions">
                <el-switch v-model="draft.models[activeModelTab].enabled" inline-prompt active-text="启用" inactive-text="停用" />
                <el-checkbox v-model="draft.models[activeModelTab].runs_local" border>本地</el-checkbox>
                <el-button type="danger" plain @click="removeModel(activeModelTab)">删除模型</el-button>
              </div>
            </div>

              <el-tabs v-model="activeSection" class="section-tabs">
                <el-tab-pane label="基本信息" name="basic">
                  <div class="editor-body form-section">
                  <div class="section-title"><h3>基本信息</h3><p>用于识别模型以及约束数据可发送的最高隐私等级。</p></div>
                  <div class="form-grid three">
                    <label class="field"><span>配置名称</span><el-input v-model="draft.models[activeModelTab].key" placeholder="sensenova_dialogue" /></label>
                    <label class="field"><span>模型名</span><el-input v-model="draft.models[activeModelTab].model" placeholder="deepseek-v4-flash" /></label>
                    <label class="field"><span>模型类型</span><el-select v-model="draft.models[activeModelTab].kind"><el-option v-for="item in modelKindOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select></label>
                    <label class="field"><span>隐私上限</span><el-select v-model="draft.models[activeModelTab].max_privacy_level"><el-option v-for="level in ['L0','L1','L2','L3']" :key="level" :label="level" :value="level" /></el-select></label>
                  </div>
                  <div class="option-row">
                    <el-checkbox v-model="draft.models[activeModelTab].supports_json_mode">支持 JSON Mode</el-checkbox>
                    <el-checkbox v-model="draft.models[activeModelTab].runs_local">本地运行</el-checkbox>
                  </div>
                  <div v-if="draft.models[activeModelTab].kind === 'text'" class="form-grid three">
                    <label class="field"><span>思考模式</span><el-select v-model="draft.models[activeModelTab].thinking_mode"><el-option label="Provider 默认" value="provider_default" /><el-option label="开启思考" value="enabled" /><el-option label="关闭思考" value="disabled" /></el-select></label>
                  </div>
                  </div>
                </el-tab-pane>

                <el-tab-pane label="连接配置" name="connection">
                  <div class="editor-body form-section">
                  <div class="section-title connection-title">
                    <div><h3>连接信息</h3><p>配置 Provider、接口地址与认证方式。</p></div>
                    <el-button type="primary" plain :loading="testingConnection" @click="testModelConnection">测试连接</el-button>
                  </div>
                  <div class="form-grid two">
                    <label class="field"><span>Provider</span><el-input v-model="draft.models[activeModelTab].provider" /></label>
                    <label class="field"><span>Base URL</span><el-input v-model="draft.models[activeModelTab].base_url" placeholder="https://token.sensenova.cn/v1" /></label>
                    <label class="field"><span>密钥方式</span><el-select v-model="draft.models[activeModelTab].secret_mode"><el-option label="直接填写" value="value" /><el-option label="环境变量引用" value="ref" /><el-option label="无密钥" value="none" /></el-select></label>
                    <label v-if="draft.models[activeModelTab].secret_mode === 'value'" class="field"><span>API Key</span><el-input v-model="draft.models[activeModelTab].secret_value" type="password" show-password placeholder="sk-..." /></label>
                    <label v-else-if="draft.models[activeModelTab].secret_mode === 'ref'" class="field"><span>环境变量</span><el-input v-model="draft.models[activeModelTab].secret_ref" placeholder="env:SENSENOVA_API_KEY" /></label>
                  </div>
                  <div v-if="draft.models[activeModelTab].runs_local" class="local-probe-hint">
                    <strong>本地服务探针</strong>
                    <code>{{ localModelsProbeUrl(draft.models[activeModelTab].base_url) }}</code>
                    <span>测试只读取模型列表，不会自动加载或下载模型。若使用 LM Studio + openai_compatible，运行时 Base URL 请填写 http://127.0.0.1:1234/v1。</span>
                  </div>
                  <div
                    v-if="connectionTest"
                    class="connection-result"
                    :class="{
                      success: connectionTest.ok && connectionTest.model_loaded !== false && connectionTest.model_available !== false,
                      warning: connectionTest.ok && (connectionTest.model_loaded === false || connectionTest.model_available === false),
                      error: !connectionTest.ok,
                    }"
                  >
                    <div class="connection-result-head">
                      <strong>{{ connectionTest.message }}</strong>
                      <span>{{ Math.round(connectionTest.latency_ms) }} ms</span>
                    </div>
                    <div class="connection-result-meta">
                      <span>探针：{{ connectionTest.probe_kind }}</span>
                      <span>目标：<code>{{ connectionTest.target_url }}</code></span>
                      <span v-if="connectionTest.error_type">错误：{{ connectionTest.error_type }}</span>
                    </div>
                    <div v-if="connectionTest.models.length" class="probe-models">
                      <span>本机模型</span>
                      <el-tag
                        v-for="item in connectionTest.models"
                        :key="item.key"
                        size="small"
                        effect="plain"
                        :type="item.loaded_instances > 0 ? 'success' : 'info'"
                      >
                        {{ item.display_name }}{{ item.loaded_instances > 0 ? ` · 已加载 ${item.loaded_instances}` : '' }}
                      </el-tag>
                    </div>
                  </div>
                  </div>
                </el-tab-pane>

                <el-tab-pane label="运行参数" name="runtime">
                  <div class="editor-body form-section">
                  <div class="section-title"><h3>当前模型运行参数</h3><p>只作用于当前选中的模型：请求超时、重试、上下文上限与成本估算。</p></div>
                  <div class="form-grid three">
                    <label class="field"><span>超时（ms）</span><el-input-number v-model="draft.models[activeModelTab].timeout_ms" :min="100" :max="120000" controls-position="right" /></label>
                    <label class="field"><span>重试次数</span><el-input-number v-model="draft.models[activeModelTab].max_retries" :min="0" :max="3" controls-position="right" /></label>
                    <label class="field"><span>max_tokens</span><el-input-number v-model="draft.models[activeModelTab].max_tokens" :min="1" :max="131072" controls-position="right" /></label>
                    <label class="field"><span>上下文 tokens</span><el-input-number v-model="draft.models[activeModelTab].max_context_tokens" :min="1" controls-position="right" /></label>
                    <label class="field"><span>输入成本 / M tokens</span><el-input-number v-model="draft.models[activeModelTab].input_cost_per_million" :min="0" :step="0.01" controls-position="right" /></label>
                    <label class="field"><span>输出成本 / M tokens</span><el-input-number v-model="draft.models[activeModelTab].output_cost_per_million" :min="0" :step="0.01" controls-position="right" /></label>
                  </div>
                  </div>
                </el-tab-pane>
              </el-tabs>
          </main>

          <div v-else class="empty-editor"><el-empty description="还没有模型"><el-button type="primary" @click="addModel">添加模型</el-button></el-empty></div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.models-page { --ink:#172033; --sub:#68748a; --border:#e3e8f2; --surface:#fff; --soft:#f7f9fd; --brand:#4f6df5; --brand-soft:#eef2ff; --green:#26b873; color:var(--ink); background:#f6f8fc; min-height:0; height:100%; padding:28px 30px 0; display:flex; flex-direction:column; gap:18px; overflow:hidden; }
.page-intro { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; flex-shrink:0; }
.page-intro h1 { margin:0; font-size:24px; letter-spacing:-.02em; }
.page-intro p { margin:7px 0 0; color:var(--sub); font-size:13px; }
.intro-meta { display:flex; align-items:center; gap:20px; }
.intro-meta>div { display:grid; gap:5px; min-width:100px; }
.intro-meta span { color:var(--sub); font-size:11px; }
.intro-meta strong,.intro-meta code { color:var(--ink); font-size:13px; }
.summary-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; flex-shrink:0; }
.summary-grid article { min-height:90px; display:flex; align-items:center; gap:13px; padding:18px; background:var(--surface); border:1px solid var(--border); border-radius:12px; box-shadow:0 5px 18px rgba(36,50,82,.035); }
.summary-grid article>div { display:grid; gap:5px; }
.summary-grid small { color:var(--sub); font-size:11px; }
.summary-grid strong { font-size:19px; }
.summary-grid b { margin-left:auto; color:#15935b; background:#e9f8f0; padding:3px 8px; border-radius:999px; font-size:10px; }
.summary-icon,.model-glyph { display:grid; place-items:center; width:42px; height:42px; border-radius:12px; color:var(--brand); background:var(--brand-soft); font-size:22px; flex:0 0 auto; }
.workspace-card,.global-card { background:var(--surface); border:1px solid var(--border); border-radius:13px; box-shadow:0 8px 24px rgba(36,50,82,.035); }
.workspace-card { display:grid; grid-template-columns:330px minmax(0,1fr); min-height:0; height:100%; overflow:hidden; box-sizing:border-box; }
.models-column { min-width:0; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:auto; }
.column-head { min-height:68px; padding:16px 18px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--border); }
.column-head h2,.editor h2 { margin:0; font-size:15px; }
.model-list { display:grid; align-content:start; border-right:0; background:#fff; }
.model-row { width:100%; height:auto; line-height:normal; border:0; border-bottom:1px solid #edf0f6; border-radius:0; background:white; color:var(--ink); display:grid; grid-template-columns:8px minmax(0,1fr) auto; gap:12px; align-items:center; text-align:left; padding:18px 18px !important; }
.model-row:hover { background:#fafbff; }
.model-row.active,.model-row.is-active { color:var(--ink); background:#f4f6ff; box-shadow:inset 3px 0 var(--brand); }
.state-dot { width:8px; height:8px; border-radius:50%; background:var(--green); }
.state-dot.off { background:#b8c0d0; }
.model-copy { min-width:0; display:grid; gap:5px; }
.model-copy strong,.model-copy small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.model-copy strong { font-size:12px; }
.model-copy small { color:var(--sub); font-size:10px; }
.model-roles { display:flex; gap:4px; flex-wrap:wrap; margin-top:2px; }
.model-roles :deep(.el-tag) { height:18px; padding:0 5px; font-size:9px; }
.privacy-pill { color:#5066d9; background:#f0f3ff; border-radius:5px; padding:4px 7px; font-size:10px; }
.model-count { margin-top:auto; padding:18px; color:var(--sub); font-size:11px; border-top:1px solid var(--border); }
.editor { min-width:0; display:grid; grid-template-rows:auto minmax(0,1fr) auto; height:100%; }
.editor-head { min-height:86px; padding:18px 24px; display:flex; align-items:center; justify-content:space-between; gap:20px; }
.editor-title { display:flex; align-items:center; gap:13px; min-width:0; }
.editor-title p { margin:5px 0 0; color:var(--sub); font-size:11px; }
.editor-actions { display:flex; align-items:center; gap:9px; }
.section-tabs { min-width:0; height:100%; display:flex; flex-direction:column; }
.section-tabs :deep(.el-tabs) { display:flex; flex-direction:column; height:100%; }
.section-tabs :deep(.el-tabs__header) { margin:0; padding:0 24px; border-bottom:1px solid var(--border); flex-shrink:0; }
.section-tabs :deep(.el-tabs__nav-wrap::after) { display:none; }
.section-tabs :deep(.el-tabs__item) { height:46px; padding:0 18px; color:#60708a; font-size:12px; }
.section-tabs :deep(.el-tabs__item.is-active) { color:var(--brand); font-weight:600; }
.section-tabs :deep(.el-tabs__content) { flex:1; overflow:auto; min-height:0; }
.main-tabs { min-width:0; flex:1; overflow:hidden; display:flex; flex-direction:column; }
.main-tabs :deep(.el-tabs) { display:flex; flex-direction:column; height:100%; }
.main-tabs :deep(.el-tabs__header) { margin:0 0 14px; border-bottom:1px solid var(--border); flex-shrink:0; }
.main-tabs :deep(.el-tabs__nav-wrap::after) { display:none; }
.main-tabs :deep(.el-tabs__item) { height:46px; padding:0 18px; color:#60708a; font-size:13px; }
.main-tabs :deep(.el-tabs__item.is-active) { color:var(--brand); font-weight:600; }
.main-tabs :deep(.el-tabs__content) { flex:1; overflow:auto; min-height:0; display:flex; flex-direction:column; }
.main-tabs :deep(.el-tab-pane) { flex:1; min-height:0; }
.main-tabs :deep(.el-tabs__active-bar) { background:var(--brand); height:2px; }
.editor-body { padding:26px 26px 30px; }
.form-section { display:grid; gap:22px; }
.section-title h3 { margin:0; font-size:14px; }
.section-title p { margin:6px 0 0; color:var(--sub); font-size:11px; }
.connection-title { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; }
.local-probe-hint { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:11px 13px; border:1px dashed #d7def0; border-radius:9px; background:#fafbff; color:var(--sub); font-size:10px; }
.local-probe-hint strong { color:#44516a; }
.local-probe-hint code { color:#4058ca; background:#eef2ff; padding:3px 6px; border-radius:5px; }
.connection-result { display:grid; gap:9px; padding:13px 14px; border:1px solid var(--border); border-radius:10px; background:#fbfcff; }
.connection-result.success { border-color:#bfe8d2; background:#f3fbf7; }
.connection-result.warning { border-color:#efdba8; background:#fffaf0; }
.connection-result.error { border-color:#efc2c2; background:#fff6f6; }
.connection-result-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.connection-result-head strong { font-size:12px; }
.connection-result-head span { color:var(--sub); font-size:10px; }
.connection-result-meta { display:flex; gap:12px; flex-wrap:wrap; color:var(--sub); font-size:10px; }
.connection-result-meta code { color:#4058ca; }
.probe-models { display:flex; align-items:center; gap:6px; flex-wrap:wrap; padding-top:2px; }
.probe-models>span { color:var(--sub); font-size:10px; margin-right:2px; }
.form-grid { display:grid; gap:18px 20px; }
.form-grid.two { grid-template-columns:repeat(2,minmax(0,1fr)); }
.form-grid.three { grid-template-columns:repeat(3,minmax(0,1fr)); }
.field { min-width:0; display:grid; gap:7px; }
.field span { color:#5f6c82; font-size:10px; }
.field :deep(.el-select),.field :deep(.el-input-number) { width:100%; }
.option-row { display:flex; gap:20px; color:#4b5870; font-size:11px; padding-top:4px; }
.option-row :deep(.el-checkbox) { margin-right:0; }
.route-list { display:grid; gap:12px; }
.route-card { display:grid; grid-template-columns:90px minmax(180px,1fr) minmax(260px,1.35fr) 120px; gap:14px; align-items:start; padding:18px; border:1px solid var(--border); border-radius:10px; background:#fbfcff; }
.voice-tts-card { display:block; }
.voice-tts-card .route-name { display:flex; gap:10px; align-items:center; margin-bottom:12px; }
.voice-tts-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:12px; }
.route-name { display:grid; gap:4px; padding-top:4px; }
.route-name strong { font-size:12px; }
.route-name span { color:var(--sub); font-size:9px; }
.global-card { padding:20px; display:grid; gap:18px; }
.global-head { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }
.global-head h2 { margin:0; font-size:15px; }
.global-head p { margin:6px 0 0; color:var(--sub); font-size:11px; line-height:1.6; }
.global-fields { max-width:900px; }
.compact-global { padding-bottom:22px; }
.capability-hint { color:var(--sub); background:var(--soft); border:1px dashed var(--border); border-radius:9px; padding:10px 12px; font-size:11px; line-height:1.6; }
.json-card { padding:0; overflow:auto; flex:1; min-height:0; }
.json-card .global-head { padding:20px 22px 0; }
.json-editor { padding:22px 24px; }
.json-editor :deep(textarea) { min-height:410px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.55; }
.empty-editor { display:grid; place-items:center; align-content:center; gap:8px; color:var(--sub); min-height:0; height:100%; }
.empty-editor strong { color:var(--ink); }
.empty-editor p { margin:0 0 6px; font-size:12px; }

.live-status { color:#15935b !important; }

@media (max-width:1180px) {
  .summary-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .workspace-card { grid-template-columns:270px minmax(0,1fr); }
  .form-grid.three { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .route-card { grid-template-columns:80px 1fr 1fr; }
  .route-card .field:last-child { grid-column:2 / -1; }
}
@media (max-width:820px) {
  .models-page { padding:18px; }
  .page-intro { flex-direction:column; }
  .intro-meta { width:100%; justify-content:space-between; }
  .workspace-card { grid-template-columns:1fr; }
  .models-column { border-right:0; border-bottom:1px solid var(--border); }
  .model-list { display:flex; overflow-x:auto; }
  .model-row { min-width:210px; border-right:1px solid #edf0f6; }
  .model-count { display:none; }
  .editor-head { align-items:flex-start; flex-direction:column; }
  .form-grid.two,.form-grid.three,.route-card { grid-template-columns:1fr; }
  .route-card .field:last-child { grid-column:auto; }
}
</style>
