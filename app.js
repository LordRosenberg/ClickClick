const toggle = document.getElementById('languageToggle');
const localized = document.querySelectorAll('[data-en][data-zh]');
function setLanguage(language) {
  document.documentElement.lang = language;
  localized.forEach((element) => { element.textContent = element.dataset[language]; });
  toggle.textContent = language === 'en' ? '中文' : 'EN';
  localStorage.setItem('clickclick-site-language', language);
}
toggle.addEventListener('click', () => setLanguage(document.documentElement.lang === 'en' ? 'zh' : 'en'));
setLanguage(localStorage.getItem('clickclick-site-language') === 'zh' ? 'zh' : 'en');
