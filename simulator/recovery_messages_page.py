"""The complete draft lifecycle, separate from messages actually sent to passengers."""
from html import escape


def section(metrics):
    history = metrics.get('recovery_messages', [])
    if not history:
        return ''
    rows = []
    for d in history:
        cells = [d.get('as_of', d.get('created', '')), d['flight_id'],
                 d.get('passenger_id') or d.get('crew_id') or 'Staff only', d['audience'],
                 f"v{d['plan_version']}", d['proposal_id'], d['status'], d.get('reason', ''), d['text']]
        rows.append('<tr>' + ''.join('<td>' + escape(str(c)) + '</td>' for c in cells) + '</tr>')
    return ('<section id="recovery-messages"><h2>Network Recovery → Communication</h2>'
            '<p>Approval does not apply a flight or crew change. Passenger and crew drafts stay held until operational '
            'records confirm it. Internal staff notes never go to passenger channels.</p>'
            '<details open><summary>Drafts, holds, sends and discards (originating plan version)</summary>'
            '<div style="max-height:650px;overflow:auto"><table><tr>'
            '<th>Time</th><th>Flight</th><th>Recipient</th><th>Audience</th><th>Plan</th><th>Proposal</th>'
            '<th>Status</th><th>Reason</th><th>Exact text</th></tr>' + ''.join(rows) + '</table></div></details></section>')
