let cases = [];
let selected = null;
let stepIndex = 0;
let filter = 'all';
const list = document.getElementById('taskList');
const detail = document.getElementById('detail');
const search = document.getElementById('search');
const pretty = (id) => id.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/([A-Za-z])([0-9])/g, '$1 $2');
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const success = (item) => item.score === 1;

function visibleCases() {
  const query = search.value.trim().toLowerCase();
  return cases.filter((item) => (filter === 'all' || (filter === 'passed') === success(item)) &&
    (!query || `${item.id} ${item.instruction}`.toLowerCase().includes(query)));
}

function renderList() {
  const visible = visibleCases();
  document.getElementById('taskCount').textContent = `${visible.length} / ${cases.length} TASKS`;
  list.innerHTML = visible.length ? visible.map((item) => `
    <button class="task-item ${selected?.id === item.id ? 'selected' : ''}" role="option" aria-selected="${selected?.id === item.id}" data-id="${escapeHtml(item.id)}">
      <span class="status-dot ${success(item) ? '' : 'fail'}"></span><span><strong>${escapeHtml(pretty(item.id))}</strong><small>${item.steps.length} steps · ${success(item) ? 'PASS' : 'FAIL'}</small></span><span class="chevron">›</span>
    </button>`).join('') : '<div class="empty">No matching tasks.</div>';
}

function buildTimeline(item) {
  const eventsAt = (afterStep) => (item.roleEvents || []).filter((event) => event.afterStep === afterStep);
  const frames = [];
  let screenNumber = 0;
  const decisionSummary = {execute: 'Plan the next stage', complete: 'Complete the task', inconclusive: 'Mark the outcome inconclusive', review: 'Request a review', replan: 'Revise the plan'};
  const addBoundary = (afterStep, screenshot, nextStep) => {
    const decisions = eventsAt(afterStep);
    decisions.forEach((event) => frames.push({
      role: event.role,
      kind: 'Decision',
      act: event.decision,
      summary: event.goal || decisionSummary[event.decision] || event.decision,
      goal: event.goal,
      reason: event.reason,
      screenshot,
      screenNumber,
    }));
    if (nextStep) frames.push({
      role: 'Executor',
      kind: nextStep.kind || 'Act',
      act: nextStep.act || nextStep.action,
      summary: nextStep.summary,
      screenshot,
      screenNumber,
    });
    if (!decisions.length && !nextStep) frames.push({
      role: '', kind: 'Observation', act: '', summary: 'Final device state', screenshot, screenNumber,
    });
  };
  addBoundary(0, item.initialScreenshot, item.steps[0]);
  item.steps.forEach((step, index) => {
    const captures = step.frames || [{screenshot: step.screenshot}];
    captures.forEach((capture, captureIndex) => {
      screenNumber += 1;
      if (captureIndex < captures.length - 1) {
        frames.push({
          summary: capture.nextAct?.startsWith('back(') ? 'Return to the previous screen.' : 'Pause video playback.',
          kind: capture.nextKind || 'Subaction',
          act: capture.nextAct || 'continue',
          role: 'Executor',
          screenshot: capture.screenshot,
          screenNumber,
        });
      } else {
        addBoundary(index + 1, capture.screenshot, item.steps[index + 1]);
      }
    });
  });
  return {events: frames, screenCount: screenNumber + 1};
}

function roleBadges(frame) {
  return frame.role ? `<span class="role-badge role-${frame.role.toLowerCase()}">${frame.role}</span>` : '';
}

