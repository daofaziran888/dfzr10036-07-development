/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 冲突比对索引：令牌+版本是续办的凭据，旧令牌只能换来“需刷新”。 */
export type ConflictDiffIndex = {
  索引令牌: string
  样本ID: number
  样本编号: string
  目标表: string
  冲突快照: string
  日志位点对: string
  候选快照ID?: string
  源日志位点对?: string
  快照版本: number
  待修键: string[]
  已确认键: string[]
  受理键: string[]
  受理键区间: { 起键: string; 止键: string; 键数: number }[]
  状态: string
}

export type ConflictDiffQuery = {
  ok: boolean
  code?: string
  message: string
  index: ConflictDiffIndex | null
  差异明细: {
    键: string
    差异类型?: string
    修复建议?: string
    候选快照ID?: string
    修复资格?: boolean
  }[]
  候选快照: Record<string, unknown> | null
  源日志: Record<string, unknown> | null
}

export type ConflictDiffRecovery = ConflictDiffQuery & {
  accepted: string[]
  受理键区间?: ConflictDiffIndex['受理键区间']
  duplicate?: boolean
  need_refresh?: boolean
  current_index?: ConflictDiffIndex
}
