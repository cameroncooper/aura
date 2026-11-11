#!/usr/bin/env python3
"""Run production exact-board/v3 arming code with ADT/MMIO failure injection."""
from pathlib import Path
import re,subprocess
R=Path(__file__).resolve().parents[2];E=Path(__file__).resolve().parent;S=R/'build/j716c-persistent-stage1-source/src'
def extract(path,name):
 text=path.read_text();m=re.search(r'^static (?:inline )?(?:bool|int) '+name+r'\([^;]*?\)\n\{',text,re.M)
 if not m:raise SystemExit('Expected preimplementation failure: missing '+name)
 at=m.end();depth=1
 while depth:depth+=(text[at]=='{')-(text[at]=='}');at+=1
 return text[m.start():at]
if not (S/'j716c_recovery.h').exists():raise SystemExit('Expected preimplementation failure: exact-board recovery header absent')
board=extract(S/'j716c_recovery.h','j716c_recovery_board')
arm=extract(S/'wdt.c','wdt_arm_j716c')
pre=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
typedef uint8_t u8;typedef uint32_t u32;typedef uint64_t u64;
#define T6050 0x6050
#define WDT_COUNT 0x10
#define WDT_ALARM 0x14
#define WDT_CTL 0x1c
#define GUARD_MARK 1
#define GUARD_SILENT 2
#define GUARD_OFF 0
#define sysop(x) do{}while(0)
static u32 chip_id,board_id;static int adt_data;static int *adt=&adt_data;
static const char *model,*target;static u32 model_len,target_len;
static u64 wdt_base;static unsigned wdt_armed_seconds;
static int version_len,node_ok,reg0_ok,reg2_ok;static u32 version;static u64 base,size,reset,reset_size;
static int exc_guard,exc_count,op_count,fault_at,write_count;
static u32 control,alarm,count,reset_value;static int bad_alarm,bad_control,bad_count;
static const void*adt_getprop(int*a,int n,const char*key,u32*len){(void)a;(void)n;if(!strcmp(key,"model")){*len=model_len;return model;}assert(!strcmp(key,"target-type"));*len=target_len;return target;}
static int adt_path_offset_trace(int*a,const char*path,int*out){(void)a;(void)out;assert(!strcmp(path,"/arm-io/wdt"));return node_ok?1:-1;}
static int prop(int*a,int n,const char*key,u32*out){(void)a;(void)n;assert(!strcmp(key,"wdt-version"));*out=version;return version_len;}
#define ADT_GETPROP(a,n,k,o) prop(a,n,k,o)
static int adt_get_reg(int*a,int*path,const char*key,int index,u64*out,u64*len){(void)a;(void)path;assert(!strcmp(key,"reg"));if(index==0){*out=base;*len=size;return reg0_ok?0:-1;}assert(index==2);*out=reset;*len=reset_size;return reg2_ok?0:-1;}
static void access_op(void){assert(exc_guard==(GUARD_MARK|GUARD_SILENT));if(++op_count==fault_at)exc_count++;}
static u32 read32(u64 addr){access_op();if(addr==reset)return reset_value;if(addr==base+WDT_CTL)return write_count&&bad_control?0:control;if(addr==base+WDT_ALARM)return bad_alarm?alarm^1:alarm;assert(addr==base+WDT_COUNT);return bad_count?alarm:count;}
static void write32(u64 addr,u32 value){access_op();write_count++;if(addr==base+WDT_ALARM){assert(write_count==1);alarm=value;}else if(addr==base+WDT_COUNT){assert(write_count==2);count=value;}else{assert(addr==base+WDT_CTL&&write_count==3&&value==4);control=value;}}
static void reset_mock(void){chip_id=T6050;board_id=0xe;model="Mac17,6";target="J716c";model_len=8;target_len=6;version=3;version_len=4;node_ok=reg0_ok=reg2_ok=1;base=0x28836c000;size=0x4000;reset=0x288330008;reset_size=4;reset_value=0;control=alarm=count=0;bad_alarm=bad_control=bad_count=0;exc_guard=exc_count=op_count=write_count=0;fault_at=-1;wdt_base=wdt_armed_seconds=0;}
'''
tests=r'''
int main(void){
 reset_mock();assert(j716c_recovery_board());
 for(int field=0;field<8;field++){reset_mock();switch(field){case 0:chip_id++;break;case 1:board_id++;break;case 2:model="Mac17,8";break;case 3:target="J716s";break;case 4:model_len--;break;case 5:target_len++;break;case 6:model=NULL;break;case 7:target=NULL;break;}assert(!j716c_recovery_board());assert(wdt_arm_j716c(160)<0&&!write_count);}
 for(int field=0;field<10;field++){reset_mock();switch(field){case 0:version=2;break;case 1:version_len=3;break;case 2:node_ok=0;break;case 3:reg0_ok=0;break;case 4:reg2_ok=0;break;case 5:base+=4;break;case 6:size-=4;break;case 7:reset+=4;break;case 8:reset_size=8;break;case 9:reset_value=1;break;}assert(wdt_arm_j716c(160)<0&&!write_count);assert(exc_guard==GUARD_OFF);}
 for(unsigned seconds=0;seconds<=180;seconds++){reset_mock();int result=wdt_arm_j716c(seconds);assert((result==0)==(seconds>=1&&seconds<=160));assert(exc_guard==GUARD_OFF);if(!result){assert(write_count==3&&alarm==seconds*24000000U&&control==4&&wdt_base==base&&wdt_armed_seconds==seconds);}else assert(!write_count);}
 reset_mock();control=4;assert(!wdt_arm_j716c(160));
 reset_mock();control=5;assert(wdt_arm_j716c(160)<0&&!write_count);
 for(int op=1;op<=8;op++){reset_mock();fault_at=op;assert(wdt_arm_j716c(160)<0);assert(!wdt_base&&!wdt_armed_seconds&&exc_guard==GUARD_OFF);if(op<=2)assert(!write_count);}
 for(int bad=1;bad<=3;bad++){reset_mock();if(bad==1)bad_alarm=1;if(bad==2)bad_control=1;if(bad==3)bad_count=1;assert(wdt_arm_j716c(160)<0&&!wdt_base&&!wdt_armed_seconds);}
 puts("PASS: exact board/ADT/seconds, 8 MMIO fault points, all readbacks and active/disabled control cases");
}
'''
f=E/'wdt-check.c';f.write_text(pre+board+'\n'+arm+'\n'+tests);exe=E/'wdt-check'
subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-format','-fsanitize=address,undefined','-g',str(f),'-o',str(exe)],check=True);subprocess.run([str(exe)],check=True)
