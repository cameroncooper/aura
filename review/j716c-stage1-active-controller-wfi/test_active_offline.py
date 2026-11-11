#!/usr/bin/env python3
"""Written before implementation: synthetic active-probe failure coverage only."""
import struct
import unittest
from active_trial import fallback_gate, classify_active, recovery_deadline
from active_probe import ActiveWire

WD='Stage 1: J716c disk watchdog verified (160 s)\n'
READY='CDC USB2 controller ready; awaiting host enumeration, status=0x502\n'
END='Stage 1: proxy window ended\n'
PNF='Chainload failed: PartitionNotFound\n'
RUN='Running proxy...\n'
FACTORY='aura 1.0.0b10-gbd9f3e82-dirty\nWDT: could not arm the boot watchdog\nJ716c: USB2 proxy setup scheduled\n'

def evidence():
 chunks=[{'time':3,'text':WD+READY,'generation':'1:10'}, {'time':10,'text':END,'generation':'1:10'},
         {'time':11,'text':PNF+RUN,'generation':'1:10'},
         {'time':12,'text':'WDT: host attached, boot watchdog off\n','generation':'1:10'},
         {'time':55,'text':FACTORY,'generation':'1:11'}]
 events=[{'kind':'active_start','time':12}, {'kind':'active_identity_verified','time':20.12},
         {'kind':'active_config_verified','time':20.14},
         {'kind':'active_arm_intent','time':20}, {'kind':'active_arm_verified','time':20.1,'retval':0},
         *[{'kind':'active_snapshot','time':20.2+i*3,'count':int((.2+i*3)*24000000),'alarm':720000000,'control':4} for i in range(3)],
         {'kind':'active_sealed','time':26.3}, {'kind':'active_probe_complete','time':26.4}]
 return {'vector_intent':0,'vector_bytes':64,'console_chunks':chunks,
         'usb_events':[{'kind':'present','time':2,'generation':'1:10'},{'kind':'absent','time':50,'generation':'1:10'},{'kind':'present','time':54,'generation':'1:11'}],
         'capture_gaps':[],'post_vector_tx_attempts':0,'interference':False,'active_events':events}

class ActiveEvidence(unittest.TestCase):
 def test_gate_requires_real_fallback_and_ended_window(self):
  e=evidence();e['console_chunks']=e['console_chunks'][:3];self.assertTrue(fallback_gate(e,now=12)['eligible'])
  for text in [WD+READY+RUN,WD+READY+END+RUN,WD+READY+PNF+RUN,WD+READY+END+'nvme: setup failed; reboot required\n'+PNF+RUN]:
   e=evidence();e['console_chunks']=[{'time':11,'text':text,'generation':'1:10'}];self.assertFalse(fallback_gate(e,now=12)['eligible'])
 def test_gate_rejects_contact_panic_or_stale_evidence(self):
  e=evidence();e['console_chunks']=e['console_chunks'][:3];self.assertFalse(fallback_gate(e,now=22)['eligible'])
  for text in ['PANIC: fail','WDT: host attached, boot watchdog off']:
   e=evidence();e['console_chunks']=e['console_chunks'][:3]+[{'time':11.1,'text':text,'generation':'1:10'}];self.assertFalse(fallback_gate(e,now=12)['eligible'])
 def test_explicit30_is_separate_from_natural160(self):
  x=classify_active(evidence());self.assertTrue(x['intended_scope_pass']);self.assertTrue(x['explicit_30s_expiry_consistent']);self.assertFalse(x['natural_160s_expiry_test'])
 def test_counter_and_alarm_control_corruption_fail(self):
  for key,val in [('alarm',3840000000),('control',0),('count',1)]:
   e=evidence();[v for v in e['active_events'] if v['kind']=='active_snapshot'][-1][key]=val;self.assertFalse(classify_active(e)['intended_scope_pass'])
 def test_missing_ack_seal_or_late_early_reset_fail(self):
  for kind in ['active_arm_verified','active_identity_verified','active_config_verified','active_sealed','active_probe_complete']:
   e=evidence();e['active_events']=[v for v in e['active_events'] if v['kind']!=kind];self.assertFalse(classify_active(e)['intended_scope_pass'],kind)
  for t in [24,40]:
   e=evidence();e['usb_events'][1]['time']=20+t;self.assertFalse(classify_active(e)['intended_scope_pass'])
 def test_fault_interference_and_extra_tx_fail(self):
  for event in [{'kind':'blocked_active_tx','time':27},{'kind':'active_error','time':27}]:
   e=evidence();e['active_events'].append(event);self.assertFalse(classify_active(e)['intended_scope_pass'])
  e=evidence();e['interference']=True;self.assertFalse(classify_active(e)['intended_scope_pass'])
 def test_recovery_uses_possible_arm_even_without_ack(self):
  e=evidence();self.assertEqual(recovery_deadline(e),90)
  e['active_events']=[x for x in e['active_events'] if x['kind']!='active_arm_verified'];self.assertEqual(recovery_deadline(e),195)
  e['active_events']=[];self.assertEqual(recovery_deadline(e),195)

