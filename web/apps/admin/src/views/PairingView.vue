<script setup lang="ts">
import { inject, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface DeviceItem {
  id: string;
  name: string;
  alias: string | null;
  client_type: string;
  granted_capabilities: string[];
  effective_capabilities: string[];
  online: boolean;
  revoked_at: string | null;
  paired_at: string;
  last_seen_at: string;
}

interface PairingCodeResult {
  pairing_code: string;
  owner_user_id: string;
  granted_capabilities: string[];
  expires_at: string;
}

const devices = ref<DeviceItem[]>([]);
const loading = ref(false);
const creating = ref(false);
const result = ref<PairingCodeResult | null>(null);

const knownCapabilities = [
  "device.ping",
  "avatar.render",
  "screen.capture",
  "notification.show",
  "browser.current_tab.read",
  "browser.current_tab.capture",
  "sensor.read",
];

const clientTypeLabels: Record<string, string> = {
  desktop: "桌面客户端",
  browser: "浏览器",
  mobile: "移动端",
  iot: "IoT",
  other: "其他",
};

const form = reactive({
  ownerUserId: "",
  ttlSeconds: 600,
  grants: ["device.ping"] as string[],
});

async function loadDevices() {
  loading.value = true;
  try {
    devices.value = await api.request<DeviceItem[]>("/api/v1/admin/devices");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function createPairingCode() {
  creating.value = true;
  try {
    const payload: Record<string, unknown> = {
      granted_capabilities: form.grants,
      ttl_seconds: form.ttlSeconds,
    };
    if (form.ownerUserId.trim()) {
      payload.owner_user_id = form.ownerUserId.trim();
    }
    result.value = await api.request<PairingCodeResult>("/api/v1/admin/devices/pairing-codes", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    ElMessage.success("配对码已生成");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "生成失败", true);
  } finally {
    creating.value = false;
  }
}

onMounted(loadDevices);
</script>

<template>
  <section class="content">
    <div class="pairing-section">
      <el-card shadow="never">
        <template #header><span>生成配对码</span></template>
        <el-form label-width="120px" size="small">
          <el-form-item label="Owner user_id">
            <el-input v-model="form.ownerUserId" placeholder="可选，单用户环境可留空" />
          </el-form-item>
          <el-form-item label="有效时长">
            <el-slider v-model="form.ttlSeconds" :min="60" :max="3600" :step="60" show-stops />
            <span class="hint">{{ form.ttlSeconds }} 秒（{{ (form.ttlSeconds / 60).toFixed(0) }} 分钟）</span>
          </el-form-item>
          <el-form-item label="授权能力">
            <el-checkbox-group v-model="form.grants">
              <el-checkbox v-for="cap in knownCapabilities" :key="cap" :value="cap">{{ cap }}</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" size="small" :loading="creating" @click="createPairingCode">生成配对码</el-button>
          </el-form-item>
        </el-form>

        <el-alert v-if="result" :title="`配对码: ${result.pairing_code}`" type="success" :closable="false" show-icon>
          <div class="result-detail">
            <div>过期时间: {{ new Date(result.expires_at).toLocaleString() }}</div>
            <div>授权能力: {{ result.granted_capabilities.join(", ") || "无" }}</div>
          </div>
        </el-alert>
      </el-card>
    </div>

    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="section-head">
          <span>已配对设备</span>
          <el-tag size="small">{{ devices.filter((d) => !d.revoked_at).length }} 台活跃</el-tag>
        </div>
      </template>
      <el-table :data="devices" size="small">
        <el-table-column prop="name" label="名称" width="160">
          <template #default="{ row }">
            <span>{{ row.alias ?? row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="client_type" label="类型" width="100">
          <template #default="{ row }">
            {{ clientTypeLabels[row.client_type] ?? row.client_type }}
          </template>
        </el-table-column>
        <el-table-column prop="online" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.online && !row.revoked_at ? 'success' : row.revoked_at ? 'info' : 'warning'" size="small">
              {{ row.revoked_at ? "已撤销" : row.online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="granted_capabilities" label="授权能力">
          <template #default="{ row }">
            <el-tag v-for="cap in row.granted_capabilities" :key="cap" size="small" class="cap-tag">{{ cap }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="effective_capabilities" label="有效能力">
          <template #default="{ row }">
            <el-tag v-for="cap in row.effective_capabilities" :key="cap" size="small" type="success" class="cap-tag">{{ cap }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="paired_at" label="配对时间" width="170">
          <template #default="{ row }">
            {{ new Date(row.paired_at).toLocaleString() }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; display: grid; gap: 14px; align-content: start; }
.pairing-section { max-width: 640px; }
.hint { color: var(--muted); font-size: 12px; margin-left: 8px; }
.result-detail { margin-top: 6px; font-size: 12px; line-height: 1.8; }
.section-head { display: flex; justify-content: space-between; align-items: center; }
.cap-tag + .cap-tag { margin-left: 4px; }
</style>
