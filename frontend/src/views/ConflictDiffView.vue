<template>
  <section class="page" data-module="conflict_diff">
    <header class="page-head">
      <div>
        <h2>冲突比对</h2>
        <p class="page-desc">
          按目标表、冲突快照与日志位点对精确检索冲突比对数据索引；快照号对不上时不会退回同表的其他冲突。
        </p>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="runQuery">
      <label>
        <span>目标表</span>
        <input v-model="targetTable" data-testid="conflict-target-table" placeholder="ods.payment_log_iceberg" />
      </label>
      <label>
        <span>冲突快照</span>
        <input v-model="snapshot" data-testid="conflict-snapshot" placeholder="IC-0002" />
      </label>
      <label>
        <span>日志位点对</span>
        <input v-model="logPosition" data-testid="conflict-log-position" placeholder="binlog.000042:158320" />
      </label>
      <button class="btn primary" data-testid="conflict-query" type="submit">查询索引</button>
      <button class="btn" type="button" data-testid="conflict-recheck" @click="runRecover('复核')">复核</button>
      <button class="btn" type="button" data-testid="conflict-recover" @click="runRecover('待修')">
        待修范围受理
      </button>
    </form>

    <p v-if="message" class="page-foot" data-testid="conflict-message">
      <span :class="{ 'error-text': !lastOk }">{{ message }}</span>
      <button
        v-if="needRefresh"
        class="btn"
        type="button"
        data-testid="conflict-refresh"
        @click="runQuery"
      >
        刷新该快照索引
      </button>
    </p>

    <template v-if="index">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">索引令牌</span>
          <strong class="stat-value" data-testid="conflict-token">{{ index.索引令牌 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">快照版本</span>
          <strong class="stat-value">{{ index.快照版本 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待修键</span>
          <strong class="stat-value">{{ index.待修键.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认键</span>
          <strong class="stat-value">{{ index.已确认键.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">受理键</span>
          <strong class="stat-value">{{ index.受理键.length }}</strong>
        </article>
      </div>

      <p class="status-legend">
        <span>候选快照：{{ candidateText }}</span>
        <span>源日志：{{ sourceText }}</span>
        <span>当前状态：{{ index.状态 }}</span>
      </p>

      <form class="filter-bar" @submit.prevent="runConfirm">
        <label class="filter-item">
          <span>已修复键（逗号分隔）</span>
          <input v-model="confirmKeys" data-testid="conflict-confirm-keys" placeholder="pay-0001" />
        </label>
        <button class="btn" type="submit" data-testid="conflict-confirm">确认修复</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>差异键</th>
            <th>差异类型</th>
            <th>修复建议</th>
            <th>关联快照</th>
            <th>关联候选快照</th>
            <th>关联日志位点对</th>
            <th>修复资格</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="detail in details" :key="detail.键">
            <td>{{ detail.键 }}</td>
            <td>{{ detail.差异类型 ?? '—' }}</td>
            <td>{{ detail.修复建议 ?? '—' }}</td>
            <td>{{ index.冲突快照 }}</td>
            <td>{{ detail.候选快照ID ?? index.候选快照ID ?? '—' }}</td>
            <td>{{ index.源日志位点对 ?? index.日志位点对 }}</td>
            <td>{{ detail.修复资格 ? '仍待修' : '已确认/已受理' }}</td>
          </tr>
          <tr v-if="!details.length">
            <td colspan="7" class="empty-state">该快照没有待处理的差异明细</td>
          </tr>
        </tbody>
      </table>

      <p class="status-legend">
        <span v-for="range in index.受理键区间" :key="`${range.起键}-${range.止键}`">
          受理区间 {{ range.起键 }} ~ {{ range.止键 }}（{{ range.键数 }} 键）
        </span>
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  confirmConflictDiff,
  queryConflictDiff,
  recoverConflictDiff,
} from '@/api/backend-service'
import type { ConflictDiffIndex, ConflictDiffQuery } from '@/data/types'

const targetTable = ref('ods.payment_log_iceberg')
const snapshot = ref('IC-0002')
const logPosition = ref('binlog.000042:158320')
const confirmKeys = ref('')

const index = ref<ConflictDiffIndex | null>(null)
const details = ref<ConflictDiffQuery['差异明细']>([])
const candidate = ref<Record<string, unknown> | null>(null)
const sourceLog = ref<Record<string, unknown> | null>(null)
const message = ref('')
const lastOk = ref(true)
const needRefresh = ref(false)

const candidateText = computed(() => {
  if (!candidate.value) return '—'
  return String(candidate.value['候选快照ID'] ?? candidate.value['快照编号'] ?? '—')
})
const sourceText = computed(() => {
  if (!sourceLog.value) return '—'
  return String(sourceLog.value['位点对'] ?? '—')
})

function apply(payload: ConflictDiffQuery) {
  index.value = payload.index
  details.value = payload.差异明细 ?? []
  candidate.value = payload.候选快照
  sourceLog.value = payload.源日志
}

async function runQuery() {
  needRefresh.value = false
  message.value = ''
  try {
    const payload = await queryConflictDiff({
      targetTable: targetTable.value.trim(),
      snapshot: snapshot.value.trim(),
      logPosition: logPosition.value.trim(),
    })
    lastOk.value = payload.ok
    message.value = payload.message
    if (payload.ok) apply(payload)
    else {
      index.value = null
      details.value = []
    }
  } catch (error) {
    lastOk.value = false
    message.value = error instanceof Error ? error.message : '查询失败'
  }
}

async function runConfirm() {
  if (!index.value) {
    lastOk.value = false
    message.value = '请先查询出该快照的索引'
    return
  }
  needRefresh.value = false
  try {
    const keys = confirmKeys.value.split(/[,，、\s]+/).filter(Boolean)
    const payload = await confirmConflictDiff(index.value, keys)
    lastOk.value = payload.ok
    message.value = payload.message
    if (payload.ok) {
      apply(payload)
      confirmKeys.value = ''
    }
  } catch (error) {
    lastOk.value = false
    message.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function runRecover(mode: '复核' | '待修') {
  if (!index.value) {
    lastOk.value = false
    message.value = '请先查询出该快照的索引'
    return
  }
  needRefresh.value = false
  try {
    const payload = await recoverConflictDiff(index.value, mode)
    lastOk.value = payload.ok
    message.value = payload.message
    if (payload.ok) apply(payload)
    else if (payload.need_refresh) {
      needRefresh.value = true
      // 过期索引不携带可信范围，只提示刷新依据，绝不拿旧范围继续派发。
      index.value = payload.current_index ?? null
      details.value = payload.差异明细 ?? []
    }
  } catch (error) {
    lastOk.value = false
    message.value = error instanceof Error ? error.message : '恢复请求失败'
  }
}
</script>
