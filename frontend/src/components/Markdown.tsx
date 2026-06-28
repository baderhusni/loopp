import type { ReactNode } from "react";

// Minimal, dependency-free, XSS-safe markdown renderer for the agent's replies.
// Builds React elements (never dangerouslySetInnerHTML), so model-generated text
// — which can be influenced by customer input — cannot inject HTML. Handles the
// formatting the model actually produces: **bold**, *italic*, `code`, bullet /
// numbered lists, headings, and paragraphs. `_` is intentionally NOT treated as
// italic so identifiers like check_refund_eligibility render verbatim.

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\n]+)\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] !== undefined) out.push(<strong key={k++}>{m[2]}</strong>);
    else if (m[3] !== undefined) out.push(<code key={k++}>{m[3]}</code>);
    else if (m[4] !== undefined) out.push(<em key={k++}>{m[4]}</em>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const isUL = (l: string) => /^\s*[-*]\s+/.test(l);
const isOL = (l: string) => /^\s*\d+\.\s+/.test(l);
const isH = (l: string) => /^\s*#{1,6}\s+/.test(l);

export default function Markdown({ text }: { text: string }) {
  const lines = (text || "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (isUL(line) || isOL(line)) {
      const ordered = isOL(line);
      const items: string[] = [];
      while (i < lines.length && (ordered ? isOL(lines[i]) : isUL(lines[i]))) {
        items.push(lines[i].replace(ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/, ""));
        i++;
      }
      const lis = items.map((it, j) => <li key={j}>{inline(it)}</li>);
      blocks.push(ordered ? <ol className="md-ul" key={key++}>{lis}</ol> : <ul className="md-ul" key={key++}>{lis}</ul>);
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    if (isH(line)) {
      blocks.push(<p className="md-h" key={key++}>{inline(line.replace(/^\s*#{1,6}\s+/, ""))}</p>);
      i++;
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && !isUL(lines[i]) && !isOL(lines[i]) && !isH(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push(
      <p className="md-p" key={key++}>
        {para.map((l, j) => (
          <span key={j}>
            {inline(l)}
            {j < para.length - 1 ? <br /> : null}
          </span>
        ))}
      </p>
    );
  }

  return <>{blocks}</>;
}
