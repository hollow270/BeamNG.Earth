"""Real Lua governor/controller/roads with deterministic engine and far-work doubles.

Execute the frame-opening statement extracted from core.lua: the test must fail if
that actual integration forgets staging, not just if a duplicate test formula does.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from test_buildingactivation import LuaGame
import test_buildingrender as fx

REPO=Path(__file__).resolve().parents[2]
CORE=REPO/'mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua'
OPEN=re.search(r"(gov:beginFrame\(.*?\))\s*\n\s*gov:chargeEngine",CORE.read_text(),re.S).group(1)
LEGACY="gov:beginFrame(farfield.hasWork() or roads.hasWork())"


def packet(n=80,*,near=False,seq=1):
    targets=[]
    for i in range(n):
        path=f'/temp/beamngEarth/activation-test/slot_{1000+i*2}.cdae'
        slots={'shape':[path,1000+i*2]}
        data=dict(shape=path,pos=[i*256,0,0],lamps=[])
        if near:
            collision=f'/temp/beamngEarth/activation-test/slot_{1001+i*2}.cdae'
            slots=dict(bld=[path,1000+i*2],bldcol=[collision,1001+i*2])
            data=dict(bld=dict(shape=path,pos=[0,0,0]),bldcol=dict(shape=collision,pos=[0,0,0]),
                      colliders=dict(bldcol=[[60,60,0,70,70,30]]))
        targets.append(dict(slot=f'{i+1:064x}',kind='near' if near else 'far',key=f'{i}_0',generation='a'*32,
            content=f'{i+1:064x}',identity=f'{i+1:040x}',reuse=False,allowEmpty=True,
            frame=[fx.LAT,fx.LON],data=data,slots=slots,exact={}))
    return dict(type='building_activate',version=1,sessionId='activation-test',epoch='e'*32,anchorId=1,
                seq=seq,transaction=f'{seq:032x}',publication='b'*32,revision='c'*64,osm=True,targets=targets)


class Scheduler:
    def __init__(self,*,seed=False,loading=True,legacy=False):
        self.game=LuaGame(); self.g=self.game.lua.globals()
        # roads' adapter uses os.clock; bind it to the same deterministic clock as
        # the governor/stopwatch, rather than charging nondeterministic host CPU.
        self.game.lua.execute('os.clock=function() return clock/1000 end')
        if seed: self.g.nearSeed('0_0',True)
        self.g.frameOpen=self.game.lua.eval('function(gov,farfield,roads,M,st,dtSim) '+(LEGACY if legacy else OPEN)+' end')
        self.game.lua.execute("""
          local original=createObject
          imports=0; farRemaining=0; farUnits=0
          createObject=function(class)
            if class=='TSStatic' then clock=clock+1.15; imports=imports+1 end
            return original(class)
          end
          gov=require('beamngEarth/governor').new({clock=function() return clock end})
          gov.b=0; gov.tokens=-32.9647; gov.est.roadWalk=1.15
          st={active=true,anchorId=1}; M={_buildingActivation=activation}
          farfield={hasWork=function() return farRemaining>0 end,
            update=function(g)
              if farRemaining>0 and g:try('cdae') then
                farRemaining=farRemaining-1; farUnits=farUnits+1
                clock=clock+0.75; g:charge('cdae',0.75)
              end
            end}
          function scheduledStep(dtSim,tickMs,heavy)
            clock=clock+1000/60
            frameOpen(gov,farfield,roads,M,st,dtSim)
            gov:chargeEngine(tickMs or 2)
            if heavy then gov:heavyNow() end
            roads.activationSimStep=dtSim>0
            if st.active then
              activation.update(gov,st.anchorId,anchor,send,dtSim)
              roads.update(gov,st.anchorId,vehicles[1]:getPosition(),nil,anchor)
              farfield.update(gov)
            end
          end
        """)
        self.g.gov.setLoading(self.g.gov,loading)

    def send(self,m):
        if len(m['targets'])<=32: return self.game.packet(m)
        for i in range(0,len(m['targets']),32):
            row=deepcopy(m); row.update(type='building_activate_batch',part=i//32+1,
                parts=(len(m['targets'])+31)//32,total=len(m['targets']),targets=m['targets'][i:i+32])
            if not self.game.packet(row): return False
        return True

    def step(self,*,paused=False,tick=2,heavy=False):
        self.g.scheduledStep(0 if paused else 1/60,tick,heavy)
        return self.game.python(self.g.activation.status())

    def until(self,phase,limit=900):
        for i in range(limit):
            status=self.step()
            if status.get('phase')==phase or status.get('status')==phase: return i+1
        raise AssertionError((phase,self.game.python(self.g.activation.status()),self.game.python(self.g.gov.snapshot(self.g.gov))))


class SchedulerTest(unittest.TestCase):
    def test_observed_idle_streaming_debt_stalls_old_predicate_then_recovers_without_reset(self):
        s=Scheduler(legacy=True); self.assertTrue(s.send(packet()))
        for _ in range(1800): s.step()
        self.assertFalse(s.g.roads.hasWork()); self.assertFalse(s.g.farfield.hasWork())
        self.assertEqual(s.g.imports,0); self.assertEqual(s.g.gov.b,0)
        self.assertAlmostEqual(s.g.gov.tokens,-32.9647)
        self.assertEqual(s.game.python(s.g.activation.status())['phase'],'staging')
        # Change only the actual core frame predicate. Do not replenish tokens manually.
        s.g.frameOpen=s.game.lua.eval('function(gov,farfield,roads,M,st,dtSim) '+OPEN+' end')
        frames=s.until('activated')
        self.assertLess(frames,180); self.assertEqual(s.g.imports,80)
        self.assertFalse(s.g.activation.hasWork())
        for _ in range(200): s.step()
        self.assertEqual(s.g.gov.b,0); self.assertEqual(s.g.gov.tokens,0)

    def test_recovery_obeys_existing_normal_and_loading_frame_and_bucket_limits(self):
        for loading in (False,True):
            with self.subTest(loading=loading):
                s=Scheduler(loading=loading); self.assertTrue(s.send(packet()))
                saw_debt=False; saw_partial=False
                for _ in range(500):
                    status=s.step(); g=s.g.gov
                    saw_debt |= g.tokens<0; saw_partial |= 0<s.g.imports<80
                    self.assertLessEqual(g.b,8 if loading else 2)
                    self.assertLessEqual(g.tokens,2*g.maxUnit(g)+1e-8)
                    self.assertLessEqual(g.frameSpent,g.b+g.maxUnit(g)+1e-8)
                    if status.get('status')=='activated': break
                self.assertEqual(status.get('status'),'activated')
                self.assertTrue(saw_debt); self.assertTrue(saw_partial)

    def test_paused_or_inactive_staging_neither_imports_nor_keeps_budget_busy(self):
        for inactive in (False,True):
            with self.subTest(inactive=inactive):
                s=Scheduler(); self.assertTrue(s.send(packet())); s.step()
                self.assertTrue(s.g.activation.hasWork(1))
                s.g.st.active=not inactive
                for _ in range(240): s.step(paused=not inactive)
                self.assertEqual(s.g.imports,0); self.assertEqual(s.g.gov.b,0)
                self.assertNotEqual(s.game.last()['status'],'activated')
                s.g.st.active=True; s.until('activated'); self.assertEqual(s.g.imports,80)

    def test_incomplete_batches_and_waiting_safety_do_not_request_staging_budget(self):
        s=Scheduler(); m=packet(); row=deepcopy(m)
        row.update(type='building_activate_batch',part=1,parts=3,total=80,targets=m['targets'][:32])
        self.assertTrue(s.game.packet(row)); self.assertFalse(s.g.activation.hasWork())
        for _ in range(30): s.step()
        self.assertEqual(s.g.imports,0); self.assertEqual(s.g.gov.b,0)
        s.g.activation.hello('activation-test','e'*32)
        near=packet(1,near=True); s.g.vehicles[1].setPosition(s.g.vehicles[1],s.g.vec3(65,65,0))
        self.assertTrue(s.send(near))
        for _ in range(30): s.step()
        self.assertEqual(s.game.python(s.g.activation.status())['phase'],'waiting')
        self.assertFalse(s.g.activation.hasWork()); self.assertEqual(s.g.gov.b,0)

    def test_cancel_supersede_disconnect_and_failed_import_clear_work(self):
        for action in ('cancel','anchor','disconnect','failure'):
            with self.subTest(action=action):
                s=Scheduler(); m=packet(); self.assertTrue(s.send(m)); s.step()
                if action!='failure':
                    for _ in range(100):
                        s.step()
                        if s.g.imports: break
                    self.assertGreater(s.g.imports,0); self.assertLess(s.g.imports,80)
                if action=='cancel':
                    cancel={k:m[k] for k in ('sessionId','epoch','anchorId','seq','transaction')}
                    self.assertTrue(s.g.activation.cancel(s.game.table(cancel),s.g.send))
                elif action=='anchor':
                    self.assertFalse(s.g.activation.hasWork(2)); s.g.st.anchorId=2; s.step()
                elif action=='disconnect': s.g.activation.disconnect()
                else:
                    s.g.failRegisterPath=m['targets'][1]['slots']['shape'][0]; s.until('failed')
                self.assertFalse(s.g.activation.hasWork())
                if action!='anchor': # the anchor-failure frame can already drain its delete queue
                    self.assertTrue(s.g.roads.hasWork(),'existing road queues must retire partially staged objects')
                for _ in range(200): s.step()
                self.assertEqual(s.g.gov.b,0)

    def test_heavy_quiet_frames_and_continuing_engine_debt_are_not_bypassed(self):
        s=Scheduler(); self.assertTrue(s.send(packet())); s.step(heavy=True)
        for _ in range(2): s.step(); self.assertEqual(s.g.imports,0)
        for _ in range(120): s.step(tick=52)
        self.assertEqual(s.g.imports,0); self.assertEqual(s.g.gov.tokens,-50)
        s.until('activated'); self.assertEqual(s.g.imports,80)

    def test_collision_ready_and_retirement_stay_with_existing_road_queues_and_physics_fences(self):
        s=Scheduler(seed=True); road=s.g.roadId(); self.assertTrue(s.send(packet(1,near=True)))
        s.until('ready'); self.assertFalse(s.g.activation.hasWork())
        for _ in range(180): s.step(paused=True)
        self.assertEqual(s.game.python(s.g.activation.status())['phase'],'ready')
        self.assertNotEqual(s.game.last()['status'],'activated')
        s.g.gov.est.roadDelete=20
        s.until('retiring')
        self.assertFalse(s.g.activation.hasWork()); self.assertTrue(s.g.roads.hasWork())
        for _ in range(120): s.step(paused=True)
        self.assertEqual(s.game.python(s.g.activation.status())['phase'],'retiring')
        self.assertNotEqual(s.game.last()['status'],'activated')
        s.until('activated'); self.assertEqual(s.g.roadId(),road)
        for _ in range(220): s.step()
        self.assertFalse(s.g.roads.hasWork()); self.assertEqual(s.g.gov.b,0)

    def test_normal_streaming_matches_legacy_budget_and_preserves_update_order(self):
        source=CORE.read_text()
        calls=['M._buildingActivation.update(gov','roads.update(gov','farfield.update(gov']
        positions=[source.index(call) for call in calls]
        self.assertEqual(positions,sorted(positions))
        old,new=Scheduler(legacy=True),Scheduler()
        for s in (old,new):
            s.g.farRemaining=120; self.assertTrue(s.send(packet()))
            s.g.roads.onTile(s.game.table(dict(anchorId=1,key='40_0',data=dict(chunks=[],colliders={},
                road=dict(shape='/temp/beamngEarth/activation-test/slot_90003.cdae',pos=[10000,0,0])),
                slots={},frame=[fx.LAT,fx.LON])),1,s.g.anchor)
        for _ in range(400):
            self.assertEqual(old.step(),new.step())
            self.assertAlmostEqual(old.g.gov.b,new.g.gov.b)
            self.assertAlmostEqual(old.g.gov.tokens,new.g.gov.tokens)
            self.assertEqual(old.g.farUnits,new.g.farUnits)
        self.assertGreater(new.g.farUnits,0); self.assertEqual(new.g.farRemaining,0)
        self.assertEqual(new.game.last()['status'],'activated')

    def test_diagnostics_are_bounded_read_only_snapshots(self):
        s=Scheduler(); self.assertTrue(s.send(packet())); s.step()
        before=s.game.python(s.g.activation.status())
        for _ in range(20): self.assertEqual(s.game.python(s.g.activation.status()),before)
        self.assertEqual(before['target_count'],80); self.assertEqual(before['target_cursor'],1)
        self.assertEqual(s.g.imports,0); self.assertAlmostEqual(s.g.gov.tokens,-32.9647)
        diagnostic=s.game.lua.execute((REPO/'tools/stage6a/diagnostics.lua').read_text())
        self.assertFalse(s.game.python(diagnostic.progress())['available'])
        s.game.lua.execute("beamngEarth_core={_buildingActivation=activation,_governor=function() return gov end}; "
                           "package.loaded['beamngEarth/farfield']=farfield; function dump() end")
        gov_before=s.game.python(s.g.gov.snapshot(s.g.gov)); count=s.g.liveCount()
        value=s.game.python(diagnostic.progress())
        self.assertTrue(value['building_work']); self.assertFalse(value['roads_work']); self.assertFalse(value['farfield_work'])
        self.assertLess(len(json.dumps(value)),4096)
        for _ in range(20): self.assertEqual(s.game.python(diagnostic.progress()),value)
        self.assertEqual(s.game.python(s.g.gov.snapshot(s.g.gov)),gov_before)
        self.assertEqual(s.g.liveCount(),count); self.assertEqual(s.g.imports,0)


if __name__=='__main__': unittest.main()
