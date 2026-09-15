<script setup lang="ts">
import { inject, onActivated, onBeforeUnmount, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

// 声音管理：SenseAudio 连接配置、音色库、试听合成与识别历史。
// 密钥只保存在 Hub 配置中心；已配置时输入框留空即保持原值（回传掩码由后端还原）。
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "sound" });

const SECRET_MASK = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__";

interface StatusResponse {
  enabled: boolean;
  key_configured: boolean;
  key_source: "inline" | "env" | "none";
  base_url: string;
  tts_model: string;
}

interface VoiceItem {
  category: string;
  voice_id: string;
  voice_name: string;
  description: string[];
  created_time: string | null;
  free_tier?: boolean;
}

interface AsrRecord {
  session_id: string;
  session_start: number;
  session_end: number;
  text: string;
  points: number;
  audio: string;
}

interface PreviewResult {
  audio_base64: string;
  audio_format: string;
  sample_rate: number | null;
  usage_characters: number | null;
  audio_length: number | null;
  audio_size: number | null;
}

interface CloneFileResult {
  file_id: string;
  filename: string;
  size_bytes: number;
}

interface CloneResult {
  label: string;
  name: string;
  description: string;
  created_at: number | null;
  demo: string | null;
}

const status = ref<StatusResponse | null>(null);
const saving = ref(false);
const form = ref({ enabled: false, base_url: "https://api.senseaudio.cn", secret_value: "", secret_ref: "", tts_model: "sensenova-tts-2.0" });

const voices = ref<VoiceItem[]>([]);
const voicesLoading = ref(false);
const voiceCategory = ref<"all" | "system" | "voice_clone" | "voice_generation">("all");
const voiceKeyword = ref("");
const freeOnly = ref(false);

const previewForm = ref({ voice_id: "", text: "你好呀，今天过得怎么样？我给你读一段刚刚看到的有趣内容。", speed: 1.0, vol: 1.0, pitch: 0, audio_format: "mp3", sample_rate: 32000 });
const previewLoading = ref(false);
const previewUrl = ref("");
const previewMeta = ref<{ usage_characters: number | null; audio_length: number | null; audio_size: number | null } | null>(null);

const cloneFile = ref<File | null>(null);
const cloneUploaded = ref<CloneFileResult | null>(null);
const cloneForm = ref({ label: "", description: "", text: "你好，这是我的克隆音色，听听像不像。" });
const cloneLoading = ref(false);
const cloneResult = ref<CloneResult | null>(null);

const records = ref<AsrRecord[]>([]);
const recordsTotal = ref(0);
const recordsPage = ref(1);
const recordsPageSize = ref(20);
const recordsLoading = ref(false);
const recordsSessionId = ref("");

const categoryLabels: Record<string, string> = {
  system: "系统音色",
  voice_clone: "克隆音色",
  voice_generation: "生成音色",
};

function fmtTime(seconds: number | null | undefined) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

function fmtDescription(item: VoiceItem) {
  return item.description.join(" · ") || "—";
}

async function loadStatus() {
  try {
    status.value = await api.request<StatusResponse>("/api/v1/admin/senseaudio/status");
    form.value.enabled = status.value.enabled;
    form.value.base_url = status.value.base_url;
    form.value.tts_model = status.value.tts_model;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "SenseAudio 状态加载失败", true);
  }
}

async function saveConnection() {
  saving.value = true;
  try {
    const secret = form.value.secret_value.trim();
    status.value = await api.request<StatusResponse>("/api/v1/admin/senseaudio/connection", {
      method: "PUT",
      body: JSON.stringify({
        enabled: form.value.enabled,
        base_url: form.value.base_url,
        secret_value: secret || (status.value?.key_source === "inline" ? SECRET_MASK : null),
        secret_ref: form.value.secret_ref.trim() || null,
        tts_model: form.value.tts_model,
      }),
    });
    form.value.secret_value = "";
    ElMessage.success("SenseAudio 连接已保存并生效");
    emit("status", "SenseAudio 连接已保存");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  } finally {
    saving.value = false;
  }
}

