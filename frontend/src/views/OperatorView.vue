<script setup lang="ts">
/**
 * Operator View — read-only operator mode route.
 *
 * Route: /#/operator/:station_id
 *
 * This view is a thin wrapper around `OperatorInteractionPanel`. It reads
 * the `station_id` route param and passes it to the panel. The view is
 * intentionally minimal — all logic lives in the panel + composable.
 *
 * **Read-only**: No sidebar navigation, no edit buttons, no sequence
 * modification. The operator can only:
 * - View the current test step and its parameters
 * - Scan barcodes (scanner input)
 * - Submit Pass/Fail/Skip/Retry/Abort actions
 * - View AI diagnosis suggestions
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import OperatorInteractionPanel from '@/components/OperatorInteractionPanel.vue'

// ─── Route param ─────────────────────────────────────────────────────────────

const route = useRoute()

/**
 * Station ID from the route param. Falls back to empty string if missing.
 */
const stationId = computed(() => {
  const param = route.params.station_id
  if (Array.isArray(param)) {
    return param[0] ?? ''
  }
  return param ?? ''
})
</script>

<template>
  <div class="operator-view" data-testid="operator-view">
    <OperatorInteractionPanel :station-id="stationId" />
  </div>
</template>

<style scoped>
.operator-view {
  height: 100%;
  min-height: 100vh;
}
</style>
