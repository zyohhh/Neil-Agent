import { createHash } from 'node:crypto'
import { lstat, readFile, readdir, writeFile } from 'node:fs/promises'
import { dirname, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDirectory = dirname(fileURLToPath(import.meta.url))
const staticRoot = resolve(scriptDirectory, '..', '..', 'src', 'neil_agent', 'web', 'static')
const manifestName = 'asset-manifest.json'
const indexPath = resolve(staticRoot, 'index.html')

const generatedIndex = await readFile(indexPath, 'utf8')
await writeFile(indexPath, generatedIndex.replace(/\r+\n/g, '\n').replace(/\r/g, '\n'), 'utf8')

const collectFiles = async (directory) => {
  const collected = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const absolutePath = resolve(directory, entry.name)
    const metadata = await lstat(absolutePath)
    if (metadata.isSymbolicLink()) throw new Error(`Static bundle cannot contain a link: ${entry.name}`)
    if (entry.isDirectory()) collected.push(...await collectFiles(absolutePath))
    if (entry.isFile() && entry.name !== manifestName) collected.push(absolutePath)
  }
  return collected
}

const files = (await collectFiles(staticRoot)).sort()
const manifestFiles = {}
for (const absolutePath of files) {
  const relativePath = relative(staticRoot, absolutePath).split(sep).join('/')
  const digest = createHash('sha256').update(await readFile(absolutePath)).digest('hex')
  manifestFiles[relativePath] = digest
}
if (!manifestFiles['index.html']) throw new Error('Static bundle does not contain index.html')

await writeFile(
  resolve(staticRoot, manifestName),
  `${JSON.stringify({ schema_version: 1, files: manifestFiles }, null, 2)}\n`,
  'utf8',
)
console.log(`Static asset manifest: ${files.length} files`)
