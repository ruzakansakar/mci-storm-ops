"""An approval is permission, not evidence that an operational change happened."""
from copy import deepcopy
from datetime import timedelta
from random import Random
from types import SimpleNamespace

import pytest

from backend.agents.communication.recovery import RecoveryMemory, prepare
from backend.agents.communication.state_at import clock, Flight
from backend.agents.communication.detect import load_settings
from backend.contracts.buffered import BufferedContext, SharedStore
from backend.contracts.context import ContractViolation
from backend.execution.executor import check_only
from backend.execution.simulated_human import SimulatedHumans
from backend.rules.recovery_message import send_problems


def fixture():
    now = clock('14:15')
    change = dict(proposal_id=12, action='swap_crews', flight_id='WN1033', crew_id='C35', role='fa')
    approval = dict(proposal_id=12, plan_version=3, status='pending', role='crew_scheduling', rule_check_ok=True)
    store = SharedStore()
    store.records['recovery_plans'].append(dict(version=3, planned_at=now, delays=[], cancels=[],
                                               crew_actions=[change], approval_status=[approval]))
    store.heartbeats['network_recovery'] = [now]
    ctx = BufferedContext(now, store=store, rows={'passengers': [], 'flights': []})
    ctx.feed_age_sec = lambda feed: 0
    trip = dict(itinerary=['WN1033'], status='delayed')
    st = SimpleNamespace(flights={'WN1033': Flight('WN1033', 'departure', now, now)},
                         truth={'PAX0001': trip}, history={'PAX0001': [(now, 1, trip)]})
    return ctx, st, RecoveryMemory(), SimpleNamespace(conflicts={}), change, approval


def run_prepare(f):
    ctx, st, memory, situation, _, _ = f
    return prepare(ctx, {'state': st}, situation, memory, load_settings(), {'PAX0001': ['app', 'sms']})


def application(f):
    ctx, _, _, _, change, _ = f
    ctx.events.append(dict(id=1, occurred_at=ctx.now, type='recovery_action_applied', station=None,
                           payload={**change, 'plan_version': 3}))


def test_drafts_are_proposal_checked_and_internal_staff_only():
    f = fixture()
    assert run_prepare(f) == []
    ctx, _, memory, _, _, _ = f
    pax = memory.drafts[(12, 'passenger', 'PAX0001')]
    crew = memory.drafts[(12, 'crew', 'C35')]
    staff = memory.drafts[(12, 'staff', 'internal')]
    # Phase 5a: the passenger hears the outcome (the departure time), never the internals; the crew hears when to report
    assert pax['text'] == 'Your flight WN1033 is now expected to leave at 14:15.'
    assert crew['text'] == 'Crew C35: you are reassigned to WN1033. Please report by 13:15.'
    assert pax['status'] == crew['status'] == 'held'
    assert pax['reason'] == 'waiting for approval of plan v3, proposal 12'
    assert staff['status'] == 'sent' and staff['internal'] and staff['channel'] == 'staff'
    assert staff['text'] == "Internal - WN1033: crew swap under review; don't give passengers a new time yet."
    assert all(h['plan_version'] == 3 for h in memory.history)
    assert len([a for a in ctx.audit if a['entity_type'] == 'events']) == len(memory.history)


@pytest.mark.parametrize('status', ['pending', 'rejected', 'superseded', 'blocked'])
def test_no_unapproved_proposal_can_send_even_if_already_true(status):
    f = fixture()
    f[-1]['status'] = status
    application(f)
    assert not run_prepare(f)


def test_approval_is_not_application():
    f = fixture()
    run_prepare(f)
    f[-1]['status'] = 'approved'
    assert not run_prepare(f)
    pax = f[2].drafts[(12, 'passenger', 'PAX0001')]
    assert pax['status'] == 'held'
    assert pax['reason'] == 'approved, waiting to take effect'
    proposal = dict(agent='communication', action='send_status_update', decision_ids=['O05'],
                    data_as_of=f[0].now, requested_level='auto', messages=[pax])
    assert check_only(proposal, f[2].world)['result'] == 'block'


def test_approved_and_applied_proposal_can_send():
    f = fixture()
    f[-1]['status'] = 'approved'
    application(f)
    ready = run_prepare(f)
    assert {x['audience'] for x in ready} == {'passenger', 'crew'}
    pax = next(x for x in ready if x['audience'] == 'passenger')
    proposal = dict(agent='communication', action='send_status_update', decision_ids=['O05'],
                    data_as_of=f[0].now, requested_level='auto', messages=[pax])
    assert check_only(proposal, f[2].world)['result'] == 'pass'


