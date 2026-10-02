'use strict';
let data, selected, variant = 'clarified', filter = 'all', stepIndex = 0;
const trajectories = new Map();
let language = localStorage.getItem('clickclick-site-language') || 'en';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const t = (en, zh) => language === 'zh' ? zh : en;
const pretty = (id) => id.replace(/([a-z0-9])([A-Z])/g, '$1 $2');
const passed = (score) => score === 1;
const result = (score) => passed(score) ? t('PASS', '通过') : t('FAIL', '失败');

function localize() {
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-en]').forEach((element) => { element.textContent = element.dataset[language] || element.dataset.en; });
  $('languageToggle').textContent = t('中文', 'English');
}
function renderLeaderboard() {
  const rows = [{model: 'ClickClick · GPT-5.6-SOL', score: 112 / 117 * 100, ours: true, note: t('Clarified · 112/117', '澄清版 · 112/117')}, ...data.leaderboard.map((x) => ({...x, note: x.category}))];
  $('leaderboardRows').innerHTML = rows.map((row, index) => `<tr class="${row.ours ? 'ours' : ''}"><td>${index + 1}</td><td>${esc(row.model)}</td><td>${row.score.toFixed(row.ours ? 2 : 1)}%<span class="score-bar"><i style="width:${row.score}%"></i></span></td><td>${esc(row.note)}</td></tr>`).join('');
  $('leaderboardRows').firstElementChild.insertAdjacentHTML('afterend', `<tr class="ours"><td>—</td><td>${t('ClickClick · original wording', 'ClickClick · 原题措辞版')}</td><td>90.60%</td><td>${t('106/117 · same system, also exceeds published rows', '106/117 · 同一系统，也高于公开榜单各项')}</td></tr>`);
}
function renderClarifications() {
  $('clarificationRows').innerHTML = data.clarifications.map((change) => `<tr><td><a href="#${encodeURIComponent(change.id)}">${esc(change.id)}</a>${change.kind === 'environment context' ? `<br><span class="badge">${t('Environment context', '环境信息补充')}</span>` : ''}</td><td>${esc(t(change.problem, change.problemZh))}</td><td>${esc(t(change.clarification, change.clarificationZh))}</td><td>${result(change.original)} → ${result(change.clarified)}</td></tr>`).join('');
}
function renderList() {
  const query = $('search').value.trim().toLowerCase();
  const rows = data.cases.filter((item) => (!query || `${item.id} ${item[variant].goal} ${item[variant].apps.join(' ')}`.toLowerCase().includes(query)) && (filter === 'all' || filter === 'changed' && item.changed || filter === 'passed' && passed(item[variant].score) || filter === 'failed' && !passed(item[variant].score)));
  $('taskCount').textContent = `${rows.length} / 117 ${t('TASKS', '题')}`;
  $('taskList').innerHTML = rows.length ? rows.map((item) => `<button class="task-item ${item.id === selected?.id ? 'selected' : ''}" data-id="${esc(item.id)}" aria-pressed="${item.id === selected?.id}"><span class="status-dot ${passed(item[variant].score) ? '' : 'fail'}"></span><span><strong>${esc(pretty(item.id))}</strong><small>${item[variant].actions} ${t('actions', '动作')} · ${result(item[variant].score)}${item.changed ? ` · ${t('variant', '澄清项')}` : ''}</small></span></button>`).join('') : `<p class="muted">${t('No matching tasks.', '没有匹配任务。')}</p>`;
}
function renderTrajectory(trajectory, url, preserveScroll = false) {
  const host = $('trajectory');
  if (!host || !trajectory?.events.length) return;
  const scroll = preserveScroll ? host.querySelector('.step-list')?.scrollTop || 0 : 0;
  stepIndex = Math.max(0, Math.min(stepIndex, trajectory.events.length - 1));
  const step = trajectory.events[stepIndex];
  const badge = (entry) => entry.role ? `<span class="role-badge role-${entry.role.toLowerCase()}">${esc(entry.role)}</span>` : '';
  const action = (entry) => entry.action ? `${entry.kind === 'Action' ? t('Action', '动作') : t('Decision', '决策')}: ${esc(entry.action)}` : '';
  const screenshot = step.screenshot ? new URL(step.screenshot, new URL(url, location.href)).href : '';
  host.innerHTML = `<div class="viewer"><div class="screen-stage">${screenshot ? `<img src="${esc(screenshot)}" alt="${esc(selected.id)} ${t('screen', '屏幕')} ${step.screenNumber}" loading="eager">` : `<p class="no-image">${t('No screenshot captured for this event.', '此事件未留存截图。')}</p>`}</div><div class="step-panel"><div class="step-kicker">${t('SCREEN', '屏幕')} ${step.screenNumber === null ? '—' : String(step.screenNumber).padStart(2, '0')} / ${String(trajectory.screenCount - 1).padStart(2, '0')} · ${t('EVENT', '事件')} ${stepIndex + 1} / ${trajectory.events.length}</div>${badge(step)}${step.role === 'Executor' ? `<div class="decision-label">${t('ClickClick chose to', 'ClickClick 选择')}</div>` : ''}<h4 title="${esc(step.summary)}">${esc(step.summary)}</h4>${step.summary.length > 100 ? `<details class="full-summary"><summary>${t('Read full step note', '查看完整步骤说明')}</summary><p>${esc(step.summary)}</p></details>` : ''}${step.action ? `<div class="step-action"><code>${action(step)}</code></div>` : ''}${step.reason ? `<details class="role-reason"><summary>${t('Why', '原因')}</summary><p>${esc(step.reason)}</p></details>` : ''}<div class="step-controls"><button id="prevStep" ${stepIndex === 0 ? 'disabled' : ''}>← ${t('Previous', '上一步')}</button><button id="nextStep" ${stepIndex === trajectory.events.length - 1 ? 'disabled' : ''}>${t('Next', '下一步')} →</button></div><div class="step-list" aria-label="${t('Run events', '运行事件')}">${trajectory.events.map((entry, index) => `<button data-step="${index}" class="${index === stepIndex ? 'active' : ''}" aria-current="${index === stepIndex ? 'step' : 'false'}"><span class="frame-no">${entry.screenNumber === null ? '—' : String(entry.screenNumber).padStart(2, '0')}</span><span class="frame-entry">${badge(entry)}<b title="${esc(entry.summary)}">${esc(entry.summary)}</b>${entry.action ? `<small>${action(entry)}</small>` : ''}</span></button>`).join('')}</div></div></div>`;
  const list = host.querySelector('.step-list');
  list.scrollTop = scroll;
  const active = list.querySelector('.active');
  const bounds = list.getBoundingClientRect(), row = active.getBoundingClientRect();
  if (row.top < bounds.top) list.scrollTop -= bounds.top - row.top;
  if (row.bottom > bounds.bottom) list.scrollTop += row.bottom - bounds.bottom;
  const select = (index) => { stepIndex = index; renderTrajectory(trajectory, url, true); };
  $('prevStep').addEventListener('click', () => select(stepIndex - 1));
  $('nextStep').addEventListener('click', () => select(stepIndex + 1));
  host.querySelectorAll('[data-step]').forEach((button) => button.addEventListener('click', () => select(Number(button.dataset.step))));
}
function renderDetail() {
  if (!selected) return;
  const item = selected, arm = item[variant];
  const change = data.clarifications.find((x) => x.id === item.id);
  $('detail').innerHTML = `<div class="detail-top"><div><div class="case-label">MOBILEWORLD / ${String(item.ordinal).padStart(3, '0')}</div><h3>${esc(pretty(item.id))}</h3></div><span class="result-badge ${passed(arm.score) ? '' : 'fail'}">${result(arm.score)}</span></div><div class="paired-score"><span>${t('Clarified', '澄清版')}: <b>${result(item.clarified.score)}</b></span><span>${t('Original wording', '原题措辞版')}: <b>${result(item.original.score)}</b></span></div><p class="task-instruction">${esc(arm.goal)}</p><div class="metadata"><span>${t('ACTIONS', '动作')} <strong>${arm.actions}</strong></span><span>${t('ROUNDS', '回合')} <strong>${arm.rounds}/${arm.budget}</strong></span><span>${t('TIME', '用时')} <strong>${arm.seconds}s</strong></span><span>${esc(arm.apps.join(' → '))}</span></div>${change ? `<p class="notice">${esc(t(change.clarification, change.clarificationZh))}</p>` : ''}<div id="trajectory"><p class="muted" role="status">${t('Loading screenshots and role events…', '正在加载截图与角色事件…')}</p></div>`;
  const host = $('trajectory'), url = arm.trajectory;
  if (!url) { host.textContent = t('No trajectory was archived.', '未留存轨迹。'); return; }
  if (!trajectories.has(url)) {
    trajectories.set(url, fetch(url).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).catch((error) => { trajectories.delete(url); throw error; }));
  }
  trajectories.get(url).then((value) => {
    if ($('trajectory') !== host || selected[variant].trajectory !== url) return;
    if (!value.events.length) { host.textContent = t('No role events were archived.', '未留存角色事件。'); return; }
    renderTrajectory(value, url);
  }).catch((error) => {
    if ($('trajectory') === host) host.textContent = `${t('Could not load trajectory', '无法加载轨迹')}: ${error.message}`;
  });
}
function selectTask(id) {
  selected = data.cases.find((x) => x.id === id) || data.cases[0];
  stepIndex = 0; renderList(); renderDetail();
}
function renderAll() { localize(); renderLeaderboard(); renderClarifications(); renderList(); renderDetail(); }
$('languageToggle').addEventListener('click', () => { language = language === 'en' ? 'zh' : 'en'; localStorage.setItem('clickclick-site-language', language); if (data) renderAll(); else localize(); });
$('search').addEventListener('input', () => { if (data) renderList(); });
$('taskList').addEventListener('click', (event) => { const button = event.target.closest('[data-id]'); if (button) { selectTask(button.dataset.id); history.replaceState(null, '', `#${encodeURIComponent(selected.id)}`); } });
document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => { filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach((x) => x.classList.toggle('active', x === button)); if (data) renderList(); }));
document.querySelectorAll('[name="variant"]').forEach((input) => input.addEventListener('change', () => { variant = input.value; stepIndex = 0; if (data) { renderList(); renderDetail(); } }));
window.addEventListener('hashchange', () => { if (data) { let id; try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; } if (data.cases.some((x) => x.id === id)) { selectTask(id); $('explore').scrollIntoView(); } } });
localize();
fetch('data.json?v=roles-20261002').then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }).then((value) => { data = value; let id; try { id = decodeURIComponent(location.hash.slice(1)); } catch { id = ''; } selectTask(id); renderAll(); if (data.cases.some((x) => x.id === id)) $('explore').scrollIntoView(); }).catch((error) => { $('detail').textContent = `${t('Could not load results', '无法加载成绩')}: ${error.message}`; });
