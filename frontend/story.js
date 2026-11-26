// "One story": what every place says about a flight right now, side by side, checked against the truth.
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const PLACES = { app: 'App', sms: 'Text message', gate_screen: 'Gate screen', call_center: 'Call center', counter: 'Ticket counter', gate_agents: 'Gate agents', crew: 'Crew' };

export function oneStoryHtml(flight) {
  if (!flight || !flight.one_story) return '';
  const tiles = flight.one_story.map((p) => {
    const cls = p.matches === true ? 'ok' : p.matches === false ? 'bad' : 'none';
    const state = p.matches === true ? `Matches${p.since ? ` since ${esc(p.since)}` : ''}` : p.matches === false ? `Out of date${p.since ? ` since ${esc(p.since)}` : ''}` : 'Nothing sent here yet';
    return `<div class="os-tile ${cls}"><span class="os-place">${esc(PLACES[p.place] || p.place)}</span>
      <span class="os-says">${p.says ? esc(p.says) : '<span class="muted">-</span>'}</span><span class="os-state">${state}</span></div>`;
  }).join('');
  const wrong = flight.one_story.filter((p) => p.matches === false).length;
  return `<div class="one-story"><p class="os-truth"><b>${esc(flight.id)}, the truth:</b> ${esc(flight.truth || flight.status || '')}
    <span class="os-sum ${wrong ? 'bad' : 'ok'}">${wrong ? `${wrong} place${wrong > 1 ? 's' : ''} out of date` : 'Every place says the same'}</span></p>
    <div class="os-tiles">${tiles}</div></div>`;
}
