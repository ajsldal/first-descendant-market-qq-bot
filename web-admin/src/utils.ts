export function fmtTime(value?: number | null): string {
  if (!value) return '-'
  const date = new Date(Number(value) * 1000)
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function fmtSize(value?: number | null): string {
  const size = Number(value || 0)
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

export function shortJson(value: any, limit = 120): string {
  if (value === null || value === undefined || value === '') return '-'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > limit ? text.slice(0, limit) + '…' : text
}
