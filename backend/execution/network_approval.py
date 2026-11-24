"""Approval records only. This bridge deliberately has no operational executor."""
from copy import deepcopy


class NetworkApprovals:
    def __init__(self):
        self.options = {}
        self.asked = set()

    def reviewed(self, ctx, proposal, verdict, publish):
        if proposal['action'] not in ('delay_flight', 'cancel_flight', 'swap_crews', 'call_in_reserve_crew', 'deadhead_crew'):
            return
        self.options[proposal['id']] = deepcopy(proposal)
        rec = ctx.saved[('recommendations', proposal['id'])]
        payload = {k: proposal[k] for k in ('action', 'flight_id', 'new_departure', 'crew_id', 'crew_role') if k in proposal}
        payload.update(proposal_id=proposal['id'], role=rec['approver_role'], rule_check_ok=verdict['result'] == 'pass')
        publish('network_proposal_reviewed', payload)

    def ask(self, statuses, humans, now):
        if humans is None:
            return
        for item in statuses:
            rid = item['proposal_id']
            if item['status'] in ('superseded', 'rejected', 'blocked'):
                humans.withdraw('network_recovery', rid)
            elif item['status'] == 'pending' and item['rule_check_ok'] and rid not in self.asked:
                self.asked.add(rid)
                humans.ask('network_recovery', rid, {**self.options[rid], 'plan_version': item['plan_version']},
                           item['role'], now, True)

    def answer(self, ctx, memory, q, now, publish):
        item = getattr(memory, 'link_state', {}).get(q.rec_id, {})
        if (item.get('status') != 'pending' or not item.get('rule_check_ok') or item.get('role') != q.role
                or item.get('plan_version') != q.proposal.get('plan_version')):
            return None
        status = q.answer.get('decision')
        if status not in ('approved', 'rejected'):
            return None
        payload = {'proposal_id': q.rec_id, 'plan_version': item['plan_version'], 'status': status,
                   'role': q.role, 'decided_at': now.isoformat(), 'applied': False}
        ctx.saved[('recommendations', q.rec_id)]['status'] = status
        ctx._audit_row('network_' + status, 'recommendation', q.rec_id, None, payload)
        publish('network_approval_recorded', payload)
        return payload
