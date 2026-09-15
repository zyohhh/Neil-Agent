import type { LiveOutputEntry } from './protocol'

export function appendLiveOutput(entries: LiveOutputEntry[], entry: LiveOutputEntry) {
  const output = [...entries]
  let characters = Array.from(entry.text)
  const previous = output.at(-1)
  if (entry.kind === 'assistant' && previous?.kind === 'assistant') {
    const count = Math.min(characters.length, 4000 - Array.from(previous.text).length)
    output[output.length - 1] = { ...previous, text: previous.text + characters.slice(0, count).join('') }
    characters = characters.slice(count)
  }
  if (characters.length === 0) return { output, truncated: false }
  if (output.length === 200) {
    const removable = output.findIndex(item => item.kind !== 'assistant')
    if (removable === -1) return { output, truncated: entry.kind === 'assistant' }
    output.splice(removable, 1)
  }
  output.push({ ...entry, text: characters.join('') })
  return { output, truncated: false }
}