async function loadVoices() {
  voicesLoading.value = true;
  try {
    const result = await api.request<{ voices: VoiceItem[] }>(
      `/api/v1/admin/senseaudio/voices?voice_type=${voiceCategory.value}`,
    );
    voices.value = result.voices;
    emit("status", `音色目录已刷新，共 ${result.voices.length} 个`);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "音色目录加载失败", true);
  } finally {
    voicesLoading.value = false;
  }
}

/** 连通性探测：直接拉一次系统音色目录 */
async function testConnection() {
  voicesLoading.value = true;
  try {
    const result = await api.request<{ voices: VoiceItem[] }>(
      "/api/v1/admin/senseaudio/voices?voice_type=system",
    );
    ElMessage.success(`连接成功，系统音色 ${result.voices.length} 个`);
    emit("status", "SenseAudio 连接正常");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "连接失败");
    emit("status", error instanceof Error ? error.message : "连接失败", true);
  } finally {
    voicesLoading.value = false;
  }
}

function useVoice(item: VoiceItem) {
  previewForm.value.voice_id = item.voice_id;
  ElMessage.info(`已选择音色：${item.voice_name}`);
}

function onCloneFileChange(uploadFile: { raw?: File }) {
  cloneResult.value = null;
  cloneUploaded.value = null;
  const raw = uploadFile.raw;
  if (!raw) return;
  const parts = raw.name.split(".");
  const ext = parts.length > 1 ? parts[parts.length - 1].toLowerCase() : "";
  if (!["mp3", "aac", "wav"].includes(ext)) {
    ElMessage.error("仅支持 MP3/AAC/WAV 格式");
    cloneFile.value = null;
    return;
  }
  if (raw.size > 50 * 1024 * 1024) {
    ElMessage.error("文件超过 50MB 上限");
    cloneFile.value = null;
    return;
  }
  cloneFile.value = raw;
}

async function runClone() {
  if (!cloneFile.value) {
    ElMessage.warning("请先选择参考音频（3-30 秒清晰人声）");
    return;
  }
  if (!cloneForm.value.label.trim() || !cloneForm.value.description.trim()) {
    ElMessage.warning("请填写音色标签和描述");
    return;
  }
  cloneLoading.value = true;
  try {
    if (!cloneUploaded.value) {
      const form = new FormData();
      form.append("file", cloneFile.value);
      cloneUploaded.value = await api.request<CloneFileResult>(
        "/api/v1/admin/senseaudio/clone/upload",
        { method: "POST", body: form },
      );
    }
    cloneResult.value = await api.request<CloneResult>("/api/v1/admin/senseaudio/clone", {
      method: "POST",
      body: JSON.stringify({
        file_id: cloneUploaded.value.file_id,
        label: cloneForm.value.label.trim(),
        description: cloneForm.value.description.trim(),
        text: cloneForm.value.text,
      }),
    });
    ElMessage.success(`克隆完成，音色 ID：${cloneResult.value.label}`);
    emit("status", `音色克隆完成：${cloneResult.value.label}`);
  } catch (error) {
    cloneUploaded.value = null; // 上传/克隆任一失败都重置，下次重传
    ElMessage.error(error instanceof Error ? error.message : "克隆失败");
    emit("status", error instanceof Error ? error.message : "克隆失败", true);
  } finally {
    cloneLoading.value = false;
  }
}

async function runPreview() {
  if (!previewForm.value.voice_id) {
    ElMessage.warning("请先选择要试听的音色");
    return;
  }
  previewLoading.value = true;
  try {
    const result = await api.request<PreviewResult>("/api/v1/admin/senseaudio/preview", {
      method: "POST",
      body: JSON.stringify(previewForm.value),
    });
    const binary = atob(result.audio_base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
    previewUrl.value = URL.createObjectURL(new Blob([bytes], { type: `audio/${result.audio_format}` }));
    previewMeta.value = {
      usage_characters: result.usage_characters,
      audio_length: result.audio_length,
      audio_size: result.audio_size,
    };
    emit("status", "试听已生成");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "试听失败");
    emit("status", error instanceof Error ? error.message : "试听失败", true);
  } finally {
    previewLoading.value = false;
  }
}

