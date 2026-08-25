<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface VersionItem {
  version: number;
  status: string;
  content_hash: string;
  created_by: string;
  created_at: string;
  published_at: string | null;
  rollback_from_version: number | null;
}

const versions = ref<VersionItem[]>([]);
const loading = ref(false);

const statusLabels: Record<string, string> = {
  draft: "草稿",
  published: "已发布",
  archived: "已归档",
};

async function refresh() {
  loading.value = true;
  try {
    versions.value = await api.request<VersionItem[]>("/api/v1/admin/config/versions");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

function statusType(status: string) {
  if (status === "published") return "success";
  if (status === "draft") return "info";
  return "";
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>
    <el-table :data="versions" v-loading="loading" style="width: 100%" size="small">
      <el-table-column prop="version" label="版本" width="80" />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusLabels[row.status] ?? row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="content_hash" label="内容哈希" width="160">
        <template #default="{ row }">
          <code>{{ row.content_hash.slice(0, 12) }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="created_by" label="操作人" width="120" />
      <el-table-column prop="created_at" label="创建时间" width="180">
        <template #default="{ row }">
          {{ new Date(row.created_at).toLocaleString() }}
        </template>
      </el-table-column>
      <el-table-column prop="published_at" label="发布时间" width="180">
        <template #default="{ row }">
          {{ row.published_at ? new Date(row.published_at).toLocaleString() : "—" }}
        </template>
      </el-table-column>
      <el-table-column prop="rollback_from_version" label="回滚来源" width="100">
        <template #default="{ row }">
          {{ row.rollback_from_version ? `v${row.rollback_from_version}` : "—" }}
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
code { font-size: 12px; background: #f5f7fa; padding: 2px 6px; border-radius: 4px; }
</style>
