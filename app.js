const toggle = document.getElementById('languageToggle');
const localized = document.querySelectorAll('[data-en][data-zh]');
function setLanguage(language) {
  document.documentElement.lang = language;
  localized.forEach((element) => { if (element.tagName === 'TEXTAREA') element.value = element.dataset[language]; else element.textContent = element.dataset[language]; });
  document.querySelectorAll('[data-install-link]').forEach(element => {
    const chinese = language === 'zh';
    const anchor = element.dataset.installLink === 'manual'
      ? (chinese ? '也可以手动安装' : 'manual-installation')
      : (chinese ? '部署' : 'deployment');
    element.href = 'https://github.com/LordRosenberg/ClickClick' + (chinese ? '/blob/main/README.zh-CN.md' : '') + '#' + encodeURIComponent(anchor);
  });
  const notice = document.getElementById('installCopyStatus');
  if (notice) notice.textContent = '';
  toggle.textContent = language === 'en' ? '中文' : 'EN';
  localStorage.setItem('clickclick-site-language', language);
}
toggle.addEventListener('click', () => setLanguage(document.documentElement.lang === 'en' ? 'zh' : 'en'));
setLanguage(localStorage.getItem('clickclick-site-language') === 'zh' ? 'zh' : 'en');

const copyPrompt = document.getElementById('copyInstallPrompt');
if (copyPrompt) copyPrompt.addEventListener('click', async () => {
  const prompt = document.getElementById('installPrompt');
  const status = document.getElementById('installCopyStatus');
  const zh = document.documentElement.lang === 'zh';
  try {
    await navigator.clipboard.writeText(prompt.value);
    status.textContent = zh ? '已复制，发送给你的 PC 端 AI 助手。' : 'Copied. Send it to your PC AI assistant.';
  } catch {
    prompt.focus(); prompt.select();
    status.textContent = zh ? '请复制已选中的提示词。' : 'Copy the selected prompt.';
  }
});