async function loadRecords() {
  recordsLoading.value = true;
  try {
    const params = new URLSearchParams({
      page: String(recordsPage.value),
      page_size: String(recordsPageSize.value),
    });
    const session = recordsSessionId.value.trim();
    if (session) params.set("session_id", session);
    const result = await api.request<{ total: number; records: AsrRecord[] }>(
      `/api/v1/admin/senseaudio/asr/records?${params.toString()}`,
    );
    records.value = result.records;
    recordsTotal.value = result.total;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "识别历史加载失败", true);
  } finally {
    recordsLoading.value = false;
  }
}

function filteredVoices() {
  const keyword = voiceKeyword.value.trim().toLowerCase();
  return voices.value.filter((item) => {
    if (freeOnly.value && !item.free_tier) return false;
    if (!keyword) return true;
    return (
      item.voice_name.toLowerCase().includes(keyword)
      || item.voice_id.toLowerCase().includes(keyword)
      || item.description.some((part) => part.toLowerCase().includes(keyword))
    );
  });
}

onActivated(() => {
  void loadStatus();
});
onBeforeUnmount(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
});
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">声音管理 · SENSEAUDIO</div>
        <h2>音色库与试听合成</h2>
        <p>Hub 代理 SenseAudio 开放平台：浏览可用音色、按文本合成试听、回看语音识别历史。API Key 只保存在服务端配置中心。</p>
      </div>
      <el-button :loading="saving" @click="loadStatus">刷新</el-button>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>服务连接</h2><p>修改后保存即热生效；已配置 Key 时留空表示保持不变。</p></div>
        <div class="head-actions">
          <el-button :loading="voicesLoading" @click="testConnection">测试连接</el-button>
          <el-button type="primary" :loading="saving" @click="saveConnection">保存</el-button>
        </div>
      </div>
      <div class="form-grid">
        <label><span>启用</span><el-switch v-model="form.enabled" /></label>
        <label><span>Base URL</span><el-input v-model="form.base_url" placeholder="https://api.senseaudio.cn" /></label>
        <label>
          <span>API Key</span>
          <el-input
            v-model="form.secret_value"
            type="password"
            show-password
            :placeholder="status?.key_source === 'inline' ? '已配置，留空保持不变' : status?.key_source === 'env' ? '由环境变量提供，填写可覆盖' : 'Bearer API Key'"
          />
        </label>
        <label><span>Key 环境变量引用</span><el-input v-model="form.secret_ref" placeholder="env:SENSEAUDIO_API_KEY（可选）" /></label>
        <label>
          <span>TTS 模型</span>
          <el-select v-model="form.tts_model">
            <el-option label="sensenova-tts-2.0（推荐）" value="sensenova-tts-2.0" />
            <el-option label="senseaudio-tts-1.5-260319" value="senseaudio-tts-1.5-260319" />
          </el-select>
        </label>
      </div>
      <p class="key-state">
        当前状态：{{ status?.enabled ? "已启用" : "未启用" }} ·
        {{ status?.key_source === "inline" ? "Key 已配置（配置中心）" : status?.key_source === "env" ? "Key 来自环境变量" : "Key 未配置" }}
      </p>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>音色库</h2><p>系统 / 克隆 / 生成音色目录；「Free 可用」为普通音色档，Free 套餐只能合成这些音色。点击「试听」带入下方合成表单。</p></div>
        <div class="head-actions">
          <el-select v-model="voiceCategory" style="width: 140px" @change="loadVoices">
            <el-option label="全部音色" value="all" />
            <el-option label="系统音色" value="system" />
            <el-option label="克隆音色" value="voice_clone" />
            <el-option label="生成音色" value="voice_generation" />
          </el-select>
          <el-input v-model="voiceKeyword" placeholder="搜索名称 / ID / 描述" clearable style="width: 200px" />
          <el-checkbox v-model="freeOnly">仅 Free 可用</el-checkbox>
          <el-button :loading="voicesLoading" @click="loadVoices">加载</el-button>
        </div>
      </div>
      <el-table v-loading="voicesLoading" :data="filteredVoices()" empty-text="点击「加载」拉取音色目录" max-height="420">
        <el-table-column label="分类" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.category === 'system' ? 'primary' : row.category === 'voice_clone' ? 'success' : 'warning'">
              {{ categoryLabels[row.category] ?? row.category }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="套餐" width="100">
          <template #default="{ row }">
            <el-tag v-if="(row as VoiceItem).free_tier" size="small" type="success" effect="plain">Free 可用</el-tag>
            <el-tag v-else-if="row.category === 'voice_clone'" size="small" type="info" effect="plain">自定义</el-tag>
            <span v-else class="tier-muted">更高套餐</span>
          </template>
        </el-table-column>
        <el-table-column prop="voice_name" label="名称" min-width="150" />
        <el-table-column label="voice_id" min-width="190">
          <template #default="{ row }"><code>{{ row.voice_id }}</code></template>
        </el-table-column>
        <el-table-column label="描述" min-width="280">
          <template #default="{ row }">{{ fmtDescription(row as VoiceItem) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ row.created_time ?? "—" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="useVoice(row as VoiceItem)">试听</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>试听合成</h2><p>按字符计费；合成音频仅在浏览器内临时播放，不做持久化。</p></div>
        <el-button type="primary" :loading="previewLoading" @click="runPreview">合成试听</el-button>
      </div>
      <div class="form-grid">
        <label><span>voice_id</span><el-input v-model="previewForm.voice_id" placeholder="从音色库点击「试听」带入" /></label>
        <label><span>音频格式</span>
          <el-select v-model="previewForm.audio_format">
            <el-option label="mp3" value="mp3" />
            <el-option label="wav" value="wav" />
            <el-option label="flac" value="flac" />
          </el-select>
        </label>
        <label><span>采样率</span>
          <el-select v-model="previewForm.sample_rate">
            <el-option v-for="rate in [16000, 22050, 24000, 32000, 44100]" :key="rate" :label="String(rate)" :value="rate" />
          </el-select>
        </label>
        <label class="wide"><span>试听文本</span>
          <el-input v-model="previewForm.text" type="textarea" :rows="3" maxlength="500" show-word-limit />
        </label>
      </div>
      <div class="sliders">
        <label><span>语速 {{ previewForm.speed.toFixed(2) }}</span><el-slider v-model="previewForm.speed" :min="0.5" :max="2" :step="0.05" /></label>
        <label><span>音量 {{ previewForm.vol.toFixed(2) }}</span><el-slider v-model="previewForm.vol" :min="0.01" :max="10" :step="0.01" /></label>
        <label><span>音调 {{ previewForm.pitch }}</span><el-slider v-model="previewForm.pitch" :min="-12" :max="12" :step="1" /></label>
      </div>
      <div v-if="previewUrl" class="player">
        <audio :src="previewUrl" controls />
        <small v-if="previewMeta">
          计费字符 {{ previewMeta.usage_characters ?? "—" }} · 时长 {{ previewMeta.audio_length != null ? `${Math.round(previewMeta.audio_length)}s` : "—" }} · 大小 {{ previewMeta.audio_size != null ? `${(previewMeta.audio_size / 1024).toFixed(1)} KB` : "—" }}
        </small>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>音色克隆</h2><p>上传 3-30 秒清晰人声（MP3/AAC/WAV，50MB 内），生成专属音色。克隆与文生音色共享套餐额度（Free 每期 2 次，超出 9.9 元/次）。</p></div>
        <el-button type="primary" :loading="cloneLoading" @click="runClone">上传并克隆</el-button>
      </div>
      <div class="clone-grid">
        <div class="clone-upload">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            accept=".mp3,.aac,.wav"
            :on-change="onCloneFileChange"
          >
            <div class="upload-hint">拖拽或点击选择参考音频</div>
            <template #tip>
              <div class="upload-tip">3-30 秒安静环境录制的人声效果最好</div>
            </template>
          </el-upload>
          <p v-if="cloneUploaded" class="upload-tip ok">已上传：{{ cloneUploaded.filename }}（{{ (cloneUploaded.size_bytes / 1024).toFixed(0) }} KB）</p>
        </div>
        <div class="clone-form">
          <label><span>音色标签（即 voice_id，字母/数字/中划线）</span><el-input v-model="cloneForm.label" placeholder="my_cloned_voice" maxlength="64" /></label>
          <label><span>音色描述</span><el-input v-model="cloneForm.description" placeholder="例如：温柔的青年女声，语速平缓" maxlength="500" /></label>
          <label><span>试听示例文本</span><el-input v-model="cloneForm.text" type="textarea" :rows="2" maxlength="500" /></label>
        </div>
      </div>
      <el-alert
        v-if="cloneResult"
        :closable="false"
        type="success"
        :title="`克隆完成：${cloneResult.name}（voice_id = ${cloneResult.label}）`"
        description="克隆音色已生成，可直接在下方试听 demo，或刷新音色库后用中文文本合成。"
      />
      <div v-if="cloneResult?.demo" class="clone-demo">
        <span class="clone-demo-label">克隆 demo</span>
        <audio :src="cloneResult.demo" controls preload="metadata" />
        <el-link :href="cloneResult.demo" target="_blank" type="primary">新窗口打开</el-link>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>识别历史</h2><p>语音识别调用记录，平台侧保留最近 7 天；音频为外部链接，点击在新窗口打开。</p></div>
        <div class="head-actions">
          <el-input v-model="recordsSessionId" placeholder="按 session_id 过滤" clearable style="width: 220px" />
          <el-button :loading="recordsLoading" @click="recordsPage = 1; loadRecords()">查询</el-button>
        </div>
      </div>
      <el-table v-loading="recordsLoading" :data="records" empty-text="暂无识别记录">
        <el-table-column label="开始" width="170"><template #default="{ row }">{{ fmtTime(row.session_start) }}</template></el-table-column>
        <el-table-column label="结束" width="170"><template #default="{ row }">{{ fmtTime(row.session_end) }}</template></el-table-column>
        <el-table-column label="会话" min-width="170">
          <template #default="{ row }"><code>{{ row.session_id }}</code></template>
        </el-table-column>
        <el-table-column prop="text" label="识别文本" min-width="260" />
        <el-table-column label="积分" width="80"><template #default="{ row }">{{ row.points }}</template></el-table-column>
        <el-table-column label="音频" width="80">
          <template #default="{ row }">
            <el-link v-if="row.audio" :href="row.audio" target="_blank" type="primary">打开</el-link>
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="recordsPage"
          v-model:page-size="recordsPageSize"
          :total="recordsTotal"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="loadRecords"
          @size-change="recordsPage = 1; loadRecords()"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p, .panel-head p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 12px; }
.head-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 16px; }
.form-grid label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
.form-grid label.wide { grid-column: 1 / -1; }
.key-state { margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.sliders { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-top: 14px; }
.sliders label { display: grid; gap: 2px; font-size: 12px; color: var(--muted); }
.player { margin-top: 14px; display: grid; gap: 6px; }
.player audio { width: 100%; max-width: 560px; }
.player small { color: var(--muted); }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.tier-muted { color: var(--muted); font-size: 12px; }
.clone-grid { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(280px, 1.4fr); gap: 18px; }
.clone-form { display: grid; gap: 12px; align-content: start; }
.clone-form label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
.upload-hint { padding: 22px 10px; color: var(--muted); font-size: 13px; }
.upload-tip { color: var(--muted); font-size: 12px; margin-top: 6px; }
.upload-tip.ok { color: var(--el-color-success); }
.clone-demo { margin-top: 14px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.clone-demo-label { color: var(--muted); font-size: 12px; }
.clone-demo audio { width: min(420px, 100%); }
@media (max-width: 900px) { .clone-grid { grid-template-columns: 1fr; } }
</style>
