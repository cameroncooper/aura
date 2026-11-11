#!/usr/bin/env python3
"""Synthetic controller checks, written before implementation; no target access."""
import errno
import importlib.util
import os
from pathlib import Path
import pty
import struct
import sys
import unittest
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from trial import classify, raw_attributes, PassiveReader
from no_tx_loader import WireSeal

WD='Stage 1: J716c disk watchdog verified (160 s)\n'
READY='CDC USB2 controller ready; awaiting host enumeration, status=0x502\n'
FAILED='Chainload failed: PartitionNotFound\nRunning proxy...\n'
FACTORY='aura 1.0.0b10-gbd9f3e82-dirty\nWDT: could not arm the boot watchdog\nJ716c: USB2 proxy setup scheduled\n'

def scenario(text=WD+READY+FAILED,reset=162.0):
    return {'vector_intent':0.0,'vector_complete':0.01,'vector_bytes':64,
            'console_chunks':[{'time':3.0,'text':text,'generation':'1:10'},
                              {'time':reset+5,'text':FACTORY,'generation':'1:11'}],
            'usb_events':[{'time':2.0,'kind':'present','generation':'1:10'},
                          {'time':reset,'kind':'absent','generation':'1:10'},
                          {'time':reset+4,'kind':'present','generation':'1:11'}],
            'post_vector_tx_attempts':0,'interference':False,'capture_gaps':[]}

class Evidence(unittest.TestCase):
 def test_nominal_scope(self):
  x=classify(scenario());self.assertTrue(x['natural_expiry_consistent']);self.assertTrue(x['intended_scope_pass']);self.assertEqual(x['storage_outcome'],'partition_not_found')
 def test_early_or_usb_only_not_natural(self):
  for s in [scenario(reset=8),scenario(text=READY+FAILED)]:self.assertFalse(classify(s)['natural_expiry_consistent'])
  s=scenario();s['console_chunks'][1]['text']='CDC USB2 controller ready\n';self.assertFalse(classify(s)['reset_observed'])
 def test_prior_nvme_failure_is_distinct(self):
  s=scenario(text=WD+READY+'chainload: NVME init failed\nRunning proxy...\n');x=classify(s);self.assertEqual(x['storage_outcome'],'nvme_failed_before_partition');self.assertFalse(x['intended_scope_pass'])
 def test_reset_and_host_evidence_invalidates(self):
  for word in ['PANIC: bad','Assertion failed','Unhandled exception, rebooting...','Exception: SYNC','Stage 1: host request received','WDT: host attached, boot watchdog off']:
   s=scenario(text=WD+READY+FAILED+word+'\n');self.assertFalse(classify(s)['natural_expiry_consistent'],word)
 def test_tx_or_capture_ambiguity_invalidates(self):
  for field,value in [('post_vector_tx_attempts',1),('interference',True),('vector_bytes',12),('capture_gaps',[{'time':30.0,'reason':'read failed'}])]:
   s=scenario();s[field]=value;self.assertFalse(classify(s)['natural_expiry_consistent'],field)
 def test_lease_bounds_and_marker_age(self):
  self.assertFalse(classify(scenario(reset=140))['natural_expiry_consistent'])
  self.assertFalse(classify(scenario(reset=190))['natural_expiry_consistent'])
  s=scenario();s['console_chunks'][0]['time']=50;self.assertFalse(classify(s)['natural_expiry_consistent'])
 def test_reboot_required_is_not_a_software_reset(self):
  s=scenario(text=WD+READY+'nvme: setup failed; reboot required\nRunning proxy...\n');x=classify(s);self.assertFalse(x['software_reset_evidence']);self.assertFalse(x['intended_scope_pass'])

class NoTransmit(unittest.TestCase):
 def test_vector_is_single_final_wire_write(self):
  events=[];wire=[];seal=WireSeal(lambda e:events.append(e))
  packet=struct.pack('<IQ',0x01aa55ff,0xb)+bytes(52)
  self.assertEqual(seal.write(lambda b:wire.append(b)or len(b),packet,vector=True),64)
  with self.assertRaises(RuntimeError):seal.write(lambda b:wire.append(b)or len(b),b'late')
  self.assertEqual(wire,[packet]);self.assertTrue(seal.sealed);self.assertEqual(events[-1]['kind'],'blocked_tx')
 def test_partial_vector_still_seals(self):
  events=[];seal=WireSeal(events.append);packet=struct.pack('<IQ',0x01aa55ff,0xb)+bytes(52)
  self.assertEqual(seal.write(lambda b:7,packet,vector=True),7);self.assertTrue(seal.sealed)
 def test_passive_reader_is_kernel_read_only_on_host_pty(self):
  master,slave=pty.openpty();name=os.ttyname(slave)
  try:
   reader=PassiveReader(name)
   try:
    with self.assertRaises(OSError)as result:os.write(reader.fd,b'forbidden')
    self.assertEqual(result.exception.errno,errno.EBADF)
    os.write(master,b'passive-only\n');self.assertEqual(reader.read(4096),b'passive-only\n')
   finally:reader.close()
  finally:os.close(master);os.close(slave)

if __name__=='__main__':unittest.main()
