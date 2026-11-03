interface DiffLine {
  kind: 'add' | 'del' | 'ctx' | 'hunk'
  oldNo: number | null
  newNo: number | null
  text: string
}

interface DiffFile {
  oldPath: string
  newPath: string
  lines: DiffLine[]
  adds: number
  dels: number
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

function stripPrefix(path: string): string {
  return path.replace(/^[ab]\//, '')
}

/** Parse a unified diff into per-file line tables. Never throws on odd input. */
export function parseDiff(raw: string): DiffFile[] {
  const files: DiffFile[] = []
  let current: DiffFile | null = null
  let oldNo = 0
  let newNo = 0
  let inHunk = false

  function ensureFile(): DiffFile {
    if (!current) {
      current = { oldPath: '', newPath: '', lines: [], adds: 0, dels: 0 }
      files.push(current)
    }
    return current
  }

  for (const line of raw.split('\n')) {
    if (line.startsWith('diff --git ')) {
      current = { oldPath: '', newPath: '', lines: [], adds: 0, dels: 0 }
      files.push(current)
      inHunk = false
      continue
    }
    if (line.startsWith('--- ') && !inHunk) {
      ensureFile().oldPath = stripPrefix(line.slice(4).trim().split('\t')[0])
      continue
    }
    if (line.startsWith('+++ ') && !inHunk) {
      ensureFile().newPath = stripPrefix(line.slice(4).trim().split('\t')[0])
      continue
    }
    const hunk = HUNK_RE.exec(line)
    if (hunk) {
      const file = ensureFile()
      oldNo = Number(hunk[1])
      newNo = Number(hunk[2])
      inHunk = true
      file.lines.push({ kind: 'hunk', oldNo: null, newNo: null, text: line })
      continue
    }
    if (!inHunk) continue
    if (line.startsWith('\\')) continue
    const file = ensureFile()
    if (line.startsWith('+')) {
      file.lines.push({ kind: 'add', oldNo: null, newNo, text: line })
      file.adds += 1
      newNo += 1
    } else if (line.startsWith('-')) {
      file.lines.push({ kind: 'del', oldNo, newNo: null, text: line })
      file.dels += 1
      oldNo += 1
    } else {
      file.lines.push({ kind: 'ctx', oldNo, newNo, text: line })
      oldNo += 1
      newNo += 1
    }
  }
  return files.filter((f) => f.lines.length > 0)
}

function displayPath(file: DiffFile): string {
  if (file.newPath && file.newPath !== '/dev/null') return file.newPath
  if (file.oldPath && file.oldPath !== '/dev/null') return file.oldPath
  return '(unknown file)'
}

export default function DiffView({ diff, label }: { diff: string; label: string }) {
  const files = parseDiff(diff)
  if (files.length === 0) {
    return (
      <pre className="diff-view" aria-label={label}>
        {diff}
      </pre>
    )
  }
  const totalAdds = files.reduce((n, f) => n + f.adds, 0)
  const totalDels = files.reduce((n, f) => n + f.dels, 0)
  return (
    <div aria-label={label}>
      <p className="hint">
        {files.length} {files.length === 1 ? 'file' : 'files'} ·{' '}
        <span className="diff-count-add">+{totalAdds}</span>{' '}
        <span className="diff-count-del">−{totalDels}</span>
      </p>
      {files.map((file, index) => (
        <div key={`${displayPath(file)}-${index}`} className="diff-file">
          <div className="diff-file-head">
            <code>{displayPath(file)}</code>
            <span className="diff-counts">
              <span className="diff-count-add">+{file.adds}</span>{' '}
              <span className="diff-count-del">−{file.dels}</span>
            </span>
          </div>
          <table className="diff-table">
            <tbody>
              {file.lines.map((line, lineIndex) => (
                <tr
                  key={lineIndex}
                  className={
                    line.kind === 'add'
                      ? 'diff-add'
                      : line.kind === 'del'
                        ? 'diff-del'
                        : line.kind === 'hunk'
                          ? 'diff-hunk'
                          : undefined
                  }
                >
                  <td className="diff-line-no" aria-hidden="true">
                    {line.oldNo ?? ''}
                  </td>
                  <td className="diff-line-no" aria-hidden="true">
                    {line.newNo ?? ''}
                  </td>
                  <td className="diff-code">{line.text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}