class PermittedWire(unittest.TestCase):
 def make(self):
  events=[];wire=[];g=ActiveWire(events.append,deadline=1000,clock=lambda:10)
  return g,events,wire,lambda b:wire.append(b) or len(b)
 def packet(self,cmd,*values):return struct.pack('<I',cmd)+struct.pack('<7Q',*values,*([0]*(7-len(values))))+bytes(4)
 def test_only30_as_first_authorized_packet_once(self):
  g,e,w,writer=self.make()
  with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x01aa55ff,0x15,30))
  self.assertEqual(w,[])
  g.fallback_authorized=True
  for packet in [self.packet(0x00aa55ff),self.packet(0x01aa55ff,0x13)]:
   with self.assertRaises(RuntimeError):g.write(writer,packet)
  for n in [0,29,31,160]:
   with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x01aa55ff,0x15,n))
  g.write(writer,self.packet(0x01aa55ff,0x15,30));self.assertEqual(len(w),1)
  with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x01aa55ff,0x15,30))
 def test_no_mmio_write_disable_or_exec(self):
  for op in [0x101,0x16,0xb,0x2,0x10,0x700]:
   g,e,w,writer=self.make();g.arm_seen=g.arm_verified=True;g.identity_verified=g.config_verified=True
   with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x01aa55ff,op,0x28836c010,4))
   self.assertEqual(w,[])
 def test_bounded_ram_regions_and_exact_mmio(self):
  g,e,w,writer=self.make();g.arm_seen=g.arm_verified=True;g.regions=[(0x1000,0x2000)];g.write(writer,self.packet(0x02aa55ff,0x1000,260))
  for addr,size in [(0xfff,260),(0x1fff,2),(0x1000,16385)]:
   with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x02aa55ff,addr,size))
  g.identity_verified=True;g.write(writer,self.packet(0x01aa55ff,0x105,0x28836c010))
  with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x01aa55ff,0x105,0x288330008))
 def test_seal_and_deadline_block_without_wire_write(self):
  g,e,w,writer=self.make();g.seal();
  with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x00aa55ff))
  self.assertEqual(w,[]);self.assertEqual(e[-1]['kind'],'blocked_active_tx')
  g,e,w,writer=self.make();g.deadline=9
  with self.assertRaises(RuntimeError):g.write(writer,self.packet(0x00aa55ff))
  self.assertEqual(w,[])
 def test_partial_arm_is_durable_possible_arm_and_sealed(self):
  g,e,w,writer=self.make();g.fallback_authorized=True
  with self.assertRaises(RuntimeError):g.write(lambda b:7,self.packet(0x01aa55ff,0x15,30))
  self.assertTrue(g.sealed);self.assertTrue(any(v['kind']=='active_arm_intent' for v in e))

if __name__=='__main__':unittest.main()
