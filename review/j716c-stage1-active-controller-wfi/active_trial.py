#!/usr/bin/env python3
"""Pure eligibility/timing checks for active fallback and explicit30s API expiry."""
import re
from trial import classify as passive_classify, WD_MARK

ACTIVE_PHASE_SECONDS=20.0
ARM_OBSERVE_SECONDS=70.0


def fallback_gate(evidence, now):
    chunks=[c for c in evidence.get('console_chunks',[]) if c['time']<=now]
    text=''.join(c['text'] for c in chunks)
    wd=text.find(WD_MARK)
    ended=text.find('Stage 1: proxy window ended',max(0,wd))
    pnf=text.find('Chainload failed: PartitionNotFound',max(0,ended))
    running=text.find('Running proxy...',max(0,pnf))
    end=running+len('Running proxy...')
    length=0;receipt=None;generation=None
    for c in chunks:
        length+=len(c['text'])
        if running>=0 and length>=end:
            receipt=c['time'];generation=c['generation'];break
    base=passive_classify(dict(evidence,console_chunks=chunks))
    current=next((u for u in reversed(evidence.get('usb_events',[])) if u['time']<=now),None)
    same=current is not None and current['kind']=='present' and current['generation']==generation
    eligible=bool(wd>=0 and ended>wd and pnf>ended and running>pnf
        and base['marker_within_25s_of_vector'] and base['storage_outcome']=='partition_not_found'
        and base['CDC_ready_message_seen'] and not base['host_attached_after_entry']
        and not base['software_reset_evidence'] and not base['guard_refusal_seen']
        and evidence.get('vector_bytes')==64 and not evidence.get('post_vector_tx_attempts')
        and not evidence.get('interference') and receipt is not None and 0<=now-receipt<=6
        and now-evidence['vector_intent']<=60 and same and not base['capture_gap_ambiguity'])
    return {'eligible':eligible,'fallback_time':receipt,'generation':generation,
            'proxy_window_ended_seen':ended>=0,'storage_outcome':base['storage_outcome']}


def recovery_deadline(evidence):
    arm=next((e['time'] for e in evidence.get('active_events',[]) if e['kind']=='active_arm_intent'),None)
    acked=any(e['kind']=='active_arm_verified' and e.get('retval')==0 for e in evidence.get('active_events',[]))
    return arm+ARM_OBSERVE_SECONDS if arm is not None and acked else evidence['vector_intent']+195.0


def samples_valid(events):
    samples=[x for x in events if x['kind']=='active_snapshot']
    arm=next((x['time'] for x in events if x['kind']=='active_arm_intent'),None)
    if len(samples)!=3 or arm is None:return False
    if not all(x['alarm']==720_000_000 and x['control']==4 and 0<=x['count']<720_000_000 for x in samples):return False
    if samples[0]['time']-arm>8 or samples[0]['count']>192_000_000 or samples[-1]['count']>=432_000_000:return False
    for a,b in zip(samples,samples[1:]):
        dt=b['time']-a['time'];ticks=b['count']-a['count']
        if not 2<=dt<=5 or ticks<=0 or abs(ticks/24_000_000-dt)>.75:return False
    return True


def classify_active(evidence):
    passive=passive_classify(evidence)
    events=evidence.get('active_events',[])
    def one(kind):return next((e for e in events if e['kind']==kind),None)
    start=one('active_start');arm=one('active_arm_intent');ack=one('active_arm_verified');sealed=one('active_sealed')
    done=one('active_probe_complete');identity=one('active_identity_verified');config=one('active_config_verified')
    initial=dict(evidence,console_chunks=[c for c in evidence.get('console_chunks',[]) if not start or c['time']<start['time']])
    gate=fallback_gate(initial,start['time'] if start else float('inf')) if start else {'eligible':False}
    reset=passive['reset_disconnect_time']
    timing=bool(arm and ack and reset is not None and arm['time']+26<=reset<=ack['time']+36)
    error=any(e['kind'] in ('active_error','blocked_active_tx','rejected_active_tx') for e in events)
    phase=bool(start and sealed and 0<=sealed['time']-start['time']<=ACTIVE_PHASE_SECONDS+1)
    ordered=bool(start and identity and config and arm and ack and sealed and done and ack['retval']==0
       and start['time']<=arm['time']<=ack['time']<=identity['time']<=sealed['time']
       and ack['time']<=config['time']<=sealed['time']<=done['time'])
    good_samples=samples_valid(events)
    consistent=bool(passive['reset_observed'] and timing and ordered and phase and good_samples and not error
       and not passive['software_reset_evidence'] and not passive['guard_refusal_seen']
       and not passive['capture_gap_ambiguity'] and not evidence.get('interference'))
    result=dict(passive)
    result.pop('natural_expiry_consistent',None)
    result.pop('lease_timing_consistent',None)
    result.pop('post_vector_wire_pristine',None)
    result.update({'scope':'Post-lookup active proxy and explicit P_WDT_ARM(30), distinct from natural160s boot expiry',
       'natural_160s_expiry_test':False,'loader_final_vector_seal_intact':passive['post_vector_wire_pristine'],'fallback_eligible_before_contact':gate['eligible'],
       'active_identity_verified':identity is not None,'active_config_verified':config is not None,
       'explicit_arm_intent_time':arm['time'] if arm else None,'explicit_arm_acknowledged':ack is not None and ack['retval']==0,
       'active_phase_bounded':phase,'active_protocol_sequence_valid':ordered,'watchdog_samples_valid':good_samples,
       'active_wire_error':error,'explicit_30s_lease_timing_consistent':timing,
       'explicit_30s_expiry_consistent':consistent,'intended_scope_pass':bool(consistent and gate['eligible']),
       'cause_limit':'Explicit30s arm/readback/source/timing inference; no reset-cause register read',
       'USB_limit':'Real bounded proxy session only after observed post-lookup fallback; passive observation after final seal'})
    return result

# Interface compatible with the proven controller's reset/recovery decisions.
classify=classify_active
