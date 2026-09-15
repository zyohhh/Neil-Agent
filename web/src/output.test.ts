import { appendLiveOutput } from './output'
import type { LiveOutputEntry } from './protocol'

const entry = (text: string, kind: LiveOutputEntry['kind'] = 'assistant'): LiveOutputEntry => ({
  entry_id: 'a'.repeat(32), kind, text, timestamp: '2026-09-15T00:00:00Z',
})

it('retains an entire answer across thousands of tiny deltas and activity pressure', () => {
  const answer = `回答开头。${'细碎文本'.repeat(1500)}回答结尾。`
  let output: LiveOutputEntry[] = []
  for (const character of answer) output = appendLiveOutput(output, entry(character)).output
  for (let index = 0; index < 250; index += 1) output = appendLiveOutput(output, entry(`Activity ${index}`, 'activity')).output
  expect(output.filter(item => item.kind === 'assistant').map(item => item.text).join('')).toBe(answer)
  expect(output.length).toBeLessThanOrEqual(200)
  expect(output.every(item => item.text.length <= 4000)).toBe(true)
})

it('keeps the beginning and reports capacity instead of silently replacing it with the tail', () => {
  const output = Array.from({ length: 200 }, () => entry('开'.repeat(4000)))
  const result = appendLiveOutput(output, entry('结尾'))
  expect(result.truncated).toBe(true)
  expect(result.output.map(item => item.text).join('')).toBe('开'.repeat(800000))
})

it('fills bounded text segments without mutating snapshots or their entry identities', () => {
  const original = entry('😀'.repeat(3999))
  const next = { ...entry('中尾'), entry_id: 'b'.repeat(32) }
  const result = appendLiveOutput([original], next)
  expect(Array.from(original.text)).toHaveLength(3999)
  expect(result.output.map(item => item.text)).toEqual(['😀'.repeat(3999) + '中', '尾'])
  expect(result.output.map(item => item.entry_id)).toEqual([original.entry_id, next.entry_id])
})
