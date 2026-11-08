<template>
  <div ref="host" class="stats-chart" :style="{ height }"></div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

interface StatPoint {
  date: string
  average_price: number
  trade_count: number
}

const props = withDefaults(
  defineProps<{ points: StatPoint[]; height?: string }>(),
  { height: '420px' },
)

const PRICE_COLOR = '#3B82F6'
const BAR_COLOR = '#BDBDBD'

const host = ref<HTMLDivElement | null>(null)
let chart: echarts.EChartsType | null = null
let observer: ResizeObserver | null = null

function fmtPrice(value: number): string {
  const num = Number(value || 0)
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function labelStep(total: number): number {
  if (total <= 10) return 1
  return Math.ceil(total / 10)
}

function buildOption(points: StatPoint[]) {
  const step = labelStep(points.length)
  return {
    animation: true,
    color: [PRICE_COLOR, BAR_COLOR],
    grid: { left: 12, right: 12, top: 54, bottom: 30, containLabel: true },
    legend: {
      top: 8,
      left: 'center',
      itemWidth: 16,
      itemHeight: 10,
      textStyle: { color: '#303133', fontSize: 12 },
      data: [
        { name: '平均价格', icon: 'line', itemStyle: { color: PRICE_COLOR } },
        { name: '成交笔数', icon: 'rect', itemStyle: { color: BAR_COLOR } },
      ],
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: '#c9ced8' } },
      backgroundColor: 'rgba(255,255,255,.96)',
      borderColor: '#e4e7ed',
      textStyle: { color: '#303133', fontSize: 12 },
      formatter: (params: any[]) => {
        const first = params?.[0]
        const point = points[first?.dataIndex ?? -1]
        if (!point) return ''
        return [
          `<div style="font-weight:600;margin-bottom:4px">${point.date}</div>`,
          `<div>平均价格：${fmtPrice(point.average_price)}</div>`,
          `<div>成交笔数：${point.trade_count} 笔</div>`,
        ].join('')
      },
    },
    xAxis: {
      type: 'category',
      data: points.map((point) => point.date),
      boundaryGap: true,
      axisLine: { lineStyle: { color: '#dcdfe6' } },
      axisTick: { show: false },
      axisLabel: {
        // 时间标签放在图表下方（官方为上方，此处按要求下移）
        show: true,
        margin: 12,
        interval: step - 1,
        fontSize: 11,
        color: '#7a8597',
        formatter: (value: string) => {
          const parts = value.split(' ')
          return `${parts[0]?.slice(5) || value}\n${parts[1]?.slice(0, 5) || ''}`
        },
      },
    },
    yAxis: [
      {
        type: 'value',
        name: '价格',
        nameTextStyle: { color: '#7a8597', fontSize: 11, padding: [0, 0, 6, 0] },
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          fontSize: 11,
          color: '#7a8597',
          formatter: (value: number) =>
            value >= 10000 ? `${(value / 10000).toFixed(1)}万` : String(value),
        },
      },
      {
        type: 'value',
        name: '笔数',
        nameTextStyle: { color: '#7a8597', fontSize: 11, padding: [0, 0, 6, 0] },
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        minInterval: 1,
        axisLabel: { fontSize: 11, color: '#7a8597' },
      },
    ],
    series: [
      {
        name: '平均价格',
        type: 'line',
        yAxisIndex: 0,
        data: points.map((point) => point.average_price),
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 2, color: PRICE_COLOR },
        itemStyle: { color: PRICE_COLOR },
        emphasis: { scale: 1.4 },
        z: 3,
      },
      {
        name: '成交笔数',
        type: 'bar',
        yAxisIndex: 1,
        data: points.map((point) => point.trade_count),
        barMaxWidth: 18,
        itemStyle: { color: BAR_COLOR },
        emphasis: { itemStyle: { color: '#9aa3b2' } },
        z: 2,
      },
    ],
  }
}

function render() {
  if (!chart) return
  chart.setOption(buildOption(props.points || []), true)
}

onMounted(() => {
  if (!host.value) return
  chart = echarts.init(host.value, undefined, { renderer: 'canvas' })
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(host.value)
})

watch(
  () => props.points,
  () => render(),
  { deep: true },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})
</script>

<style scoped>
.stats-chart {
  width: 100%;
  min-height: 240px;
}
</style>
