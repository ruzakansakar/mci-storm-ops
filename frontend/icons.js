// Filled icons for the five agents and the orchestrator (24 x 24, currentColor).
export const ICONS = {
  situation: '<path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5zm0 12.5a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/>',
  network_recovery: '<path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/>',
  passenger_care: '<path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/>',
  station_staff: '<path d="M8.5 2h7l1.2 4H7.3zM6 7.5h12L16.3 12H7.7zM9.6 13.5h4.8L15.5 22h-7z"/>',
  communication: '<path d="M20 2H4a2 2 0 0 0-2 2v18l4-4h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zM7 9h10v2H7zm0-3h10v2H7zm0 6h7v2H7z" fill-rule="evenodd"/>',
  orchestrator: '<circle cx="12" cy="12.5" r="3.6"/><circle cx="12" cy="3.8" r="2.6"/><circle cx="4.2" cy="18.2" r="2.6"/><circle cx="19.8" cy="18.2" r="2.6"/><path d="M11 5.5h2v4h-2zM5.4 16.4l3.3-2.1 1 1.7-3.3 2.1zM18.6 16.4l-3.3-2.1-1 1.7 3.3 2.1z"/>',
};
export const icon = (name, cls = 'ico') => `<svg class="${cls}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${ICONS[name] || ''}</svg>`;
