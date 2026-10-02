'use strict';
let data, selected, variant = 'clarified', filter = 'all', stepIndex = 0;
let language = localStorage.getItem('clickclick-site-language') || 'en';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const t = (en, zh) => language === 'zh' ? zh : en;
const pretty = (id) => id.replace(/([a-z0-9])([A-Z])/g, '$1 $2');
const passed = (score) => score === 1;
const result = (score) => passed(score) ? t('PASS', '通过') : t('FAIL', '失败');
const media = (name) => `media/${encodeURIComponent(name)}`;

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
function renderDemos() {
  $('demoGrid').innerHTML = data.demos.map((demo) => `<article class="demo-card"><a href="${media(demo.video)}"><img src="${media(demo.poster)}" alt="${esc(t(demo.title, demo.titleZh))}" loading="lazy"></a><div class="demo-copy"><small>${esc(demo.apps)}</small><h3>${esc(t(demo.title, demo.titleZh))}</h3><p>${esc(t(demo.description, demo.descriptionZh))}</p><div class="demo-meta">${demo.actions} ${t('actions', '动作')} · ${t(demo.variant === 'clarified' ? 'Clarified' : 'Original wording', demo.variant === 'clarified' ? '澄清版' : '原题版')} · ✓ ${result(demo.score)}</div><div class="demo-links"><a href="${media(demo.video)}">${t('Full video · 3× ↗', '完整视频 · 3× ↗')}</a><a href="#${encodeURIComponent(demo.task)}">${t('Inspect steps ↓', '检查步骤 ↓')}</a></div></div></article>`).join('');
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
function renderDetail() {
  if (!selected) return;
  const item = selected, arm = item[variant], demo = data.demos.find((x) => x.task === item.id && (x.variant === variant || !item.changed));
  const change = data.clarifications.find((x) => x.id === item.id);
  const step = demo?.steps[stepIndex];
  $('detail').innerHTML = `<div class="detail-top"><div><div class="case-label">MOBILEWORLD / ${String(item.ordinal).padStart(3, '0')}</div><h3>${esc(pretty(item.id))}</h3></div><span class="result-badge ${passed(arm.score) ? '' : 'fail'}">${result(arm.score)}</span></div><div class="paired-score"><span>${t('Clarified', '澄清版')}: <b>${result(item.clarified.score)}</b></span><span>${t('Original wording', '原题措辞版')}: <b>${result(item.original.score)}</b></span></div><p class="task-instruction">${esc(arm.goal)}</p><div class="metadata"><span>${t('ACTIONS', '动作')} <strong>${arm.actions}</strong></span><span>${t('ROUNDS', '回合')} <strong>${arm.rounds}/${arm.budget}</strong></span><span>${t('TIME', '用时')} <strong>${arm.seconds}s</strong></span><span>${esc(arm.apps.join(' → '))}</span></div>${change ? `<p class="notice">${esc(t(change.clarification, change.clarificationZh))}</p>` : ''}${demo ? `<div class="recording"><video controls preload="none" poster="${media(demo.poster)}" src="${media(demo.video)}" aria-label="${esc(t(demo.title, demo.titleZh))}"></video><p class="muted">${t('Full recording · 3× speed · synthetic benchmark data', '完整录像 · 3 倍速 · 合成测试数据')}</p></div>${step ? `<div class="demo-steps"><img src="${media(step.screenshot)}" alt="${esc(item.id)} ${t('screen before decision', '决策前屏幕')} ${step.sequence}"><div><span class="eyebrow">${t('EXECUTOR / PRE-DECISION SCREEN', '执行器 / 决策前屏幕')} ${stepIndex + 1}/${demo.steps.length}</span><h4>${esc(step.summary)}</h4><code>${esc(step.action)}</code><div class="step-controls"><button id="prevStep" ${stepIndex === 0 ? 'disabled' : ''}>← ${t('Previous', '上一步')}</button><button id="nextStep" ${stepIndex === demo.steps.length - 1 ? 'disabled' : ''}>${t('Next', '下一步')} →</button></div></div></div>` : ''}` : `<p class="notice">${t('Both scores and task instructions are available above. Recorded video and step screenshots are published for the six selected complex successes.', '上方提供两版成绩与题目指令。六个精选复杂成功用例另有完整录像及逐步截图。')}</p>`}`;
  $('prevStep')?.addEventListener('click', () => { stepIndex--; renderDetail(); });
  $('nextStep')?.addEventListener('click', () => { stepIndex++; renderDetail(); });
}
function selectTask(id) {
  selected = data.cases.find((x) => x.id === id) || data.cases.find((x) => x.id === data.demos[0].task);
  stepIndex = 0; renderList(); renderDetail();
}
function renderAll() { localize(); renderLeaderboard(); renderDemos(); renderClarifications(); renderList(); renderDetail(); }
$('languageToggle').addEventListener('click', () => { language = language === 'en' ? 'zh' : 'en'; localStorage.setItem('clickclick-site-language', language); if (data) renderAll(); else localize(); });
$('search').addEventListener('input', () => { if (data) renderList(); });
$('taskList').addEventListener('click', (event) => { const button = event.target.closest('[data-id]'); if (button) { selectTask(button.dataset.id); history.replaceState(null, '', `#${encodeURIComponent(selected.id)}`); } });
document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => { filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach((x) => x.classList.toggle('active', x === button)); if (data) renderList(); }));
document.querySelectorAll('[name="variant"]').forEach((input) => input.addEventListener('change', () => { variant = input.value; stepIndex = 0; if (data) { renderList(); renderDetail(); } }));
window.addEventListener('hashchange', () => { if (data) { let id; try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; } if (data.cases.some((x) => x.id === id)) { selectTask(id); $('explore').scrollIntoView(); } } });
localize();
fetch('data.json').then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }).then((value) => { data = value; let id; try { id = decodeURIComponent(location.hash.slice(1)); } catch { id = ''; } selectTask(id); renderAll(); if (data.cases.some((x) => x.id === id)) $('explore').scrollIntoView(); }).catch((error) => { $('detail').textContent = `${t('Could not load results', '无法加载成绩')}: ${error.message}`; });
