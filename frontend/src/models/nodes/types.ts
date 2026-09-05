export interface ScriptStepData {
  stepId: string
  scriptName: string
  scriptVersion?: string
  params: Record<string, unknown>
  preconditions: unknown[]
  resources: string[]
  timeout: number
  onFail: 'stop' | 'skip' | 'ignore'
  exportOutputs: boolean
  status?: 'idle' | 'running' | 'passed' | 'failed' | 'error'
  groupId?: string
  skipIf?: string | null
}

export interface VariableData {
  variables: Record<string, unknown>
  groupId?: string
}

export interface LoopContainerData {
  loopId: string
  loopType: 'for' | 'while' | 'foreach'
  condition: string
  iterationVar?: string
  collectionExpr?: string
  count?: number
  executionMode: 'serial' | 'parallel'
  maxConcurrency: number
  status?: 'idle' | 'running' | 'passed' | 'failed' | 'error'
  skipIf?: string | null
}

export interface NodeGroup {
  id: string
  name: string
  color?: string
  collapsed?: boolean
}

export type NodeData = ScriptStepData | VariableData | LoopContainerData

/**
 * Node type identifiers matching X6 custom shapes
 */
export type NodeType = 'step-node' | 'decision-node' | 'start-node' | 'end-node'

/**
 * Type guard to check if data is ScriptStepData
 */
export function isScriptStepData(data: unknown): data is ScriptStepData {
  return (
    typeof data === 'object' &&
    data !== null &&
    'stepId' in data &&
    'scriptName' in data &&
    'params' in data
  )
}

/**
 * Type guard to check if data is VariableData
 */
export function isVariableData(data: unknown): data is VariableData {
  return (
    typeof data === 'object' &&
    data !== null &&
    'variables' in data &&
    typeof (data as VariableData).variables === 'object'
  )
}

/**
 * Type guard to check if data is LoopContainerData.
 * Accepts `unknown` so it can be used directly on X6 `node.getData()` results.
 */
export function isLoopContainerData(data: unknown): data is LoopContainerData {
  return (
    typeof data === 'object' &&
    data !== null &&
    'loopId' in data &&
    'loopType' in data
  )
}

/**
 * Default values for a new ScriptStepData
 */
export function createDefaultScriptStepData(): ScriptStepData {
  return {
    stepId: '',
    scriptName: '',
    scriptVersion: '',
    params: {},
    preconditions: [],
    resources: [],
    timeout: 30000,
    onFail: 'stop',
    exportOutputs: false,
    status: 'idle',
    skipIf: null,
  }
}

/**
 * Default values for a new VariableData
 */
export function createDefaultVariableData(): VariableData {
  return {
    variables: {},
  }
}

/**
 * Default values for a new LoopContainerData
 */
export function createDefaultLoopContainerData(): LoopContainerData {
  return {
    loopId: '',
    loopType: 'for',
    condition: '',
    count: 3,
    executionMode: 'serial',
    maxConcurrency: 1,
    status: 'idle',
    skipIf: null,
  }
}