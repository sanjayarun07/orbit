(() => {
  const menu = document.getElementById('menuToggle');
  const links = document.getElementById('navLinks');
  const stage = document.getElementById('artStage');
  const hero = document.querySelector('.hero-art');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const narrow = window.matchMedia('(max-width: 850px)');

  const closeMenu = () => {
    links.classList.remove('open');
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', 'Open navigation');
  };

  menu?.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    links.classList.toggle('open', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  });
  links?.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeMenu();
  });
  document.addEventListener('click', event => {
    if (!menu?.contains(event.target) && !links?.contains(event.target)) closeMenu();
  });
  narrow.addEventListener('change', closeMenu);

  const reveal = document.querySelectorAll('[data-reveal]');
  if ('IntersectionObserver' in window && !reducedMotion.matches) {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: .09, rootMargin: '0px 0px -35px 0px' });
    reveal.forEach(el => observer.observe(el));
  } else {
    reveal.forEach(el => el.classList.add('is-visible'));
  }

  // The signal graph follows the pointer slightly; touch and reduced-motion
  // devices keep the relationships still.
  if (stage && hero && !reducedMotion.matches && window.matchMedia('(pointer: fine)').matches) {
    let frame = 0;
    let x = 0;
    let y = 0;
    hero.addEventListener('pointermove', event => {
      const bounds = hero.getBoundingClientRect();
      x = (event.clientX - bounds.left) / bounds.width - .5;
      y = (event.clientY - bounds.top) / bounds.height - .5;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        stage.style.setProperty('--shift-x', `${Math.max(-1, Math.min(1, x * 2)) * 5}px`);
        stage.style.setProperty('--shift-y', `${Math.max(-1, Math.min(1, y * 2)) * 5}px`);
        frame = 0;
      });
    });
    hero.addEventListener('pointerleave', () => {
      stage.style.setProperty('--shift-x', '0px');
      stage.style.setProperty('--shift-y', '0px');
    });
  }

  document.getElementById('year').textContent = String(new Date().getFullYear());
})();