@pytest.mark.parametrize('failure', ['wrong_version', 'wrong_crew', 'cancelled', 'stale', 'conflict', 'tampered_text', 'wrong_role'])
def test_send_time_guard_fails_closed(failure):
    f = fixture()
    f[-1]['status'] = 'approved'
    application(f)
    pax = next(x for x in run_prepare(f) if x['audience'] == 'passenger')
    world = deepcopy(f[2].world)
    if failure == 'wrong_version':
        world['recovery_applications'][12]['plan_version'] = 2
    elif failure == 'wrong_crew':
        world['recovery_crews'][('WN1033', 'fa')] = 'C36'
    elif failure == 'cancelled':
        world['recovery_flights']['WN1033']['status'] = 'cancelled'
    elif failure == 'stale':
        world['recovery_fresh'] = False
    elif failure == 'conflict':
        world['open_conflicts'] = {'WN1033'}
    elif failure == 'tampered_text':
        pax = {**pax, 'text': 'Your flight leaves at 15:00.'}
    elif failure == 'wrong_role':
        world['recovery_view']['facts']['approval_status'][0]['role'] = 'customer_service_supervisor'
    assert send_problems(pax, world)


@pytest.mark.parametrize('status', ['rejected', 'superseded'])
def test_rejected_or_superseded_drafts_are_discarded(status):
    f = fixture()
    run_prepare(f)
    f[-1]['status'] = status
    assert not run_prepare(f)
    assert f[2].drafts[(12, 'passenger', 'PAX0001')]['status'] == 'discarded'
    assert f[2].drafts[(12, 'crew', 'C35')]['status'] == 'discarded'
    assert not any(x['audience'] == 'passenger' and x['status'] == 'sent' for x in f[2].history)


def test_changed_trip_redrafts_before_send():
    f = fixture()
    run_prepare(f)
    f[1].truth['PAX0001'] = dict(itinerary=['WN1033'], status='boarding')
    f[-1]['status'] = 'approved'
    application(f)
    ready = run_prepare(f)
    pax = next(x for x in ready if x['audience'] == 'passenger')
    assert pax['revision'] == 2 and pax['trip_facts']['status'] == 'boarding'
    assert any(x['status'] == 'discarded' and x['revision'] == 1 for x in f[2].history)


def test_contract_strips_private_nested_fields_and_future_plan():
    f = fixture()
    ctx = f[0]
    row = ctx.store.records['recovery_plans'][0]
    row['crew_actions'][0].update(option={'secret': 'x'}, private_note='x')
    row['warnings'] = ['private']
    row['approval_status'][0]['private_note'] = 'x'
    ctx.store.records['recovery_plans'].append({**row, 'version': 4, 'planned_at': ctx.now + timedelta(minutes=1)})
    view = ctx.read_shared('recovery_plans')
    assert view['version'] == 3
    assert set(view['facts']) == {'delays', 'cancels', 'crew_actions', 'approval_status'}
    assert 'option' not in view['facts']['crew_actions'][0]
    assert 'private_note' not in str(view)
    with pytest.raises(ContractViolation):
        ctx.read('recovery_plans')


def test_network_humans_ignore_blocked_and_leave_supervisor_random_stream_unchanged():
    now = clock('14:15')
    a, b = [SimulatedHumans(Random(7), lambda *args: None) for _ in range(2)]
    assert a.ask('network_recovery', 1, {'action': 'swap_crews'}, 'crew_scheduling', now, False) is None
    q = a.ask('network_recovery', 2, {'action': 'swap_crews'}, 'crew_scheduling', now, True)
    assert q.status == 'waiting'
    args = ('communication', 3, {'action': 'draft_message'}, 'customer_service_supervisor', now, True)
    assert a.ask(*args).due_at == b.ask(*args).due_at


def test_executor_releases_an_approved_applied_passenger_draft_through_context():
    from backend.execution.copilot import Copilot
    f = fixture()
    f[-1]['status'] = 'approved'
    application(f)
    item = next(x for x in run_prepare(f) if x['audience'] == 'passenger')
    executor = SimpleNamespace(recovery=f[2], humans=None)
    delivered = []
    Copilot._send_recovery(executor, f[0], item, f[0].now, lambda p: delivered.append(p))
    assert len(delivered) == 1 and delivered[0]['messages'][0]['plan_version'] == 3
    assert item['status'] == 'sent'
    assert f[2].history[-1]['status'] == 'sent'


@pytest.mark.parametrize('change', ['rejected', 'superseded', 'cancelled'])
def test_queued_recovery_message_is_dropped_if_approval_or_truth_changes(change):
    from simulator.channels import Channels
    from simulator.config import load_config
    f = fixture()
    f[-1]['status'] = 'approved'
    application(f)
    item = next(x for x in run_prepare(f) if x['audience'] == 'passenger')
    channels = Channels(deepcopy(load_config()), Random(1), {'PAX0001': dict(item['trip_facts'])}, 0)
    channels.msg_rng = Random(2)
    channels.recovery_guard = lambda m: send_problems(m, f[2].world)
    assert channels.deliver_messages(10, {'messages': [item]})['accepted']
    if change == 'cancelled':
        f[2].world['recovery_flights']['WN1033']['status'] = 'cancelled'
    else:
        f[2].world['recovery_view']['facts']['approval_status'][0]['status'] = change
    channels.apply_messages(1000, recovery_only=True)
    assert [e['type'] for e in channels.events] == ['message_dropped']
    assert channels.events[0]['payload']['plan_version'] == 3
