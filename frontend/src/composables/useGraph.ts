import { ref, onMounted, onBeforeUnmount } from 'vue'
import { Graph, Scroller, Selection, Keyboard, History, Clipboard, Snapline } from '@antv/x6'

export function useGraph(containerId: string) {
  const graph = ref<Graph | null>(null)
  const container = ref<HTMLElement | null>(null)

  const initGraph = () => {
    container.value = document.getElementById(containerId)
    if (!container.value) return

    graph.value = new Graph({
      container: container.value,
      width: container.value.clientWidth,
      height: container.value.clientHeight,
      grid: { size: 10, visible: true },
      panning: { enabled: true, modifiers: 'shift' },
      mousewheel: { enabled: true, modifiers: 'ctrl', minScale: 0.5, maxScale: 2.0 },
      connecting: {
        snap: { radius: 20 },
        allowBlank: false,
        allowLoop: false,
        allowMulti: true,
        router: 'manhattan',
        connector: { name: 'rounded', args: { radius: 8 } },
      },
      highlighting: {
        default: {
          name: 'stroke',
          args: { padding: 4, attrs: { 'stroke-width': 2, stroke: '#409EFF' } }
        }
      }
    })

    // Use plugins
    graph.value.use(new Scroller({ enabled: true, pannable: true, pageWidth: 2000, pageHeight: 2000 }))
    graph.value.use(new Selection({ rubberband: true, multiple: true }))
    graph.value.use(new Keyboard({ enabled: true, global: true }))
    graph.value.use(new History({ enabled: true }))
    graph.value.use(new Clipboard({ enabled: true }))
    graph.value.use(new Snapline({ enabled: true, sharp: true }))

    // Bind keyboard shortcuts using graph.bindKey
    graph.value.bindKey(['ctrl+z', 'meta+z'], () => {
      ;(graph.value?.getPlugin('history') as History)?.undo()
    })

    graph.value.bindKey(['ctrl+y', 'meta+y'], () => {
      ;(graph.value?.getPlugin('history') as History)?.redo()
    })

    return graph.value
  }

  const disposeGraph = () => {
    if (graph.value) {
      graph.value.dispose()
      graph.value = null
    }
  }

  onMounted(initGraph)
  onBeforeUnmount(disposeGraph)

  return { graph, container, initGraph, disposeGraph }
}
