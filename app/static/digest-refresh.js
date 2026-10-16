(() => {
  let openDetails = [];
  const replacesResults = (event) => event.detail?.target?.id === 'digest-results';

  document.addEventListener('htmx:beforeSwap', (event) => {
    if (!replacesResults(event)) return;
    openDetails = Array.from(document.querySelectorAll('#digest-results details[id][open]'),
      (details) => details.id);
  });

  document.addEventListener('htmx:afterSwap', (event) => {
    if (!replacesResults(event)) return;
    for (const id of openDetails) {
      const details = document.getElementById(id);
      if (details) details.open = true;
    }
  });
})();
