import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  ConflictDiffIndex,
  ConflictDiffQuery,
  ConflictDiffRecovery,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 按目标表 + 冲突快照 + 日志位点对检索冲突比对数据索引（快照号必须精确命中）。 */
export async function queryConflictDiff(params: {
  targetTable: string
  snapshot: string
  logPosition: string
}): Promise<ConflictDiffQuery> {
  const response = await request('/api/conflict_diff/query', {
    method: 'POST',
    body: JSON.stringify({
      目标表: params.targetTable,
      冲突快照: params.snapshot,
      日志位点对: params.logPosition,
    }),
  })
  if (!response.ok) throw new Error('冲突比对索引读取失败')
  return await response.json()
}

/** 确认快照内某些键已修复；只会落在该快照仍未确认、未受理的键上。 */
export async function confirmConflictDiff(
  index: ConflictDiffIndex,
  keys: string[],
): Promise<ConflictDiffRecovery> {
  const response = await request('/api/conflict_diff/confirm', {
    method: 'POST',
    body: JSON.stringify({ index, keys }),
  })
  if (!response.ok) throw new Error('修复确认失败')
  return await response.json()
}

/** 提交失败恢复：用索引发起复核或待修范围受理；旧索引会返回 need_refresh。 */
export async function recoverConflictDiff(
  index: ConflictDiffIndex,
  mode: '复核' | '待修' = '待修',
): Promise<ConflictDiffRecovery> {
  const response = await request('/api/conflict_diff/recover', {
    method: 'POST',
    body: JSON.stringify({ index, mode }),
  })
  if (!response.ok) throw new Error('提交失败恢复请求失败')
  return await response.json()
}
