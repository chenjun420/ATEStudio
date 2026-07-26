export interface EdgeData {
  condition?: {
    status?: 'passed' | 'failed' | 'any' | 'skipped'
    expression?: string
  }
}
