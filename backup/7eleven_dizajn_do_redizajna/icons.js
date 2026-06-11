/* Набор SVG-иконок (стиль lucide, stroke = currentColor).
   Использование: icon7('search') или icon7('bus', 16). */
(function () {
  const P = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    pin: '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 1v4M12 19v4M1 12h4M19 12h4"/>',
    bus: '<path d="M4 6 2 7"/><path d="M10 6h4"/><path d="m22 7-2-1"/><rect x="4" y="3" width="16" height="14" rx="2"/><path d="M4 11h16"/><circle cx="8" cy="14.5" r=".8"/><circle cx="16" cy="14.5" r=".8"/><path d="M6 17v3M18 17v3"/>',
    train: '<rect x="4" y="3" width="16" height="14" rx="2"/><path d="M4 11h16"/><circle cx="8.5" cy="14" r=".8"/><circle cx="15.5" cy="14" r=".8"/><path d="m8 20-1 1.5M16 20l1 1.5M7 20h10"/>',
    tram: '<rect x="5" y="5" width="14" height="12" rx="2"/><path d="M5 10h14M9 2l3 3 3-3"/><circle cx="9" cy="13.5" r=".8"/><circle cx="15" cy="13.5" r=".8"/><path d="m8 17-1 3M16 17l1 3"/>',
    ferry: '<path d="M3 16c1 1 2 1 3 0s2-1 3 0 2 1 3 0 2-1 3 0 2 1 3 0 2-1 3 0"/><path d="M5 13 4 9h16l-1 4"/><path d="M8 9V5h8v4"/>',
    walk: '<circle cx="13" cy="4" r="2"/><path d="m9.5 22 2-5.5L9 14v-4l3-2 3 3 3 1"/><path d="M9 10 7 13l-3-1"/><path d="m13.5 16.5 2.5 5.5"/>',
    store: '<path d="M3 9 4.5 4h15L21 9"/><path d="M4 9v11h16V9"/><path d="M9 20v-6h6v6"/><path d="M3 9h18"/>',
    cart: '<circle cx="9" cy="20" r="1.6"/><circle cx="17" cy="20" r="1.6"/><path d="M2 3h2.5l2.7 12.4a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.6L21 7H6"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    flask: '<path d="M10 2v6L4.5 18.5A2 2 0 0 0 6.2 21.5h11.6a2 2 0 0 0 1.7-3L14 8V2"/><path d="M8.5 2h7"/><path d="M7 15h10"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    eraser: '<path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/><path d="M22 21H7"/><path d="m5 11 9 9"/>',
  };
  window.icon7 = function (name, size = 14) {
    const body = P[name] || P.pin;
    return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
      `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
      `aria-hidden="true">${body}</svg>`;
  };
  // подставить иконки в статичную разметку: <i data-ic="search" data-sz="16"></i>
  window.applyStaticIcons = function (root) {
    (root || document).querySelectorAll("[data-ic]").forEach((el) => {
      el.outerHTML = window.icon7(el.dataset.ic, +(el.dataset.sz || 14));
    });
  };
})();
