import React from "react";

export function Markdown({
  text,
  onPage,
  onSource,
}: {
  text: string;
  onPage?: (id: string) => void;
  onSource?: (id: string) => void;
}) {
  const inline = (line: string) =>
    line
      .split(/(\[[^\]]+\]\([^)]+\)|`[^`]+`|\*\*[^*]+\*\*)/g)
      .map((part, i) => {
        const match = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (match) {
          const [, label, url] = match;
          const page = url.match(/^(?:\.\/)?([A-Za-z0-9_-]+)\.md$/);
          const source = url.match(/^\/api\/v1\/sources\/([A-Za-z0-9_-]+)/);
          if (page && onPage)
            return (
              <button
                className="inline-link"
                key={i}
                onClick={() => onPage(page[1])}
              >
                {label}
              </button>
            );
          if (source && onSource)
            return (
              <button
                className="inline-link"
                key={i}
                onClick={() => onSource(source[1])}
              >
                {label}
              </button>
            );
          if (/^https?:\/\//.test(url))
            return (
              <a key={i} href={url} target="_blank" rel="noopener noreferrer">
                {label}
              </a>
            );
          return <React.Fragment key={i}>{label}</React.Fragment>;
        }
        if (part.startsWith("`"))
          return <code key={i}>{part.slice(1, -1)}</code>;
        if (part.startsWith("**"))
          return <strong key={i}>{part.slice(2, -2)}</strong>;
        return <React.Fragment key={i}>{part}</React.Fragment>;
      });
  const blocks: any[] = [];
  let code: string[] | null = null;
  let table: string[][] = [];
  const flushTable = () => {
    if (!table.length) return;
    const rows = table;
    table = [];
    blocks.push(
      <div className="table-scroll" key={"table-" + blocks.length}>
        <table>
          <thead>
            <tr>
              {rows[0].map((cell, i) => (
                <th key={i}>{inline(cell)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(1).map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{inline(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
  };
  (text || "").split("\n").forEach((line, i) => {
    if (!code && line.trim().startsWith("|")) {
      if (!/^\s*\|[\s:|\-]+\|\s*$/.test(line))
        table.push(
          line
            .trim()
            .replace(/^\||\|$/g, "")
            .split("|")
            .map((cell) => cell.trim()),
        );
      return;
    }
    flushTable();
    if (line.startsWith("```")) {
      if (code) {
        blocks.push(
          <pre key={i}>
            <code>{code.join("\n")}</code>
          </pre>,
        );
        code = null;
      } else code = [];
      return;
    }
    if (code) {
      code.push(line);
      return;
    }
    const h = line.match(/^(#{1,4})\s+(.+)/);
    if (h) {
      const Tag = ("h" + Math.min(4, h[1].length)) as any;
      blocks.push(<Tag key={i}>{inline(h[2])}</Tag>);
    } else if (/^\s*[-*]\s/.test(line))
      blocks.push(
        <div className="md-list" key={i}>
          <span>•</span>
          <span>{inline(line.replace(/^\s*[-*]\s/, ""))}</span>
        </div>,
      );
    else if (/^\s*\d+\.\s/.test(line))
      blocks.push(<p key={i}>{inline(line)}</p>);
    else if (line.trim()) blocks.push(<p key={i}>{inline(line)}</p>);
  });
  if (code) blocks.push(<pre key="last">{code.join("\n")}</pre>);
  flushTable();
  return <div className="markdown">{blocks}</div>;
}
