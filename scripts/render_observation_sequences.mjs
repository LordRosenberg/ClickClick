/** Render the Mermaid subset used by the bilingual consistency guide to SVG.
 * Diagram sources live in docs/diagrams/*.mmd; no external packages are needed.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ink = '#263449';
const muted = '#586575';
const blue = '#456B96';
const green = '#287568';
const font = 'Segoe UI, Microsoft YaHei, Arial, sans-serif';
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');

export function parse(source) {
  const participants = [];
  const events = [];
  const stack = [{ events }];
  for (const raw of source.trim().split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line === 'sequenceDiagram' || line.startsWith('%%')) continue;
    let match = line.match(/^participant (\w+) as (.+)$/u);
    if (match) {
      participants.push({ id: match[1], label: match[2] });
      continue;
    }
    match = line.match(/^Note over (\w+)(?:,(\w+))?:\s*(.+)$/iu);
    if (match) {
      stack.at(-1).events.push({ kind: 'note', from: match[1], to: match[2] || match[1], label: match[3] });
      continue;
    }
    match = line.match(/^(par|alt) (.+)$/u);
    if (match) {
      const branch = { label: match[2], events: [] };
      const group = { kind: match[1], branches: [branch] };
      stack.at(-1).events.push(group);
      stack.push({ group, events: branch.events });
      continue;
    }
    match = line.match(/^(and|else) (.+)$/u);
    if (match) {
      const scope = stack.at(-1);
      assert(scope.group && ((scope.group.kind === 'par' && match[1] === 'and') || (scope.group.kind === 'alt' && match[1] === 'else')));
      const branch = { label: match[2], events: [] };
      scope.group.branches.push(branch);
      scope.events = branch.events;
      continue;
    }
    if (line === 'end') {
      assert(stack.length > 1);
      stack.pop();
      continue;
    }
    match = line.match(/^(\w+)(-->>|->>)(\w+):\s*(.+)$/u);
    assert(match, `Unsupported diagram statement: ${line}`);
    stack.at(-1).events.push({ kind: 'message', from: match[1], to: match[3], reply: match[2] === '-->>', label: match[4] });
  }
  assert.equal(stack.length, 1);
  assert.equal(new Set(participants.map(p => p.id)).size, participants.length);
  const messages = flatten(events);
  for (const message of [...messages, ...notesIn(events)]) {
    assert(participants.some(p => p.id === message.from));
    assert(participants.some(p => p.id === message.to));
  }
  return { participants, events, messages };
}

function flatten(events) {
  return events.flatMap(event => event.kind === 'message' ? [event] : event.kind === 'note' ? [] : event.branches.flatMap(branch => flatten(branch.events)));
}

function notesIn(events) {
  return events.flatMap(event => event.kind === 'note' ? [event] : event.kind === 'message' ? [] : event.branches.flatMap(branch => notesIn(branch.events)));
}

function width(text, size) {
  return [...text].reduce((sum, ch) => sum + size * (
    /[\u2e80-\u9fff\uff00-\uffef]/u.test(ch) ? 1 :
    /\s/u.test(ch) ? 0.3 : /[ilI.,:!|]/u.test(ch) ? 0.29 :
    /[MW@]/u.test(ch) ? 0.9 : /[A-Z0-9]/u.test(ch) ? 0.65 : 0.56
  ), 0);
}

function wrap(text, maxWidth, size) {
  const tokens = text.match(/[\u2e80-\u9fff\uff00-\uffef]|[^\s\u2e80-\u9fff\uff00-\uffef]+|\s+/gu) ?? [];
  const lines = [];
  let line = '';
  for (const token of tokens) {
    const next = line + token;
    if (line && width(next, size) > maxWidth) {
      lines.push(line.trim());
      line = token.trimStart();
    } else line = next;
  }
  if (line.trim()) lines.push(line.trim());
  return lines;
}

export function render(diagram, title, language) {
  const count = diagram.participants.length;
  const positions = new Map(diagram.participants.map((p, i) => [p.id,
    count === 5 ? 160 + i * 320 : count === 3 ? 220 + i * 580 :
      count === 1 ? 800 : 160 + i * 1280 / (count - 1)]));
  const frames = [];
  const messages = [];
  const notes = [];
  const annotated = notesIn(diagram.events).length > 0;
  let index = 0;
  function place(events, startY) {
    let y = startY;
    for (const event of events) {
      if (event.kind === 'message') {
        const x1 = positions.get(event.from);
        const x2 = positions.get(event.to);
        const self = event.from === event.to;
        const maxWidth = self ? (diagram.participants.length === 5 ? 250 : 440) : Math.abs(x2 - x1) - 36;
        const lines = wrap(event.label, maxWidth, 17);
        const arrowY = y + lines.length * 22 + 8;
        messages.push({ ...event, x1, x2, self, lines, top: y, arrowY, index: ++index });
        y = arrowY + (self ? 43 : 25);
      } else if (event.kind === 'note') {
        const left = Math.min(positions.get(event.from), positions.get(event.to));
        const right = Math.max(positions.get(event.from), positions.get(event.to));
        const x = Math.max(40, left - 120), end = Math.min(1560, right + 120);
        const lines = wrap(event.label, end - x - 32, 16);
        assert(lines.every(line => width(line, 16) <= end - x - 32), `Note text exceeds its width: ${event.label}`);
        const h = lines.length * 22 + 22;
        notes.push({ ...event, x, y, w: end - x, h, lines });
        y += h + 18;
      } else {
        const children = event.branches.flatMap(b => b.events);
        const involved = [...flatten(children), ...notesIn(children)].flatMap(m => [positions.get(m.from), positions.get(m.to)]);
        const frame = { kind: event.kind, x: Math.min(...involved) - 65, y, right: Math.max(...involved) + 65, branches: [] };
        frames.push(frame);
        const childStart = frames.length, noteStart = notes.length;
        y += 8;
        event.branches.forEach((branch, i) => {
          frame.branches.push({ y, label: branch.label, tag: i === 0 ? event.kind : event.kind === 'par' ? 'and' : 'else', separator: i > 0 });
          y = place(branch.events, y + 29) + 9;
        });
        frame.height = y - frame.y;
        if (annotated) {
          // Enclose annotation cards and inset nested alternatives without crossing lifelines.
          const children = frames.slice(childStart), annotations = notes.slice(noteStart);
          frame.x = Math.max(28, Math.min(frame.x, ...children.map(f => f.x - 12), ...annotations.map(n => n.x - 12)));
          frame.right = Math.min(1572, Math.max(frame.right, ...children.map(f => f.right + 12), ...annotations.map(n => n.x + n.w + 12)));
        }
        y += 20;
      }
    }
    return y;
  }
  const bottom = place(diagram.events, 156);
  const height = Math.ceil(bottom + 34);
  const parts = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="${height}" viewBox="0 0 1600 ${height}" role="img" aria-labelledby="title desc">`,
    `<title id="title">${escape(title)}</title>`,
    `<desc id="desc">${language === 'zh-CN' ? '时间从上到下，实线为调用，虚线为返回；并行与条件分支标注在框内。' : 'Time runs downward. Solid arrows are calls; dashed arrows are returns. Frames identify parallel and alternative branches.'}</desc>`,
    '<defs>',
    `<marker id="request" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 1L9 5L0 9Z" fill="${ink}"/></marker>`,
    `<marker id="response" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 1L9 5L0 9Z" fill="${green}"/></marker>`,
    '</defs>',
    `<rect width="1600" height="${height}" fill="white"/>`,
  ];
  function text(x, y, value, size = 17, color = ink, anchor = 'start', weight = 400) {
    const lines = Array.isArray(value) ? value : [value];
    parts.push(`<text x="${x}" y="${y}" font-family="${font}" font-size="${size}" font-weight="${weight}" fill="${color}" text-anchor="${anchor}">${lines.map((line, i) => `<tspan x="${x}" dy="${i === 0 ? 0 : 22}">${escape(line)}</tspan>`).join('')}</text>`);
  }
  text(40, 39, title, 25, ink, 'start', 500);
  text(40, 65, language === 'zh-CN' ? '时间从上到下 · 实线调用 / 虚线返回' : 'Time runs downward · solid calls / dashed returns', 16, muted);
  for (const frame of frames) {
    parts.push(`<rect x="${frame.x}" y="${frame.y}" width="${frame.right - frame.x}" height="${frame.height}" rx="5" fill="#F7FAFD" stroke="#C5D1DE" stroke-width="1.2"/>`);
    for (const branch of frame.branches) {
      if (branch.separator) parts.push(`<path d="M${frame.x} ${branch.y - 4}H${frame.right}" stroke="#C5D1DE" stroke-width="1" stroke-dasharray="5 4"/>`);
      text(frame.x + 12, branch.y + 16, branch.tag, 14, blue, 'start', 500);
      text(frame.x + 58, branch.y + 16, branch.label, 15, muted);
    }
  }
  for (const participant of diagram.participants) {
    const x = positions.get(participant.id);
    parts.push(`<path d="M${x} 130V${bottom + 6}" stroke="#B3BFCC" stroke-width="1.1" stroke-dasharray="5 5"/>`);
    parts.push(`<rect x="${x - 120}" y="86" width="240" height="45" rx="5" fill="#EDF3FA" stroke="${blue}" stroke-width="1.3"/>`);
    text(x, 114, participant.label, 18, ink, 'middle', 500);
  }
  for (const note of notes) {
    parts.push(`<g data-note="true" data-from="${escape(note.from)}" data-to="${escape(note.to)}" data-label="${escape(note.label)}"><rect x="${note.x}" y="${note.y}" width="${note.w}" height="${note.h}" rx="5" fill="#EEF7F4" stroke="#BCD5CC" stroke-width="1"/>`);
    text(note.x + 16, note.y + 25, note.lines, 16, green);
    parts.push('</g>');
  }
  for (const message of messages) {
    const color = message.reply ? green : ink;
    const marker = message.reply ? 'response' : 'request';
    const dash = message.reply ? ' stroke-dasharray="6 5"' : '';
    parts.push(`<g data-message="${message.index}" data-from="${escape(message.from)}" data-to="${escape(message.to)}" data-label="${escape(message.label)}">`);
    if (message.self) {
      text(message.x1 + 61, message.top + 17, message.lines, 17, color);
      parts.push(`<path d="M${message.x1} ${message.arrowY}h43v23h-43" fill="none" stroke="${color}" stroke-width="1.5"${dash} marker-end="url(#${marker})"/>`);
    } else {
      text((message.x1 + message.x2) / 2, message.top + 17, message.lines, 17, color, 'middle');
      parts.push(`<path d="M${message.x1} ${message.arrowY}H${message.x2}" stroke="${color}" stroke-width="1.5"${dash} marker-end="url(#${marker})"/>`);
    }
    parts.push('</g>');
  }
  parts.push('</svg>');
  assert.equal(messages.length, diagram.messages.length);
  return parts.join('\n') + '\n';
}

export function rebuildObservationSequences(check = false) {
  const names = ['main', 'node-click', 'text-input'];
  const titles = {
    'zh-CN': ['观测与执行一致性：主时序', '原生节点点击：属性复核与执行', '定向输入：字段、焦点与读回'],
    en: ['Observation and execution consistency', 'Native node click: attributes and execution', 'Targeted input: field, focus and readback'],
  };
  let count = 0;
  for (const [language, suffix] of [['en', ''], ['zh-CN', '.zh-CN']]) {
    const markdown = readFileSync(resolve(root, `docs/observation-execution-consistency${suffix}.md`), 'utf8');
    names.forEach((name, i) => {
      const sourcePath = `diagrams/observation-execution-${name}${suffix}.mmd`;
      assert(markdown.includes(`](${sourcePath})`), `Missing source link: ${sourcePath}`);
      const source = readFileSync(resolve(root, 'docs', sourcePath), 'utf8');
      const output = resolve(root, `docs/assets/observation-execution-${name}${suffix}.svg`);
      const svg = render(parse(source), titles[language][i], language);
      if (check) assert.equal(readFileSync(output, 'utf8'), svg, `Stale SVG: ${output}`);
      else writeFileSync(output, svg, 'utf8');
      count++;
    });
  }
  return count;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const check = process.argv.includes('--check');
  process.stdout.write(`${check ? 'Verified' : 'Rendered'} ${rebuildObservationSequences(check)} observation sequence SVGs.\n`);
}