function renderDetail(preserveFrameScroll = false) {
  if (!selected) return;
  const frameScroll = preserveFrameScroll ? detail.querySelector('.step-list')?.scrollTop || 0 : 0;
  const item = selected;
  const {events: frames, screenCount} = buildTimeline(item);
  stepIndex = Math.max(0, Math.min(stepIndex, frames.length - 1));
  const frame = frames[stepIndex];
  detail.innerHTML = `
    <div class="detail-top"><div><div class="case-label">ANDROIDWORLD / TASK ${String(cases.indexOf(item) + 1).padStart(3, '0')}</div><h3>${escapeHtml(pretty(item.id))}</h3></div><span class="result-badge ${success(item) ? '' : 'fail'}">${success(item) ? '✓ PASS' : '× FAIL'}</span></div>
    <p class="task-instruction">${escapeHtml(item.instruction)}</p>
    <div class="metadata"><span>SCORE <strong>${item.score.toFixed(1)}</strong></span><span>STEPS <strong>${item.steps.length}</strong></span><span>SCREENS <strong>${screenCount}</strong></span><span>ELAPSED <strong>${item.seconds.toFixed(1)}s</strong></span><span>MODEL <strong>GPT-5.6-SOL</strong></span></div>
    <div class="viewer"><div class="screen-stage">${frame.screenshot ? `<img src="${encodeURI(frame.screenshot)}" alt="${escapeHtml(item.id)} screenshot ${frame.screenNumber}" loading="eager">` : '<div class="no-image">No screenshot captured for this observation.</div>'}</div>
      <div class="step-panel"><div class="step-kicker">SCREEN ${String(frame.screenNumber).padStart(2, '0')} / ${String(screenCount - 1).padStart(2, '0')} · EVENT ${stepIndex + 1} / ${frames.length}</div>${roleBadges(frame)}${frame.role === 'Executor' ? '<div class="decision-label">ClickClick chose to</div>' : ''}<h4 title="${escapeHtml(frame.summary)}">${escapeHtml(frame.summary)}</h4>${frame.summary.length > 100 ? `<details class="full-summary"><summary>Read full step note</summary><p>${escapeHtml(frame.summary)}</p></details>` : ''}${frame.act ? `<div class="step-action"><code>${frame.role === 'Executor' && frame.kind !== 'Decision' ? 'Action' : 'Decision'}: ${escapeHtml(frame.act)}</code></div>` : ''}${frame.reason ? `<details class="role-reason"><summary>Why</summary><p>${escapeHtml(frame.reason)}</p></details>` : ''}
        <div class="step-controls"><button id="prev" ${stepIndex === 0 ? 'disabled' : ''} aria-label="Previous event">← Previous</button><button id="next" ${stepIndex === frames.length - 1 ? 'disabled' : ''} aria-label="Next event">Next →</button></div>
        <div class="step-list" aria-label="Run events">${frames.map((entry, index) => `<button data-step="${index}" class="${index === stepIndex ? 'active' : ''}" aria-current="${index === stepIndex ? 'step' : 'false'}"><span class="frame-no">${String(entry.screenNumber).padStart(2, '0')}</span><span class="frame-entry">${roleBadges(entry)}<b title="${escapeHtml(entry.summary)}">${escapeHtml(entry.summary)}</b>${entry.act ? `<small title="${escapeHtml(entry.act)}">${entry.role === 'Executor' && entry.kind !== 'Decision' ? 'Action' : 'Decision'}: ${escapeHtml(entry.act)}</small>` : ''}</span></button>`).join('')}</div>
      </div></div>`;
  const frameList = detail.querySelector('.step-list');
  frameList.scrollTop = frameScroll;
  const activeFrame = frameList.querySelector('.active');
  const listBounds = frameList.getBoundingClientRect();
  const activeBounds = activeFrame.getBoundingClientRect();
  if (activeBounds.top < listBounds.top) frameList.scrollTop -= listBounds.top - activeBounds.top;
  if (activeBounds.bottom > listBounds.bottom) frameList.scrollTop += activeBounds.bottom - listBounds.bottom;
  detail.querySelector('#prev')?.addEventListener('click', () => selectStep(stepIndex - 1));
  detail.querySelector('#next')?.addEventListener('click', () => selectStep(stepIndex + 1));
  detail.querySelectorAll('[data-step]').forEach((button) => button.addEventListener('click', () => selectStep(Number(button.dataset.step))));
}

function selectStep(index) { stepIndex = index; renderDetail(true); }
function selectTask(id, updateHash = true) {
  selected = cases.find((item) => item.id === id) || cases[0];
  stepIndex = 0;
  renderList(); renderDetail(false);
  if (updateHash) history.replaceState(null, '', `#${encodeURIComponent(selected.id)}`);
}

list.addEventListener('click', (event) => {
  const button = event.target.closest('[data-id]');
  if (button) selectTask(button.dataset.id);
});
search.addEventListener('input', renderList);
document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
  filter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach((other) => other.classList.toggle('active', other === button));
  renderList();
}));
window.addEventListener('keydown', (event) => {
  if (document.activeElement === search || !selected) return;
  if (event.key === 'ArrowRight' && stepIndex < buildTimeline(selected).events.length - 1) selectStep(stepIndex + 1);
  if (event.key === 'ArrowLeft' && stepIndex > 0) selectStep(stepIndex - 1);
});
window.addEventListener('hashchange', () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (cases.some((item) => item.id === id)) selectTask(id, false);
});

fetch('data.json').then((response) => {
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}).then((data) => {
  cases = data.cases;
  document.getElementById('allCount').textContent = cases.length;
  selectTask(decodeURIComponent(location.hash.slice(1)), false);
}).catch((error) => { detail.innerHTML = `<div class="loading">Could not load the gallery data: ${escapeHtml(error.message)}</div>`; });
