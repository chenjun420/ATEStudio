import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import { Graph, Shape } from '@antv/x6'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { initTheme } from './composables/useTheme'
import './style.css'

const app = createApp(App)

// Install Pinia for state management
app.use(createPinia())

// Install Vue Router for navigation
app.use(router)

// Install Element Plus UI components
app.use(ElementPlus)

// Install vue-i18n for internationalization
app.use(i18n)

// Initialize theme — auto-detect system dark/light preference
initTheme()

// Register custom X6 node shapes
// These will be extended later with Vue components for rich rendering
Graph.registerNode('step-node', {
  inherit: Shape.Rect,
  width: 120,
  height: 60,
  attrs: {
    body: {
      fill: '#409eff',
      stroke: '#337ecc',
      strokeWidth: 2,
      rx: 8,
      ry: 8,
    },
    label: {
      fill: '#ffffff',
      fontSize: 14,
      refX: '50%',
      refY: '50%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
  ports: {
    groups: {
      top: {
        position: 'top',
        attrs: {
          circle: {
            r: 4,
            magnet: true,
            stroke: '#7c3aed',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
      bottom: {
        position: 'bottom',
        attrs: {
          circle: {
            r: 4,
            magnet: true,
            stroke: '#7c3aed',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
    },
  },
  portMarkup: [
    {
      tagName: 'circle',
      selector: 'circle',
    },
  ],
}, true)

// Register decision node (diamond shape)
Graph.registerNode('decision-node', {
  inherit: Shape.Polygon,
  width: 80,
  height: 80,
  attrs: {
    body: {
      fill: '#f59e0b',
      stroke: '#d97706',
      strokeWidth: 2,
      refPoints: '0,10 10,0 20,10 10,20',
    },
    label: {
      fill: '#ffffff',
      fontSize: 12,
      refX: '50%',
      refY: '50%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
}, true)

// Register start/end nodes
Graph.registerNode('start-node', {
  inherit: Shape.Circle,
  width: 40,
  height: 40,
  attrs: {
    body: {
      fill: '#10b981',
      stroke: '#059669',
      strokeWidth: 2,
    },
    label: {
      fill: '#ffffff',
      fontSize: 10,
      refX: '50%',
      refY: '50%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
}, true)

Graph.registerNode('end-node', {
  inherit: Shape.Circle,
  width: 40,
  height: 40,
  attrs: {
    body: {
      fill: '#ef4444',
      stroke: '#dc2626',
      strokeWidth: 2,
    },
    label: {
      fill: '#ffffff',
      fontSize: 10,
      refX: '50%',
      refY: '50%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
}, true)

// Register variable node (rectangular with green border)
Graph.registerNode('variable-node', {
  inherit: Shape.Rect,
  width: 200,
  height: 120,
  attrs: {
    body: {
      fill: '#ffffff',
      stroke: '#10b981',
      strokeWidth: 2,
      rx: 8,
      ry: 8,
    },
    label: {
      fill: '#111827',
      fontSize: 12,
      refX: '50%',
      refY: '50%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
  ports: {
    groups: {
      top: {
        position: 'top',
        attrs: {
          circle: {
            r: 4,
            magnet: true,
            stroke: '#10b981',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
      bottom: {
        position: 'bottom',
        attrs: {
          circle: {
            r: 4,
            magnet: true,
            stroke: '#10b981',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
    },
  },
  portMarkup: [
    {
      tagName: 'circle',
      selector: 'circle',
    },
  ],
}, true)

// Register Script Step Node - Professional script execution node
// Dimensions: 180x80px with rounded corners
// Used for displaying script steps with status indicators
Graph.registerNode('script-step-node', {
  inherit: Shape.Rect,
  width: 180,
  height: 80,
  // Explicit markup: body + label plus two breakpoint marker sub-elements
  // (task 23). `bpHalo` (amber ring, BREAKPOINT_HIT) and `bpBadge` (red dot,
  // armed step breakpoint) are hidden by default via display:'none' and
  // toggled through attrs by views/SequenceEditor/breakpointMarkers.ts.
  markup: [
    { tagName: 'rect', selector: 'body' },
    { tagName: 'text', selector: 'label' },
    {
      tagName: 'rect',
      selector: 'bpHalo',
      attrs: { display: 'none' },
    },
    {
      tagName: 'circle',
      selector: 'bpBadge',
      attrs: { display: 'none' },
    },
  ],
  attrs: {
    body: {
      fill: '#ffffff',
      stroke: '#e5e7eb',
      strokeWidth: 2,
      rx: 12,
      ry: 12,
    },
    label: {
      fill: '#111827',
      fontSize: 14,
      fontWeight: 600,
      refX: '50%',
      refY: '40%',
      textAnchor: 'middle',
      textVerticalAnchor: 'middle',
    },
  },
  // Port configuration for input/output connections
  ports: {
    groups: {
      input: {
        position: 'left',
        attrs: {
          circle: {
            r: 6,
            magnet: true,
            stroke: '#e5e7eb',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
      output: {
        position: 'right',
        attrs: {
          circle: {
            r: 6,
            magnet: true,
            stroke: '#e5e7eb',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
    },
  },
  portMarkup: [
    {
      tagName: 'circle',
      selector: 'circle',
    },
  ],
}, true)

// Register Loop Container Node - Container for loop constructs (for/while/foreach)
// Dimensions: 300x200px with dashed border and lighter fill
// Used for grouping steps inside a loop with input/output/loop-back ports
Graph.registerNode('loop-container-node', {
  inherit: Shape.Rect,
  width: 300,
  height: 200,
  attrs: {
    body: {
      fill: '#f0f7ff',
      stroke: '#3b82f6',
      strokeWidth: 2,
      strokeDasharray: '5,5',
      rx: 12,
      ry: 12,
    },
    label: {
      fill: '#1e40af',
      fontSize: 13,
      fontWeight: 600,
      refX: 16,
      refY: 16,
      textAnchor: 'start',
      textVerticalAnchor: 'top',
    },
  },
  // Port configuration: input (left), output (right), loop-back (bottom)
  ports: {
    groups: {
      input: {
        position: 'left',
        attrs: {
          circle: {
            r: 6,
            magnet: true,
            stroke: '#3b82f6',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
      output: {
        position: 'right',
        attrs: {
          circle: {
            r: 6,
            magnet: true,
            stroke: '#3b82f6',
            strokeWidth: 2,
            fill: '#ffffff',
          },
        },
      },
      'loop-back': {
        position: 'bottom',
        attrs: {
          circle: {
            r: 6,
            magnet: true,
            stroke: '#3b82f6',
            strokeWidth: 2,
            fill: '#dbeafe',
          },
        },
      },
    },
  },
  portMarkup: [
    {
      tagName: 'circle',
      selector: 'circle',
    },
  ],
}, true)

app.mount('#app')